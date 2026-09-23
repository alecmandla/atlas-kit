#!/usr/bin/env python3
"""atlas-graduate — promote an emerging-patterns slug to a tracked surface.

See SKILL.md for the contract. v1 is rule-based: no LLM body content.
Takes a slug, walks PARA notes for keyword matches, creates the destination
page, backfills #thread/<slug> tags on matched notes, appends a row to
vault AGENTS.md, writes last-run.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT_DEFAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([\d-]+)\s*\|\s*([\d-]+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*`([a-z0-9-]+)`\s*\|.*\|$")
THREAD_ROW_RE = re.compile(r"^\|\s*`#thread/([a-z0-9-]+)`\s*\|", re.MULTILINE)

PARA_DIRS = [CFG.folder_name(k) for k in ("inbox", "projects", "areas", "resources")]
# Dataview FROM clause over the PARA folders, e.g. "20 - Projects" OR "30 - Areas" OR "40 - Resources"
DATAVIEW_FROM_PARA = " OR ".join(f'"{CFG.folder_name(k)}"' for k in ("projects", "areas", "resources"))

TARGET_PATHS = {
    "concept": (CFG.rel("wiki", "concepts"), "{slug}.md", "wiki-concept"),
    "project": (CFG.rel("projects", "Work", "{slug}"), "{slug}-MOC.md", "project-moc"),
    "area": (CFG.rel("areas", "{slug}"), "{slug}-MOC.md", "area-moc"),
    "resource": (CFG.rel("resources", "{slug}"), "{slug}-MOC.md", "resource-moc"),
}

EDITABLE_START = "<!-- atlas-graduate:editable-start -->"
EDITABLE_END = "<!-- atlas-graduate:editable-end -->"


def title_case(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def infer_target(vault: Path, slug: str) -> str:
    """Best-effort target type for unattended (--auto) graduation.

    Bias toward `concept` — the lowest-structural-risk surface (a wiki page +
    tag backfill, no new Projects/Areas folder). Only escalate to area/project
    when the slug clearly maps to an existing `30 - Areas/` or
    `20 - Projects/Clients/` folder. See DEC-019.
    """
    norm = slug.replace("-", "").lower()
    areas = vault / CFG.folder_name("areas")
    if areas.exists():
        for d in areas.iterdir():
            if d.is_dir() and d.name.replace("-", "").lower() == norm:
                return "area"
    clients = vault / CFG.folder_name("projects") / "Clients"
    if clients.exists():
        for d in clients.iterdir():
            if d.is_dir() and d.name.replace("-", "").lower() == norm:
                return "project"
    return "concept"


def parse_emerge_report(report_path: Path) -> dict[str, dict]:
    """Return {slug: {keywords, first, latest, items, sources}} from the report table."""
    text = report_path.read_text(encoding="utf-8")
    patterns: dict[str, dict] = {}
    for line in text.splitlines():
        m = TABLE_ROW_RE.match(line)
        if not m:
            continue
        _, keywords_raw, first, latest, items, sources_raw, slug = m.groups()
        keywords = [k.strip() for k in keywords_raw.split(";") if k.strip()]
        sources = [s.strip() for s in sources_raw.split(",") if s.strip()]
        patterns[slug] = {
            "keywords": keywords,
            "first": first,
            "latest": latest,
            "items": int(items),
            "sources": sources,
        }
    return patterns


def keyword_match_patterns(keywords: list[str], slug: str) -> list[re.Pattern]:
    """Compile regex patterns that match either the slug or any keyword variant."""
    targets = set()
    targets.add(slug)
    targets.add(slug.replace("-", " "))
    for kw in keywords:
        targets.add(kw.lower())
        targets.add(kw.lower().replace("-", " "))
    pats: list[re.Pattern] = []
    for t in sorted(targets, key=len, reverse=True):
        if len(t) < 4:
            continue
        # Word-boundary match, case-insensitive
        escaped = re.escape(t)
        pats.append(re.compile(rf"\b{escaped}\b", re.IGNORECASE))
    return pats


def _strip_scalar(v: str):
    """Strip surrounding quotes from a scalar value; empty -> None (yaml parity)."""
    v = v.strip()
    if not v:
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _parse_inline_list(v: str) -> list:
    """`[a, b, c]` -> ['a','b','c'], quote-stripped. Naive comma split (Atlas list
    elements — emails, slugs, wikilinks — contain no commas)."""
    inner = v.strip()[1:-1].strip()
    if not inner:
        return []
    return [s for s in (_strip_scalar(p) for p in inner.split(",")) if s is not None]


def parse_frontmatter_block(block: str) -> dict:
    """Parse a frontmatter block (between the --- fences) without PyYAML.

    Handles `key: scalar`, `key: [a, b]` inline lists, and `key:` + `- item`
    block lists. Values are str or list[str]; empty -> None. A deliberately
    small YAML subset — sufficient for Atlas's flat frontmatter; call sites
    coerce ints/dates. Validated against yaml.safe_load on the full vault
    (0 mismatches on read keys). See DEC-021.
    """
    fm: dict = {}
    pending_key = None  # key awaiting block-list items
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


def parse_frontmatter(text: str) -> tuple[dict | None, str, str]:
    """Return (parsed-fm, raw-fm-text, body)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, "", text
    raw = m.group(1)
    return parse_frontmatter_block(raw), raw, text[m.end():]


