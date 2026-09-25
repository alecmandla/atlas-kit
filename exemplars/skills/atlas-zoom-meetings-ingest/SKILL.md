---
name: atlas-zoom-meetings-ingest
description: Ingest Zoom meetings into raw/zoom/meetings/. Takes AI Companion meeting summaries and cloud-recording transcripts from any of three feeders (the official Zoom for Claude connector in a session, a stdlib REST client for headless runs, or .vtt transcripts downloaded by hand into the folders in zoom-sources.json) and emits one vault file per meeting with the summary, next steps, attendees, and either a transcript pointer or the full transcript. Cross-links to Fireflies, Wispr, Gemini, and Teams records of the same call. Idempotent, incremental, rewrites a file when the source changes. Triggers on "sync zoom", "ingest zoom meetings", "zoom transcripts", "zoom summaries", "rebuild raw zoom", or any scheduled nightly run.
exemplar-of: atlas-zoom-meetings-ingest
status: active
requires: [cli/python3]
---

# atlas-zoom-meetings-ingest

You ingest the owner's Zoom meetings into `{{vault_root}}/{{folders.raw}}/zoom/meetings/`. One meeting becomes one markdown file.

This is the fifth meeting-capture ingest, beside `atlas-fireflies-ingest`, `atlas-wispr-meetings-ingest`, `atlas-gemini-meetings-ingest`, and `atlas-teams-meetings-ingest`. An owner whose organization runs on Zoom may have Zoom's own AI Companion as their **only** recorder, so this skill stands alone and only cross-links to the others when their raw files are present.

## What Zoom produces, and where it lives

| Artifact | Needs | Where |
| --- | --- | --- |
| **AI Companion meeting summary** | Pro plan or higher, "Meeting Summary with AI Companion" on for the host | Zoom's servers; emailed to the host; REST `GET /meetings/{uuid}/meeting_summary`; the connector's `get_meeting_assets` / `get_recording_resource` |
| **Cloud recording audio transcript** (`.vtt`) | cloud recording plus the "Audio transcript" setting | Zoom's servers; the web portal's Recordings page; REST `recording_files` with `file_type: TRANSCRIPT` |
| **Local recording** | the desktop client | `~/Documents/Zoom/YYYY-MM-DD HH.MM.SS <topic>/`: video, audio, chat. No transcript. |

Saved closed captions (`meeting_saved_closed_caption.txt`) are legacy: Zoom stopped letting users save captions on 2026-05-18. The script ignores them.

The summary's current field is `summary_content`, one Markdown document. The older `summary_overview`, `summary_details`, and `next_steps` fields are deprecated but still returned by some accounts; the script reads either.

## Three feeders, one script

Pick whichever the owner's plan and permissions allow. They can be combined; a meeting that arrives by two feeders becomes one file.

| Feeder | Needs | Runs headless | Notes |
| --- | --- | --- | --- |
| **Zoom for Claude connector** | the connector added in Claude, a licensed plan, the owner hosts the meetings | no, session only (DEC-031) | the easiest start; summaries and transcripts |
| **REST client** (`fetch.py`) | a Zoom Marketplace app. Creating one needs the "Zoom for developers" role permission, which a non-admin must ask an admin for | yes (DEC-035) | the only fully unattended path |
| **Downloaded `.vtt` files** | nothing beyond the portal | yes | transcripts only, unless the owner pastes the summary into a notes sidecar |

Credentials for the REST client never go in a file the kickoff generates. Set `ZOOM_CLIENT_ID` and `ZOOM_CLIENT_SECRET` (and `ZOOM_ACCOUNT_ID` for a server-to-server app) in the environment, or in `~/.config/atlas/zoom-credentials.json` with mode 600. A scheduler does not source the interactive shell, so a headless run needs the file or the scheduler's own environment block.

## Transcript policy

Follows DEC-032 as generalized by DEC-035, recorded per file in `transcript_policy:`:

- `pointer`: the meeting has a summary and the transcript can be re-fetched (connector or `fetch.py transcript <uuid>`). The file carries the summary; the transcript stays at Zoom.
- `only-content`: no summary exists (AI Companion was off), so the transcript is the record and is written in full.
- `file`: the transcript came from a downloaded file that no API can re-fetch, so it is written in full, the same exception the Teams ingest makes.

- `full`: `--include-transcript` was passed, so a meeting with a summary also carries its transcript.

A downloaded transcript merged into a meeting that has a summary follows the summary's policy: it stays a pointer unless `--include-transcript` is passed.

## Workflow A: the Zoom for Claude connector (session)

### 0. Print the window

```bash
cd {{skills_root}}/engine/atlas-zoom-meetings-ingest
python3 ingest.py --print-window --incremental
```

Prints `from=YYYY-MM-DD to=YYYY-MM-DD`: the state cursor minus `lookback_days` (summaries and transcripts land after the call), or 30 days back on a first run. `--since 2026-06-01` overrides it for a backfill.

