#!/usr/bin/env python3
"""atlas-people-extract — walk meeting notes, produce stub person notes in CRM/People/.

Modes:
  --dry-run   preview only; print plan, write a dry-run report, no vault mutations
  --execute   create or update person notes; write last-run.md

Idempotency contract:
  - On re-run, an existing person note is updated *only* in its frontmatter:
    mention_count, last_seen (and email/company/domain if previously empty).
  - The body (Bio, Recent Interactions, Open Threads, Notes) is preserved verbatim.
  - The owner can hand-edit any field except mention_count and last_seen
    without those edits being clobbered.

Disambiguation (DEC-012):
  - Filename: First-Last.md.
  - On collision with a different email, the new note gets First-Last-CompanyShort.md.
  - "CompanyShort" is the domain's primary label, kebab-cased
    (pinecrestlodge.example → pinecrestlodge; harborlane.example → harborlane).
"""
from __future__ import annotations
import argparse, datetime as dt, glob, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT = CFG.vault_root
PEOPLE_DIR = CFG.folder("crm") / "People"
SCAN_ROOTS = [CFG.folder("projects"), CFG.folder("areas")]
SKILL_DIR = Path(__file__).resolve().parent
# Dataview FROM clause over the meeting-bearing PARA folders, e.g. "20 - Projects" OR "30 - Areas"
DATAVIEW_FROM_PARA = " OR ".join(f'"{CFG.folder_name(k)}"' for k in ("projects", "areas"))

# Email local-parts to skip — these are role/distribution addresses with no
# meaningful person behind them.
ROLE_LOCALS = {"accounting", "billing", "support", "info", "hello", "team",
               "admin", "office", "sales", "contact", "noreply", "no-reply"}

# Dossier markers — anything between these lines is owned by the
# skill and may be regenerated. Owner content goes OUTSIDE the markers.
DOSSIER_START = "<!-- atlas-people-extract:dossier-start -->"
DOSSIER_END = "<!-- atlas-people-extract:dossier-end -->"


def tier_from_mentions(n: int) -> int:
    """Mention-count → tier. 8+=T1, 3-7=T2, 1-2=T3."""
    if n >= 8:
        return 1
    if n >= 3:
        return 2
    return 3


def render_dossier_block() -> str:
    """Tier-1 dossier skeleton. Inserted once on T1 promotion; preserved
    thereafter (owner fills in body, skill never re-touches it)."""
    return f"""{DOSSIER_START}
## Dossier

### Background
*How they came into your orbit. Current role. Significant moments.*

### Role
*Day-to-day responsibilities. Reporting lines.*

### Decision style
*How they decide. What evidence they want. Likely objections.*

## Last 5 Interactions

```dataview
TABLE date as "Date", file.link as "Meeting"
FROM {DATAVIEW_FROM_PARA}
WHERE contains(participants, this.file.link)
SORT date DESC
LIMIT 5
```

{DOSSIER_END}
"""


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
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    return parse_frontmatter_block(text[4:end])


def _set_fm_scalars(fm_block: str, updates: dict) -> str:
    """Surgically set scalar keys in a frontmatter block: replace each managed
    key's line in place, or append it if absent. Every other line — owner
    custom fields, the `tags:` list, blank lines — is preserved verbatim. This
    is the stdlib replacement for the old parse->mutate->yaml.safe_dump round
    trip; it never reformats or reorders untouched fields. See DEC-021."""
    remaining = dict(updates)
    out = []
    for line in fm_block.split("\n"):
        m = re.match(r"^([^:\s][^:]*):", line)
        key = m.group(1).strip() if m else None
        if key is not None and key in remaining:
            out.append(f"{key}: {remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}: {val}")
    return "\n".join(out)


def parse_meeting_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    # Try a few common shapes seen in the vault.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s[: len(fmt) + (4 if "%S" in fmt else 0)], fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    return None


def normalize_email(e) -> str | None:
    if not isinstance(e, str):
        return None
    e = e.strip().lower()
    if "@" not in e or " " in e:
        return None
    return e


def derive_name(local: str) -> tuple[str, str]:
    """Return (display_name, kebab_slug)."""
    # Strip a trailing numeric tag like "jordan2" → "jordan".
    base = re.sub(r"\d+$", "", local)
    # Split on common separators.
    parts = re.split(r"[._\-+]+", base)
    parts = [p for p in parts if p]
    if not parts:
        parts = [local]
    # Title-case each part.
    display = " ".join(p.capitalize() for p in parts)
    slug = "-".join(p.lower() for p in parts)
    # Strip any non-[a-z0-9-] chars from slug for filename safety.
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    return display, slug