def file_matches_keywords(text: str, title: str, pats: list[re.Pattern]) -> bool:
    """Return True if any keyword pattern matches title or first 2000 chars of body."""
    haystack = (title + "\n" + text[:2000]).lower()
    for p in pats:
        if p.search(haystack):
            return True
    return False


def is_para_file(path: Path, vault: Path) -> bool:
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return False
    if not rel.parts:
        return False
    first = rel.parts[0]
    if first in {CFG.folder_name("raw"), CFG.folder_name("wiki")}:
        return False
    # Limit to PARA directories
    return first in PARA_DIRS


def walk_para(vault: Path, pats: list[re.Pattern]) -> list[dict]:
    """Return list of {path, fm, body, title} for PARA notes matching the patterns."""
    matches: list[dict] = []
    for para_dir in PARA_DIRS:
        root = vault / para_dir
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            if p.name.startswith("."):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm, _, body = parse_frontmatter(text)
            tm = TITLE_RE.search(body)
            title = tm.group(1).strip() if tm else p.stem
            if not file_matches_keywords(body, title, pats):
                continue
            matches.append({
                "path": p,
                "fm": fm or {},
                "body": body,
                "title": title,
                "raw_text": text,
            })
    return matches


def aggregate_contributors(matches: list[dict], top_n: int = 10) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for m in matches:
        participants = m["fm"].get("participants")
        if not participants:
            continue
        # participants is a list or a string; handle both
        if isinstance(participants, str):
            entries = WIKILINK_RE.findall(participants)
        elif isinstance(participants, list):
            entries = []
            for e in participants:
                if isinstance(e, str):
                    found = WIKILINK_RE.findall(e)
                    if found:
                        entries.extend(found)
                    else:
                        # Bare name
                        entries.append(e.strip())
        else:
            entries = []
        for name in entries:
            name = name.strip()
            if name:
                counter[name] += 1
    return counter.most_common(top_n)


def related_raw_sources(
    vault: Path,
    pats: list[re.Pattern],
    limit: int = 10,
) -> list[tuple[str, str, str]]:
    """Walk raw/ for items whose title/body matches; return [(stem, date, source)]."""
    items: list[tuple[str, str, str]] = []
    raw_root = vault / CFG.folder_name("raw")
    if not raw_root.exists():
        return items
    for src_dir in sorted(raw_root.iterdir()):
        if not src_dir.is_dir():
            continue
        src_name = src_dir.name
        for p in src_dir.rglob("*.md"):
            if p.name == "README.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm, _, body = parse_frontmatter(text)
            tm = TITLE_RE.search(body)
            title = tm.group(1).strip() if tm else p.stem
            if not file_matches_keywords(body, title, pats):
                continue
            date_v = ""
            if fm:
                d = fm.get("date") or fm.get("timestamp") or fm.get("started")
                if isinstance(d, (dt.date, dt.datetime)):
                    date_v = d.isoformat()[:10]
                elif isinstance(d, str):
                    date_v = d[:10]
            items.append((p.stem, date_v, src_name))
            if len(items) >= limit:
                return items
    return items


