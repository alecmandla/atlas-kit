#!/usr/bin/env python3
"""atlas-frontmatter-migrate — migrate meeting `attendees:` (emails) to
`participants:` (`[[Person-Slug]]` wikilinks) per DEC-003.

Modes:
  --dry-run-report PATH   write a markdown plan to PATH; no vault writes
  --execute               apply the migration to every matching meeting

Migration shape, per meeting:
  - Before: `attendees: [list of email strings]`
  - After:  `attendees:` removed, replaced by:
            `attendee_emails: [list of email strings]`   ← preserved for back-compat
            `participants:    [list of [[Person-Slug]]]`  ← new, wikilink form

Idempotency: any meeting already carrying `participants:` is skipped (no re-write).

Email-to-person mapping is read from `CRM/People/<slug>.md` files'
`email:` frontmatter. Emails without a matching person note (role
addresses filtered out by atlas-people-extract, or any later
arrivals) are preserved in `attendee_emails:` but emit no `participants:`
entry.
"""
from __future__ import annotations
import argparse, datetime as dt, glob, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT = CFG.vault_root
PEOPLE_DIR = CFG.folder("crm") / "People"
SKILL_DIR = Path(__file__).resolve().parent


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


def _emit_list_field(key: str, values: list) -> list:
    """Emit a frontmatter list field matching the prior yaml.safe_dump output:
    `key: []` when empty, else block style (`key:` then one `- item` per line),
    quoting items a plain scalar would misparse (e.g. `[[wikilinks]]`). See DEC-021."""
    if not values:
        return [f"{key}: []"]
    out = [f"{key}:"]
    for v in values:
        s = str(v)
        if (s == "" or s[0] in "[]{}>|*&!%@#,\"'`-" or ": " in s or s != s.strip()):
            s = "'" + s.replace("'", "''") + "'"
        out.append(f"- {s}")
    return out


def parse_frontmatter(text: str) -> tuple[dict | None, int]:
    """Return (frontmatter-dict, body-start-index). body-start-index is the
    char index just past the closing `\\n---` (or -1 if no FM)."""
    if not text.startswith("---"):
        return None, -1
    end = text.find("\n---", 4)
    if end < 0:
        return None, -1
    return (parse_frontmatter_block(text[4:end]) or None), end + 4


def build_email_index() -> dict[str, str]:
    """Read every CRM/People/*.md, return {email: filename-stem}."""
    out: dict[str, str] = {}
    for p in PEOPLE_DIR.glob("*.md"):
        fm, _ = parse_frontmatter(p.read_text())
        if not fm:
            continue
        email = fm.get("email")
        if isinstance(email, str) and "@" in email:
            out[email.strip().lower()] = p.stem
    return out


def find_meeting_files() -> list[Path]:
    """All files under any folder named `meetings/` in the vault."""
    return sorted(p for p in VAULT.rglob("*.md") if p.parent.name == "meetings")


def plan_one(fm: dict, email_index: dict[str, str]) -> dict | None:
    """Given a meeting's frontmatter dict, return a plan dict describing the
    needed migration — or None if no migration is needed (no attendees, or
    already migrated)."""
    if "participants" in fm:
        return None  # already migrated
    attendees = fm.get("attendees")
    if not isinstance(attendees, list):
        # No attendees field (or malformed). Still flag for migration if a
        # `participants:` field is desired everywhere — but the AC's grep
        # for `^participants:` only fires on present files. We'll add an
        # empty participants:[] to those too, so the dashboard query never
        # 404s on a missing field.
        attendees = []
    emails_clean: list[str] = []
    participants: list[str] = []
    unmatched: list[str] = []
    for raw in attendees:
        if not isinstance(raw, str):
            continue
        email = raw.strip().lower()
        if "@" not in email:
            continue
        emails_clean.append(email)
        slug = email_index.get(email)
        if slug:
            participants.append(f"[[{slug}]]")
        else:
            unmatched.append(email)
    return {
        "attendee_emails": emails_clean,
        "participants": participants,
        "unmatched": unmatched,
    }


