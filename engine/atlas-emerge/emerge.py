#!/usr/bin/env python3
"""atlas-emerge — surface unnamed patterns from raw/ within a sliding window.

See SKILL.md for the contract. This implementation is the v1 rule-based
extractor (no LLM). It walks raw/*/ for items within the window, extracts
candidate phrases, dedupes against wiki/entities/, wiki/concepts/, and
#thread/<slug> tags, ranks, and writes Emerging-Patterns.md + last-run.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT_DEFAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent

SOURCE_DIRS = ["fireflies", "wispr", "gemini", "teams", "zoom", "gong", "claude-history", "github",
               "gmail", "slack", "monday", "distill"]
# Patterns a run is expected to surface before last-run.md carries a diagnostic. Small
# on purpose: a new vault has little corpus, and zero patterns is a fact, not a failure.
# Raise it with --min-patterns once the corpus is large enough that a low count means
# the thresholds are too strict.
MIN_PATTERNS_DEFAULT = 1

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
HASHTAG_RE = re.compile(r"(?:^|\s)#([a-z][\w/-]*)", re.IGNORECASE)
CAP_PHRASE_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[ -][A-Z][a-zA-Z0-9]+){1,3})\b")
KEBAB_RE = re.compile(r"\b([a-z][a-z0-9]+(?:-[a-z0-9]+){1,4})\b")
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—-]?\s*")
PAREN_DUR_RE = re.compile(r"\s*\([^)]*\d+\s*[smh][^)]*\)\s*", re.IGNORECASE)

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "these", "those",
    "have", "has", "had", "was", "were", "are", "you", "your", "their",
    "what", "when", "where", "why", "how", "but", "not", "all", "any",
    "some", "into", "out", "about", "more", "less", "than", "then", "now",
    "next", "last", "previous", "first", "second", "third",
    "meeting", "meetings", "call", "calls", "discussion", "discussions",
    "note", "notes", "follow", "follow-up", "follow-ups", "followup",
    "action", "actions", "item", "items", "step", "steps", "task", "tasks",
    "thanks", "today", "yesterday", "tomorrow", "week", "month", "year",
    "day", "days", "weeks", "months", "years", "time", "times",
    "morning", "afternoon", "evening", "night",
    "intro", "outro", "summary", "summaries",
    "good", "great", "nice", "okay", "yeah", "sure", "right", "wrong",
    "really", "very", "just", "only", "also", "even", "still", "much",
    "want", "need", "needs", "needed", "would", "could", "should", "might",
    "make", "made", "making", "makes",
    "look", "looking", "looks", "looked",
    "think", "thinking", "thinks", "thought",
    "talk", "talking", "talks", "talked",
    "work", "working", "works", "worked",
    "see", "seen", "sees", "saw",
    "get", "got", "getting", "gets",
    "going", "gone",
    "say", "said", "says", "saying",
    "use", "used", "using", "uses",
    "thing", "things", "stuff",
    "people", "person", "user", "users",
    "way", "ways",
    "let", "lets", "letting",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten",
    "true", "false", "none", "null",
}

ATLAS_NOISE = {
    "next-steps", "action-items", "follow-up", "follow-ups",
    "click-here", "view-meeting", "view-here",
    "high-priority", "low-priority", "medium-priority",
    "to-do", "to-dos", "todo", "todos",
    "in-progress", "done", "not-started",
}

GOVERNANCE_TAG_PREFIXES = ("thread/", "client/", "status/", "area/", "project/")


def kebab(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def fuzzy_key(s: str) -> str:
    """Normalize for entity-equivalence comparison."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


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


def parse_frontmatter(text: str) -> dict | None:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    return parse_frontmatter_block(m.group(1))


def extract_date(fm: dict, file_path: Path) -> dt.date | None:
    for key in ("date", "timestamp", "started", "created_at", "ingested_at"):
        v = fm.get(key)
        if v is None:
            continue
        if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
            return v
        if isinstance(v, dt.datetime):
            return v.date()
        if isinstance(v, str):
            try:
                return dt.date.fromisoformat(v[:10])
            except ValueError:
                continue
    try:
        return dt.date.fromtimestamp(file_path.stat().st_mtime)
    except OSError:
        return None


def body_of(text: str) -> str:
    m = FRONTMATTER_RE.match(text)
    if m:
        return text[m.end():]
    return text


def title_of(body: str) -> str:
    m = TITLE_RE.search(body)
    if not m:
        return ""
    raw = m.group(1).strip()
    raw = DATE_PREFIX_RE.sub("", raw)
    raw = PAREN_DUR_RE.sub("", raw)
    return raw.strip()