def render_destination_page(
    slug: str,
    target: str,
    pattern: dict,
    contributors: list[tuple[str, int]],
    raw_sources: list[tuple[str, str, str]],
    today: dt.date,
    existing_editable: str | None = None,
    graduation_mode: str = "manual",
) -> str:
    type_label = TARGET_PATHS[target][2]
    title = title_case(slug)
    sources_yaml = "[" + ", ".join(pattern["sources"]) + "]"
    tags_yaml = f"[{type_label}, thread/{slug}]"

    contributors_section = "\n".join(
        f"- [[{name}]] — {count} meeting{'s' if count != 1 else ''}"
        for name, count in contributors
    ) or "- _(no participant-bearing matches in PARA — fill in by hand)_"

    raw_section = "\n".join(
        f"- [[{stem}]] — {date or 'unknown'} ({source})"
        for stem, date, source in raw_sources
    ) or "- _(no raw/ matches — re-run after sources land)_"

    editable_body = existing_editable or (
        "_Hand-add wikilinks, notes, and decisions here. "
        "This region is preserved across re-runs._"
    )

    dataview = (
        "```dataview\n"
        "TABLE date as \"Date\", file.link as \"Note\"\n"
        f"FROM {DATAVIEW_FROM_PARA}\n"
        f"WHERE contains(file.tags, \"thread/{slug}\")\n"
        "SORT date DESC\n"
        "```"
    )

    return (
        "---\n"
        f"type: {type_label}\n"
        f"canonical_id: {slug}\n"
        "graduated_from: emerging-patterns\n"
        f"graduated_at: {today.isoformat()}\n"
        f"graduation_mode: {graduation_mode}\n"
        f"first_mention: {pattern['first']}\n"
        f"last_mention: {pattern['latest']}\n"
        f"mention_count: {pattern['items']}\n"
        f"sources: {sources_yaml}\n"
        f"tags: {tags_yaml}\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        f"> Graduated from `atlas-emerge` on {today.isoformat()}. "
        f"First mention: {pattern['first']}. "
        f"{pattern['items']} items across {len(pattern['sources'])} sources.\n"
        "\n"
        "## Current state\n"
        "\n"
        "_<one-line placeholder — owner fills in>_\n"
        "\n"
        "## Key contributors\n"
        "\n"
        f"{contributors_section}\n"
        "\n"
        "## Open questions\n"
        "\n"
        "- _<placeholder>_\n"
        "\n"
        "## Related raw sources\n"
        "\n"
        f"{raw_section}\n"
        "\n"
        "## Thread query\n"
        "\n"
        f"{dataview}\n"
        "\n"
        "## Editable\n"
        "\n"
        f"{EDITABLE_START}\n"
        f"{editable_body}\n"
        f"{EDITABLE_END}\n"
    )


def extract_editable(existing_text: str) -> str | None:
    """Pull the content between editable markers from an existing destination page."""
    pat = re.compile(
        rf"{re.escape(EDITABLE_START)}\s*\n(.*?)\n\s*{re.escape(EDITABLE_END)}",
        re.DOTALL,
    )
    m = pat.search(existing_text)
    if not m:
        return None
    return m.group(1).strip()


