---
name: atlas-teams-meetings-ingest
description: Ingest Microsoft Teams meeting transcripts into raw/teams/meetings/. Reads exported .vtt and .docx transcript files from the folders listed in teams-sources.json (a download folder, or a synced OneDrive Recordings folder), and emits one markdown file per meeting with the speaker-labeled transcript, an optional notes sidecar such as a pasted Copilot recap, and speakers in frontmatter. Stores the transcript in full because no API can be relied on to re-fetch it. Cross-links to Fireflies, Wispr, and Gemini records of the same call. Idempotent, incremental, no network. Triggers on "sync teams", "ingest teams meetings", "teams transcripts", "rebuild raw teams", or any scheduled nightly run.
exemplar-of: atlas-teams-meetings-ingest
status: active
requires: [cli/python3]
---

# atlas-teams-meetings-ingest

You ingest the owner's Microsoft Teams meeting transcripts into `{{vault_root}}/{{folders.raw}}/teams/meetings/`. One meeting becomes one markdown file.

This is the fourth meeting-capture ingest, beside `atlas-fireflies-ingest`, `atlas-wispr-meetings-ingest`, and `atlas-gemini-meetings-ingest`. An owner whose organization runs on Microsoft 365 may have Teams as their **only** recorder, so this skill stands alone and only cross-links to the others when their raw files are present.

## Why a folder, not an API

Teams stores each transcript as a `.vtt` or `.docx` file in the organizer's OneDrive (usually a `Recordings` folder), or in the team's SharePoint site for channel meetings. Anyone in the meeting can also download it from the meeting's **Recap** tab.

Microsoft Graph can serve transcripts, but it is not a dependable path for a personal pipeline:

- Tenants gate Graph access to transcripts behind an admin control that is **off by default**, so most users cannot turn it on themselves.
- Microsoft's own Teams MCP covers chats and channels, not meeting transcripts. Third-party Graph MCPs exist but need an app registration and delegated consent.

A folder of exported files works in every tenant with nothing installed, which is why this skill has no MCP requirement. Anything that *can* fetch transcripts only has to drop the `.vtt` into a configured folder: Graph returns exactly that format, so a Graph MCP or a Power Automate flow becomes a feeder for this skill, not a replacement for it.

## Transcript policy (an exception to DEC-032)

The Fireflies and Gemini ingests store a summary record and leave the transcript at the source, fetched on demand (DEC-032). That rule presumes an API the query side can call later. Teams has none the owner can rely on, and a raw file holding only a pointer to a OneDrive path is close to useless to `atlas-emerge` and `atlas-synthesize`. So this skill writes the **full transcript** into the raw file, the same exception `atlas-wispr-meetings-ingest` makes for the same reason.

## Getting transcripts into the folder

Pick one and record the folder in `{{skills_root}}/engine/atlas-teams-meetings-ingest/teams-sources.json` (copied from `exemplars/configs/teams-sources.example.json`):

| Feeder | Setup | Freshness |
| --- | --- | --- |
| **Synced OneDrive folder** | Sync the organizer's `Recordings` folder with the OneDrive desktop client and list the local path | automatic for meetings the owner organized |
| **Manual download** | Recap tab → Transcript → Download (`.vtt` or `.docx`) into one folder | whenever the owner downloads |
| **Graph MCP or Power Automate** | a flow that writes each new transcript's `.vtt` into the folder | automatic, if the tenant allows it |

**Name files so the start time survives.** Teams exports carry no reliable start time inside a `.vtt`. The script reads it from the file name, and recognizes two shapes:

- OneDrive's recording stamp: `Pinecrest Lodge weekly sync-20260910_145700-Meeting Recording.vtt`
- an ISO date with an optional time: `2026-09-10 1457 Portal roadmap review.vtt`

A `.docx` carries the date and duration in its header, so it can keep any name. A file with no date anywhere falls back to the file's own date, is written with `date_source: file-mtime`, and is never cross-linked. Rename it to fix.

**Notes sidecar.** A `.md` or `.txt` file with the same name as a transcript is attached as that meeting's `## Notes`. This is where a Copilot or Teams Premium recap goes: copy it out of the Recap tab and paste it into the sidecar.

## Workflow

```bash
cd {{skills_root}}/engine/atlas-teams-meetings-ingest

# Dry run (default — writes nothing) with a report table
python3 ingest.py --dry-run-report /tmp/atlas-teams-meetings-ingest-dryrun.md

# Write
python3 ingest.py --execute

# Nightly / incremental (skips files not modified since state.json cursor_mtime)
python3 ingest.py --execute --incremental

# One-off: a folder or single files, bypassing teams-sources.json
python3 ingest.py --input-dir ~/Downloads --execute
python3 ingest.py --input "<file>.vtt" --execute
```

