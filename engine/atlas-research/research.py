#!/usr/bin/env python3
"""atlas-research — on-demand query / answer over the EXISTING Atlas vault.

Deterministic, read-only retrieval backbone for the atlas-research skill
(v1: query-only — it never ingests, never mutates the knowledge spine).
Implements progressive drilldown over the vault's natural layers:

    wiki/index + MOCs            (layer 0 — what exists)
    wiki/synthesis               (layer 1 — most-synthesized prose)
    wiki/concepts                (layer 2 — thread hubs)
    wiki/entities                (layer 3 — hub / disambiguation)
    20/30/40 - PARA, CRM, 60-Meta(layer 4 — curated notes)
    raw/<source>/                (layer 5 — ground truth, read last)

The Claude session (SKILL.md) does the judgment + prose; this script does
the side-effect-free work: layer-weighted retrieval, the "read-first"
evidence map, the id->path drill map, and the citation-resolution gate.
Stdlib-only per DEC-021 (no PyYAML — copied parser from atlas-emerge).

Subcommands
  search   --question "<q>" [--terms a,b] [--days N] [--limit N]
           Emit the read-first evidence map (direct hubs + ranked candidates
           per layer + id->path drill map). READ-ONLY.
  resolve  (--file F | stdin)
           Citation gate: every [[link]] must resolve to a real vault file.
           Prints resolved / unresolved. READ-ONLY.
  write    --slug S --question "<q>" --answer-file F [--confidence C]
           [--sources N] [--layers a,b]
           Persist a research answer note + append the research log. WRITES.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
DEFAULT_VAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent

EXCLUDE_DIRS = {".obsidian", ".git", ".trash", ".smart-env", "node_modules", CFG.folder_name("attachments")}
RESEARCH_SUBDIR = (CFG.folder_name("meta"), "Research")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
TITLE_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)

# Layer weights — a synthesis hit is worth far more than a raw hit, so the
# agent reads condensed prose first and only drills to raw when forced.
LAYER_RULES = [
    (CFG.rel("wiki", "synthesis"), "synthesis", 5.0),
    (CFG.rel("wiki", "concepts"), "concept", 4.0),
    (CFG.rel("wiki", "index.md"), "index", 4.5),
    (CFG.rel("meta", "MOCs"), "index", 4.5),
    (CFG.rel("wiki", "entities"), "entity", 3.0),
    (CFG.rel("meta", "Dashboards"), "dashboard", 2.5),
    (CFG.rel("crm", "People"), "people", 2.2),
    (CFG.folder_name("projects"), "para", 2.0),
    (CFG.folder_name("areas"), "para", 2.0),
    (CFG.folder_name("resources"), "para", 2.0),
    (CFG.folder_name("meta"), "para", 1.8),
    (CFG.folder_name("daily"), "daily", 1.5),
    (CFG.folder_name("clippings"), "clippings", 1.2),
    (CFG.folder_name("inbox"), "inbox", 1.0),
    (CFG.folder_name("raw"), "raw", 1.0),
    (CFG.folder_name("archive"), "archive", 0.5),
]

# Per-layer caps so the evidence map stays small (context-budget discipline).
LAYER_CAPS = {
    "synthesis": 99, "concept": 99, "index": 5, "dashboard": 6, "entity": 10,
    "people": 8, "para": 12, "daily": 5, "clippings": 5, "inbox": 5,
    "archive": 4, "raw": 15, "other": 8,
}
RAW_PER_SOURCE_CAP = 5  # diversity cap within raw/ (mirrors atlas-synthesize)

STOPWORDS = {
    "the", "and", "for", "are", "was", "what", "whats", "with", "from", "this",
    "that", "have", "has", "how", "does", "did", "about", "into", "over", "any",
    "all", "our", "out", "who", "why", "when", "where", "which", "their", "they",
    "been", "were", "will", "would", "could", "should", "than", "then", "them",
    "your", "you", "get", "got", "can", "but", "not", "its", "his", "her",
    "show", "tell", "give", "list", "find", "summarize", "summary", "status",
    "current", "key", "decisions", "decision", "happening", "going",
}

LAYER_ORDER = ["index", "synthesis", "concept", "entity", "dashboard", "people",
               "para", "daily", "clippings", "inbox", "raw", "archive", "other"]


# ---------------------------------------------------------------------------
# stdlib frontmatter parser — copied verbatim from atlas-emerge (DEC-021).
# ---------------------------------------------------------------------------
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


def parse_frontmatter_block(block: str) -> dict:
    fm: dict = {}
    pending_key = None
    for raw_line in block.split("\n"):
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m_item = re.match(r"^\s*-\s+(.*)$", line)
        if m_item and pending_key is not None:
            val = _strip_scalar(m_item.group(1))
            cur = fm.get(pending_key)
            if isinstance(cur, list):
                cur.append(val)
            else:
                fm[pending_key] = [val] if val is not None else []
            continue
        m_kv = re.match(r"^([^:\s][^:]*):(.*)$", line)
        if not m_kv:
            continue
        key, rest = m_kv.group(1).strip(), m_kv.group(2).strip()
        if rest == "":
            fm[key] = None
            pending_key = key
            continue
        pending_key = None
        if rest.startswith("[") and rest.endswith("]"):
            fm[key] = _parse_inline_list(rest)
        else:
            fm[key] = _strip_scalar(rest)
    return fm


def parse_frontmatter(text: str) -> dict | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    return parse_frontmatter_block(m.group(1))


def kebab(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def fuzzy_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------------------
# vault traversal + indexing
# ---------------------------------------------------------------------------
def iter_notes(vault: Path):
    for p in vault.rglob("*.md"):
        rel_parts = p.relative_to(vault).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        yield p


def build_stem_index(vault: Path) -> dict[str, list[Path]]:
    """Map lowercased filename stem -> [paths]. Doubles as the citation gate
    (does a [[link]] resolve?) and the drill map ([[id]] -> file)."""
    idx: dict[str, list[Path]] = {}
    for p in iter_notes(vault):
        idx.setdefault(p.stem.lower(), []).append(p)
    return idx


def layer_of(path: Path, vault: Path) -> tuple[str, float]:
    rel = str(path.relative_to(vault))
    for prefix, name, weight in LAYER_RULES:
        if rel == prefix or rel.startswith(prefix + "/") or rel.startswith(prefix):
            return name, weight
    return "other", 1.0


def raw_source_of(path: Path, vault: Path) -> str:
    try:
        rel = path.relative_to(vault / CFG.folder_name("raw"))
    except ValueError:
        return ""
    return rel.parts[0] if rel.parts else ""


def note_title(path: Path, fm: dict | None, body: str) -> str:
    if fm:
        for k in ("title", "canonical_id", "name"):
            if fm.get(k):
                return str(fm[k])
    m = TITLE_RE.search(body)
    if m:
        return m.group(1).strip()
    return path.stem


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
def extract_terms(question: str, override: str | None) -> list[str]:
    if override:
        return [t.strip() for t in override.split(",") if t.strip()]
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]+", question.lower())
    seen, terms = set(), []
    for t in toks:
        t = t.strip("'-")
        if len(t) < 3 or t in STOPWORDS or t in seen:
            continue
        seen.add(t)
        terms.append(t)
    return terms[:8]


def rg_counts(vault: Path, terms: list[str]) -> dict[Path, int]:
    """Return {path: matching_line_count} via ripgrep."""
    cmd = ["rg", "-i", "-c", "--no-messages", "-g", "*.md"]
    for ex in EXCLUDE_DIRS:
        cmd += ["-g", f"!{ex}/**"]
    for t in terms:
        cmd += ["-e", t]
    cmd.append(str(vault))
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    counts: dict[Path, int] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        path_s, cnt = line.rsplit(":", 1)
        try:
            counts[Path(path_s)] = int(cnt)
        except ValueError:
            continue
    return counts


def py_counts(vault: Path, terms: list[str]) -> dict[Path, int]:
    """Pure-python fallback when ripgrep is absent."""
    pats = [re.compile(re.escape(t), re.I) for t in terms]
    counts: dict[Path, int] = {}
    for p in iter_notes(vault):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = sum(len(pat.findall(text)) for pat in pats)
        if hits:
            counts[p] = hits
    return counts


def cmd_search(args) -> int:
    vault = atlas_config.vault_from_arg(args.vault)
    terms = extract_terms(args.question, args.terms)
    if not terms:
        print("No usable search terms extracted. Pass --terms a,b,c.", file=sys.stderr)
        return 2

    counts = rg_counts(vault, terms) if shutil.which("rg") else py_counts(vault, terms)

    cutoff = None
    if args.days:
        cutoff = dt.date.today() - dt.timedelta(days=args.days)

    rows = []
    for path, hits in counts.items():
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = parse_frontmatter(text)
        body = text[FRONTMATTER_RE.match(text).end():] if FRONTMATTER_RE.match(text) else text
        layer, weight = layer_of(path, vault)
        rows.append({
            "path": path, "rel": str(path.relative_to(vault)), "hits": hits,
            "layer": layer, "score": round(hits * weight, 2),
            "cite": path.stem, "title": note_title(path, fm, body),
            "source": raw_source_of(path, vault) if layer == "raw" else "",
        })

    # Direct hub lookups: a query term that IS a synthesis/concept/entity slug.
    hubs = []
    entities_dir = vault / CFG.folder_name("wiki") / "entities"
    entity_stems = {fuzzy_key(p.stem): p for p in entities_dir.glob("*.md")} \
        if entities_dir.exists() else {}
    for t in terms:
        slug = kebab(t)
        for sub, label in (("synthesis", "synthesis"), ("concepts", "concept")):
            cand = vault / CFG.folder_name("wiki") / sub / f"{slug}.md"
            if cand.exists():
                hubs.append((label, cand))
        ent = entity_stems.get(fuzzy_key(t))
        if ent:
            hubs.append(("entity", ent))
    seen_hub = set()
    hub_lines = []
    for label, cand in hubs:
        if cand in seen_hub:
            continue
        seen_hub.add(cand)
        fm = parse_frontmatter(cand.read_text(encoding="utf-8", errors="ignore")) or {}
        prov = fm.get("evidence_count") or fm.get("mention_count") or "?"
        hub_lines.append(f"- [[{cand.stem}]] — {cand.relative_to(vault)} ({label}, evidence/mentions: {prov})")

    # group by layer, apply caps + raw per-source diversity cap
    by_layer: dict[str, list] = {}
    for r in rows:
        by_layer.setdefault(r["layer"], []).append(r)

    print(f"# atlas-research search")
    print(f'question: "{args.question}"')
    print(f"terms: {terms}")
    print(f"vault: {vault}")
    print(f"matched_files: {len(rows)}" + (f"  (window: last {args.days}d)" if cutoff else ""))
    print()
    if hub_lines:
        print("## Direct hubs — READ THESE FIRST (a query term is a known synthesis/concept/entity)")
        print("\n".join(hub_lines))
        print()

    print("## Ranked candidates by layer (read top-down; stop when answered)")
    drill = []
    for layer in LAYER_ORDER:
        items = by_layer.get(layer)
        if not items:
            continue
        items.sort(key=lambda r: (-r["score"], -r["hits"], r["rel"]))
        if layer == "raw":
            kept, per_source = [], {}
            for r in items:
                s = r["source"]
                if per_source.get(s, 0) >= RAW_PER_SOURCE_CAP:
                    continue
                per_source[s] = per_source.get(s, 0) + 1
                kept.append(r)
                if len(kept) >= LAYER_CAPS["raw"]:
                    break
            items = kept
        else:
            items = items[: LAYER_CAPS.get(layer, 8)]
        if not items:
            continue
        print(f"\n### {layer}  ({len(by_layer[layer])} matched)")
        for r in items:
            src = f" | source: {r['source']}" if r["source"] else ""
            print(f"- score {r['score']:>6} | hits {r['hits']:>3} | [[{r['cite']}]] — {r['title']}{src}")
            print(f"    {r['rel']}")
            drill.append((r["cite"], r["rel"]))

    if drill:
        print("\n## Drill map (cite id -> path) — open by path; resolution is by filename")
        for cite, rel in drill:
            print(f"[[{cite}]] -> {rel}")

    print("\n## Reading guidance")
    print("1. Read Direct hubs + top `synthesis`/`concept` fully — they are short and already cited.")
    print("2. Drill into `para` then `raw` ONLY to answer specifics or verify a claim. Never bulk-read raw.")
    print("3. Stop as soon as the question is answered. Cite every claim with the [[id]] shown above.")
    print("4. Before finalizing, run: research.py resolve --file <draft> (citation gate).")
    return 0


# ---------------------------------------------------------------------------
# resolve  (citation gate)
# ---------------------------------------------------------------------------
def cmd_resolve(args) -> int:
    vault = atlas_config.vault_from_arg(args.vault)
    text = Path(args.file).read_text(encoding="utf-8", errors="ignore") if args.file else sys.stdin.read()
    idx = build_stem_index(vault)
    resolved, unresolved, seen = [], [], set()
    for name in WIKILINK_RE.findall(text):
        key = name.strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        (resolved if key.lower() in idx else unresolved).append(key)

    print(f"citations: {len(seen)} unique | resolved: {len(resolved)} | unresolved: {len(unresolved)}")
    if unresolved:
        print("\nUNRESOLVED (fix or drop before shipping — never leave a broken citation):")
        for u in unresolved:
            print(f"  [[{u}]]")
        return 1
    print("\n✓ all citations resolve to real vault files")
    return 0


# ---------------------------------------------------------------------------
# write  (optional persistence)
# ---------------------------------------------------------------------------
EDITABLE_START = "<!-- atlas-research:editable-start -->"
EDITABLE_END = "<!-- atlas-research:editable-end -->"


def extract_editable(existing: str) -> str | None:
    pat = re.compile(rf"{re.escape(EDITABLE_START)}\s*\n(.*?)\n\s*{re.escape(EDITABLE_END)}", re.DOTALL)
    m = pat.search(existing)
    return m.group(1).strip() if m else None


def cmd_write(args) -> int:
    vault = atlas_config.vault_from_arg(args.vault)
    today = dt.date.today().isoformat()
    slug = kebab(args.slug)
    answer = Path(args.answer_file).read_text(encoding="utf-8", errors="ignore").strip()

    # citation gate on the answer body
    idx = build_stem_index(vault)
    unresolved = sorted({n.strip() for n in WIKILINK_RE.findall(answer)
                         if n.strip() and n.strip().lower() not in idx})

    research_dir = vault.joinpath(*RESEARCH_SUBDIR)
    research_dir.mkdir(parents=True, exist_ok=True)
    dest = research_dir / f"{today}-{slug}.md"

    editable = extract_editable(dest.read_text(encoding="utf-8", errors="ignore")) if dest.exists() else None
    layers = [s.strip() for s in (args.layers or "").split(",") if s.strip()]

    fm = [
        "---",
        "type: research-answer",
        "generated_by: atlas-research",
        f"generated_at: {today}",
        f'question: "{args.question.replace(chr(34), chr(39))}"',
        f"canonical_slug: {slug}",
        f"confidence: {args.confidence or 'medium'}",
        f"sources_cited: {args.sources if args.sources is not None else 0}",
        f"layers_used: [{', '.join(layers)}]",
        f"tags: [research, research/{slug}]",
        "---",
    ]
    parts = ["\n".join(fm), "",
             f"# Research — {args.question}", "",
             "> Generated by atlas-research from existing vault material. Verify against the cited [[sources]].", "",
             answer, ""]
    if unresolved:
        parts += ["## ⚠ Unresolved citations", "These [[links]] did not resolve to a vault file — verify or fix:", ""]
        parts += [f"- [[{u}]]" for u in unresolved] + [""]
    parts += [EDITABLE_START, "", editable or "_Your notes — preserved across regenerations._", "", EDITABLE_END, ""]
    dest.write_text("\n".join(parts), encoding="utf-8")

    # append-only greppable log
    log = research_dir / "_research-log.md"
    if not log.exists():
        log.write_text("# atlas-research — query log\n\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"- [{today}] {slug} | conf={args.confidence or 'medium'} | "
                 f"sources={args.sources if args.sources is not None else 0} | {args.question}\n")

    # last-run audit
    (SKILL_DIR / "last-run.md").write_text(
        f"# atlas-research — last run\n\n"
        f"- timestamp: {today}\n- mode: write\n- slug: {slug}\n"
        f"- dest: {dest.relative_to(vault)}\n- confidence: {args.confidence or 'medium'}\n"
        f"- sources_cited: {args.sources if args.sources is not None else 0}\n"
        f"- unresolved_citations: {len(unresolved)}\n"
        + ("".join(f"  - [[{u}]]\n" for u in unresolved) if unresolved else ""),
        encoding="utf-8")

    print(f"wrote {dest}")
    if unresolved:
        print(f"⚠ {len(unresolved)} unresolved citation(s) flagged in the note + last-run.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="atlas-research — query/answer over the existing vault")
    ap.add_argument("--vault", default=None, help="vault root (default: vault_root from Atlas config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="emit the read-first evidence map")
    s.add_argument("--question", required=True)
    s.add_argument("--terms", help="comma-separated override for the derived search terms")
    s.add_argument("--days", type=int, help="optional recency window (filters nothing yet; reserved)")
    s.add_argument("--limit", type=int, help="reserved")
    s.set_defaults(func=cmd_search)

    r = sub.add_parser("resolve", help="citation gate: do all [[links]] resolve?")
    r.add_argument("--file", help="file to check (default: stdin)")
    r.set_defaults(func=cmd_resolve)

    w = sub.add_parser("write", help="persist a research answer note + log")
    w.add_argument("--slug", required=True)
    w.add_argument("--question", required=True)
    w.add_argument("--answer-file", required=True)
    w.add_argument("--confidence", choices=["high", "medium", "low"])
    w.add_argument("--sources", type=int)
    w.add_argument("--layers", help="comma-separated layers used, e.g. synthesis,para,raw")
    w.set_defaults(func=cmd_write)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
