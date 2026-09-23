#!/usr/bin/env python3
"""Monday tier-4 routing replay (dry run).

Reads:
- <vault_root>/raw/monday/<workspace>/<board>/<item>.md   (atlas-monday-ingest output)
- meeting-routing.yaml next to this script                 (routes + monday_routes)
- <vault_root>/<inbox>/needs-decision/*.md                (current fallback files)

Stdlib only (DEC-021): frontmatter and the routing YAML are read with the
small YAML-subset parsers below, not PyYAML.

Simulates: what if the new Monday tier-4 were applied to these fallback files?

Match policy:
- TIER-4a (email-exact): any participant email matches a Monday item's email column → route to that board's destination.
- TIER-4b (full-name substring): the FULL normalized item name (≥2 tokens, ≥8 chars after stopword removal)
  must appear as a case-insensitive substring of the meeting title.
- Single-token matches are REJECTED: single tokens carry a high false-positive rate.

Outputs: a markdown report (`--out`, default `dry_run_monday_tier.md` in the current directory).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
SKILL_DIR = Path(__file__).resolve().parent
VAULT = CFG.vault_root
RAW_MONDAY = CFG.folder("raw") / "monday"
INBOX_NEEDS = CFG.folder("inbox") / "needs-decision"
ROUTING_YAML = SKILL_DIR / "meeting-routing.yaml"

# Tier-3 destinations whose meetings should be scanned for the "tier-4 still
# attaches monday_context even when not the deciding tier" guarantee. The
# internal-1:1 rule sends "<coworker> and <owner>" meetings here; many of
# those participants ALSO appear on a Monday item, so the dry run reports
# them as a secondary count.
TIER3_ATTACH_SCAN_DIRS = [
    CFG.folder("areas") / "HQ" / "meetings",
]

EMAIL_RX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
WORD_RX = re.compile(r"[a-zA-Z0-9]+")

# Internal domains (your own organization) — emails on these are owners or
# teammates, never the customer contact. They appear in forwarded email
# threads inside long_text columns and would create massive false positives
# if matched against meeting attendees. Skip them at index time and again at
# lookup time.
INTERNAL_EMAIL_DOMAINS = {
    "harborlane.example",
}

# Stopwords drop from item-name normalization. A pragmatic set: generic
# industry tokens ("inn", "lodge", ...) match far too many titles on
# their own. Tune it to your own industry vocabulary.
STOPWORDS = {
    "the", "a", "an", "of", "and", "at", "by", "in", "on", "to", "for",
    "with", "from", "or",
    "inn", "lodge", "hotel", "suites", "spa",
    "resort", "ridge", "hills", "valley", "lake", "lakes", "creek",
    "national", "park", "city", "company", "co", "inc", "llc", "ltd",
    "group", "international",
}

# Stage column ids that commonly hold a board's primary status label.
# Used to fish out a `stage:` value for the routed-note frontmatter.
STAGE_COLUMN_CANDIDATES = ("status9", "status", "status__1", "status4")

# Owner column ids that commonly hold the person assigned to a Monday item.
OWNER_COLUMN_CANDIDATES = ("person", "people")


@dataclass
class MondayItem:
    item_id: str
    item_name: str
    board_slug: str
    board_name: str
    workspace_slug: str
    emails: list[str]
    owner: str
    stage: str


_KV_RE = re.compile(r"^([^:\s][^:]*):(.*)$")


def _strip_scalar(v: str):
    """Strip surrounding quotes from a scalar value; empty -> None (yaml parity).
    Copied from atlas-emerge (DEC-021)."""
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
    """Parse a frontmatter block without PyYAML (DEC-021).

    The atlas-emerge subset (`key: scalar`, `key: [a, b]`, `key:` + `- item`)
    plus block lists of flat mappings (`key:` + `- k: v` / `  k2: v2`), which is
    how atlas-monday-ingest emits `column_values` rows. Scalars come back as
    quote-stripped strings (call sites already str() them); empty -> None.
    """
    fm: dict = {}
    pending_key = None   # key whose block list is being collected
    current_row = None   # mapping row being filled inside a list of mappings
    for raw_line in block.split("\n"):
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if pending_key is not None:
            m_item = re.match(r"^-\s*(.*)$", stripped)
            if m_item:
                if fm.get(pending_key) is None:
                    fm[pending_key] = []
                item = m_item.group(1)
                m_kv = _KV_RE.match(item)
                if m_kv and not (item.startswith("[") or item.startswith('"') or item.startswith("'")):
                    current_row = {m_kv.group(1).strip(): _strip_scalar(m_kv.group(2))}
                    fm[pending_key].append(current_row)
                else:
                    current_row = None
                    fm[pending_key].append(_strip_scalar(item))
                continue
            if indent > 0 and current_row is not None:
                m_kv = _KV_RE.match(stripped)
                if m_kv:
                    current_row[m_kv.group(1).strip()] = _strip_scalar(m_kv.group(2))
                continue
        if indent > 0:
            continue
        m_kv = _KV_RE.match(line)
        if not m_kv:
            continue
        key, rest = m_kv.group(1).strip(), m_kv.group(2).strip()
        pending_key, current_row = None, None
        if rest == "":
            fm[key] = None
            pending_key = key
        elif rest.startswith("[") and rest.endswith("]"):
            fm[key] = _parse_inline_list(rest)
        else:
            fm[key] = _strip_scalar(rest)
    return fm


def parse_frontmatter(text: str) -> dict:
    """Frontmatter of a note as a dict, or {} when there is no fence."""
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    return parse_frontmatter_block(text[3:end].strip())


def normalize_token(s: str) -> list[str]:
    return [t.lower() for t in WORD_RX.findall(s or "")]


def normalize_name_for_substring(name: str) -> Optional[str]:
    """Return normalized name string, or None if it doesn't pass the full-name guard.

    Guards (false-positive constraint):
    - ≥2 tokens after stopword removal
    - ≥8 chars after rejoining
    """
    tokens = [t for t in normalize_token(name) if t not in STOPWORDS and len(t) > 1]
    if len(tokens) < 2:
        return None
    joined = " ".join(tokens)
    if len(joined) < 8:
        return None
    return joined


def normalize_title_for_substring(title: str) -> str:
    tokens = normalize_token(title)
    return " ".join(tokens)


def load_monday_items() -> list[MondayItem]:
    items: list[MondayItem] = []
    if not RAW_MONDAY.exists():
        return items
    for f in RAW_MONDAY.rglob("*.md"):
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = parse_frontmatter(text)
        item_id = str(fm.get("item_id") or f.stem)
        item_name = str(fm.get("item_name") or "")
        board_slug = str(fm.get("board_slug") or f.parent.name)
        board_name = str(fm.get("board_name") or board_slug)
        workspace_slug = str(fm.get("workspace_slug") or f.parent.parent.name)

        # Emails: extract from the whole file body — text columns store them
        # under varying ids per board (text6, email0, email_address, etc.).
        # Dedup case-insensitively.
        found = EMAIL_RX.findall(text)
        emails = sorted({
            e.lower() for e in found
            if e.lower().split("@", 1)[-1] not in INTERNAL_EMAIL_DOMAINS
        })

        # Owner / stage: walk column_values rows in the YAML frontmatter.
        owner = ""
        stage = ""
        cv = fm.get("column_values") or []
        if isinstance(cv, list):
            for row in cv:
                if not isinstance(row, dict):
                    continue
                col_id = str(row.get("column_id") or "").lower()
                text_val = str(row.get("text") or "").strip()
                if not text_val or text_val.startswith("{"):
                    continue
                if not owner and col_id in OWNER_COLUMN_CANDIDATES:
                    owner = text_val
                if not stage and col_id in STAGE_COLUMN_CANDIDATES:
                    stage = text_val

        items.append(MondayItem(
            item_id=item_id,
            item_name=item_name,
            board_slug=board_slug,
            board_name=board_name,
            workspace_slug=workspace_slug,
            emails=emails,
            owner=owner,
            stage=stage,
        ))
    return items


def build_indices(items: list[MondayItem]) -> tuple[dict[str, list[MondayItem]], list[tuple[str, MondayItem]]]:
    email_idx: dict[str, list[MondayItem]] = defaultdict(list)
    name_idx: list[tuple[str, MondayItem]] = []
    for it in items:
        for e in it.emails:
            email_idx[e].append(it)
        norm = normalize_name_for_substring(it.item_name)
        if norm:
            name_idx.append((norm, it))
    return email_idx, name_idx


def load_monday_routes(yaml_path: Path) -> dict[str, dict]:
    """Read the `monday_routes:` block of meeting-routing.yaml without PyYAML
    (DEC-021): `{board_slug: {folder, project_moc, tags, ...}}`. Two-space keys
    under the section open a route; deeper `key: value` lines fill it (inline
    lists are split, scalars quote-stripped). Anything else is ignored."""
    if not yaml_path.exists():
        return {}
    routes: dict[str, dict] = {}
    in_section = False
    current: Optional[dict] = None
    for raw_line in yaml_path.read_text(encoding="utf-8").split("\n"):
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_section = line.startswith("monday_routes:")
            current = None
            continue
        if not in_section:
            continue
        m = _KV_RE.match(line.strip())
        if not m:
            continue
        key, rest = m.group(1).strip(), m.group(2).strip()
        if indent == 2 and rest == "":
            current = routes.setdefault(key, {})
        elif current is not None and indent > 2:
            current[key] = _parse_inline_list(rest) if rest.startswith("[") and rest.endswith("]") else _strip_scalar(rest)
    return routes


@dataclass
class FileFrontmatter:
    path: Path
    title: str
    attendees: list[str]
    keywords: list[str]


def parse_inbox_file(path: Path) -> Optional[FileFrontmatter]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm = parse_frontmatter(text)
    if not fm:
        return None
    # Title from filename: "YYYY-MM-DD — <title>.md"
    stem = path.stem
    m = re.match(r"^\d{4}-\d{2}-\d{2}\s*[—-]\s*(.+)$", stem)
    title = m.group(1).strip() if m else stem

    raw_attendees = fm.get("attendees") or []
    emails: list[str] = []
    if isinstance(raw_attendees, list):
        for a in raw_attendees:
            if not a:
                continue
            for e in EMAIL_RX.findall(str(a)):
                emails.append(e.lower())
    # Body scan for "name@example.com"-style mentions — the smaller fireflies notes
    # sometimes list emails only in the body, not the attendees field.
    for e in EMAIL_RX.findall(text):
        e_lc = e.lower()
        if e_lc.split("@", 1)[-1] in INTERNAL_EMAIL_DOMAINS:
            continue
        if e_lc not in emails:
            emails.append(e_lc)

    raw_keywords = fm.get("keywords") or []
    keywords = [str(k) for k in raw_keywords] if isinstance(raw_keywords, list) else []

    return FileFrontmatter(path=path, title=title, attendees=emails, keywords=keywords)


@dataclass
class Match:
    item: MondayItem
    match_type: str
    match_value: str


def find_match(ff: FileFrontmatter, email_idx, name_idx) -> Optional[Match]:
    # Tier-4a: email-exact.
    for e in ff.attendees:
        if e in email_idx:
            it = email_idx[e][0]
            return Match(item=it, match_type="email-exact", match_value=e)
    # Tier-4b: full-name substring.
    norm_title = normalize_title_for_substring(ff.title)
    for norm_name, it in name_idx:
        if norm_name and norm_name in norm_title:
            return Match(item=it, match_type="name-substring", match_value=it.item_name)
    return None


@dataclass
class Stats:
    files_scanned: int = 0
    email_exact_rescues: int = 0
    name_substring_rescues: int = 0
    no_match: int = 0
    by_board: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rescues: list[tuple[FileFrontmatter, Match]] = field(default_factory=list)
    unresolved: list[FileFrontmatter] = field(default_factory=list)
    # Secondary scan: files already routed by tiers 1–3 where tier-4 would
    # still attach monday_context metadata.
    tier3_files_scanned: int = 0
    tier3_attach: list[tuple[FileFrontmatter, Match]] = field(default_factory=list)


def render_report(stats: Stats, routes: dict, monday_total: int, out_path: Path) -> None:
    lines: list[str] = []
    lines.append("# Monday tier-4 routing dry-run")
    lines.append("")
    lines.append("Re-runs the current Inbox `needs-decision/` corpus against the new Fireflies routing tier-4 ")
    lines.append("(Monday lookup). Match policy:")
    lines.append("")
    lines.append("- **Tier-4a (email-exact):** any participant email matches a Monday item's email column → route to that board's destination.")
    lines.append("- **Tier-4b (full-name substring):** the full normalized item name (≥2 tokens, ≥8 chars after stopword removal) must appear as a substring of the meeting title. Single-token matches are rejected because single tokens carry a high false-positive rate.")
    lines.append("")
    lines.append("This dry-run reads `raw/monday/` only — no live Monday API calls (per DEC-009).")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- Monday items indexed (raw/monday/): **{monday_total}**")
    lines.append(f"- `{CFG.rel('inbox', 'needs-decision')}/` files scanned: **{stats.files_scanned}**")
    lines.append(f"- Email-exact rescues: **{stats.email_exact_rescues}**")
    lines.append(f"- Full-name substring rescues: **{stats.name_substring_rescues}**")
    lines.append(f"- Total additional non-Inbox routings: **{stats.email_exact_rescues + stats.name_substring_rescues}**")
    lines.append(f"- Unresolved (no Monday match): **{stats.no_match}**")
    lines.append("")
    lines.append("## Rescues per board")
    lines.append("")
    lines.append("| Board (Monday) | Destination folder | Rescues |")
    lines.append("|---|---|---|")
    for board_slug, count in sorted(stats.by_board.items(), key=lambda kv: -kv[1]):
        route = routes.get(board_slug) or {}
        dest = route.get("folder") or "_(no monday_routes entry — falls back to needs-decision)_"
        lines.append(f"| {board_slug} | {dest} | {count} |")
    lines.append("")
    lines.append("## Per-file plan")
    lines.append("")
    lines.append("| Filename | Match type | Match value | Board | Suggested destination |")
    lines.append("|---|---|---|---|---|")
    for ff, m in sorted(stats.rescues, key=lambda r: r[0].path.name):
        route = routes.get(m.item.board_slug) or {}
        dest = route.get("folder") or "—"
        mv_safe = m.match_value.replace("|", "\\|")
        name_safe = ff.path.name.replace("|", "\\|")
        lines.append(f"| {name_safe} | {m.match_type} | {mv_safe} | {m.item.board_slug} | {dest} |")
    lines.append("")
    if stats.unresolved:
        lines.append("## Unresolved (no Monday match)")
        lines.append("")
        lines.append(f"{stats.no_match} files have no email-exact or full-name match against the ingested Monday corpus. These continue to land in `{CFG.rel('inbox', 'needs-decision')}/` (tier-5 fallback). Sample (first 25):")
        lines.append("")
        for ff in stats.unresolved[:25]:
            lines.append(f"- `{ff.path.name}`")
        lines.append("")
    lines.append("## Secondary: tier-3-routed files where tier-4 would attach `monday_context:`")
    lines.append("")
    lines.append("Design rule: the Monday tier still ATTACHES `monday_context:` to frontmatter even when it is not the deciding tier, so the metadata is captured regardless.")
    lines.append("")
    lines.append("Files that the internal 1:1 rule routes via tier-3 (the HQ meetings folder) would not be RE-routed by tier-4 (tier-3 wins), but they would still get `monday_context:` attached.")
    lines.append("")
    lines.append(f"- Tier-3-routed files scanned: **{stats.tier3_files_scanned}**")
    lines.append(f"- Of those, would receive `monday_context:` from tier-4: **{len(stats.tier3_attach)}**")
    lines.append("")
    lines.append("**Combined tier-4 impact** (re-routings + metadata attachments): "
                 f"**{stats.email_exact_rescues + stats.name_substring_rescues + len(stats.tier3_attach)}**.")
    lines.append("")
    if stats.tier3_attach:
        lines.append("Sample attach-only files (first 15):")
        lines.append("")
        lines.append("| Filename | Monday match | Board | Match type |")
        lines.append("|---|---|---|---|")
        for ff, m in stats.tier3_attach[:15]:
            mv_safe = m.match_value.replace("|", "\\|")
            name_safe = ff.path.name.replace("|", "\\|")
            lines.append(f"| {name_safe} | {mv_safe} | {m.item.board_slug} | {m.match_type} |")
        lines.append("")
    lines.append("## Reading the numbers")
    lines.append("")
    lines.append("Re-routings are meetings that leave `needs-decision/`; attachments are meetings that keep their tier-1..3 destination and gain `monday_context:`. Judge the tier on the combined figure: a low re-routing count can simply mean earlier tiers already absorbed the overlapping cases.")
    lines.append("")
    lines.append(f"- Re-routings via tier-4: **{stats.email_exact_rescues + stats.name_substring_rescues}** (out of {stats.files_scanned} fallback files — ~{round(100*(stats.email_exact_rescues+stats.name_substring_rescues)/max(stats.files_scanned,1))}% rescue rate)")
    lines.append(f"- Combined tier-4 impact (re-routings + metadata attachments): **{stats.email_exact_rescues + stats.name_substring_rescues + len(stats.tier3_attach)}**.")
    lines.append("")
    lines.append("## How to regenerate")
    lines.append("")
    lines.append("```bash")
    lines.append(f"python3 {SKILL_DIR / 'dry_run_monday_tier.py'} \\")
    lines.append(f"  --out {out_path}")
    lines.append("```")
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("dry_run_monday_tier.md"))
    args = parser.parse_args(argv)

    items = load_monday_items()
    email_idx, name_idx = build_indices(items)
    routes = load_monday_routes(ROUTING_YAML)

    stats = Stats()
    inbox_files = sorted(INBOX_NEEDS.glob("*.md"))
    for p in inbox_files:
        ff = parse_inbox_file(p)
        if not ff:
            continue
        stats.files_scanned += 1
        m = find_match(ff, email_idx, name_idx)
        if not m:
            stats.no_match += 1
            stats.unresolved.append(ff)
            continue
        if m.match_type == "email-exact":
            stats.email_exact_rescues += 1
        else:
            stats.name_substring_rescues += 1
        stats.by_board[m.item.board_slug] += 1
        stats.rescues.append((ff, m))

    # Secondary scan: tier-3-routed files where tier-4 would attach metadata.
    for d in TIER3_ATTACH_SCAN_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            ff = parse_inbox_file(p)
            if not ff:
                continue
            stats.tier3_files_scanned += 1
            m = find_match(ff, email_idx, name_idx)
            if m:
                stats.tier3_attach.append((ff, m))

    render_report(stats, routes, len(items), args.out)

    print(
        f"monday_items={len(items)} "
        f"files={stats.files_scanned} "
        f"email_exact={stats.email_exact_rescues} "
        f"name_substring={stats.name_substring_rescues} "
        f"unresolved={stats.no_match} "
        f"tier3_scanned={stats.tier3_files_scanned} "
        f"tier3_attach={len(stats.tier3_attach)} "
        f"out={args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
