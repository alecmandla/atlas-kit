#!/usr/bin/env python3
"""atlas-synthesize — deterministic evidence gatherer + change detector (DEC-020).

The narrative half of `atlas-synthesize` is written by the orchestrating Claude
session (see SKILL.md), exactly as `atlas-weekly` writes its review. This script
owns only the deterministic, token-bounded part:

  --list [--stale-only]   Enumerate every #thread/<slug> in the vault. For each,
                          compute an evidence fingerprint and compare it to the
                          fingerprint stored in wiki/synthesis/<slug>.md, so the
                          orchestrator re-synthesizes ONLY changed threads.

  --thread <slug>         Emit a ranked, citation-ready, token-bounded evidence
                          bundle for one thread: the PARA notes tagged with it +
                          the best raw/ matches across sources (recency +
                          source-diversity capped). Includes a ready-to-paste
                          FRONTMATTER block (fingerprint, counts, dates, sources).

raw/ is read-only (DEC-009). This script never writes to the vault.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT_DEFAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
THREAD_TAG_RE = re.compile(r"#thread/([a-z0-9][a-z0-9-]*)")

PARA_DIRS = [CFG.folder_name(k) for k in ("inbox", "projects", "areas", "resources")]
RAW_SOURCE_ORDER = ["fireflies", "gmail", "slack", "monday", "github",
                    "claude-history", "wispr"]

# Token-bounding caps (DEC-020). Keep the bundle inside a few-thousand-token budget.
PER_SOURCE_CAP = 5       # at most N raw items per source (diversity)
TOTAL_RAW_CAP = 20       # at most N raw items total
RAW_EXCERPT = 600        # chars of body per raw item
PARA_EXCERPT = 1000      # chars of body per tagged PARA note (curated → richer)


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


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse frontmatter WITHOUT PyYAML — headless-safe, mirrors atlas-graduate
    (DEC-020). Small YAML subset: `key: scalar`, `key: [inline list]`, and
    `key:` + `- block list`. Sufficient for Atlas's flat frontmatter; date
    scalars come back as strings (call sites already coerce)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict = {}
    pending_key = None  # key awaiting block-list items
    for raw_line in m.group(1).split("\n"):
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
    return fm, text[m.end():]


def note_title(body: str, fallback: str) -> str:
    tm = TITLE_RE.search(body)
    return tm.group(1).strip() if tm else fallback


def note_date(fm: dict, path: Path) -> str:
    for key in ("date", "timestamp", "started", "created_at"):
        v = fm.get(key)
        if isinstance(v, (dt.date, dt.datetime)):
            return v.isoformat()[:10]
        if isinstance(v, str) and v.strip():
            return v.strip()[:10]
    try:
        return dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return ""


def excerpt(body: str, n: int) -> str:
    # Drop the leading H1 title line, collapse whitespace, trim to n chars.
    body = TITLE_RE.sub("", body, count=1)
    flat = " ".join(body.split())
    return (flat[:n] + "…") if len(flat) > n else flat


def keyword_patterns(slug: str, extra: list[str]) -> list[re.Pattern]:
    targets = {slug, slug.replace("-", " ")}
    for kw in extra:
        kw = kw.strip().lower()
        if kw:
            targets.add(kw)
            targets.add(kw.replace("-", " "))
    pats = []
    for t in sorted(targets, key=len, reverse=True):
        if len(t) < 4:
            continue
        pats.append(re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE))
    return pats


def matches(haystack: str, pats: list[re.Pattern]) -> bool:
    return any(p.search(haystack) for p in pats)


def discover_threads(vault: Path) -> dict[str, None]:
    """All distinct #thread/<slug> slugs across the vault (excluding raw/)."""
    slugs: dict[str, None] = {}
    for para in PARA_DIRS + [CFG.folder_name(k) for k in ("wiki", "archive", "meta", "crm")]:
        root = vault / para
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for m in THREAD_TAG_RE.finditer(text):
                slug = m.group(1)
                if slug:
                    slugs.setdefault(slug, None)
    return slugs


def thread_keywords(vault: Path, slug: str) -> list[str]:
    """Slug-derived keywords, enriched from a graduated concept page if present."""
    extra: list[str] = []
    concept = vault / CFG.folder_name("wiki") / "concepts" / f"{slug}.md"
    if concept.exists():
        fm, _ = parse_frontmatter(concept.read_text(encoding="utf-8"))
        kw = fm.get("keywords")
        if isinstance(kw, list):
            extra.extend(str(k) for k in kw)
    return extra


def gather_para(vault: Path, slug: str) -> list[dict]:
    """PARA notes carrying the #thread/<slug> tag (authoritative, curated)."""
    tag = f"thread/{slug}"
    out: list[dict] = []
    for para in PARA_DIRS:
        root = vault / para
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if f"#thread/{slug}" not in text and tag not in text:
                continue
            fm, body = parse_frontmatter(text)
            tags = fm.get("tags") or []
            tagged = (f"thread/{slug}" in [str(t) for t in tags]
                      if isinstance(tags, list) else False) or f"#thread/{slug}" in text
            if not tagged:
                continue
            out.append({
                "path": str(p.relative_to(vault)),
                "id": p.stem,
                "date": note_date(fm, p),
                "title": note_title(body, p.stem),
                "excerpt": excerpt(body, PARA_EXCERPT),
                "mtime": int(p.stat().st_mtime),
            })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def gather_raw(vault: Path, pats: list[re.Pattern]) -> list[dict]:
    """Best raw/ matches: per-source cap + total cap, recency-ranked, diverse."""
    by_source: dict[str, list[dict]] = {}
    raw_root = vault / CFG.folder_name("raw")
    if not raw_root.exists():
        return []
    for src_dir in raw_root.iterdir():
        if not src_dir.is_dir():
            continue
        src = src_dir.name
        for p in src_dir.rglob("*.md"):
            if p.name == "README.md":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm, body = parse_frontmatter(text)
            title = note_title(body, p.stem)
            if not matches(title + "\n" + body[:2500], pats):
                continue
            by_source.setdefault(src, []).append({
                "source": src,
                "path": str(p.relative_to(vault)),
                "id": p.stem,
                "date": note_date(fm, p),
                "title": title,
                "excerpt": excerpt(body, RAW_EXCERPT),
                "mtime": int(p.stat().st_mtime),
            })
    # Per-source recency cap, then merge + total recency cap.
    picked: list[dict] = []
    for src in by_source:
        items = sorted(by_source[src], key=lambda r: r["date"], reverse=True)
        picked.extend(items[:PER_SOURCE_CAP])
    picked.sort(key=lambda r: r["date"], reverse=True)
    return picked[:TOTAL_RAW_CAP]


