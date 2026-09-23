---
name: atlas-health
description: Sunday 9 PM vault hygiene auditor. Scans the vault for orphaned notes, broken wikilinks, frontmatter schema violations, inbox overflow, and stale raw/ entries. Regenerates {{folders.meta}}/Dashboards/Vault-Health.md with counts and clickable wikilink lists per category. Idempotent snapshot — re-running overwrites the report. Triggers on "atlas health", "vault health", "audit vault", "/atlas-health", or any scheduled Sunday 21:00 run.
exemplar-of: atlas-health
status: active
requires: [cli/python3]
---

# atlas-health

You are the vault hygiene auditor. You run once per week on Sunday at 21:00 in `{{timezone}}` (scheduled via `mcp__scheduled-tasks`). Your job:

1. Walk the vault and detect five categories of hygiene issues.
2. Build a snapshot report at `{{folders.meta}}/Dashboards/Vault-Health.md`.
3. Surface a "Needs attention" alert at the top for any category with ≥ 1 finding.
4. Write a per-run summary at `{{skills_root}}/engine/atlas-health/last-run.md`.

This skill is **diagnostic, not corrective**. It never deletes, moves, or edits files in the vault. It only **reads** and produces a regenerated dashboard.

## Five hygiene categories

### Category 1 — Orphaned notes (no inbound links)

A note is orphaned if no other vault file contains `[[<note-stem>]]` referring to it.

**Scope:** all `.md` files under PARA (`{{folders.projects}}/`, `{{folders.areas}}/`, `{{folders.resources}}/`, `{{folders.archive}}/`) and `{{folders.crm}}/`. Excludes `{{folders.raw}}/` (raw is append-only and not expected to be linked), `{{folders.wiki}}/entities/` (entities are leaf-most by design), `{{folders.inbox}}/` (inbox overflow is its own category), and `{{folders.daily}}/` (daily notes are timestamped, not linked).

**Detection:**

```python
for note in para_files:
    stem = note.stem
    target_patterns = [f"[[{stem}]]", f"[[{stem}|"]  # plain + aliased
    # Search the whole vault (minus the note itself) for either pattern
    if not any_match:
        orphans.append(note)
```

Cap at 50 in the report; surface the cap.

**Resolution hint:** orphans are candidates for `{{folders.archive}}/`. The skill **surfaces**, the owner **decides**.

### Category 2 — Broken wikilinks

A wikilink `[[X]]` (or `[[X|alias]]`, `[[X#section]]`, `[[X#section|alias]]`) is broken if no vault file named `X.md` exists.

**Scope:** every `.md` file in the vault, including `{{folders.raw}}/` and `{{folders.wiki}}/`.

**Detection:** extract every `[[...]]` token; resolve the target stem (everything before `|` or `#`); check if `<stem>.md` exists anywhere in the vault.

```python
all_stems = {p.stem for p in vault.rglob("*.md")}
for note in vault.rglob("*.md"):
    for m in re.finditer(r'\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]', text):
        target = m.group(1).strip()
        if target not in all_stems:
            broken.append((note, target))
```

Group by target. The report shows: `[[Missing-Target]]` referenced by N files (with the file paths as a collapsible list).

Cap at 50 distinct targets.

### Category 3 — Frontmatter schema violations

Per file-type, required frontmatter fields:

| File type | Detection | Required fields |
|---|---|---|
| Meeting note | `type: meeting` OR path under `*/meetings/` | `date`, `participants`, `meeting_id` (preferred) or fallback to `fireflies_id` |
| Person note | `type: person` OR path under `{{folders.crm}}/People/` | `tier`, `email` (or `emails:` list), `canonical_id` |
| Daily note | `type: daily` OR path matches `{{folders.daily}}/.../YYYY-MM-DD.md` | `date`, `tags: [daily]` |
| Raw item | path under `{{folders.raw}}/<source>/` | varies per source — only check `date:` or `timestamp:` exists |
| Wiki entity | path under `{{folders.wiki}}/entities/` | `canonical_id`, `type: entity` |

A violation = file matches a type detection rule but is missing one or more required fields. Report: filename + the missing field names.

Cap at 50 violations per category subtype.

### Category 4 — Inbox overflow

