---
name: atlas-people-extract
description: Walk every meeting note in the owner's Obsidian vault, extract every unique attendee email, and produce or refresh a stub person note in CRM/People/<First-Last>.md. Idempotent and non-destructive. Triggers on "extract people", "build people crm", "people crm refresh", "rebuild people index", or any scheduled nightly run. Maintains mention_count and last_seen automatically; never clobbers owner-added body content or owner-edited frontmatter fields.
exemplar-of: atlas-people-extract
status: active
requires: [cli/python3]
---

# atlas-people-extract

You bootstrap and maintain the owner's People CRM. Every person who has appeared in a Fireflies meeting transcript gets a stub note at `{{folders.crm}}/People/<First-Last>.md`. The skill is **append-and-refresh only**: it never overwrites owner-authored content.

## Mental model

The vault uses wikilink-based people attribution (DEC-003). Meetings reference people as `[[Person-Name]]`, not raw emails. To make those wikilinks resolve, every person needs a `{{folders.crm}}/People/<First-Last>.md` page, even an empty stub.

The source of truth for "who is a person" is the `attendees:` (legacy) or `attendee_emails:` (current) frontmatter field on meeting notes in `{{folders.projects}}/` and `{{folders.areas}}/`. Every unique email there becomes one person note.

**The skill bootstraps the tier signal but does not curate.** It assigns an initial tier from mention count and inserts a Tier-1 dossier skeleton on first promotion, but the actual personality (Bio prose, role notes, decision-style observations) is the owner's to write. Owner-curated body content is preserved verbatim across every re-run.

## Workflow

### 1. Walk the meeting corpus

Scan `{{vault_root}}/{{folders.projects}}/**/*.md` and `{{vault_root}}/{{folders.areas}}/**/*.md`. For each markdown file with a YAML frontmatter block, extract the `attendees:` list (or `attendee_emails:` if migrated). Skip any file without a parseable frontmatter.

Skip role addresses (`accounting@`, `billing@`, `support@`, `info@`, `hello@`, `team@`, `admin@`, `office@`, `sales@`, `contact@`, `noreply@`, `no-reply@`); these are not individual people.

### 2. Derive identity

For each unique email `local@domain`:

- **Display name:** title-case each `.`/`_`/`-`/`+`-separated segment of `local`. Strip trailing digits. `jordan.vale@…` → `Jordan Vale`; `priyaokafor21@…` → `Priyaokafor`; `sam@…` → `Sam`.
- **Filename slug:** kebab-case of the title-cased segments. `Jordan Vale` → `Jordan-Vale.md`.
- **Company shortname:** the primary label of the domain (rightmost-non-TLD segment, lowercased, alphanumeric-only). `harborlane.example` → `harborlane`; `saltmarshinn.example` → `saltmarshinn`.
- **Mention count:** number of meeting files containing this email.
- **Last seen:** max `date:` (frontmatter) across all matching meetings.

### 3. Resolve DEC-012 collisions

If two distinct emails derive to the same `First-Last` slug, both files get a company suffix per DEC-012: `First-Last-CompanyShort.md`. Example: `sam@pinecrestlodge.example` → `Sam-Pinecrestlodge.md` and `sam@saltmarshinn.example` → `Sam-Saltmarshinn.md`.

### 4. Assign tier from mention count

Each person's tier is derived from their `mention_count`:

| Tier | Mentions | Body shape |
|---|---|---|
| **T1** | ≥ 8 | Stub body + a dossier block (Background / Role / Decision style / Last 5 Interactions) inserted between the H1 and the Bio, wrapped in `<!-- atlas-people-extract:dossier-{start,end} -->` markers |
| **T2** | 3–7 | Stub body (Bio, Recent Interactions, Open Threads, Notes) |
| **T3** | 1–2 | Stub body — same as T2 |

The `tier:` frontmatter field is the truth. On `--execute`, the skill computes a candidate tier from the current mention count and compares to the file's existing `tier:`. **The skill never auto-demotes**: an owner who manually promoted someone to T1 keeps that T1 even if their mention count drops below 8. The implementation rule: `new_tier = min(old_tier, computed_tier)`.

### 5. Write or refresh person notes

For each derived person:

