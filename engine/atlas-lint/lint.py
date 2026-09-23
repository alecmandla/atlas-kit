#!/usr/bin/env python3
"""atlas-lint — wiki-spine health linter + synthesis work-list generator.

Complements atlas-health (which owns orphans, broken links, frontmatter,
inbox, stale-raw). atlas-lint is scoped to the KNOWLEDGE SPINE and answers:
"what's thin, duplicated, or un-synthesized in wiki/, and what should I
synthesize next?" Read-only on the spine; writes only its dashboard +
last-run.md. Stdlib-only per DEC-021.

Checks (all mechanical):
  duplicate-slugs   fuzzy + token-overlap collisions across synthesis /
                    concepts / entities / #thread tags (e.g. portal-rewrite
                    vs PortalRewrite; ledgerline vs ledgerline-integration).
  missing-synthesis #thread/<slug> with >= MIN evidence files and no
                    wiki/synthesis page — the synthesis WORK-LIST.
  stale-synthesis   a synthesis page with thread evidence newer than its
                    recorded last_evidence (re-run atlas-synthesize).
  hollow-pages      synthesis/concept pages with evidence_count <= 1.

Subcommands:
  report   (default) full lint -> stdout summary + 60 - Meta/Dashboards/Wiki-Lint.md + last-run.md
  worklist machine-readable synthesis work-list (consumed by the synthesis backfill):
           one line per slug:  <slug> | files=<n> | sources=<csv> | status=missing|stale [| dup_of=<slug>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
DEFAULT_VAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent
EXCLUDE_DIRS = {".obsidian", ".git", ".trash", ".smart-env", "node_modules", CFG.folder_name("attachments")}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
THREAD_TAG_RE = re.compile(r"#thread/([A-Za-z0-9_-]+)")
MIN_EVIDENCE = 3  # a thread needs >= this many *evidence* files to be synthesis-worthy
# Tune to your own industry vocabulary: tokens that appear in many slugs but never
# distinguish them (the fictional defaults are hospitality words).
GENERIC_TOKENS = {"the", "and", "for", "inn", "lodge", "hotel", "project", "roll", "up", "in"}

# The vault constitution is skipped outright, as atlas-morning's registry guard does:
# its #thread/<slug> mentions are illustrations and registry rows, not tags on notes,
# and on a fresh vault they would otherwise surface as thin threads and duplicate slugs.
CONSTITUTION = "AGENTS.md"

# Administrative / index files that *list* threads but are NOT evidence about them
# (dashboards/MOCs, generated weekly reviews, daily notes). Their #thread tags count
# toward duplicate-slug detection but NOT toward the evidence count that decides
# whether a thread is worth synthesizing.
ADMIN_PREFIXES = (CFG.folder_name("meta"), CFG.rel("areas", "Weekly-Reviews"), CFG.folder_name("daily"))


def is_admin(rel: str) -> bool:
    return any(rel.startswith(p) for p in ADMIN_PREFIXES)


# --- stdlib frontmatter parser (from atlas-emerge, DEC-021) ---
def _strip_scalar(v: str):
    v = v.strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _parse_inline_list(v: str) -> list:
    inner = v.strip()[1:-1].strip()
    if not inner:
        return []
    return [s for s in (_strip_scalar(p) for p in inner.split(",")) if s is not None]


def parse_frontmatter(text: str) -> dict | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm: dict = {}
    pending = None
    for raw in m.group(1).split("\n"):
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        mi = re.match(r"^\s*-\s+(.*)$", line)
        if mi and pending is not None:
            val = _strip_scalar(mi.group(1))
            cur = fm.get(pending)
            fm[pending] = (cur + [val]) if isinstance(cur, list) else ([val] if val is not None else [])
            continue
        mk = re.match(r"^([^:\s][^:]*):(.*)$", line)
        if not mk:
            continue
        key, rest = mk.group(1).strip(), mk.group(2).strip()
        if rest == "":
            fm[key] = None
            pending = key
            continue
        pending = None
        fm[key] = _parse_inline_list(rest) if (rest.startswith("[") and rest.endswith("]")) else _strip_scalar(rest)
    return fm


def fuzzy_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tokens(slug: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", slug.lower()) if len(t) >= 3 and t not in GENERIC_TOKENS}


def iter_notes(vault: Path):
    for p in vault.rglob("*.md"):
        rel = p.relative_to(vault)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.parts == (CONSTITUTION,):
            continue
        yield p


def note_date(fm: dict | None, path: Path) -> dt.date | None:
    if fm:
        for k in ("date", "timestamp", "last_evidence", "generated_at", "ingested_at"):
            v = fm.get(k)
            if isinstance(v, str):
                try:
                    return dt.date.fromisoformat(v[:10])
                except ValueError:
                    continue
    try:
        return dt.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def collect(vault: Path):
    """One pass: thread evidence map + wiki page inventories."""
    threads: dict[str, dict] = {}
    syn_dir, con_dir, ent_dir = (vault / CFG.folder_name("wiki") / d for d in ("synthesis", "concepts", "entities"))
    syn = {p.stem: parse_frontmatter(p.read_text(encoding="utf-8", errors="ignore")) or {}
           for p in syn_dir.glob("*.md")} if syn_dir.exists() else {}
    con = {p.stem: parse_frontmatter(p.read_text(encoding="utf-8", errors="ignore")) or {}
           for p in con_dir.glob("*.md")} if con_dir.exists() else {}
    ent = {p.stem for p in ent_dir.glob("*.md")} if ent_dir.exists() else set()

    for p in iter_notes(vault):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        tags = set(THREAD_TAG_RE.findall(text))
        if not tags:
            continue
        rel = p.relative_to(vault)
        rel_s = str(rel)
        admin = is_admin(rel_s)
        src = rel.parts[1] if rel.parts[0] == CFG.folder_name("raw") and len(rel.parts) > 1 else rel.parts[0]
        d = note_date(parse_frontmatter(text), p)
        for slug in tags:
            t = threads.setdefault(slug, {"files": set(), "evidence": set(), "sources": set(), "latest": None})
            t["files"].add(rel_s)              # all tags — feeds duplicate-slug detection
            if admin:
                continue                        # admin mentions are not evidence
            t["evidence"].add(rel_s)
            t["sources"].add(src)
            if d and (t["latest"] is None or d > t["latest"]):
                t["latest"] = d
    return threads, syn, con, ent


def find_synthesis_for(slug: str, syn: dict) -> str | None:
    """Exact or fuzzy match of a thread slug to an existing synthesis page."""
    fk = fuzzy_key(slug)
    for stem in syn:
        if fuzzy_key(stem) == fk:
            return stem
    return None


def possible_dup_synthesis(slug: str, syn: dict) -> str | None:
    """Lower-confidence: a synthesis page sharing a meaningful token (catches
    ledgerline <-> ledgerline-integration via shared 'ledgerline')."""
    st = tokens(slug)
    for stem in syn:
        if st & tokens(stem):
            return stem
    return None


def build_worklist(threads: dict, syn: dict) -> list[dict]:
    """Deduped synthesis work-list from #thread tags, using *evidence* files
    (admin/index mentions excluded). status=missing means worth synthesizing
    now (evidence >= MIN); status=thin means tagged but too sparse to bother.
    Re-synthesis of *existing* pages is atlas-synthesize's fingerprint job, not
    ours — synthesized threads are dropped here."""
    by_fuzzy: dict[str, dict] = {}
    for slug, t in threads.items():
        fk = fuzzy_key(slug)
        g = by_fuzzy.setdefault(fk, {"slugs": set(), "evidence": set(), "sources": set(), "latest": None})
        g["slugs"].add(slug)
        g["evidence"] |= t["evidence"]
        g["sources"] |= t["sources"]
        if t["latest"] and (g["latest"] is None or t["latest"] > g["latest"]):
            g["latest"] = t["latest"]

    work = []
    for fk, g in by_fuzzy.items():
        canonical = sorted(g["slugs"], key=lambda s: (-len(s), s))[0]  # most descriptive variant
        if find_synthesis_for(canonical, syn):
            continue  # already synthesized — re-synth gating is atlas-synthesize's job
        n = len(g["evidence"])
        work.append({
            "slug": canonical, "evidence": n, "sources": sorted(g["sources"]),
            "latest": g["latest"].isoformat() if g["latest"] else "?",
            "status": "missing" if n >= MIN_EVIDENCE else "thin",
            "dup_of": possible_dup_synthesis(canonical, syn),
            "variants": sorted(g["slugs"] - {canonical}),
        })
    work.sort(key=lambda w: (-w["evidence"], w["slug"]))
    return work


def find_duplicate_slugs(threads, syn, con, ent) -> tuple[list, list]:
    """High-confidence (fuzzy-equal) and lower-confidence (token-overlap) slug collisions."""
    items = []  # (kind, slug)
    items += [("synthesis", s) for s in syn] + [("concept", s) for s in con]
    items += [("entity", s) for s in ent] + [("thread", s) for s in threads]
    by_fuzzy: dict[str, set] = {}
    for kind, slug in items:
        by_fuzzy.setdefault(fuzzy_key(slug), set()).add(f"{slug} ({kind})")
    exact = [sorted(v) for v in by_fuzzy.values() if len({x.split(" (")[0] for x in v}) > 1]

    # token-overlap across DISTINCT fuzzy groups (semantic near-dups)
    reps = {}  # fuzzy_key -> representative slug
    for kind, slug in items:
        reps.setdefault(fuzzy_key(slug), slug)
    keys = list(reps.items())
    possible = []
    seen = set()
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = reps[keys[i][0]], reps[keys[j][0]]
            shared = tokens(a) & tokens(b)
            if shared and (a, b) not in seen:
                seen.add((a, b))
                possible.append((a, b, sorted(shared)))
    return exact, possible


def find_hollow(syn, con) -> list:
    hollow = []
    for stem, fm in {**{f"synthesis/{k}": v for k, v in syn.items()},
                     **{f"concept/{k}": v for k, v in con.items()}}.items():
        ec = fm.get("evidence_count")
        try:
            n = int(ec) if ec is not None else None
        except (ValueError, TypeError):
            n = None
        if n is not None and n <= 1:
            hollow.append((stem, n))
        elif n is None and stem.startswith("concept/"):
            hollow.append((stem, "no evidence_count"))
    return hollow


def cmd_worklist(args) -> int:
    threads, syn, con, ent = collect(atlas_config.vault_from_arg(args.vault))
    for w in build_worklist(threads, syn):
        if w["status"] != "missing":
            continue  # actionable only — 'thin' threads lack the evidence to synthesize
        dup = f" | dup_of={w['dup_of']}" if w["dup_of"] else ""
        print(f"{w['slug']} | evidence={w['evidence']} | sources={','.join(w['sources'])} | status=missing{dup}")
    return 0


def cmd_report(args) -> int:
    vault = atlas_config.vault_from_arg(args.vault)
    threads, syn, con, ent = collect(vault)
    work = build_worklist(threads, syn)
    exact_dups, possible_dups = find_duplicate_slugs(threads, syn, con, ent)
    hollow = find_hollow(syn, con)
    missing = [w for w in work if w["status"] == "missing"]
    thin = [w for w in work if w["status"] == "thin"]
    today = dt.date.today().isoformat()

    lines = [
        "---", "type: dashboard-wiki-lint", "generated_by: atlas-lint",
        f"generated_at: {today}", "tags: [dashboard, wiki-lint]", "---", "",
        f"# Wiki Lint — {today}", "",
        "> Spine-quality lint (complements `atlas-health`). Surfaces, never edits.", "",
        "## Summary", "",
        "| Check | Count |", "|---|---|",
        f"| Synthesis work-list (missing, evidence ≥ {MIN_EVIDENCE}) | {len(missing)} |",
        f"| Tagged but too thin to synthesize | {len(thin)} |",
        f"| Duplicate slugs (fuzzy) | {len(exact_dups)} |",
        f"| Possible duplicate topics (token-overlap) | {len(possible_dups)} |",
        f"| Hollow pages | {len(hollow)} |", "",
        f"_{len(syn)} synthesis · {len(con)} concept · {len(ent)} entity pages · {len(threads)} distinct #thread tags_", "",
        f"> **Staleness of *existing* synthesis pages is not checked here** — that is `atlas-synthesize`'s fingerprint job. Run `python3 {SKILL_DIR.parent / 'atlas-synthesize' / 'synthesize.py'} --list` for the authoritative new/changed status.", "",
        "## Synthesis work-list — threads with real evidence but no synthesis page", "",
        f"*Evidence = `#thread/<slug>` tags outside admin/index files (dashboards, weekly reviews, daily notes; the constitution `AGENTS.md` is not scanned). Run `/atlas-synthesize <slug>` on these.*", "",
    ]
    if missing:
        lines += ["| Slug | Evidence | Sources | Note |", "|---|---|---|---|"]
        for w in missing:
            note = f"⚠ possible dup of `{w['dup_of']}`" if w["dup_of"] else ""
            if w["variants"]:
                note = (note + " · " if note else "") + f"merged variants: {', '.join(w['variants'])}"
            lines.append(f"| `{w['slug']}` | {w['evidence']} | {', '.join(w['sources'])} | {note} |")
    else:
        lines.append("_none — every evidence-rich thread is already synthesized._")
    lines.append("")

    if thin:
        lines += ["## Tagged but too thin (evidence < %d — skip until they accrue more)" % MIN_EVIDENCE, "",
                  "| Slug | Evidence | Note |", "|---|---|---|"]
        for w in thin:
            note = f"⚠ possible dup of `{w['dup_of']}`" if w["dup_of"] else ""
            lines.append(f"| `{w['slug']}` | {w['evidence']} | {note} |")
        lines.append("")

    lines += ["## Duplicate slugs (fuzzy — same topic, variant spellings)", ""]
    if exact_dups:
        lines += [f"- {' / '.join(g)}" for g in exact_dups]
    else:
        lines.append("_none_")
    lines.append("")

    if possible_dups:
        lines += ["## Possible duplicate topics (shared token — review)", ""]
        lines += [f"- `{a}` ~ `{b}`  (shared: {', '.join(sh)})" for a, b, sh in possible_dups[:25]]
        if len(possible_dups) > 25:
            lines.append(f"- _… and {len(possible_dups) - 25} more_")
        lines.append("")

    lines += ["## Hollow pages (evidence_count <= 1)", ""]
    lines += ([f"- `{s}` ({n})" for s, n in hollow] if hollow else ["_none_"]) + [""]

    dash = vault / CFG.folder_name("meta") / "Dashboards" / "Wiki-Lint.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("\n".join(lines), encoding="utf-8")

    (SKILL_DIR / "last-run.md").write_text(
        f"# atlas-lint — last run\n\n- timestamp: {today}\n- mode: report\n"
        f"- dashboard: {dash.relative_to(vault)}\n"
        f"- synthesis_worklist_missing: {len(missing)}\n- tagged_too_thin: {len(thin)}\n"
        f"- duplicate_slugs_fuzzy: {len(exact_dups)}\n- possible_duplicate_topics: {len(possible_dups)}\n"
        f"- hollow_pages: {len(hollow)}\n", encoding="utf-8")

    # stdout summary for the agent
    print(f"atlas-lint — {today}")
    print(f"  synthesis work-list (missing, evidence >= {MIN_EVIDENCE}): {len(missing)}  -> {', '.join(w['slug'] for w in missing) or 'none'}")
    print(f"  tagged but too thin: {len(thin)}  -> {', '.join(w['slug'] for w in thin) or 'none'}")
    print(f"  duplicate slugs (fuzzy): {len(exact_dups)}")
    for g in exact_dups:
        print(f"    - {' / '.join(g)}")
    print(f"  possible duplicate topics (token-overlap): {len(possible_dups)}")
    print(f"  hollow pages: {len(hollow)}  -> {', '.join(s for s, _ in hollow) or 'none'}")
    print(f"  dashboard: {dash}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="atlas-lint — wiki-spine linter + synthesis work-list")
    ap.add_argument("--vault", default=None, help="vault root (default: vault_root from Atlas config)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("report", help="full lint -> dashboard + stdout (default)")
    r.set_defaults(func=cmd_report)
    w = sub.add_parser("worklist", help="machine-readable synthesis work-list")
    w.set_defaults(func=cmd_worklist)
    args = ap.parse_args()
    if not getattr(args, "func", None):
        args.func = cmd_report
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