def source_type_for(path: Path, vault: Path) -> str:
    try:
        rel = path.relative_to(vault / CFG.folder_name("raw"))
    except ValueError:
        return "unknown"
    return rel.parts[0] if rel.parts else "unknown"


def build_deny_list(vault: Path, skill_dir: Path) -> set[str]:
    deny: set[str] = set()

    # wiki/entities/*.md canonical_id
    entities_dir = vault / CFG.folder_name("wiki") / "entities"
    if entities_dir.exists():
        for p in entities_dir.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
                fm = parse_frontmatter(text) or {}
                cid = fm.get("canonical_id")
                name = fm.get("entity_name")
                if cid:
                    deny.add(fuzzy_key(str(cid)))
                if name:
                    deny.add(fuzzy_key(str(name)))
                # Also drop the filename stem
                deny.add(fuzzy_key(p.stem))
            except OSError:
                continue

    # wiki/concepts/*.md filename stems
    concepts_dir = vault / CFG.folder_name("wiki") / "concepts"
    if concepts_dir.exists():
        for p in concepts_dir.glob("*.md"):
            deny.add(fuzzy_key(p.stem))

    # CRM/People/*.md filename stems (people aren't concept patterns)
    people_dir = vault / CFG.folder_name("crm") / "People"
    if people_dir.exists():
        for p in people_dir.glob("*.md"):
            deny.add(fuzzy_key(p.stem))

    # #thread/<slug> tags grep-wide
    for p in vault.rglob("*.md"):
        # Skip the raw/ tree itself (we're looking for tags in PARA/wiki/etc.)
        try:
            rel = p.relative_to(vault)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == CFG.folder_name("raw"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in HASHTAG_RE.finditer(text):
            tag = m.group(1).lower()
            if tag.startswith("thread/"):
                slug = tag[len("thread/"):]
                if slug:
                    deny.add(fuzzy_key(slug))

    # suppress.txt
    suppress_file = skill_dir / "suppress.txt"
    if suppress_file.exists():
        for line in suppress_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            deny.add(fuzzy_key(line))

    # Stopword baseline
    for w in STOPWORDS:
        deny.add(fuzzy_key(w))
    for w in ATLAS_NOISE:
        deny.add(fuzzy_key(w))

    return deny


def walk_raw(vault: Path, window_days: int, today: dt.date) -> list[dict]:
    """Return list of {path, source, date, title, body} for items in window."""
    raw_root = vault / CFG.folder_name("raw")
    cutoff = today - dt.timedelta(days=window_days)
    items: list[dict] = []

    for src in SOURCE_DIRS:
        src_dir = raw_root / src
        if not src_dir.exists():
            continue
        for p in src_dir.rglob("*.md"):
            if p.name.startswith("."):
                continue
            if p.name == "README.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm = parse_frontmatter(text) or {}
            d = extract_date(fm, p)
            if d is None or d < cutoff:
                continue
            body = body_of(text)
            title = title_of(body)
            items.append({
                "path": p,
                "source": src,
                "date": d,
                "title": title,
                "body": body,
            })
    return items


def extract_candidates(item: dict) -> list[str]:
    """Return list of candidate kebab-slugs for one item."""
    candidates: list[str] = []
    title = item["title"]
    body = item["body"][:1000]
    combined = title + "\n" + body

    # Hashtags (non-governance)
    for m in HASHTAG_RE.finditer(combined):
        tag = m.group(1).lower()
        if any(tag.startswith(p) for p in GOVERNANCE_TAG_PREFIXES):
            continue
        candidates.append(kebab(tag.replace("/", "-")))

    # Wikilinks — strip surrounding spaces, drop date-only stamps
    for m in WIKILINK_RE.finditer(combined):
        target = m.group(1).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", target):
            continue
        slug = kebab(target)
        if slug:
            candidates.append(slug)

    # Capitalized multi-word phrases
    for m in CAP_PHRASE_RE.finditer(combined):
        candidates.append(kebab(m.group(1)))

    # Existing kebab-case tokens
    for m in KEBAB_RE.finditer(combined.lower()):
        candidates.append(m.group(1))

    return candidates


def cluster_candidates(items: list[dict], deny: set[str]) -> dict[str, dict]:
    """Cluster by kebab-slug, applying deny-list filter. Returns slug -> cluster."""
    clusters: dict[str, dict] = defaultdict(lambda: {
        "variants": set(),
        "items": set(),
        "sources": set(),
        "dates": [],
    })
    for item in items:
        seen_in_item: set[str] = set()
        for cand in extract_candidates(item):
            slug = cand.strip()
            if not slug:
                continue
            if len(slug) < 5:
                continue
            if slug.isdigit():
                continue
            # Single-token short slugs filter (already covered by len<5)
            # Deny-list filter (fuzzy)
            if fuzzy_key(slug) in deny:
                continue
            # Atlas-noise filter
            if slug in ATLAS_NOISE:
                continue
            seen_in_item.add(slug)
        for slug in seen_in_item:
            c = clusters[slug]
            c["variants"].add(slug)
            c["items"].add(str(item["path"]))
            c["sources"].add(item["source"])
            c["dates"].append(item["date"])
    return clusters


def rank_clusters(clusters: dict[str, dict], min_items: int, min_sources: int) -> list[dict]:
    ranked: list[dict] = []
    for slug, c in clusters.items():
        items_n = len(c["items"])
        sources_n = len(c["sources"])
        if items_n < min_items:
            continue
        if sources_n < min_sources:
            continue
        first = min(c["dates"])
        latest = max(c["dates"])
        score = items_n * (1.0 + (sources_n - 1) * 0.5)
        ranked.append({
            "slug": slug,
            "variants": sorted(c["variants"]),
            "items": items_n,
            "sources": sorted(c["sources"]),
            "first": first,
            "latest": latest,
            "score": score,
        })
    ranked.sort(key=lambda r: (-r["score"], -r["items"], r["slug"]))
    return ranked


def render_report(
    patterns: list[dict],
    window_days: int,
    window_start: dt.date,
    window_end: dt.date,
    items_scanned: int,
    source_dirs: list[str],
    deny_size: int,
    today: dt.date,
) -> str:
    sources_str = ", ".join(source_dirs)
    rows: list[str] = []
    for i, p in enumerate(patterns, start=1):
        keywords = "; ".join(p["variants"][:3])
        sources = ", ".join(p["sources"])
        slug = p["slug"]
        rows.append(
            f"| {i} | {keywords} | {p['first'].isoformat()} | {p['latest'].isoformat()} | "
            f"{p['items']} | {sources} | `{slug}` | [Graduate](/atlas-graduate {slug}) |"
        )
    table_body = "\n".join(rows) if rows else "| — | _(no patterns surfaced)_ | | | 0 | | | |"

    return (
        "---\n"
        "type: dashboard-emerging-patterns\n"
        "generated_by: atlas-emerge\n"
        f"generated_at: {today.isoformat()}\n"
        f"window_days: {window_days}\n"
        f"window_start: {window_start.isoformat()}\n"
        f"window_end: {window_end.isoformat()}\n"
        f"items_scanned: {items_scanned}\n"
        f"patterns_surfaced: {len(patterns)}\n"
        "tags: [dashboard, emerging-patterns]\n"
        "---\n"
        "\n"
        f"# Emerging Patterns — last {window_days} days\n"
        "\n"
        f"Generated {today.isoformat()} by `atlas-emerge`. "
        f"Window: `{window_start.isoformat()}` → `{window_end.isoformat()}`. "
        f"Scanned `{items_scanned}` raw items across `{len(source_dirs)}` source types.\n"
        "\n"
        "This is a regenerated projection of recurring topics in `raw/` that don't yet have a "
        "`#thread/<slug>` tag or a `wiki/concepts/<slug>.md` page. "
        "Promote a pattern with `/atlas-graduate <slug>`.\n"
        "\n"
        "## Patterns\n"
        "\n"
        "| # | Keywords | First | Latest | Items | Sources | Slug | Graduate |\n"
        "|---|---|---|---|---|---|---|---|\n"
        f"{table_body}\n"
        "\n"
        "## Methodology\n"
        "\n"
        f"- Window: last {window_days} days from generation date.\n"
        f"- Sources walked: {sources_str}.\n"
        f"- Deny-list size: {deny_size} known identifiers (entities, concepts, threads, stopwords).\n"
        "- Minimum thresholds: ≥ 3 distinct items, ≥ 2 source types, ≥ 5 chars per slug.\n"
        "\n"
        "## Notes\n"
        "\n"
        "- Hand-edit nothing in this file — it's regenerated on every run.\n"
        "- To suppress a false-positive cluster permanently, add its slug to "
        f"`{SKILL_DIR / 'suppress.txt'}` (one slug per line).\n"
        "- To force-promote a pattern to a tracked surface, run `/atlas-graduate <slug>`.\n"
    )


def render_last_run(
    mode: str,
    window_days: int,
    items_scanned: int,
    candidates_extracted: int,
    candidates_after_dedup: int,
    patterns_surfaced: int,
    duration_seconds: float,
    report_path: str,
    today_iso: str,
    min_patterns: int = MIN_PATTERNS_DEFAULT,
) -> str:
    met = "true" if patterns_surfaced >= min_patterns else "false"
    body = ""
    if patterns_surfaced < min_patterns:
        body = (
            "\n## Diagnostic\n\n"
            f"Fewer than {min_patterns} pattern(s) surfaced ({patterns_surfaced}); candidates "
            f"{candidates_after_dedup} after dedup ({candidates_extracted} pre-dedup). "
            "A small corpus surfaces little; extend `--window-days` if the window is too short, "
            "or lower `--min-items` / `--min-sources` if signal is genuinely scarce.\n"
        )
    return (
        "# atlas-emerge — last run\n"
        "\n"
        f"- timestamp: {today_iso}\n"
        f"- mode: {mode}\n"
        f"- window_days: {window_days}\n"
        f"- items_scanned: {items_scanned}\n"
        f"- candidates_extracted: {candidates_extracted}\n"
        f"- candidates_after_dedup: {candidates_after_dedup}\n"
        f"- patterns_surfaced: {patterns_surfaced}\n"
        f"- duration_seconds: {duration_seconds:.2f}\n"
        f"- min_patterns: {min_patterns}\n"
        f"- min_patterns_met: {met}\n"
        f"- report_path: {report_path}\n"
        f"{body}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Surface emerging patterns from raw/.")
    ap.add_argument("--vault", type=Path, default=None, help="Vault root (default: vault_root from Atlas config)")
    ap.add_argument("--window-days", type=int, default=30, help="Sliding window in days (default: 30)")
    ap.add_argument("--top", type=int, default=25, help="Max patterns to surface (default: 25)")
    ap.add_argument("--min-items", type=int, default=3, help="Minimum distinct items per cluster")
    ap.add_argument("--min-sources", type=int, default=2, help="Minimum distinct source types per cluster")
    ap.add_argument("--min-patterns", type=int, default=MIN_PATTERNS_DEFAULT,
                    help=f"Patterns expected per run; fewer adds a diagnostic to last-run.md (default: {MIN_PATTERNS_DEFAULT})")
    ap.add_argument("--dry-run", action="store_true", help="Print summary only; don't write files")
    ap.add_argument("--report-path", type=Path, default=None, help="Override report output path")
    ap.add_argument("--today", type=str, default=None, help="Override today date (YYYY-MM-DD)")
    args = ap.parse_args()

    start_t = time.time()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    window_start = today - dt.timedelta(days=args.window_days)

    vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    deny = build_deny_list(vault, SKILL_DIR)
    items = walk_raw(vault, args.window_days, today)
    clusters = cluster_candidates(items, deny)
    candidates_extracted = sum(len(c["items"]) for c in clusters.values())
    candidates_after_dedup = len(clusters)
    patterns = rank_clusters(clusters, args.min_items, args.min_sources)[: args.top]

    duration = time.time() - start_t
    today_iso = dt.datetime.now().replace(microsecond=0).isoformat()

    report_path = args.report_path or (vault / CFG.folder_name("meta") / "Dashboards" / "Emerging-Patterns.md")
    last_run_path = SKILL_DIR / "last-run.md"

    report_text = render_report(
        patterns,
        args.window_days,
        window_start,
        today,
        len(items),
        SOURCE_DIRS,
        len(deny),
        today,
    )
    last_run_text = render_last_run(
        mode="dry-run" if args.dry_run else "execute",
        window_days=args.window_days,
        items_scanned=len(items),
        candidates_extracted=candidates_extracted,
        candidates_after_dedup=candidates_after_dedup,
        patterns_surfaced=len(patterns),
        duration_seconds=duration,
        report_path=str(report_path),
        today_iso=today_iso,
        min_patterns=args.min_patterns,
    )

    if args.dry_run:
        print(f"=== dry-run ===")
        print(f"items_scanned: {len(items)}")
        print(f"candidates_after_dedup: {candidates_after_dedup}")
        print(f"patterns_surfaced: {len(patterns)}")
        print(f"report would write to: {report_path}")
        print(f"--- last-run.md preview ---")
        print(last_run_text)
        return 0

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")
    last_run_path.write_text(last_run_text, encoding="utf-8")

    print(f"wrote {report_path}")
    print(f"wrote {last_run_path}")
    print(f"items_scanned={len(items)} candidates_after_dedup={candidates_after_dedup} "
          f"patterns_surfaced={len(patterns)} duration={duration:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