def fingerprint(para: list[dict], raw: list[dict]) -> str:
    """Stable hash over the evidence set — changes iff the evidence changes."""
    sig = sorted(f"{r['path']}:{r['mtime']}" for r in (para + raw))
    return hashlib.sha1("\n".join(sig).encode("utf-8")).hexdigest()[:16]


def stored_fingerprint(vault: Path, slug: str) -> str | None:
    page = vault / CFG.folder_name("wiki") / "synthesis" / f"{slug}.md"
    if not page.exists():
        return None
    fm, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    fp = fm.get("evidence_fingerprint")
    return str(fp) if fp else None


def evidence_for(vault: Path, slug: str) -> tuple[list[dict], list[dict]]:
    pats = keyword_patterns(slug, thread_keywords(vault, slug))
    return gather_para(vault, slug), gather_raw(vault, pats)


def all_sources(para: list[dict], raw: list[dict]) -> list[str]:
    seen = [r["source"] for r in raw]
    if para:
        seen = ["para"] + seen
    ordered = [s for s in (["para"] + RAW_SOURCE_ORDER) if s in seen]
    return ordered


def span(para: list[dict], raw: list[dict]) -> tuple[str, str]:
    dates = [r["date"] for r in (para + raw) if r["date"]]
    return (min(dates), max(dates)) if dates else ("", "")


def cmd_list(vault: Path, stale_only: bool) -> int:
    slugs = discover_threads(vault)
    rows = []
    for slug in sorted(slugs):
        para, raw = evidence_for(vault, slug)
        fp = fingerprint(para, raw)
        stored = stored_fingerprint(vault, slug)
        if stored is None:
            status = "new"
        elif stored != fp:
            status = "changed"
        else:
            status = "current"
        rows.append((status, slug, len(para) + len(raw), fp))
    if stale_only:
        for status, slug, _n, _fp in rows:
            if status in ("new", "changed"):
                print(slug)
        return 0
    print(f"{'STATUS':9} {'N':>4}  SLUG")
    for status, slug, n, _fp in rows:
        print(f"{status:9} {n:>4}  {slug}")
    stale = sum(1 for r in rows if r[0] in ("new", "changed"))
    print(f"\n{len(rows)} threads · {stale} need (re)synthesis")
    return 0


def cmd_thread(vault: Path, slug: str) -> int:
    para, raw = evidence_for(vault, slug)
    if not para and not raw:
        print(f"no evidence found for thread '{slug}'", file=sys.stderr)
        return 1
    fp = fingerprint(para, raw)
    srcs = all_sources(para, raw)
    first, last = span(para, raw)

    out: list[str] = []
    out.append(f"# Evidence bundle — thread/{slug}")
    out.append("")
    out.append("## FRONTMATTER (paste into the synthesis page)")
    out.append("```yaml")
    out.append("type: wiki-synthesis")
    out.append(f"canonical_id: {slug}")
    out.append("generated_by: atlas-synthesize")
    out.append(f"evidence_fingerprint: {fp}")
    out.append(f"evidence_count: {len(para) + len(raw)}")
    out.append(f"sources: [{', '.join(srcs)}]")
    out.append(f"first_evidence: {first}")
    out.append(f"last_evidence: {last}")
    out.append(f"tags: [wiki-synthesis, thread/{slug}]")
    out.append("```")
    out.append("")
    out.append(f"Evidence: {len(para)} tagged PARA note(s) + {len(raw)} raw item(s) "
               f"across {len(srcs)} source(s), spanning {first or '?'}–{last or '?'}. "
               "Cite each claim with the item's `[[id]]`. raw/ items below are the "
               "top matches (recency + source-diversity capped); they are NOT the "
               "full set — say so if coverage looks thin.")
    out.append("")

    if para:
        out.append("## Tagged PARA notes (authoritative)")
        for r in para:
            out.append(f"\n### [[{r['id']}]] — {r['date']} · `{r['path']}`")
            out.append(f"**{r['title']}**")
            out.append(r["excerpt"])
        out.append("")

    if raw:
        out.append("## Raw source matches (ranked)")
        for r in raw:
            out.append(f"\n### [[{r['id']}]] — {r['date']} · {r['source']} · `{r['path']}`")
            out.append(f"**{r['title']}**")
            out.append(r["excerpt"])
        out.append("")

    print("\n".join(out))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="atlas-synthesize evidence gatherer (DEC-020).")
    ap.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--list", action="store_true", help="List threads + staleness.")
    ap.add_argument("--stale-only", action="store_true",
                    help="With --list: print only slugs needing (re)synthesis.")
    ap.add_argument("--thread", type=str, default=None, help="Emit one thread's bundle.")
    args = ap.parse_args()

    vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    if args.thread:
        return cmd_thread(vault, args.thread)
    if args.list:
        return cmd_list(vault, args.stale_only)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
