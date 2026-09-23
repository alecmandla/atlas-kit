#!/usr/bin/env python3
"""atlas-wiki-materialize.

Walk vault entity sources (client folders, area folders, CRM/People orgs,
seeded products/technologies), count mentions across the meeting corpus,
and emit wiki/entities/<entity>.md + wiki/index.md.

Idempotent. Preserves `## Related` editable region across re-runs via
HTML-comment markers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT = CFG.vault_root
CLIENTS_DIR = CFG.folder("projects") / "Clients"
AREAS_DIR = CFG.folder("areas")
PEOPLE_DIR = CFG.folder("crm") / "People"
WIKI_DIR = CFG.folder("wiki")
# Vault-relative strings for path matching and Dataview predicates.
CLIENTS_REL = CFG.rel("projects", "Clients")
AREAS_REL = CFG.folder_name("areas")
DATAVIEW_FROM_PARA = " OR ".join(f'"{CFG.folder_name(k)}"' for k in ("projects", "areas"))
ENTITIES_DIR = WIKI_DIR / "entities"
INDEX_FILE = WIKI_DIR / "index.md"

SEED_FILE = Path(__file__).parent / "entity_seeds.json"


def tilde(p: Path) -> str:
    """Render a path with the home directory as `~` (for human-facing note text)."""
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
EDITABLE_REGION_RE = re.compile(
    r"<!-- atlas-wiki-materialize:editable-start -->(.*?)<!-- atlas-wiki-materialize:editable-end -->",
    re.DOTALL,
)


@dataclass
class Entity:
    name: str
    entity_type: str  # "client" | "area" | "org" | "product" | "technology"
    canonical_id: str
    description: str = ""
    mention_count: int = 0
    first_mention: Optional[str] = None  # YYYY-MM-DD
    last_mention: Optional[str] = None
    mention_paths: list[str] = field(default_factory=list)  # for dry-run report

    @property
    def file_path(self) -> Path:
        return ENTITIES_DIR / f"{self.canonical_id}.md"


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).split("\n"):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip("'\"")
    return fm


def normalize_date(value: str) -> Optional[str]:
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return m.group(1) if m else None


def fuzzy_company_match(company: str, client_canonical_ids: set[str], aliases: dict[str, str]) -> Optional[str]:
    """Return matching client canonical_id if the CRM company maps to one, else None."""
    c = re.sub(r"[^a-z0-9]", "", company.lower())
    if c in aliases:
        return aliases[c]
    for cid in client_canonical_ids:
        cid_norm = re.sub(r"[^a-z0-9]", "", cid.lower())
        if c == cid_norm or c.startswith(cid_norm) or cid_norm.startswith(c):
            if len(c) >= 4 and len(cid_norm) >= 4:
                return cid
    return None


def title_case_company(slug: str) -> str:
    """Convert 'pinecrestlodge' → 'Pinecrestlodge' (single-word fallback); better seeds via alias table."""
    return slug.capitalize() if slug.islower() else slug


def discover_entities(seeds: dict) -> list[Entity]:
    entities: list[Entity] = []
    client_ids: set[str] = set()

    # Clients
    if CLIENTS_DIR.exists():
        for d in sorted(CLIENTS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            entities.append(Entity(
                name=d.name.replace("-", " ").replace("_", " "),
                entity_type="client",
                canonical_id=d.name,
                description=f"Client engagement (project root: `20 - Projects/Clients/{d.name}/`).",
            ))
            client_ids.add(d.name)

    # Areas
    if AREAS_DIR.exists():
        for d in sorted(AREAS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            entities.append(Entity(
                name=d.name.replace("-", " "),
                entity_type="area",
                canonical_id=d.name,
                description=f"Personal area of responsibility (folder: `30 - Areas/{d.name}/`).",
            ))

    # Orgs (companies from CRM/People, deduped against clients)
    company_to_emails: dict[str, list[str]] = defaultdict(list)
    if PEOPLE_DIR.exists():
        for p in PEOPLE_DIR.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            fm = parse_frontmatter(text)
            company = fm.get("company", "").strip()
            email = fm.get("email", "").strip()
            if company and email:
                company_to_emails[company].append(email)

    aliases = seeds.get("client_aliases", {})
    seen_orgs: set[str] = set()
    for company, emails in sorted(company_to_emails.items()):
        if not company:
            continue
        matched_client = fuzzy_company_match(company, client_ids, aliases)
        if matched_client:
            continue
        canonical = title_case_company(company)
        if canonical in seen_orgs:
            continue
        seen_orgs.add(canonical)
        entities.append(Entity(
            name=canonical,
            entity_type="org",
            canonical_id=canonical,
            description=f"Organization surfaced from {len(emails)} CRM person record(s) with `company: {company}`.",
        ))

    # Products
    for prod in seeds.get("products", []):
        entities.append(Entity(
            name=prod["name"],
            entity_type="product",
            canonical_id=prod["canonical_id"],
            description=prod.get("description", ""),
        ))

    # Technologies
    for tech in seeds.get("technologies", []):
        entities.append(Entity(
            name=tech["name"],
            entity_type="technology",
            canonical_id=tech["canonical_id"],
            description=tech.get("description", ""),
        ))

    return entities


def build_person_company_map() -> dict[str, str]:
    """Map person canonical_id (basename of CRM/People/*.md) → company value."""
    out: dict[str, str] = {}
    if not PEOPLE_DIR.exists():
        return out
    for p in PEOPLE_DIR.glob("*.md"):
        text = p.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        company = fm.get("company", "").strip()
        if company:
            out[p.stem] = company
    return out


def count_mentions(entities: list[Entity], seeds: dict) -> None:
    """Walk meeting notes, count mentions for each entity, update first/last."""
    meetings = sorted(VAULT.glob("**/meetings/*.md"))

    person_company = build_person_company_map()

    # Build company→entity_id map for org mentions.
    company_to_entity: dict[str, str] = {}
    aliases = seeds.get("client_aliases", {})
    client_ids = {e.canonical_id for e in entities if e.entity_type == "client"}
    for e in entities:
        if e.entity_type == "org":
            for crm_company, _ in [(c, emails) for c, emails in defaultdict(list, {company: [] for company in person_company.values()}).items()]:
                pass
    # Direct build: find which CRM company → which entity
    crm_companies = set(person_company.values())
    for company in crm_companies:
        matched_client = fuzzy_company_match(company, client_ids, aliases)
        if matched_client:
            company_to_entity[company] = matched_client
        else:
            canonical = title_case_company(company)
            company_to_entity[company] = canonical

    # Compile product/tech patterns
    pattern_entities: list[tuple[re.Pattern, str]] = []
    for source_key in ("products", "technologies"):
        for entry in seeds.get(source_key, []):
            for pat in entry["patterns"]:
                pattern_entities.append((re.compile(pat, re.IGNORECASE), entry["canonical_id"]))

    entity_by_id = {e.canonical_id: e for e in entities}

    for m in meetings:
        text = m.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        date = normalize_date(fm.get("date", ""))

        rel_path = m.relative_to(VAULT)
        path_str = str(rel_path)

        # Title and summary for regex matching
        title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
        title = title_match.group(1) if title_match else m.stem
        summary_match = re.search(r"^## Summary\s*\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
        summary = summary_match.group(1) if summary_match else ""
        searchable = f"{title}\n{summary}"

        # Track which entities this meeting mentioned (avoid double-counting)
        mentioned_ids: set[str] = set()

        # Client: path-based + project: frontmatter
        for cid in client_ids:
            if f"{CLIENTS_REL}/{cid}/" in path_str:
                mentioned_ids.add(cid)
            elif fm.get("project", "").find(cid) != -1:
                mentioned_ids.add(cid)

        # Area: path-based
        for e in entities:
            if e.entity_type == "area" and f"{AREAS_REL}/{e.canonical_id}/" in path_str:
                mentioned_ids.add(e.canonical_id)

        # Org: via participants (parse multi-line YAML list from frontmatter)
        fm_match = FRONTMATTER_RE.match(text)
        wikilinks: list[str] = []
        if fm_match:
            fm_block = fm_match.group(1)
            # Find the participants block and capture lines until next top-level key
            p_match = re.search(
                r"^participants:\s*(.*?)(?=^[a-zA-Z_][a-zA-Z_0-9-]*:|\Z)",
                fm_block,
                re.MULTILINE | re.DOTALL,
            )
            if p_match:
                participants_block = p_match.group(1)
                wikilinks = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", participants_block)
        for person_id in wikilinks:
            company = person_company.get(person_id)
            if company:
                entity_id = company_to_entity.get(company)
                if entity_id and entity_id in entity_by_id and entity_by_id[entity_id].entity_type == "org":
                    mentioned_ids.add(entity_id)

        # Products + technologies: regex in title+summary
        for pat, eid in pattern_entities:
            if pat.search(searchable):
                mentioned_ids.add(eid)

        # Apply mentions
        for eid in mentioned_ids:
            if eid not in entity_by_id:
                continue
            e = entity_by_id[eid]
            e.mention_count += 1
            if date:
                if e.first_mention is None or date < e.first_mention:
                    e.first_mention = date
                if e.last_mention is None or date > e.last_mention:
                    e.last_mention = date
            e.mention_paths.append(path_str)


def render_entity_page(e: Entity, preserved_related: str = "") -> str:
    today = dt.date.today().isoformat()

    if e.entity_type == "client":
        dataview_pred = f'startswith(file.path, "{CLIENTS_REL}/{e.canonical_id}/")'
    elif e.entity_type == "area":
        dataview_pred = f'startswith(file.path, "{AREAS_REL}/{e.canonical_id}/")'
    elif e.entity_type == "org":
        dataview_pred = f'contains(file.outlinks, this.file.link)'
    else:
        # product / technology — best-effort link search
        dataview_pred = f'contains(string(file), "{e.name}")'

    related_default = "*Hand-add wikilinks here. Survives re-materialization via the editable markers below.*\n- (none yet)"
    related_body = preserved_related.strip() if preserved_related.strip() else related_default

    body = (
        "---\n"
        "type: wiki-entity\n"
        f"entity_name: {e.name}\n"
        f"entity_type: {e.entity_type}\n"
        f"canonical_id: {e.canonical_id}\n"
        f"first_mention: {e.first_mention or 'unknown'}\n"
        f"last_mention: {e.last_mention or 'unknown'}\n"
        f"mention_count: {e.mention_count}\n"
        f"generated_by: atlas-wiki-materialize\n"
        f"generated_at: {today}\n"
        f"tags: [wiki-entity, entity-type/{e.entity_type}]\n"
        "---\n"
        "\n"
        f"# {e.name}\n"
        "\n"
        f"*{e.description}*\n"
        "\n"
        "## Mentions\n"
        "\n"
        "```dataview\n"
        'TABLE date as "Date", file.link as "Meeting"\n'
        f"FROM {DATAVIEW_FROM_PARA}\n"
        f"WHERE {dataview_pred}\n"
        "SORT date DESC\n"
        "```\n"
        "\n"
        "## Related\n"
        "\n"
        "<!-- atlas-wiki-materialize:editable-start -->\n"
        f"{related_body}\n"
        "<!-- atlas-wiki-materialize:editable-end -->\n"
    )
    return body


def extract_editable_region(file_path: Path) -> str:
    if not file_path.exists():
        return ""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = EDITABLE_REGION_RE.search(text)
    return m.group(1) if m else ""


def write_index(entities: list[Entity]) -> str:
    today = dt.date.today().isoformat()
    by_type: dict[str, list[Entity]] = defaultdict(list)
    for e in entities:
        if e.mention_count > 0:
            by_type[e.entity_type].append(e)
    for k in by_type:
        by_type[k].sort(key=lambda x: x.name.lower())

    lines = [
        "---",
        "type: wiki-index",
        "generated_by: atlas-wiki-materialize",
        f"generated_at: {today}",
        f"total_entities: {sum(len(v) for v in by_type.values())}",
        "---",
        "",
        "# Wiki index",
        "",
        f"Last regenerated: {today}",
        "",
        "## Entities by type",
        "",
    ]
    type_labels = [("client", "Client"), ("area", "Area"), ("org", "Org"), ("product", "Product"), ("technology", "Technology")]
    for tkey, tlabel in type_labels:
        items = by_type.get(tkey, [])
        if not items:
            continue
        lines.append(f"### {tlabel} ({len(items)})")
        lines.append("")
        for e in items:
            lines.append(f"- [[{e.canonical_id}|{e.name}]] — last mention {e.last_mention or '—'}, {e.mention_count} mention(s)")
        lines.append("")

    lines += [
        "## Concepts",
        "",
        "*Empty in v1. Surfaces in M5 via `atlas-emerge`.*",
        "",
        "## Conventions",
        "",
        f"See `{tilde(VAULT)}/AGENTS.md` for the rewriteable rule and the per-page `## Related` editable-region protocol.",
        "",
    ]
    return "\n".join(lines)


def write_dry_run_report(report_path: Path, entities: list[Entity]) -> None:
    today = dt.date.today().isoformat()
    materializable = [e for e in entities if e.mention_count > 0]
    by_type: dict[str, list[Entity]] = defaultdict(list)
    for e in materializable:
        by_type[e.entity_type].append(e)

    lines = [
        f"# atlas-wiki-materialize dry-run — {today}",
        "",
        "Skill: `atlas-wiki-materialize`",
        f"Sources: `{CLIENTS_DIR.relative_to(VAULT)}/`, `{AREAS_DIR.relative_to(VAULT)}/`, `{PEOPLE_DIR.relative_to(VAULT)}/`, seeded products + technologies.",
        f"Output: `{ENTITIES_DIR.relative_to(VAULT)}/<entity>.md` + `{INDEX_FILE.relative_to(VAULT)}`.",
        "",
        "## Summary",
        "",
        f"- Total entities discovered: **{len(entities)}**",
        f"- Entities with ≥1 mention (will be materialized): **{len(materializable)}**",
        f"- Entities with 0 mentions (skipped): {len(entities) - len(materializable)}",
        "",
        "### By type",
        "",
    ]
    for tkey, tlabel in [("client", "Client"), ("area", "Area"), ("org", "Org"), ("product", "Product"), ("technology", "Technology")]:
        total_count = sum(1 for e in entities if e.entity_type == tkey)
        materializable_count = len(by_type.get(tkey, []))
        lines.append(f"- **{tlabel}**: {materializable_count} / {total_count} (mentioned / discovered)")
    lines += [
        "",
        "## Acceptance checks",
        "",
        f"- ≥ 30 entity pages: **{'PASS' if len(materializable) >= 30 else 'FAIL'}** — {len(materializable)} entities will be materialized.",
        "- frontmatter shape: each page carries `entity_type`, `first_mention`, `last_mention`, `mention_count`.",
        "- `## Mentions` Dataview block: each page has a typed Dataview query (path-based for client/area, outlinks-based for org, file-contains-based for product/tech).",
        "- idempotency: editable-region markers preserve `## Related` content across re-runs.",
        "",
        "## Per-entity plan (top 50 by mention count)",
        "",
        "| Entity | Type | Mentions | First | Last |",
        "|---|---|---|---|---|",
    ]
    sorted_by_mentions = sorted(materializable, key=lambda x: -x.mention_count)
    for e in sorted_by_mentions[:50]:
        lines.append(f"| `{e.canonical_id}` | {e.entity_type} | {e.mention_count} | {e.first_mention or '—'} | {e.last_mention or '—'} |")
    if len(sorted_by_mentions) > 50:
        lines.append("")
        lines.append(f"_(+ {len(sorted_by_mentions) - 50} more — full list in `wiki/index.md` after execute.)_")
    lines += [
        "",
        "## Zero-mention entities (skipped)",
        "",
    ]
    skipped = [e for e in entities if e.mention_count == 0]
    if skipped:
        for e in skipped[:30]:
            lines.append(f"- `{e.canonical_id}` ({e.entity_type})")
        if len(skipped) > 30:
            lines.append(f"- _(+ {len(skipped) - 30} more.)_")
    else:
        lines.append("None.")

    lines += [
        "",
        "## Next action",
        "",
        "Re-invoke with `--execute` (or let the next `/atlas-phase` iteration detect State C).",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_materialization(entities: list[Entity]) -> tuple[int, int, int]:
    """Write entity pages + index. Returns (created, updated, preserved_regions)."""
    ENTITIES_DIR.mkdir(parents=True, exist_ok=True)
    created = 0
    updated = 0
    preserved = 0

    for e in entities:
        if e.mention_count == 0:
            continue
        preserved_region = extract_editable_region(e.file_path)
        if preserved_region.strip() and not preserved_region.strip().startswith("*Hand-add wikilinks"):
            preserved += 1
        content = render_entity_page(e, preserved_related=preserved_region)
        existed = e.file_path.exists()
        e.file_path.write_text(content, encoding="utf-8")
        if existed:
            updated += 1
        else:
            created += 1

    INDEX_FILE.write_text(write_index(entities), encoding="utf-8")
    return created, updated, preserved


def write_last_run(entities: list[Entity], mode: str, counts: tuple[int, int, int]) -> None:
    created, updated, preserved = counts
    materializable = [e for e in entities if e.mention_count > 0]
    by_type: dict[str, int] = defaultdict(int)
    for e in materializable:
        by_type[e.entity_type] += 1

    here = Path(__file__).parent / "last-run.md"
    lines = [
        "# atlas-wiki-materialize — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **Total entities discovered:** {len(entities)}",
        f"- **Materialized (mention_count > 0):** {len(materializable)}",
        f"- **By type:** {dict(by_type)}",
        f"- **Created:** {created}",
        f"- **Updated:** {updated}",
        f"- **Editable regions preserved:** {preserved}",
    ]
    here.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Materialize wiki entity pages + index.")
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit-entities", type=int, default=None)
    args = parser.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2

    if not VAULT.exists():
        print(f"error: vault not found at {VAULT}", file=sys.stderr)
        return 2

    if SEED_FILE.exists():
        seeds = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    else:
        print(f"warning: {SEED_FILE} not found; running with no product or technology seeds "
              "(copy exemplars/configs/entity_seeds.example.json next to this script)", file=sys.stderr)
        seeds = {}
    entities = discover_entities(seeds)
    count_mentions(entities, seeds)

    if args.limit_entities is not None:
        # Keep only top-N by mention count (for smoke testing)
        entities.sort(key=lambda x: -x.mention_count)
        entities = entities[: args.limit_entities]

    counts = (0, 0, 0)
    if args.execute:
        counts = apply_materialization(entities)

    if args.dry_run_report:
        write_dry_run_report(args.dry_run_report, entities)

    mode = "execute" if args.execute else "dry-run"
    write_last_run(entities, mode, counts)

    materialized = sum(1 for e in entities if e.mention_count > 0)
    print(
        f"discovered={len(entities)} materialized={materialized} "
        f"created={counts[0]} updated={counts[1]} preserved={counts[2]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
