---
name: atlas-morning
description: Daily 8 AM agent. Creates today's daily note (if not yet created) and injects an "Atlas morning report" section with three synthesized lists: today's calendar events, overdue PARA tasks, and active idea-threads from the last 7 days. Idempotent — running again overwrites only the morning report section, preserving any other edits. Triggers on "atlas morning", "morning report", "create daily note", "/atlas-morning", or any scheduled 8 AM run.
exemplar-of: atlas-morning
status: active
requires: [cli/python3]
---

# atlas-morning

You are the morning synthesis agent. You run once per day at 08:00 in `{{timezone}}` (scheduled via `mcp__scheduled-tasks`). Your job is:

1. Ensure today's daily note exists at the correct path.
2. Build three synthesis lists from the vault's current state.
3. Inject (or overwrite) the "## Atlas morning report" section in the daily note.

This skill is synthesis-only — it reads from `{{folders.raw}}/` and PARA but does **not** pull new data. All ingest runs nightly under `atlas-nightly` (DEC-013).

## Step 1 — Establish today's date and paths

```
TODAY = current date in YYYY-MM-DD format
YEAR  = TODAY[:4]             # e.g. "2026"
MONTH = TODAY[:7]             # e.g. "2026-05"
DAILY_NOTE_DIR = {{vault_root}}/{{folders.daily}}/<YEAR>/<MONTH>/
DAILY_NOTE_PATH = <DAILY_NOTE_DIR>/<TODAY>.md
```

## Step 2 — Create the daily note if it doesn't exist

Check whether `DAILY_NOTE_PATH` already exists.

**If it does NOT exist:**

1. Create the directory chain `DAILY_NOTE_DIR` if needed.
2. Build the note content by instantiating the template at `{{vault_root}}/{{folders.meta}}/Templates/Daily-Note.md`, replacing all Templater variables with actual values:
   - `<% tp.date.now("YYYY-MM-DD") %>` → `TODAY`
   - `<% tp.date.now("dddd, MMMM D, YYYY") %>` → human-readable date (e.g. "Thursday, May 21, 2026")

3. Write the instantiated template to `DAILY_NOTE_PATH`.

**If it DOES exist:** do not modify any content outside the morning report section (Step 4 handles the idempotent injection).

## Step 3 — Build the three synthesis lists

### 3a — Today's calendar

**Source 1: Wispr CalendarEvents SQLite** (only if the owner uses Wispr Flow; otherwise skip to Source 2)

Open `~/Library/Application Support/Wispr Flow/flow.sqlite` in read-only mode:

`startAtUtc` and `endAtUtc` are **BIGINT epoch milliseconds**, not ISO strings. A bare `DATE(startAtUtc)` returns empty for every row and silently reports zero events. Divide by 1000 and convert:

```sql
SELECT DATE(startAtUtc/1000,'unixepoch','localtime') AS d,
       TIME(startAtUtc/1000,'unixepoch','localtime') AS t,
       title, conferenceUrl
FROM CalendarEvents
WHERE DATE(startAtUtc/1000,'unixepoch','localtime') = '<TODAY>'
ORDER BY startAtUtc ASC
```

`'localtime'` matters: events are stored in UTC and the owner's calendar is in `{{timezone}}`, so a UTC-only comparison shifts morning events onto the wrong day.

If the CalendarEvents table is empty or absent, skip silently — this is expected for many Wispr installs.

**Cross-check against `{{folders.raw}}/wispr/calendar/`.** The SQLite DB is only as fresh as the last Wispr sync (it can be days stale). The nightly `atlas-wispr-ingest` writes one file per event with a `start_utc:` frontmatter field, so today's events also appear as `{{folders.raw}}/wispr/calendar/*_<YYYYMMDD>T*.md`. Prefer the union of both sources.

**Source 2: Fireflies raw**

Scan `{{vault_root}}/{{folders.raw}}/fireflies/` for files whose frontmatter `date:` equals `TODAY`. These are today's recorded meetings (Fireflies syncs after a meeting ends; seeing them in `{{folders.raw}}/` means they already happened or are being processed). List their title + project frontmatter fields.

Format the calendar list as:

