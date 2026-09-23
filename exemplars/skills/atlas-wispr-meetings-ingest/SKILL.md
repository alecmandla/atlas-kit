---
name: atlas-wispr-meetings-ingest
description: Ingest Wispr Flow's Meeting Notetaker into raw/wispr/meetings/. Reads the local flow.sqlite Meetings table read-only plus the per-meeting refined.ndjson transcript on disk, and emits one markdown file per meeting carrying the owner's in-meeting notes, Wispr's summary, and the full speaker-labeled transcript. Cross-links to Fireflies when both captured the same event. Idempotent, incremental, no network. Triggers on "sync wispr meetings", "ingest wispr meetings", "wispr notetaker", "rebuild raw wispr meetings", or any scheduled nightly run.
exemplar-of: atlas-wispr-meetings-ingest
status: active
requires: [cli/python3]
---

# atlas-wispr-meetings-ingest

You ingest the owner's Wispr Flow **Meeting Notetaker** sessions into `{{vault_root}}/{{folders.raw}}/wispr/meetings/`. One meeting becomes one markdown file.

This is a **sibling of, not a replacement for, `atlas-wispr-ingest`**. That skill mirrors Wispr's dictation `History` (voice-to-text snippets typed into other apps). This skill mirrors Wispr's *meeting* capture, which lives in entirely different tables and files.

## Why this exists

Wispr's notetaker is native (no browser, no bot joining the call), so it captures meetings Fireflies structurally cannot: in-person meetings above all. It is also the only place the owner's **hand-typed live notes** exist. Fireflies has no equivalent field: it produces a summary and action items, never the human's own notes. That makes the two sources complementary rather than redundant, which is why this skill ingests every meeting and cross-links rather than deduplicating (see "Fireflies cross-linking").

## Mental model

Three local sources, no auth and no network:

| Source | Provides |
| --- | --- |
| `~/Library/Application Support/Wispr Flow/flow.sqlite` → `Meetings` | `title`, `notes` (the owner's own), `summary`, `participantNames`, `speakerMap`, `createdAt`, `endedAt`, `refineStatus`, `calendarEventExternalId` |
| `.../Wispr Flow/meetings/<uuid>/refined.ndjson` | cleaned, timestamped, speaker-labeled transcript (**preferred**) |
| `.../Wispr Flow/meetings/<uuid>/live.ndjson` | raw live transcript (**fallback** when refinement has not completed) |

The per-meeting directory also holds `upload.ogg`. **Audio is never copied**; the directory path is recorded in `audio_path` frontmatter so the recording stays one click away without bloating the vault.

## Workflow

### 1. Open the SQLite read-only

```python
conn = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True, timeout=20)
```

`?mode=ro` is load-bearing: Wispr writes to this database concurrently while running. The skill never writes back.

### 2. Query the Meetings table

```sql
SELECT id, title, createdAt, modifiedAt, endedAt, notes, summary,
       participantNames, speakerMap, calendarEventExternalId,
       refineStatus, finalized, isDeleted
FROM Meetings
WHERE COALESCE(isDeleted, 0) = 0
  AND modifiedAt > :since        -- only when --incremental / --since
ORDER BY createdAt ASC
```

The cursor is on `modifiedAt`, **not** `createdAt`, because meetings mutate after they end (see "Idempotency").

### 3. Load and render the transcript

Prefer `refined.ndjson`; fall back to `live.ndjson`; record which in `transcript_source`. The first line of `live.ndjson` is a `{"meta": ...}` header and is skipped.

Consecutive segments from the same speaker are collapsed into one paragraph, rendered as `**Speaker** (mm:ss) text`. A 1-hour meeting is roughly 400 raw segments and collapses to well under half that.

A meeting with **neither** transcript nor notes is skipped; it carries nothing.

### 4. Resolve speakers

`speakerMap` is JSON of the shape:

```json
{"people": {"<uuid>": {"name": "{{owner_name}}", "origin": "self"}},
 "assignments": {"1": {"consensus": "<uuid>", "mic": "<uuid>", "user": null, "llm": null}}}
```

Resolution order per assignment: `consensus` → `user` → `mic` → `llm`. Unresolved speakers fall back to `Speaker N`.

**Partial resolution is normal and is Wispr's limitation, not a defect here.** Wispr often resolves only the mic-side speaker (the owner). The full roster is written to `participants` frontmatter so an unresolved `Speaker 2` can still be disambiguated by hand or by a later pass.

### 5. Cross-link to Fireflies

Scan `{{folders.raw}}/fireflies/*.md` frontmatter for `date` + `meeting_id` + `meeting_note`, and match a Wispr meeting to the nearest Fireflies meeting whose start is within **5 minutes**. Both sources store UTC, so the comparison is direct.

The window was calibrated against a few dozen real meetings. Genuine matches cluster at 3 minutes or less (most under 1 minute); the nearest false positive sat near 12 minutes. Five minutes separates the two populations with margin on both sides. A wide window (45 minutes, the first attempt) produces false links against a dense meeting calendar, including in-person meetings wrongly matched to unrelated calls. Keep the window tight.

**Titles are deliberately not compared.** Wispr auto-titles from content while Fireflies uses the calendar name, so the same meeting legitimately carries two unrelated titles: "Onboarding checklist review" and "Jordan and {{owner_name}} 1:1" can be one event.

A match adds `fireflies_id` / `fireflies_note` frontmatter and a callout line in the body. Nothing is ever suppressed.

### 6. Write one file per meeting

Path: `{{folders.raw}}/wispr/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md`. The 8-char id suffix keeps recurring meetings ("Weekly stand-up") from colliding while the date and slug keep the filename readable.

Body order is **My Notes → Summary → Transcript**. Notes lead because they are the scarcest and highest-signal content in the file, and the only part with no equivalent anywhere else.

Writes are atomic (`.md.tmp` + `os.replace`).

## Idempotency

This skill **deviates from the strict append-only rule** the other raw ingests follow (DEC-009), and does so deliberately.

A Wispr meeting is mutable after it ends: refinement runs asynchronously (`refineStatus` goes `→ complete`), and the owner adds or edits notes hours later. A first-sight-only write would permanently freeze an empty shell.

So each file records `source_modified_at` in frontmatter, and on every run:

- file absent → **create**
- file present, `modifiedAt` unchanged → **skip**
- file present, `modifiedAt` newer → **rewrite in place**

The path is derived deterministically from the meeting id, so an update overwrites rather than duplicating. Verify this on first setup: forcing a stale `source_modified_at` on one file must produce `updated=1` with the file count unchanged.

## Invocation

```bash
cd {{skills_root}}/engine/atlas-wispr-meetings-ingest

# Dry run (default — writes nothing)
python3 ingest.py

# Dry run with a report table
python3 ingest.py --dry-run-report /tmp/atlas-wispr-meetings-ingest-dryrun.md

# Full pass
python3 ingest.py --execute

# Nightly / incremental (uses state.json cursor_ts)
python3 ingest.py --execute --incremental

# Backfill a window
python3 ingest.py --execute --since 2026-08-01
```

Scheduled runs should use `--execute --incremental`.

## Frontmatter contract

```yaml
type: raw-wispr-meeting
wispr_meeting_id: <uuid>          # stable PK
title: "<Wispr title>"
date: YYYY-MM-DD
timestamp: YYYY-MM-DD HH:MM:SS    # UTC
source_modified_at: ...           # drives the update check — do not hand-edit
duration: "1h 02m"
participants: [...]               # full roster from participantNames
speakers_resolved: [...]          # subset Wispr actually mapped
transcript_source: refined | live | none
transcript_segments: <int>
refine_status: complete | failed | ""
has_notes: true | false
calendar_event_id: "..."          # joins CalendarEvents
audio_path: "..."                 # .ogg stays on disk, never copied
fireflies_id: <id>                # only when matched
fireflies_note: "[[...]]"         # only when matched
ingested_at: <ISO>
```

`has_notes: true` is the field worth querying: it isolates the meetings where the owner was actively thinking, which is a much stronger signal than the transcript alone.

## Operational notes

- **Run lock:** `.run.lock`, stale after 3600s, same pattern as the other ingests.
- **State:** `state.json` holds `cursor_ts` (max `modifiedAt` seen), `last_run_iso`, `last_run_count`.
- **Report:** `last-run.md` after every run.
- **Failure isolation:** a single unreadable meeting appends to `errors` and does not abort the run.
- **Scale:** growth is a few meetings per weekday; a full non-incremental pass takes seconds.
- Wispr's `Notes` and `NoteVersions` tables are a *separate* Wispr feature and may be empty on a given install. `atlas-wispr-ingest` already covers them if they ever populate.

## Relationship to other skills

- `atlas-wispr-ingest` — sibling; dictation history, not meetings.
- `atlas-fireflies-ingest` — writes the `{{folders.raw}}/fireflies/` records this skill cross-links against. Under `atlas-nightly` this skill runs after the Fireflies ingest so cross-links resolve against fresh files.
- `atlas-emerge` / `atlas-synthesize` — consume `{{folders.raw}}/wispr/meetings/` like any other raw source.