- **If the file at `{{folders.crm}}/People/<slug>.md` does not exist** → write a fresh stub using the schema below. Initial `tier:` from Step 4 (T1 stubs get the dossier inline; T2/T3 stubs don't).
- **If the file exists** → re-serialize only `mention_count`, `last_seen`, and (if it changed) `tier` in the frontmatter. Backfill `email`, `company`, `domain` if previously blank. Preserve every other frontmatter field and every line of the body. On *promotion to T1* (old tier was 2 or 3, new tier is 1), insert the dossier block once if no `<!-- atlas-people-extract:dossier-start -->` marker is present yet.

Stub schema:

```markdown
---
type: person
name: <Display Name>
email: <email>
company: <company shortname>
domain: <domain>
tier: 3
mention_count: <N>
last_seen: <YYYY-MM-DD>
tags: [person]
---

# <Display Name>

## Bio
*One paragraph: role, company, how we know each other, anything memorable.*

## Recent Interactions

`​``dataview
TABLE date as "Date", file.link as "Meeting"
FROM "{{folders.projects}}" OR "{{folders.areas}}"
WHERE contains(participants, this.file.link)
SORT date DESC
LIMIT 20
`​``

## Open Threads
-

## Notes
```

Schema matches `{{folders.resources}}/Templates/Person-Note.md`; a person note created by hand via Templater is mutually compatible with a note created by this skill.

### 6. Write the per-run summary

After the walk completes, write `{{skills_root}}/atlas-people-extract/last-run.md` with:

- Timestamp.
- Mode (`execute` vs `dry-run`).
- Counts: emails scanned, notes created, notes updated.
- Current `{{folders.crm}}/People/` file count.
- **Initial tier assignment** breakdown for any newly-created notes (e.g. `T1=3, T2=8, T3=42`).
- **Tier transitions** for any existing notes whose tier changed this run, in the format `<File>: T<old> ↑ T<new>` (with `↑` for promotion, `↓` for demotion). Stable runs produce zero transitions.

### 7. Report

Print a one-line summary: `created=<n> updated=<n> transitions=<n> total_emails=<n>`.

## Invocation

The mechanical work lives in `extract.py`. From the skill directory:

```bash
# Preview the plan (no vault writes). Writes a dry-run report.
python3 extract.py --dry-run-report /tmp/atlas-people-extract-dryrun.md

# Apply the plan. Writes person notes; updates last-run.md.
python3 extract.py --execute
```

When invoked via trigger phrase ("extract people", "build people crm", etc.) without arguments, run the dry-run first and surface the planned counts. If they look sane (a healthy count of stubs on first run, idempotent zero-create on subsequent), invoke with `--execute`.

When invoked from a scheduled run (per DEC-013, nightly under `atlas-nightly`), skip the dry-run preview and go straight to `--execute`.

## Idempotency contract

Re-running the skill must:

- **Never create** a duplicate person note (no `Jordan-Vale-2.md` ever).
- **Never overwrite** body content. Owner-authored Bio, Open Threads, Notes survive every run.
- **Never clobber** owner-promoted `tier:`, custom frontmatter fields, hand-edited `name:`, or hand-curated `company:`/`domain:` values.
- **Always refresh** `mention_count` and `last_seen` to reflect the current meeting corpus.

Verify mechanically by re-running `python3 extract.py --execute` twice in a row: first run reports `created=N updated=0`; second run reports `created=0 updated=N`. Simulating a tier promotion + a custom frontmatter field + a body edit, then re-running, must leave all three preserved with only `mention_count`/`last_seen` refreshed.

## Edge cases

- **Single-name emails** (`sam@…`, `priya@…`) produce single-word display names and filenames (`Sam.md`, `Priya.md`). The owner can promote/rename later if a real last name is known.
- **Numeric trailing tags** (`priyaokafor21@…`) are stripped from the name derivation. `Priyaokafor.md`, not `Priyaokafor21.md`.
- **Multi-dot domains** (`inn.example`, `co.uk`-style) produce single-label company shortnames from the rightmost non-TLD segment.
- **Missing `date:` frontmatter** on a meeting: that meeting still contributes to mention counts, but does not advance `last_seen`.
- **Unicode in email local-parts**: the slug strips non-`[a-z0-9-]` characters for filesystem safety. The display name keeps the original characters.

## Relationship to other skills

- `atlas-fireflies-ingest` — writes the meeting notes whose `attendee_emails:` this skill reads.
- `atlas-wiki-materialize` — consumes `{{folders.crm}}/People/*.md` `company:` values for its org branch.
- `atlas-gmail-ingest` — cross-references the CRM for `participants:` wikilinks.
- `atlas-health` — audits person notes for the required `tier` / `email` / `canonical_id` fields.