def add_tag_to_frontmatter(text: str, tag: str) -> tuple[str, bool]:
    """Return (new_text, changed)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — add one with tags only
        new_fm = f"---\ntags: [{tag}]\n---\n"
        return new_fm + text, True

    fm_block = m.group(1)
    body = text[m.end():]

    # Parse the frontmatter (stdlib, no PyYAML) to find existing tags.
    fm = parse_frontmatter_block(fm_block)

    tags = fm.get("tags")
    if tags is None:
        # No tags field — append a line
        new_fm_block = fm_block.rstrip() + f"\ntags: [{tag}]"
        return f"---\n{new_fm_block}\n---\n{body}", True

    # Normalize tags to a list
    if isinstance(tags, str):
        existing = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
    elif isinstance(tags, list):
        existing = [str(t).strip() for t in tags]
    else:
        return text, False

    if tag in existing:
        return text, False  # Idempotent no-op

    lines = fm_block.split("\n")
    # Locate the `tags:` line.
    tags_idx = next((i for i, ln in enumerate(lines) if re.match(r"^tags\s*:", ln)), None)
    if tags_idx is None:
        new_fm_block = fm_block.rstrip() + "\ntags: [" + ", ".join(existing + [tag]) + "]"
        return f"---\n{new_fm_block}\n---\n{body}", True

    inline = lines[tags_idx].split(":", 1)[1].strip()
    if inline == "":
        # Block-list style (`tags:` then `- item` lines): insert a new block item
        # after the last existing item, PRESERVING the note's block style. (The
        # prior implementation rewrote the `tags:` line to an inline list but left
        # the old `- item` lines dangling, producing invalid YAML — DEC: fix-at-source.)
        j = tags_idx + 1
        indent = "- "
        while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
            indent = re.match(r"^(\s*-\s+)", lines[j]).group(1)
            j += 1
        lines.insert(j, f"{indent}{tag}")
    else:
        # Inline list (`tags: [a, b]`) or scalar (`tags: a b`) — emit an inline list.
        lines[tags_idx] = "tags: [" + ", ".join(existing + [tag]) + "]"

    new_fm_block = "\n".join(lines)
    return f"---\n{new_fm_block}\n---\n{body}", True


def update_agents_md(agents_path: Path, slug: str, today: dt.date) -> bool:
    """Append a row for <slug> in vault AGENTS.md's threads table. Return True if changed."""
    if not agents_path.exists():
        return False
    text = agents_path.read_text(encoding="utf-8")
    # Check idempotency
    for m in THREAD_ROW_RE.finditer(text):
        if m.group(1) == slug:
            return False  # Row already exists

    new_row = (
        f"| `#thread/{slug}` | "
        f"_Graduated {today.isoformat()} — see [[{slug}]] for context._ |"
    )

    # Find the threads table by searching for the canonical example rows.
    lines = text.splitlines()
    insertion_idx = None
    in_threads_table = False
    last_table_row_idx = None
    for i, line in enumerate(lines):
        if "canonical examples" in line.lower():
            in_threads_table = True
        if in_threads_table and line.startswith("| `#thread/"):
            last_table_row_idx = i
        if in_threads_table and last_table_row_idx is not None and not line.startswith("|") and line.strip():
            insertion_idx = last_table_row_idx + 1
            break

    if insertion_idx is None:
        if last_table_row_idx is not None:
            insertion_idx = last_table_row_idx + 1
        else:
            return False  # Couldn't find table

    lines.insert(insertion_idx, new_row)
    agents_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return True