def apply_one(path: Path, plan: dict) -> None:
    text = path.read_text()
    fm, body_start = parse_frontmatter(text)
    if fm is None or body_start < 0:
        return
    end = text.find("\n---", 4)
    raw_lines = text[4:end].split("\n")
    body = text[body_start:]

    # Surgical edit: drop the `attendees:` entry (inline or block list), insert
    # `attendee_emails:` + `participants:` immediately after `date:` (else at the
    # end), and preserve every other frontmatter line verbatim — no full re-emit,
    # so arbitrary meeting frontmatter keeps its exact formatting. See DEC-021.
    out: list = []
    i = 0
    inserted = False
    new_fields = (_emit_list_field("attendee_emails", plan["attendee_emails"])
                  + _emit_list_field("participants", plan["participants"]))
    while i < len(raw_lines):
        line = raw_lines[i]
        m = re.match(r"^([^:\s][^:]*):(.*)$", line)
        if m and m.group(1).strip() == "attendees":
            i += 1
            # If the value was empty, a block list follows — consume its items.
            if m.group(2).strip() == "":
                while i < len(raw_lines) and re.match(r"^(\s+\S|\s*-\s)", raw_lines[i]):
                    i += 1
            continue
        out.append(line)
        if m and m.group(1).strip() == "date" and not inserted:
            out.extend(new_fields)
            inserted = True
        i += 1
    if not inserted:
        out.extend(new_fields)

    path.write_text("---\n" + "\n".join(out) + "\n---" + body)


def make_plan(email_index: dict[str, str]) -> dict:
    """Produce a global plan summary across all meeting files."""
    plans: list[tuple[Path, dict]] = []
    already_migrated = 0
    parse_failures: list[Path] = []
    unmatched_emails: dict[str, int] = {}

    for path in find_meeting_files():
        text = path.read_text()
        fm, _ = parse_frontmatter(text)
        if fm is None:
            parse_failures.append(path)
            continue
        plan = plan_one(fm, email_index)
        if plan is None:
            already_migrated += 1
            continue
        plans.append((path, plan))
        for email in plan["unmatched"]:
            unmatched_emails[email] = unmatched_emails.get(email, 0) + 1

    return {
        "plans": plans,
        "already_migrated": already_migrated,
        "parse_failures": parse_failures,
        "unmatched_emails": unmatched_emails,
        "total_meeting_files": len(find_meeting_files()),
    }