def company_short(domain: str) -> str:
    # Primary label of the domain (everything before the last dot).
    if "." in domain:
        label = domain.rsplit(".", 1)[0]
        # If it's a multi-dot domain, take the rightmost label that isn't a known TLD.
        # Simple rule: take the last dotted segment before the TLD.
        label = label.split(".")[-1] if "." in label else label
    else:
        label = domain
    return re.sub(r"[^a-z0-9]", "", label.lower())


def walk_attendee_corpus() -> dict[str, dict]:
    """Return {email: {'name': str, 'slug': str, 'domain': str, 'company': str,
                       'mention_count': int, 'last_seen': date|None}}."""
    records: dict[str, dict] = {}
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            try:
                with p.open() as f:
                    text = f.read()
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if not fm:
                continue
            atts = fm.get("attendees") or fm.get("attendee_emails") or []
            if not isinstance(atts, list):
                continue
            date = parse_meeting_date(fm.get("date"))
            for raw in atts:
                email = normalize_email(raw)
                if not email:
                    continue
                local, _, domain = email.partition("@")
                if local in ROLE_LOCALS:
                    continue
                display, slug = derive_name(local)
                rec = records.setdefault(email, {
                    "email": email,
                    "name": display,
                    "slug": slug,
                    "domain": domain,
                    "company": company_short(domain),
                    "mention_count": 0,
                    "last_seen": None,
                })
                rec["mention_count"] += 1
                if date and (rec["last_seen"] is None or date > rec["last_seen"]):
                    rec["last_seen"] = date
    return records


def resolve_filenames(records: dict[str, dict]) -> dict[str, str]:
    """Return {email: filename-without-ext} honouring DEC-012 collision rule."""
    slug_to_emails: dict[str, list[str]] = {}
    for email, rec in records.items():
        # Capitalize segments for the actual filename: First-Last.md (not first-last).
        title_slug = "-".join(p.capitalize() for p in rec["slug"].split("-") if p)
        slug_to_emails.setdefault(title_slug, []).append(email)

    out: dict[str, str] = {}
    for slug, emails in slug_to_emails.items():
        if len(emails) == 1:
            out[emails[0]] = slug
        else:
            # DEC-012 disambiguation: append company shortname (capitalized for
            # the filename).
            for email in emails:
                short = records[email]["company"].capitalize()
                out[email] = f"{slug}-{short}" if short else slug
    return out


def render_stub(rec: dict, title_slug: str) -> str:
    last_seen = rec["last_seen"].isoformat() if rec["last_seen"] else ""
    display = title_slug.replace("-", " ")
    tier = tier_from_mentions(rec["mention_count"])
    dossier = f"\n{render_dossier_block()}" if tier == 1 else ""
    return f"""---
type: person
name: {rec['name']}
email: {rec['email']}
company: {rec['company']}
domain: {rec['domain']}
tier: {tier}
mention_count: {rec['mention_count']}
last_seen: {last_seen}
tags: [person]
---

# {display}
{dossier}
## Bio
*One paragraph: role, company, how we know each other, anything memorable.*

## Recent Interactions

```dataview
TABLE date as "Date", file.link as "Meeting"
FROM {DATAVIEW_FROM_PARA}
WHERE contains(participants, this.file.link)
SORT date DESC
LIMIT 20
```

## Open Threads
-

## Notes
"""


def update_existing_note(text: str, rec: dict) -> tuple[str, int, int]:
    """Return (new_text, old_tier, new_tier).

    Updates `mention_count` and `last_seen` in frontmatter (always). Updates
    `tier` if the mention-count formula now produces a different tier than
    what's recorded — but a owner-promoted higher tier is never
    automatically demoted (a T1 stays T1 even if mentions drop).

    On promotion *to* T1 (and only the first time the note crosses into T1
    without a dossier marker present), the T1 dossier skeleton is inserted
    between the H1 and the Bio section. Owner body content outside the
    `<!-- atlas-people-extract:dossier-* -->` markers is preserved verbatim.
    Once the dossier exists, the skill never re-touches it on later runs.
    """
    fm = parse_frontmatter(text) or {}

    old_tier = int(fm.get("tier") or 3)
    computed_tier = tier_from_mentions(rec["mention_count"])
    # Never auto-demote: owner-curated promotions stick.
    new_tier = min(old_tier, computed_tier)

    # Managed fields: always refresh mention_count/last_seen/tier; fill
    # email/company/domain only if currently empty. Everything else in the
    # frontmatter (owner custom fields, tags, ordering) is preserved
    # verbatim by the surgical setter — no full re-emit. See DEC-021.
    updates = {
        "mention_count": str(rec["mention_count"]),
        "last_seen": rec["last_seen"].isoformat() if rec["last_seen"] else "",
        "tier": str(new_tier),
    }
    for k in ("email", "company", "domain"):
        if not fm.get(k):
            updates[k] = rec[k]

    end = text.find("\n---", 4)
    raw_fm = text[4:end] if end >= 0 else text[4:]
    body = text[end + 4:] if end >= 0 else ""

    new_fm = _set_fm_scalars(raw_fm, updates)

    # Dossier insertion — only fires on promotion (old != T1) AND no
    # markers present. Once present, never re-inserted.
    if new_tier == 1 and old_tier != 1 and DOSSIER_START not in body:
        body = _insert_dossier(body)

    return f"---\n{new_fm}\n---{body}", old_tier, new_tier


