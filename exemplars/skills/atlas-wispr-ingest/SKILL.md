---
name: atlas-wispr-ingest
description: Ingest Wispr Flow's local SQLite history into raw/wispr/. Reads ~/Library/Application Support/Wispr Flow/flow.sqlite in read-only mode, queries the History/Notes/CalendarEvents tables, and emits one markdown file per transcript with stable wispr_id frontmatter. Idempotent and incremental via state.json. Triggers on "sync wispr", "wispr ingest", "ingest wispr flow", "rebuild raw wispr", or any scheduled nightly run.
exemplar-of: atlas-wispr-ingest
status: active
requires: [cli/python3]
---

# atlas-wispr-ingest

You ingest the owner's Wispr Flow voice-journal history from its local SQLite database into `{{vault_root}}/{{folders.raw}}/wispr/`. Each Wispr History row becomes one markdown file under `{{folders.raw}}/wispr/<wispr_id>.md`, with Notes under `{{folders.raw}}/wispr/notes/` and CalendarEvents under `{{folders.raw}}/wispr/calendar/`.

This is the simplest ingest skill in the suite: no auth, no rate limits, no network. The Wispr SQLite is local and the schema has been stable across Wispr versions (verify against the version the owner is running when the skill is first set up).

## Mental model

Wispr Flow is a voice-to-text tool that captures spoken phrases and lets the user dictate into any text field. Its History table holds every transcription with metadata (app, URL, duration, word count, status). This skill mirrors that history into the vault's `{{folders.raw}}/` layer per DEC-009. Once mirrored, the materializer and the pattern-surfacing skills can extract entities and concepts from the corpus.

The Wispr database is large (on the order of a gigabyte) and includes blob audio and screenshot fields per row. **You do not copy the blobs**, only the textual fields. The skill's output is text-only markdown.

## Workflow

### 1. Open Wispr SQLite read-only

```python
conn = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True)
```

The `?mode=ro` flag is load-bearing: the ingest must survive concurrent Wispr writes. The skill never writes back to the Wispr DB.

### 2. Load state

`{{skills_root}}/atlas-wispr-ingest/state.json`:

```json
{
  "last_run_iso": "2026-05-20T10:00:00Z",
  "last_run_count": 720,
  "schema_version": 1
}
```

First-run sentinel: `last_run_iso` is the 30-day-ago boundary (`datetime.now() - timedelta(days=30)`).

### 3. Query History table

```sql
SELECT transcriptEntityId, asrText, formattedText, editedText, timestamp,
       app, url, duration, numWords, status, language
FROM History
WHERE timestamp > :since_iso
  AND COALESCE(isArchived, 0) = 0
ORDER BY timestamp ASC
```

The body-text precedence: `editedText` (user-corrected) → `formattedText` (Wispr-formatted) → `asrText` (raw ASR). First non-null wins; record which level was used in `body_source` frontmatter.

### 4. Write one file per History row

`{{vault_root}}/{{folders.raw}}/wispr/<transcriptEntityId>.md`:

```markdown
---
type: raw-wispr
wispr_id: <transcriptEntityId>
timestamp: <ISO datetime>
date: <YYYY-MM-DD>
app: <app>
url: <url>
duration: <float>
numWords: <int>
status: <status>
language: <language>
body_source: editedText | formattedText | asrText
ingested_at: <ISO datetime of this run>
---

# <date_human> — <app friendly name> (<duration_human>)

<body text>
```

`app_friendly_name`: derive from the bundle ID. `com.anthropic.claudefordesktop` → `Claude Desktop`; `com.tinyspeck.slackmacgap` → `Slack`; etc. Fallback: raw bundle ID.

`duration_human`: `90.5s` for <120s; `2m 30s` for ≥120s.

### 5. Query Notes + CalendarEvents (optional content)

Same pattern:

- `Notes` → `{{folders.raw}}/wispr/notes/<id>.md`. Body = `content` field.
- `CalendarEvents` → `{{folders.raw}}/wispr/calendar/<externalId>.md`. Body = `summary` field; metadata = title, startAtUtc, endAtUtc, conferenceUrl.

These tables may be empty on a given install (they were empty on the reference install when this skill was first built). The skill writes the folder structure regardless so future Wispr versions that populate these tables ingest cleanly.

### 6. Write the per-run summary

`{{skills_root}}/atlas-wispr-ingest/last-run.md` + update `state.json`:

- `last_run_iso` = current run start time (ISO).
- `last_run_count` = total History rows written across all runs to date (cumulative since first ingest).

## Invocation

```bash
# Dry-run preview (no vault writes)
python3 ingest.py --dry-run-report /tmp/atlas-wispr-ingest-dryrun.md

# First-run execution
python3 ingest.py --execute

# Incremental run (uses state.json's last_run_iso)
python3 ingest.py --execute --incremental

# Override the time window (e.g. backfill 90 days)
python3 ingest.py --execute --since 2026-02-01
```

When invoked from a scheduled run (per DEC-013, nightly under `atlas-nightly`), prefer `--execute --incremental`.

## Idempotency contract

- The `wispr_id` (transcriptEntityId) is the stable PK. If `{{folders.raw}}/wispr/<wispr_id>.md` already exists, the skill **skips** the row (raw is append-only per DEC-009).
- Re-running with the same `--since` is a no-op: every row's file is already there.
- `last_run_iso` advances on every successful execute. Incremental runs only see rows newer than the last cursor.

## Edge cases

- **NULL `editedText` AND NULL `formattedText`** — fall back to `asrText`. If all three are NULL, write a stub body `(no text content)` and log to `skipped_empty_body`.
- **Concurrent Wispr writes** — `?mode=ro` blocks your process from locking. SQLite returns transient `database is locked` errors only when a write transaction is committing; the skill retries up to 3x with 200ms backoff.
- **Wispr DB missing or corrupt** — skill exits with a clear error to stderr; never writes a partial vault state.
- **`app` field is `null` or `unknown`** — skill writes the file with `app: unknown` and the title shape becomes `<date> — Unknown app (<duration>)`.
- **Encoding** — `editedText` and `formattedText` are UTF-8 in the DB. The skill writes UTF-8 markdown.
- **Archived rows** — `isArchived = 1` rows are excluded by the query (`AND COALESCE(isArchived, 0) = 0`). If a user later un-archives, the row will appear in the next incremental run.

## Schema version

If a future Wispr version adds or renames fields, the skill should detect a schema drift before writing. Stub `schema_version: 1` in state.json; raise the version when the skill is updated.

## Relationship to other skills

- `atlas-fireflies-ingest` — the same pattern for the meeting domain. atlas-wispr-ingest cribs the markdown shape but uses local SQLite instead of an API.
- `atlas-wispr-meetings-ingest` — sibling skill for Wispr's Meeting Notetaker, which lives in different tables and files.
- `atlas-wiki-materialize` — may eventually read `{{folders.raw}}/wispr/*.md` for entity extraction (v1 reads only meeting summaries + CRM).
- `atlas-emerge` — mines the Wispr corpus for thought-threads (per DEC-014, Claude history is fair game; the same logic applies to Wispr by extension).
- `atlas-morning` — reads `{{folders.raw}}/wispr/calendar/` for today's calendar events.