```markdown
### Today's calendar

- **HH:MM** — <event title> [(join)](<conferenceUrl>)
- ...
- *(No Fireflies meetings today)* — if raw/fireflies has no TODAY-dated files and CalendarEvents is empty
```

If CalendarEvents is empty but Fireflies has files for today, show those. If both are empty, write `*(No calendar events found for today.)*`

### 3b — Overdue tasks

Scan all `.md` files under:
- `{{vault_root}}/{{folders.projects}}/`
- `{{vault_root}}/{{folders.areas}}/`

For each file, extract lines matching:
```
^- \[ \] .*📅 (\d{4}-\d{2}-\d{2})
```
or the plain-text Tasks plugin format:
```
^- \[ \] .* due:(\d{4}-\d{2}-\d{2})
```
where the captured date < TODAY.

**Exclude no-nudge practice tasks.** Drop any matching line that contains a `#monk/` tag. Practice priorities in the owner's reflection folder are intentionally quiet carry-over, not "overdue" — the owner opted out of nudges (DEC-027). They surface only in their own gentle practice hub note, never in this morning report.

Collect: file path (relative to vault root), task text, due date. Sort by due date ascending (oldest overdue first). Cap at 10 items to avoid flooding the morning report; if more exist, append a `… and N more` line.

Format:
```markdown
### Overdue tasks

- [ ] <task text> — [<project>](<relative/path.md>) (due <YYYY-MM-DD>)
- ...
- *(No overdue tasks — clear conscience!)*
```

### 3c — Active idea-threads (last 7 days)

Scan the entire vault for `#thread/<slug>` tags in files whose last-modified timestamp (file mtime) is within the last 7 days.

**Write this as a Python script, not bash.** A bash `while read` loop over the candidate list hangs past the scheduler's timeout; the equivalent Python walk finishes in about a second.

**Three guards are mandatory. Without them this section reports garbage — an inflated thread list sourced from files that merely enumerate slugs rather than discuss them.**

**Guard 1 — exclude registry, dashboard, and generated files.** These enumerate thread slugs by definition, so counting them pushes nearly every slug past the ≥2-file threshold and defeats the filter entirely:

- path contains: `/{{folders.raw}}/`, `/{{folders.meta}}/`, `/{{folders.wiki}}/`, `/Dashboards/`, `/Templates/`
- filename is: `AGENTS.md` (vault constitution — lists every graduated slug), `Thread-Review-Queue.md` (`generated_by: atlas-auto-graduate`), `Emerging-Patterns.md`

Note that `{{folders.wiki}}/synthesis/*.md` pages are regenerated during the nightly run. They are generated artifacts, not organic sources — excluding `/{{folders.wiki}}/` is what keeps them from double-counting the slugs they were built from.

**Guard 1b — exclude any file whose frontmatter declares `generated_by:`.** Guard 1's hardcoded path/filename list cannot keep up with new aggregator surfaces. Aggregator files such as weekly reviews carry `generated_by: atlas-weekly`, live in `{{folders.areas}}/Weekly-Reviews/` where Guard 1 does not look, and enumerate slugs in prose like ``- `#thread/portal-rewrite` — last activity …``. Two aggregators quoting the same slugs is enough to clear the ≥2-file bar on its own, even when `atlas-weekly` itself correctly reported zero active threads for the window.

Parse the leading `---` frontmatter block and skip the file if a `generated_by:` key is present. This generalizes Guard 1 and makes it self-maintaining as new Atlas agents add surfaces.

**Guard 2 — strip prior morning reports before matching.** Within each candidate file, drop any `## Atlas morning report` section and any `### Active threads` subsection before extracting tags. Without this the report feeds on itself: yesterday's daily note counts as a "file" for every slug it printed, so any slug reported once is permanently self-sustaining. This guard does real work every run — recent daily notes are always in the 7-day window and must contribute **zero** slugs.

Sanity check: if today's own daily note contributes a slug, Guard 2 is broken.

Second sanity check: if every slug in the final list traces back to the same one or two source files, Guard 1/1b is leaking — those files are almost certainly aggregators. Print the per-slug file list before formatting the section and confirm the sources are genuinely distinct organic notes.