def _insert_dossier(body: str) -> str:
    """Insert the dossier block between the H1 line and the next ## section."""
    # Find the H1 line ("# Display Name") and the first H2 that follows.
    h2_idx = body.find("\n## ")
    if h2_idx < 0:
        # No H2 — just append.
        return body.rstrip() + "\n\n" + render_dossier_block()
    return body[:h2_idx + 1] + render_dossier_block() + "\n" + body[h2_idx + 1:]


def plan(records: dict[str, dict], filenames: dict[str, str]) -> dict:
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    to_create, to_update, collisions = [], [], []
    for email, name in filenames.items():
        path = PEOPLE_DIR / f"{name}.md"
        if path.exists():
            to_update.append((email, path))
        else:
            to_create.append((email, path))
        if "-" in name and records[email]["company"].capitalize() in name.split("-"):
            collisions.append((email, path))
    return {
        "create": to_create,
        "update": to_update,
        "collisions": collisions,
        "total_emails": len(records),
    }


def _preview_transitions(records, filenames, plan_out):
    """For each update target, peek at current frontmatter tier and predict
    the new tier. Returns (transitions, tier_counts) where transitions is
    a sorted list of (filename, old_tier, new_tier) and tier_counts is a
    {1|2|3: count} dict over the *post-run* tier distribution."""
    transitions = []
    tier_counts = {1: 0, 2: 0, 3: 0}
    for email, path in plan_out["create"]:
        new_tier = tier_from_mentions(records[email]["mention_count"])
        tier_counts[new_tier] += 1
    for email, path in plan_out["update"]:
        try:
            fm = parse_frontmatter(path.read_text()) or {}
        except OSError:
            fm = {}
        old_tier = int(fm.get("tier") or 3)
        computed = tier_from_mentions(records[email]["mention_count"])
        new_tier = min(old_tier, computed)  # never auto-demote
        tier_counts[new_tier] += 1
        if old_tier != new_tier:
            transitions.append((path.name, old_tier, new_tier))
    return transitions, tier_counts


def write_dry_run(records, filenames, plan_out, target_path: Path):
    transitions, tier_counts = _preview_transitions(records, filenames, plan_out)
    lines = ["# atlas-people-extract dry-run",
             "",
             f"**Date:** {dt.date.today().isoformat()}",
             "**Mode:** dry-run (no vault writes)",
             "**Source corpus:** `20 - Projects/`, `30 - Areas/` (recursive)",
             "",
             "## Plan summary",
             "",
             f"- Unique emails extracted: **{plan_out['total_emails']}**",
             f"- New person stubs to create: **{len(plan_out['create'])}**",
             f"- Existing person notes to update: **{len(plan_out['update'])}**",
             f"- DEC-012 collision-disambiguated filenames: **{len(plan_out['collisions'])}**",
             f"- Tier transitions on this run: **{len(transitions)}**",
             "",
             "## Post-run tier distribution (predicted)",
             "",
             f"- **Tier 1** (≥ 8 mentions): {tier_counts[1]}",
             f"- **Tier 2** (3–7 mentions): {tier_counts[2]}",
             f"- **Tier 3** (1–2 mentions): {tier_counts[3]}",
             "",
             "## Top 20 by mention_count",
             "",
             "| Email | Display name | Mentions | Tier | Last seen | Filename |",
             "|---|---|---|---|---|---|"]
    by_mentions = sorted(records.items(), key=lambda kv: -kv[1]["mention_count"])
    for email, rec in by_mentions[:20]:
        ls = rec["last_seen"].isoformat() if rec["last_seen"] else "—"
        tier = tier_from_mentions(rec["mention_count"])
        lines.append(
            f"| `{email}` | {rec['name']} | {rec['mention_count']} | "
            f"T{tier} | {ls} | `{filenames[email]}.md` |"
        )
    if transitions:
        lines += ["", "## Tier transitions planned",
                  "",
                  "| File | From | To |",
                  "|---|---|---|"]
        for fname, old, new in sorted(transitions, key=lambda t: (t[2], t[0])):
            arrow = "↑" if new < old else "↓"
            lines.append(f"| `{fname}` | T{old} | {arrow} T{new} |")
    if plan_out["collisions"]:
        lines += ["", "## DEC-012 disambiguations",
                  "",
                  "| Email | Disambiguated filename |",
                  "|---|---|"]
        for email, path in plan_out["collisions"]:
            lines.append(f"| `{email}` | `{path.name}` |")
    lines += ["", "## Acceptance gates",
              "",
              f"- ≥ 80 person stubs after first execution: "
              f"**{'PASS' if (len(plan_out['create']) + len(plan_out['update'])) >= 80 else 'FAIL'}** "
              f"(planned total: {len(plan_out['create']) + len(plan_out['update'])})",
              f"- ≥ 5 Tier 1 people: "
              f"**{'PASS' if tier_counts[1] >= 5 else 'FAIL'}** "
              f"(predicted: {tier_counts[1]})",
              f"- ≥ 20 Tier 2 people: "
              f"**{'PASS' if tier_counts[2] >= 20 else 'FAIL'}** "
              f"(predicted: {tier_counts[2]})",
              "",
              "Re-run with `--execute` to apply the plan."]
    target_path.write_text("\n".join(lines) + "\n")