### 1. List and fetch

With the connector loaded, list the owner's meetings in the window with `search_meetings` or `recordings_list`. For each meeting, call `get_meeting_assets` (and `get_recording_resource` if the transcript comes back separately). Write one JSON file per meeting to a scratch directory:

```json
{
  "uuid": "<meeting UUID>",
  "id": 81234567890,
  "topic": "Pinecrest Lodge weekly sync",
  "start_time": "2026-09-10T18:57:00Z",
  "duration": 75,
  "host_email": "jordan.vale@harborlane.example",
  "participants": [{"name": "Priya Okafor", "email": "priya@pinecrestlodge.example"}],
  "summary": {"summary_content": "## Key takeaways\n- ...", "summary_doc_url": "..."},
  "transcript_vtt": "WEBVTT\n\n1\n00:00:04.230 --> ...",
  "recording_url": "<share link>"
}
```

Tool results vary by connector version, so map fields by meaning; the script accepts common aliases (`meeting_uuid`, `meeting_topic`, `meeting_start_time`, `meeting_host_email`, flattened summary fields, `transcript`, `share_url`) and unwraps a raw `{"result": "..."}` envelope. Keep only meetings the owner attended; the connector sees what the owner can see, which on a shared account can be more.

### 2. Dry run, then execute

```bash
python3 ingest.py --input-json /tmp/atlas-zoom/*.json --dry-run-report /tmp/atlas-zoom-meetings-ingest-dryrun.md
python3 ingest.py --input-json /tmp/atlas-zoom/*.json --execute --incremental
```

## Workflow B: the REST client (headless)

One-time setup, attended:

1. In the Zoom App Marketplace, create a **General app** (user-managed) with the scopes `meeting:read:list_summaries`, `meeting:read:summary`, `cloud_recording:read:list_user_recordings`, `cloud_recording:read:list_recording_files`, and `cloud_recording:read:meeting_transcript`. Set its redirect URL to the `api.redirect_uri` in `zoom-sources.json`.
2. Export the client id and secret (see above).
3. Authorize once. `fetch.py auth` prints a URL; open it, approve, copy the `code=` value out of the browser's address bar, and exchange it:

   ```bash
   python3 fetch.py auth
   python3 fetch.py auth --code <code>
   python3 fetch.py check
   ```

   The refresh token lands in `~/.config/atlas/zoom-token.json` (mode 600). Zoom rotates it on every refresh and expires it after 90 days unused; the script writes each new one back before using it, so a nightly run keeps it alive. If `check` fails after a long gap, run `auth` again.

An admin with an account-level **server-to-server** app can set `api.auth` to `server-to-server` instead and skip step 3.

Then, attended or scheduled:

```bash
python3 ingest.py --fetch --execute --incremental
```

`--fetch` lists AI summaries and cloud recordings in the window, downloads each transcript, and ingests them exactly as Workflow A would. A summary the account will not release over the API (error 2305, "only share meeting summaries by email") is a warning; the meeting is ingested from its transcript.

## Workflow C: downloaded transcripts

List the folders in `{{skills_root}}/engine/atlas-zoom-meetings-ingest/zoom-sources.json` (copied from `exemplars/configs/zoom-sources.example.json`), then download each transcript from the portal's Recordings page into one of them.

```bash
python3 ingest.py --dry-run-report /tmp/atlas-zoom-meetings-ingest-dryrun.md
python3 ingest.py --execute --incremental

# One-off: a folder or a file, bypassing zoom-sources.json
python3 ingest.py --input-dir ~/Downloads --execute
```

The start time comes from the file name. The portal's `GMT20260910-185700_Recording.transcript.vtt` is a UTC stamp. A file inside a local-recording folder named `2026-09-10 14.57.00 Pinecrest Lodge weekly sync` takes the folder's local time and topic. An ISO name (`2026-09-10 1457 Portal roadmap review.vtt`) is read as local time. A file with no date anywhere falls back to the file's own date, is written with `date_source: file-mtime`, and is never cross-linked; rename it to fix.

A `.md` or `.txt` with the same name as a transcript is attached as `## Notes`. Paste the AI Companion summary email there when the API path is not available.

The export folders are scanned on every run, including `--fetch` and `--input-json` runs, so the nightly command picks up downloads too.

## What the parser does

1. **VTT.** Zoom numbers each cue and writes the speaker as `Name: text` inside it, without `<v>` tags; a cue with no name is `Unknown speaker`. Cues are three to five seconds long, and consecutive cues from one speaker collapse into one paragraph, `**Speaker** (hh:mm:ss) text`.
2. **Two formats, one meeting.** A `.transcript.vtt` and a `.cc.vtt` for the same meeting resolve to one id; the one with more segments is kept and the other is counted under `dup_format`.
3. **Summary.** `summary_content` goes under `## Summary` with its own headings demoted to H3, so the file keeps one H2 spine. With only the deprecated fields, the overview becomes the first paragraph and each detail an H3. The `next_steps` count comes from the `next_steps` list, or from the bullets under an "Action items" or "Next steps" heading.
4. **Merge.** A downloaded transcript whose start is within two minutes of a meeting from the connector or the API is merged into that meeting's record rather than written twice. The same meeting arriving from both `--input-json` and `--fetch` is merged by UUID. Both count under `merged`.
5. **Renames.** An existing record is found by the 8-hex id at the end of its file name, so a topic renamed in Zoom or a corrected date rewrites the same file instead of creating a second one.