For each surviving file, extract every unique `#thread/<slug>` token. Group by slug; record the most-recent file mtime per slug.

Filter: include only slugs that appear in at least 2 distinct files (active threads, not isolated mentions).

Sort by recency descending.

Format:
```markdown
### Active threads (last 7 days)

- `#thread/portal-rewrite` — last activity 2026-05-20 (3 files)
- `#thread/ledgerline-integration` — last activity 2026-05-19 (2 files)
- *(No thread activity in the last 7 days.)*
```

## Step 4 — Inject the morning report section (idempotent)

The daily note has a section delimiter pattern. Locate the "Atlas morning report" section:

```
## Atlas morning report
```

**If the section does NOT exist in the daily note:**  
Append it before the final `## Scratch` section (or at end of file if no Scratch section):

```markdown
## Atlas morning report

*Generated by `atlas-morning` at <HH:MM> on <TODAY>.*

### Today's calendar

<calendar list>

### Overdue tasks

<overdue list>

### Active threads (last 7 days)

<threads list>
```

**If the section DOES exist (re-run idempotency):**  
Replace everything from `## Atlas morning report` to the next `## ` heading (or end of file) with the freshly generated content. This overwrites the previous morning report while leaving any of the owner's edits to other sections (Today's meetings, Today's tasks, Scratch) untouched.

## Step 5 — Write last-run summary

`{{skills_root}}/atlas-morning/last-run.md`:

```markdown
# atlas-morning — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- daily_note_path: <DAILY_NOTE_PATH>
- daily_note_created: <true | false>
- calendar_events: <N>
- overdue_tasks: <N> (capped at 10)
- active_threads: <N>
- duration_seconds: <float>
```

## Idempotency contract

- **Daily note creation**: creates the file only if it doesn't exist. Never overwrites an existing daily note wholesale.
- **Morning report section**: always overwrites only the `## Atlas morning report` section. Never touches any other section.
- **`{{folders.raw}}/` is read-only**: this skill never writes to `{{folders.raw}}/` (DEC-009).
- **Vault-external writes**: only two files are ever written — the daily note (if new) and `last-run.md`.

Re-running on the same day produces the same morning report (modulo fresh timestamps). Re-running on a new day creates a new daily note for that day.

## Edge cases

- **CalendarEvents table missing**: skip silently; write `*(CalendarEvents not available.)*` in the calendar section.
- **Wispr DB locked**: open in read-only mode (`?mode=ro`); if still locked, fall back to `{{folders.raw}}/wispr/calendar/` files for today.
- **No PARA task files**: write `*(No overdue tasks found.)*`.
- **Daily note template missing**: hard-fail with a clear error rather than writing a blank note. Surface: `"Daily-Note.md template not found at {{vault_root}}/{{folders.meta}}/Templates/Daily-Note.md"`.
- **Year/month directories missing**: create them. `mkdir -p` is safe.

## Invocation

When invoked manually (via Claude Code):
1. Read this SKILL.md.
2. Execute Steps 1–5 sequentially.
3. Report: daily note path, whether note was created or already existed, counts for each list, any errors.

When invoked by the scheduled task:
- The scheduled task fires a Claude Code session with the prompt: `"Run atlas-morning — execute Steps 1–5 in {{skills_root}}/atlas-morning/SKILL.md."` (registered via DEC-008 / `mcp__scheduled-tasks__create_scheduled_task`).
- The skill runs unattended; results appear in the daily note and `last-run.md`.

## Schedule

Cron: `0 8 * * *` (08:00 in `{{timezone}}`, every day).

Registered via `mcp__scheduled-tasks__create_scheduled_task`.

## Relationship to other skills

- `atlas-nightly` — sister scheduled agent. Runs every ingest skill at 22:00. Morning reads what nightly wrote the night before.
- `atlas-emerge` — morning references active threads discovered by emerge; emerge writes `{{folders.meta}}/Dashboards/Emerging-Patterns.md`, which morning could optionally consume in a future iteration.
- `atlas-wispr-ingest` — writes `{{folders.raw}}/wispr/calendar/` files that morning reads for calendar events.
- `atlas-fireflies-ingest` — writes `{{folders.raw}}/fireflies/` files that morning reads for today's meetings.