def execute(records, filenames, plan_out) -> tuple[int, int, list]:
    """Apply plan to disk. Returns (created, updated, transitions).

    `transitions` is a list of (filename, old_tier, new_tier) tuples for
    notes whose tier actually changed this run. Notes created from scratch
    appear with old_tier=None.
    """
    created = updated = 0
    transitions: list[tuple[str, int | None, int]] = []
    for email, path in plan_out["create"]:
        rec = records[email]
        new_tier = tier_from_mentions(rec["mention_count"])
        path.write_text(render_stub(rec, filenames[email]))
        created += 1
        transitions.append((path.name, None, new_tier))
    for email, path in plan_out["update"]:
        text = path.read_text()
        new_text, old_tier, new_tier = update_existing_note(text, records[email])
        path.write_text(new_text)
        updated += 1
        if old_tier != new_tier:
            transitions.append((path.name, old_tier, new_tier))
    return created, updated, transitions


def write_last_run(created, updated, total, mode: str, transitions=None):
    p = SKILL_DIR / "last-run.md"
    lines = [
        "# atlas-people-extract — last run",
        "",
        f"**When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"**Mode:** {mode}",
        "",
        f"- Unique attendee emails scanned: {total}",
        f"- Person notes created: {created}",
        f"- Person notes updated: {updated}",
        f"- Output folder: `CRM/People/` "
        f"({len(list(PEOPLE_DIR.glob('*.md')))} files now)",
    ]
    if transitions:
        promotions = [t for t in transitions if t[1] is not None and t[1] != t[2]]
        creates = [t for t in transitions if t[1] is None]
        if creates:
            tier_hist = {}
            for _, _, new in creates:
                tier_hist[new] = tier_hist.get(new, 0) + 1
            tier_str = ", ".join(f"T{t}={n}" for t, n in sorted(tier_hist.items()))
            lines += ["", f"## Initial tier assignment ({len(creates)} new notes)", "",
                      f"- {tier_str}"]
        if promotions:
            lines += ["", f"## Tier transitions ({len(promotions)} this run)", ""]
            for fname, old, new in sorted(promotions, key=lambda t: (t[2], t[0])):
                arrow = "↑" if new < old else "↓"
                lines.append(f"- {fname}: T{old} {arrow} T{new}")
    p.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Extract person stubs from meeting attendees.")
    ap.add_argument("--execute", action="store_true",
                    help="Actually write to the vault. Without this, dry-run only.")
    ap.add_argument("--dry-run-report", default=None,
                    help="Path to write dry-run report (markdown). Implies dry-run.")
    args = ap.parse_args()

    records = walk_attendee_corpus()
    filenames = resolve_filenames(records)
    plan_out = plan(records, filenames)

    if args.execute:
        created, updated, transitions = execute(records, filenames, plan_out)
        write_last_run(created, updated, len(records), mode="execute",
                       transitions=transitions)
        promotions = sum(1 for _, o, n in transitions if o is not None and o != n)
        print(f"created={created} updated={updated} "
              f"transitions={promotions} total_emails={len(records)}")
    else:
        report_path = Path(args.dry_run_report) if args.dry_run_report else (
            SKILL_DIR / "last-dry-run.md"
        )
        write_dry_run(records, filenames, plan_out, report_path)
        print(f"dry-run: would create {len(plan_out['create'])}, "
              f"update {len(plan_out['update'])}; report → {report_path}")


if __name__ == "__main__":
    main()