## Cross-linking

Matches the nearest `{{folders.raw}}/fireflies/*.md` (`date`), `{{folders.raw}}/wispr/meetings/*.md`, `{{folders.raw}}/gemini/meetings/*.md`, and `{{folders.raw}}/teams/meetings/*.md` (`timestamp`) whose start is within **10 minutes** of the Zoom start. Zoom's API start is the real call start, but the Gemini and Teams starts it is compared against lag the call by up to about nine minutes, so the window matches theirs. Titles are not compared. Undated files are never linked. A match adds `<source>_id` and `<source>_note` frontmatter and a callout line; nothing is suppressed.

## Idempotency

The meeting UUID is the id when known; a file-only meeting gets `zoom-<12 hex>` from its title and start. The path derives from it: `{{folders.raw}}/zoom/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md`. Each file records `source_hash`, a hash of the parsed summary, next steps, transcript, and notes. On every run:

- file absent → **create**
- file present, same hash → **skip**
- file present, different hash → **rewrite in place**

This deviates from strict append-only (DEC-009) on purpose: the host can edit an AI Companion summary after the meeting, and a transcript often arrives a day after the summary.

## File contract

```yaml
type: raw-zoom-meeting
zoom_id: <meeting UUID, or zoom-<12 hex>>   # stable PK; the Gong ingest links on this
zoom_meeting_number: <numeric meeting id>
title: "<meeting topic>"
date: YYYY-MM-DD                     # local
timestamp: YYYY-MM-DD HH:MM:SS       # UTC; empty when only a date is known
timestamp_local: "YYYY-MM-DD HH:MM <tz>"
date_source: api | filename-gmt | folder-name | filename | file-mtime
input: api | file
source_file: "<file name>"           # file input, or a file merged into an API record
source_hash: <16 hex>                # drives the update check — do not hand-edit
duration: "1h 15m"
host_email: <email>
attendee_emails: [...]               # sorted
participants: [...]
has_summary: true | false
next_steps: <int>
summary_doc_url: <Zoom Docs link>
recording_url: <share link>
transcript_available: true | false
transcript_included: true | false
transcript_policy: pointer | only-content | file | full
transcript_segments: <int>
fireflies_id / fireflies_note        # only when matched
wispr_meeting_id / wispr_note        # only when matched
gemini_doc_id / gemini_note          # only when matched
teams_meeting_id / teams_note        # only when matched
ingested_at: <ISO>
```

Body order: Summary → Next steps → Notes → Transcript. Attendees and the summary sit at the top because `atlas-synthesize` keyword-matches only the first ~2,500 characters. `attendee_emails` feeds `atlas-people-extract`; transcript-only records carry speaker names but no emails.

## Operational notes

- **Run lock:** `.run.lock`, stale after 3600s, same pattern as the other ingests.
- **State:** `{{skills_root}}/engine/atlas-zoom-meetings-ingest/state.json` holds `cursor_start` (newest meeting start seen, UTC), `cursor_mtime` (newest transcript file seen), `last_run_iso`, `last_run_count`. Only `--execute` advances it.
- **Report:** `last-run.md` next to the script after every run; `--dry-run-report <path>` for a per-meeting table.
- **Failure isolation:** a meeting or file that fails to parse appends to `errors` and does not abort the run. A missing export folder is an error line, not a crash.
- **Rate limits:** the REST client pauses between calls and honors `Retry-After` on a 429.
- **Tests:** `python3 test_ingest.py` and `python3 test_fetch.py` (stdlib, fictional fixtures, no network). Run them after touching the parser; Zoom changes its payloads without notice.

## Relationship to other skills

- `atlas-fireflies-ingest`, `atlas-wispr-meetings-ingest`, `atlas-gemini-meetings-ingest`, `atlas-teams-meetings-ingest` — sibling meeting ingests and cross-link targets. Under `atlas-nightly` this skill runs after all four.
- `atlas-gong-meetings-ingest` — runs after this one and links Gong's record of a Zoom call to this file through `zoom_id`.
- `atlas-people-extract` — reads the `attendee_emails:` this skill writes.
- `atlas-research` — may fetch a `pointer` record's transcript on demand with `python3 {{skills_root}}/engine/atlas-zoom-meetings-ingest/fetch.py transcript <uuid>` or the connector, within its cap (DEC-032, DEC-035).
- `atlas-emerge` / `atlas-synthesize` — consume `{{folders.raw}}/zoom/meetings/` like any other raw source.