Scheduled runs use `--execute --incremental`. Run this **after** the other meeting ingests so the cross-links resolve against fresh files. Report the printed count line and anything under `errors`.

## What the parser does

1. **`.vtt`.** Each cue's start becomes the segment offset. The speaker comes from the `<v Name>` voice tag; a cue without one falls back to a leading `Name: ` and then to `Unknown speaker`. Other inline tags are stripped. Duration is the last cue's end.
2. **`.docx`.** Paragraphs are read from `word/document.xml`. The header block gives the title, a `September 10, 2026, 2:57PM` date line, and a `1h 15m 14s` duration line. A body line of the form `Speaker<tab or two+ spaces>m:ss` starts a segment, and the lines after it are what that speaker said. A single-space form (`Marcus Hale 1:02:10`) is accepted only when the name is one to five capitalized words, so a sentence ending in a clock time stays text. `started transcription` / `stopped transcription` events are dropped.
3. **Start time.** From the file name first, then the `.docx` header, then the file date. The start is local to `{{timezone}}` and converted to UTC for `timestamp`.
4. **Same meeting, two formats.** A `.vtt` and a `.docx` for one meeting resolve to the same id; the one with more segments is kept and the other is counted under `dup_format`.
5. **Rendering.** Consecutive segments from one speaker collapse into one paragraph, `**Speaker** (hh:mm:ss) text`.

## Cross-linking

Matches the nearest `{{folders.raw}}/fireflies/*.md` (`date`), `{{folders.raw}}/wispr/meetings/*.md` (`timestamp`), and `{{folders.raw}}/gemini/meetings/*.md` (`timestamp`) whose start is within **10 minutes** of the Teams start. A Teams start reflects when transcription began, so it lags the call the way Gemini's does; the window matches the Gemini ingest's for the same reason. Titles are not compared. Undated files are never linked. A match adds `<source>_id` and `<source>_note` frontmatter plus a callout line; nothing is suppressed.

## Idempotency

There is no stable source id, so the meeting id is a hash of the title slug plus the local start (or the file name when undated). The path derives from it: `{{folders.raw}}/teams/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md`.

Each file records `source_hash`, a hash of the parsed segments and notes. On every run:

- file absent → **create**
- file present, same hash → **skip**
- file present, different hash → **rewrite in place**

This deviates from strict append-only (DEC-009) on purpose: a Teams transcript can be edited in Teams after the meeting, and a re-download should replace the stale copy rather than sit beside it. Renaming a transcript so its title or start changes produces a new id; delete the old raw file by hand if that happens.

## File contract

```yaml
type: raw-teams-meeting
teams_meeting_id: teams-<12 hex>     # hash of title + local start
title: "<meeting title>"
date: YYYY-MM-DD                     # local
timestamp: YYYY-MM-DD HH:MM:SS       # UTC; empty when only a date is known
date_source: filename | docx-header | file-mtime
source_file: "<exported file name>"
source_hash: <16 hex>                # drives the update check — do not hand-edit
duration: "1h 15m"
participants: [...]                  # speakers, first-appearance order
transcript_segments: <int>
has_notes: true | false
fireflies_id / fireflies_note        # only when matched
wispr_meeting_id / wispr_note        # only when matched
gemini_doc_id / gemini_note          # only when matched
ingested_at: <ISO>
```

Body order: `## Notes` (when a sidecar exists) → `## Transcript`. Teams exports carry display names only, never email addresses, so `atlas-people-extract` has nothing to key on here; a cross-linked Fireflies or Gemini record supplies the emails for the same call.

## Operational notes

- **Run lock:** `.run.lock`, stale after 3600s, same pattern as the other ingests.
- **State:** `{{skills_root}}/engine/atlas-teams-meetings-ingest/state.json` holds `cursor_mtime` (newest file or sidecar mtime seen, UTC), `last_run_iso`, `last_run_count`. Only `--execute` advances it.
- **Report:** `last-run.md` next to the script after every run; `--dry-run-report <path>` for a per-file table.
- **Failure isolation:** a file that fails to parse appends to `errors` and does not abort the run. A missing export folder is an error line, not a crash.
- **Temp files:** names starting with `~$` (Word lock files) or `.` are ignored.
- **Tests:** `python3 test_ingest.py` (stdlib, fictional fixture, builds a `.docx` in memory). Run it after touching the parser; Microsoft changes the export layout without notice.

## Relationship to other skills

- `atlas-fireflies-ingest`, `atlas-wispr-meetings-ingest`, `atlas-gemini-meetings-ingest` — sibling meeting ingests and cross-link targets. Under `atlas-nightly` this skill runs after all three.
- `atlas-emerge` / `atlas-synthesize` — consume `{{folders.raw}}/teams/meetings/` like any other raw source.