def render_last_run(
    slug: str,
    target: str,
    destination: Path,
    mode: str,
    para_tagged: int,
    para_matched: int,
    agents_updated: bool,
    duration: float,
    today_iso: str,
) -> str:
    return (
        "# atlas-graduate — last run\n"
        "\n"
        f"- timestamp: {today_iso}\n"
        f"- slug: {slug}\n"
        f"- target: {target}\n"
        f"- destination: {destination}\n"
        f"- mode: {mode}\n"
        f"- para_notes_matched: {para_matched}\n"
        f"- para_notes_tagged: {para_tagged}\n"
        f"- agents_md_updated: {str(agents_updated).lower()}\n"
        f"- duration_seconds: {duration:.2f}\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Graduate an emerging-patterns slug.")
    ap.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--report-path", type=Path, default=None)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--target", choices=list(TARGET_PATHS.keys()), default=None)
    ap.add_argument("--auto", action="store_true",
                    help="Unattended mode (DEC-019): infer target when --target is "
                         "omitted; stamp graduation_mode: auto on the page + last-run.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--today", type=str, default=None)
    args = ap.parse_args()

    start = time.time()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    report_path = args.report_path or (vault / CFG.folder_name("meta") / "Dashboards" / "Emerging-Patterns.md")
    if not report_path.exists():
        print(f"emerge report not found: {report_path}", file=sys.stderr)
        return 2

    patterns = parse_emerge_report(report_path)
    pattern = patterns.get(args.slug)
    if pattern is None and args.force and args.target is not None:
        # --force re-graduation: try recovering pattern data from existing destination.
        dest_dir_template, file_template, _ = TARGET_PATHS[args.target]
        candidate_dest = vault / dest_dir_template.format(slug=args.slug) / file_template.format(slug=args.slug)
        if candidate_dest.exists():
            existing = candidate_dest.read_text(encoding="utf-8")
            fm, _, _ = parse_frontmatter(existing)
            if fm and fm.get("canonical_id") == args.slug:
                first = fm.get("first_mention", "")
                latest = fm.get("last_mention", "")
                count = fm.get("mention_count", 0)
                sources = fm.get("sources") or []
                if isinstance(sources, str):
                    sources = [s.strip() for s in sources.strip("[]").split(",") if s.strip()]
                pattern = {
                    "keywords": [args.slug, args.slug.replace("-", " ")],
                    "first": str(first)[:10],
                    "latest": str(latest)[:10],
                    "items": int(count) if isinstance(count, (int, str)) and str(count).isdigit() else 0,
                    "sources": [str(s) for s in sources],
                }
                print(f"--force: slug not in emerge report; recovered pattern from existing destination", file=sys.stderr)
    if pattern is None:
        print(f"slug '{args.slug}' not in report. Available:", file=sys.stderr)
        for s in sorted(patterns):
            print(f"  - {s}", file=sys.stderr)
        return 2

    if args.target is None:
        if args.auto:
            args.target = infer_target(vault, args.slug)
            print(f"--auto: inferred target '{args.target}' for slug '{args.slug}'")
        else:
            print(f"slug '{args.slug}' found. Pick a target:")
            for t in TARGET_PATHS:
                print(f"  --target {t}")
            return 0

    # Compute destination
    dest_dir_template, file_template, _ = TARGET_PATHS[args.target]
    dest_dir = vault / dest_dir_template.format(slug=args.slug)
    dest_path = dest_dir / file_template.format(slug=args.slug)

    existing_text: str | None = None
    if dest_path.exists():
        if not args.force:
            print(f"destination already exists (use --force to regenerate): {dest_path}",
                  file=sys.stderr)
            return 3
        existing_text = dest_path.read_text(encoding="utf-8")

    pats = keyword_match_patterns(pattern["keywords"], args.slug)
    matches = walk_para(vault, pats)
    contributors = aggregate_contributors(matches)
    raw_sources = related_raw_sources(vault, pats)

    existing_editable = extract_editable(existing_text) if existing_text else None

    page_text = render_destination_page(
        slug=args.slug,
        target=args.target,
        pattern=pattern,
        contributors=contributors,
        raw_sources=raw_sources,
        today=today,
        existing_editable=existing_editable,
        graduation_mode="auto" if args.auto else "manual",
    )

    para_tagged = 0
    new_tag = f"thread/{args.slug}"

    if args.dry_run:
        agents_updated_plan = False
        agents_path = vault / "AGENTS.md"
        if agents_path.exists():
            agents_text = agents_path.read_text(encoding="utf-8")
            has_row = any(m.group(1) == args.slug for m in THREAD_ROW_RE.finditer(agents_text))
            agents_updated_plan = not has_row
        para_would_tag = 0
        for m in matches:
            new_text, changed = add_tag_to_frontmatter(m["raw_text"], new_tag)
            if changed:
                para_would_tag += 1
        duration = time.time() - start
        print(f"=== dry-run for slug={args.slug} target={args.target} ===")
        print(f"destination: {dest_path}")
        print(f"PARA notes matched: {len(matches)}")
        print(f"PARA notes would tag: {para_would_tag}")
        print(f"AGENTS.md would update: {agents_updated_plan}")
        print(f"contributors: {contributors[:5]}")
        print(f"raw sources sampled: {len(raw_sources)}")
        print(f"--- page preview (first 40 lines) ---")
        for line in page_text.splitlines()[:40]:
            print(line)
        return 0

    # Execute
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(page_text, encoding="utf-8")

    for m in matches:
        new_text, changed = add_tag_to_frontmatter(m["raw_text"], new_tag)
        if changed:
            m["path"].write_text(new_text, encoding="utf-8")
            para_tagged += 1

    agents_path = vault / "AGENTS.md"
    agents_updated = update_agents_md(agents_path, args.slug, today)

    duration = time.time() - start
    today_iso = dt.datetime.now().replace(microsecond=0).isoformat()

    last_run_path = SKILL_DIR / "last-run.md"
    last_run_path.write_text(
        render_last_run(
            slug=args.slug,
            target=args.target,
            destination=dest_path,
            mode="execute-auto" if args.auto else "execute",
            para_tagged=para_tagged,
            para_matched=len(matches),
            agents_updated=agents_updated,
            duration=duration,
            today_iso=today_iso,
        ),
        encoding="utf-8",
    )

    print(f"wrote {dest_path}")
    print(f"para_notes_matched={len(matches)} para_notes_tagged={para_tagged} "
          f"agents_md_updated={agents_updated} duration={duration:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