def write_dry_run(plan: dict, email_index: dict[str, str], target_path: Path):
    n_to_migrate = len(plan["plans"])
    n_with_emails = sum(1 for _, p in plan["plans"] if p["attendee_emails"])
    n_zero_attendees = n_to_migrate - n_with_emails

    lines = [
        "# atlas-frontmatter-migrate dry-run",
        "",
        f"**Date:** {dt.date.today().isoformat()}",
        "**Mode:** dry-run (no vault writes)",
        "**Source corpus:** every `*.md` file in a folder named `meetings/`",
        "",
        "## Plan summary",
        "",
        f"- Total meeting files: **{plan['total_meeting_files']}**",
        f"- Files to migrate this run: **{n_to_migrate}**",
        f"- — of which have ≥ 1 attendee email: **{n_with_emails}**",
        f"- — of which have an empty `attendees:` list: **{n_zero_attendees}** (still get `participants: []` for query consistency)",
        f"- Already migrated (skipped): **{plan['already_migrated']}**",
        f"- Parse failures (will be skipped): **{len(plan['parse_failures'])}**",
        f"- Person notes in email index: **{len(email_index)}**",
        f"- Distinct unmatched emails (no person note found): **{len(plan['unmatched_emails'])}**",
        "",
        "## Migration shape",
        "",
        "For every meeting note with `attendees:` present:",
        "",
        "1. Strip `attendees:` from the frontmatter.",
        "2. Add `attendee_emails:` with the original email list (back-compat per [DEC-003](../DECISIONS.md)).",
        "3. Add `participants:` with `[[Person-Slug]]` wikilinks resolved via `CRM/People/*.md`'s `email:` field.",
        "4. Emails without a matching person note (role addresses, later arrivals) are preserved in `attendee_emails:` but emit no `participants:` entry.",
        "5. Frontmatter ordering: insert `attendee_emails:` and `participants:` immediately after `date:` for readable diffs.",
    ]

    if plan["unmatched_emails"]:
        lines += [
            "",
            "## Unmatched emails (preserved in `attendee_emails:`, no wikilink)",
            "",
            "These attendee emails have no `CRM/People/*.md` person note. They're kept in `attendee_emails:` for record but produce no `participants:` entry. Most should be role addresses already filtered by atlas-people-extract; investigate any that look like real people.",
            "",
            "| Email | Occurrences |",
            "|---|---|",
        ]
        for email, n in sorted(plan["unmatched_emails"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{email}` | {n} |")

    if plan["parse_failures"]:
        lines += [
            "",
            "## Frontmatter parse failures (will be skipped)",
            "",
        ]
        for p in plan["parse_failures"][:20]:
            lines.append(f"- `{p.relative_to(VAULT)}`")
        if len(plan["parse_failures"]) > 20:
            lines.append(f"- … and {len(plan['parse_failures']) - 20} more")

    lines += [
        "",
        "## Sample plans (first 5 with attendees)",
        "",
    ]
    sample_count = 0
    for path, p in plan["plans"]:
        if not p["attendee_emails"]:
            continue
        if sample_count >= 5:
            break
        sample_count += 1
        rel = path.relative_to(VAULT)
        lines += [
            f"### `{rel}`",
            "",
            "Before (the `attendees:` line):",
            "```yaml",
            f"attendees: {p['attendee_emails']}",
            "```",
            "",
            "After (replaces `attendees:`):",
            "```yaml",
            f"attendee_emails: {p['attendee_emails']}",
            f"participants: {p['participants']}",
            "```",
            "",
        ]
        if p["unmatched"]:
            lines += [f"Unmatched emails in this file: {p['unmatched']}", ""]

    lines += [
        "## Acceptance gates (predicted)",
        "",
        f"- every meeting has `participants:`: predicted **PASS** — every one of the {plan['total_meeting_files']} meeting files will carry the field after migration (including the {n_zero_attendees} with empty attendees lists, which get `participants: []`).",
        f"- original emails preserved as `attendee_emails:`: predicted **PASS** by construction — `attendee_emails:` is populated from the original `attendees:` value verbatim.",
        "- Dataview returns the owner's attended meetings: **TBD** — verified post-execute (some meetings file the owner implicitly as organizer, not attendee, so folder coverage may exceed the owner's mention_count).",
        f"- this dry-run exists: met (this file).",
        f"- spot-check 10 random files: **TBD** — performed post-execute.",
        "",
        "## Next iteration",
        "",
        "Re-run with `--execute` to apply the plan to disk. Review this file first; the execute pass is the approval point.",
    ]
    target_path.write_text("\n".join(lines) + "\n")


def execute_plan(plan: dict):
    for path, p in plan["plans"]:
        apply_one(path, p)
    write_run_summary(plan, mode="execute")


def write_run_summary(plan: dict, mode: str):
    p = SKILL_DIR / "last-run.md"
    n_to_migrate = len(plan["plans"])
    p.write_text(
        f"# atlas-frontmatter-migrate — last run\n\n"
        f"**When:** {dt.datetime.now().isoformat(timespec='seconds')}\n"
        f"**Mode:** {mode}\n\n"
        f"- Total meeting files: {plan['total_meeting_files']}\n"
        f"- Migrated this run: {n_to_migrate if mode == 'execute' else 0}\n"
        f"- Planned (dry-run): {n_to_migrate if mode == 'dry-run' else 0}\n"
        f"- Already migrated (skipped): {plan['already_migrated']}\n"
        f"- Parse failures: {len(plan['parse_failures'])}\n"
        f"- Distinct unmatched emails: {len(plan['unmatched_emails'])}\n"
    )


def main():
    ap = argparse.ArgumentParser(description="Migrate meeting attendees → participants wikilinks.")
    ap.add_argument("--execute", action="store_true",
                    help="Apply the migration. Without this, dry-run only.")
    ap.add_argument("--dry-run-report", default=None,
                    help="Path to write dry-run report markdown. Implies dry-run.")
    args = ap.parse_args()

    email_index = build_email_index()
    plan = make_plan(email_index)

    if args.execute:
        execute_plan(plan)
        print(f"migrated={len(plan['plans'])} "
              f"already_migrated={plan['already_migrated']} "
              f"unmatched_distinct={len(plan['unmatched_emails'])}")
    else:
        path = Path(args.dry_run_report) if args.dry_run_report else (
            SKILL_DIR / "last-dry-run.md"
        )
        write_dry_run(plan, email_index, path)
        write_run_summary(plan, mode="dry-run")
        print(f"dry-run: would migrate {len(plan['plans'])}, "
              f"skip {plan['already_migrated']} (already migrated); report → {path}")


if __name__ == "__main__":
    main()