Threshold (shared with the fireflies ingest's overflow warning): `{{folders.inbox}}/*.md` (excluding subfolders like `needs-decision/`) count > 20.

**Detection:**

```bash
find "{{vault_root}}/{{folders.inbox}}" -maxdepth 1 -name "*.md" -type f | wc -l
```

If > 20, surface as a finding with the count + sample filenames. The report links to the `Unreviewed-Inbox.md` dashboard if it exists.

### Category 5 — Stale `{{folders.raw}}/` entries

A raw file is stale if:
- Its `date:` (or fallback `timestamp:` / mtime) is older than 30 days, AND
- It hasn't been **materialized** into `{{folders.wiki}}/` (no `{{folders.wiki}}/entities/<stem>.md` mentioning it), AND
- It isn't **referenced** by any PARA note (no `[[<raw-file-stem>]]` link from outside `{{folders.raw}}/`).

**Scope:** every `.md` under `{{vault_root}}/{{folders.raw}}/`.

**Detection:**

```python
for raw_file in raw_dir.rglob("*.md"):
    if raw_file.age_days < 30: continue
    if mentioned_in_wiki(raw_file.stem): continue
    if linked_from_para(raw_file.stem): continue
    stale.append(raw_file)
```

Cap at 50 in the report; rank by age descending (oldest first).

**Resolution hint:** stale raw items are candidates for the `atlas-emerge` / `atlas-wiki-materialize` flow — they're sitting in the corpus without being surfaced.

## Step 1 — Compute report metadata

```python
from datetime import date
TODAY = date.today()
REPORT_PATH = "{{vault_root}}/{{folders.meta}}/Dashboards/Vault-Health.md"
```

## Step 2 — Run each detection pass

Run categories 1–5 sequentially. Capture per-category:
- `count`: total findings (uncapped)
- `findings`: list of structured records (capped at 50 each)
- `duration_seconds`: how long the scan took
- `error`: any exception (don't abort; log and continue to the next category)

Per the failure-isolation contract (mirrored from `atlas-nightly`): one category's failure does NOT abort the run.

## Step 3 — Build the report

```markdown
---
type: dashboard-vault-health
generated_by: atlas-health
generated_at: <YYYY-MM-DDTHH:MM:SS>
report_date: <TODAY>
tags: [dashboard, vault-health]
---

# Vault Health — <TODAY>

> **Needs attention** — <K> categories with findings
>
> - **Orphaned notes**: <N> (cap shown: 50)
> - **Broken wikilinks**: <N> distinct missing targets
> - **Frontmatter violations**: <N>
> - **Inbox overflow**: <count> / 20 threshold
> - **Stale raw/ entries**: <N>
>
> *If a line shows 0 (or "ok") it isn't a finding — only ≥ 1 trips the alert.*

## Summary

| Category | Count | Status |
|---|---|---|
| Orphaned notes | <N> | ⚠️ needs attention / ✅ ok |
| Broken wikilinks | <N> | ⚠️ / ✅ |
| Frontmatter violations | <N> | ⚠️ / ✅ |
| Inbox overflow | <N> / 20 | ⚠️ / ✅ |
| Stale raw/ entries | <N> | ⚠️ / ✅ |

*Generated by `atlas-health` at <HH:MM> on <TODAY>. Scan duration: <Xs>.*

## Orphaned notes (<N>)

*Notes under PARA / CRM with no inbound wikilinks. Candidates for the archive folder.*

- [[<Note-Stem>]] — `<vault-relative path>`
- ...
- *(... and N more — see `last-run.md` for full list)*

## Broken wikilinks (<N> distinct missing targets)

*Wikilinks pointing to files that don't exist. May indicate typos, renames, or planned-but-unwritten notes.*

| Missing target | Referenced by |
|---|---|
| `[[Target-One]]` | [Note-A](path), [Note-B](path) |
| `[[Target-Two]]` | [Note-C](path) |
| ... | ... |

## Frontmatter schema violations (<N>)

*Files matching a type-detection rule but missing one or more required frontmatter fields.*

### Meeting notes (<N>)

- [[Meeting-Name]] — missing: `participants`, `meeting_id`
- ...

### Person notes (<N>)

- [[Person-Name]] — missing: `tier`
- ...

### Daily notes (<N>)

- [[2026-05-15]] — missing: `tags: [daily]`
- ...

### Wiki entities (<N>)

- [[Entity-Name]] — missing: `canonical_id`
- ...

### Raw items (<N>)

- `raw/fireflies/<id>.md` — missing: `date`
- ...

## Inbox overflow

Current inbox count: **<N>** (threshold: 20)

<if over threshold>
Sample files:
- [<filename>](<url-encoded inbox folder>/<filename>.md)
- ...

Recommended action: triage with `/atlas-fireflies-ingest` or move to a PARA folder.
</if>

## Stale `raw/` entries (<N>)

*Raw files > 30 days old, not materialized into the wiki, not referenced by any PARA note.*

| Age | Path | Source |
|---|---|---|
| 87d | `raw/fireflies/<id>.md` | fireflies |
| 64d | `raw/wispr/<id>.md` | wispr |
| ... | ... | ... |

Recommended action: re-run `atlas-wiki-materialize` and `atlas-emerge` to surface these into the wiki layer; or, if genuinely uninteresting, move to `raw/archive/<source>/` (not implemented in v1; future cleanup task).

## Methodology

- **Orphaned notes** — Scope: PARA + CRM. Excluded: raw, wiki entities, inbox, daily notes. Detection: no `[[<stem>]]` or `[[<stem>|alias]]` token anywhere in the vault.
- **Broken wikilinks** — Scope: every `.md`. Detection: stem before `|` or `#` doesn't match any `.md` filename in the vault.
- **Frontmatter violations** — Scope: type-tagged files. Required fields per type table above.
- **Inbox overflow** — Threshold: 20 files at the inbox top level (not the `needs-decision/` subfolder).
- **Stale raw/** — Age cutoff: 30 days. Materialized: any wiki entity page mentions the raw file's stem. Referenced: any non-raw file contains `[[<raw-stem>]]`.

## Notes

- This report is a **regenerated snapshot** — every Sunday 21:00 overwrites the file. No history is kept inline; the report itself isn't git-tracked (DEC-011), so historical trends are a v2 concern.
- Findings are surfaces, not commands. The owner decides what to act on.
- A category showing 0 findings is healthy. Don't optimize for empty categories at the expense of corpus signal — some orphan notes are intentional (one-off references, scratch).
- The 50-per-category cap exists to keep the dashboard readable. The `last-run.md` per-run summary records the full counts.
```

## Step 4 — Write last-run.md

`{{skills_root}}/engine/atlas-health/last-run.md`:

```markdown
# atlas-health — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- report_path: {{vault_root}}/{{folders.meta}}/Dashboards/Vault-Health.md
- duration_seconds: <float>
- counts:
  - orphaned_notes: <N> (cap shown in report: 50)
  - broken_wikilinks_distinct_targets: <N>
  - frontmatter_violations:
    - meeting: <N>
    - person: <N>
    - daily: <N>
    - entity: <N>
    - raw: <N>
  - inbox_count: <N>
  - stale_raw: <N>
- categories_needing_attention: <K>  # number of categories with ≥ 1 finding
- errors_per_category:
  - <category>: <error or null>
```

## Idempotency contract

- The report at `{{folders.meta}}/Dashboards/Vault-Health.md` is **always overwritten** on each run. Never appended.
- Re-running the same Sunday produces an identical report (modulo the `generated_at:` timestamp).
- The skill **never modifies** any source-of-truth file (notes, `{{folders.raw}}/`, `{{folders.wiki}}/`). It only writes the dashboard and `last-run.md`.

Verified mechanically: two consecutive runs on an unchanged corpus produce byte-identical reports modulo timestamp.

## Failure isolation

If one detection category errors (e.g. permission failure scanning a subdir), the skill captures the error and continues to the next category. The report shows the failed category's row as `⚠️ scan failed — see logs` rather than omitting it. Errors are logged to `{{skills_root}}/engine/atlas-health/logs/<TODAY>/<category>.stderr.log`.

## Edge cases

- **Very large vault** (> 10k files): scan may take > 60s. Acceptable; this runs weekly, not hourly.
- **Symlinks in vault**: skip; they're not real notes. Don't follow.
- **Files with non-UTF-8 encoding**: log and skip; don't crash.
- **Empty `{{folders.raw}}/` folder**: stale-raw count is 0; not an error.
- **Empty `{{folders.meta}}/Dashboards/` folder**: `mkdir -p` before writing.
- **Wikilinks across vault renames**: a broken link may indicate a rename that didn't update references. The report surfaces but doesn't auto-fix (DEC-007 explicit decisions only).

## Invocation

When invoked manually (via Claude Code):
1. Read this SKILL.md.
2. Execute Steps 1–4 sequentially.
3. Report: report path, per-category counts, any scan errors.

When invoked by the scheduled task:
- Cron: `0 21 * * 0` (Sunday 21:00 in `{{timezone}}`).
- Registered via `mcp__scheduled-tasks__create_scheduled_task`.
- The scheduled task fires a Claude Code session with a self-contained prompt that points back to this SKILL.md.

## Relationship to other skills

- **Upstream**: `atlas-nightly` populates `{{folders.raw}}/` and runs `atlas-wiki-materialize` and `atlas-emerge` each evening — by Sunday night, the corpus reflects a full week of ingestion and materialization. Stale-raw signal is meaningful only against a corpus that's actively being materialized.
- **Sister scheduled agents**: `atlas-morning` reads the dashboard the morning after a Sunday run. `atlas-weekly` is the Friday narrative; `atlas-health` is the Sunday inventory. Complementary, not redundant.
- **Downstream**: the owner reviews on Monday morning, archives orphan notes via Obsidian's normal move flow, fixes broken wikilinks, and triages inbox overflow.

## Anti-goals (NOT v1)

- Auto-archiving orphans. Surface, don't decide.
- Auto-fixing broken wikilinks (e.g. by fuzzy-matching to similar filenames). Too risky; surface candidates instead in v2.
- Per-category history tracking. The dashboard is a snapshot. Historical trends are a v2 concern.
- Alerting via Slack / email. Read-only into the vault.
- Scoring vault "health" with a single number. Numbers are surfaced per-category; aggregation hides signal.
