---
name: atlas-gemini-meetings-ingest
description: Ingest Google Meet's "Notes by Gemini" documents into raw/gemini/meetings/. Lists the Gemini notes docs in Google Drive via the Drive MCP, fetches each as markdown, and emits one vault file per meeting carrying the summary, decisions, next steps, details, and quick notes, with attendees in frontmatter. The transcript stays in the Google Doc and is fetched on demand (DEC-032). Cross-links to Fireflies and Wispr when they captured the same call. Idempotent, incremental, rewrites a file when the doc changes. Triggers on "sync gemini", "ingest gemini meetings", "gemini notes", "google meet notes", "rebuild raw gemini", or any scheduled nightly run.
exemplar-of: atlas-gemini-meetings-ingest
status: active
requires: [mcp/google-drive, cli/python3]
---

# atlas-gemini-meetings-ingest

You ingest Google Meet's **Gemini "Take notes for me"** output into `{{vault_root}}/{{folders.raw}}/gemini/meetings/`. One Gemini notes doc becomes one markdown file.

This is the third meeting-capture ingest, beside `atlas-fireflies-ingest` (bot joins the call, summary record + routed meeting note) and `atlas-wispr-meetings-ingest` (native notetaker, the owner's live notes + transcript). An owner whose team lives in Google Workspace may have Gemini as their **only** meeting recorder, so this skill stands alone: it never assumes Fireflies or Wispr exist, and only cross-links to them when their raw files are present.

## Why this exists

Gemini writes its notes into a Google Doc in the organizer's Drive, never to a local database or file. The doc carries content the other two tools do not produce in the same form: a **Decisions** block split into *Aligned* and *Shelved*, **Next steps** as owner-tagged checkboxes, and a compressed **Quick notes** tab. Attendees are listed with emails, which is what `atlas-people-extract` and the Fireflies routing tiers key on.

## Mental model

| Source | Provides |
| --- | --- |
| Drive file named `<Title> - YYYY/MM/DD HH:MM TZ - Notes by Gemini` | stable `id`, `modifiedTime` (drives the update check), `createdTime`, link |
| Doc tab `Full notes` (or the whole doc when single-tab) | date, title, `Invited …` roster with emails, `### Summary`, `### Decisions`, `### Next steps`, `### Details` |
| Doc tab `Quick notes` (newer docs) | one-line blurb + topic bullets |
| Doc tab `Transcript` (newer docs) | `### HH:MM:SS` chunks of `**Speaker: **text`, closing `Transcription ended after HH:MM:SS` |

Two doc shapes exist. Older docs are single-tab (no H1 headings, no transcript). Newer docs carry three H1 tabs. `ingest.py` handles both; the tab split is on H1 lines and a doc without any H1 is treated as one `Full notes` tab.

Gemini also emails a "Notes: …" message to attendees from its own notification sender. That email is **not** the source: it carries only the quick notes and no stable id. Ignore it; `atlas-gmail-ingest` will mirror it like any other message.

## Runtime model

Like `atlas-gmail-ingest`, this depends on **MCP rather than a local CLI**. The script itself never touches the network (DEC-021: stdlib only, no Google client library).

| Path | When | How |
| --- | --- | --- |
| **Interactive (Claude session)** | nightly chain, on-demand sync, backfill | You list docs with `search_drive_files`, fetch each with `get_doc_as_markdown`, write JSON, run `ingest.py --input-json` |
| **Markdown exports** | no Google Drive MCP available | Download each doc as Markdown from Google Docs (File → Download → Markdown) into a folder, run `ingest.py --input-dir <folder>` |

## Workflow (interactive)

### 0. Print the Drive query

```bash
cd {{skills_root}}/engine/atlas-gemini-meetings-ingest
python3 ingest.py --print-query --incremental
```

This prints the exact Drive search string, with a `modifiedTime > <cursor>` clause when `state.json` has a cursor. On a first run or a backfill, drop `--incremental` (or pass `--since 2026-01-01`) to get the unbounded query.

### 1. List the docs

With the Google Drive MCP loaded, using whichever Google account owns the meetings:

```text
search_drive_files(query="<printed query>", file_type="document",
                   order_by="modifiedTime desc", page_size=50)
```

Page with `page_token` until a response has no `nextPageToken`. Each line gives `Name`, `ID`, `Created`, `Modified`, and `Link`. Keep only names ending in `- Notes by Gemini`. Gemini sometimes writes a separate `<Title> - Transcript` doc; skip those, the notes doc already links to its transcript.

### 2. Fetch each doc as markdown

```text
get_doc_as_markdown(document_id="<ID>", include_comments=false)
```

Comments are excluded on purpose: attendee comment threads are not the meeting record. Write one JSON file per doc to a scratch directory:

```json
{
  "id": "<ID>",
  "name": "Pinecrest Lodge weekly sync - 2026/09/10 14:57 EDT - Notes by Gemini",
  "created_time": "2026-09-10T20:17:01.978Z",
  "modified_time": "2026-09-10T20:17:56.874Z",
  "link": "https://docs.google.com/document/d/<ID>/edit",
  "markdown": "<the tool's result string>"
}
```

`markdown` may be the tool's raw `{"result": "..."}` envelope; the script unwraps it. A file may also hold a list of these objects, or `{"docs": [...]}`. Key aliases (`file_id`, `modifiedTime`, `webViewLink`, `content`) are accepted.

### 3. Dry run, then execute

```bash
# Dry run (default — writes nothing) with a report table
python3 ingest.py --input-json /tmp/atlas-gemini/*.json --dry-run-report /tmp/atlas-gemini-meetings-ingest-dryrun.md

# Write
python3 ingest.py --input-json /tmp/atlas-gemini/*.json --execute

# Nightly / incremental (skips docs whose modifiedTime is at or before state.json cursor_ts)
python3 ingest.py --input-json /tmp/atlas-gemini/*.json --execute --incremental
```

Scheduled runs use `--execute --incremental`. Run this **after** `atlas-fireflies-ingest` and `atlas-wispr-meetings-ingest` so the cross-links resolve against fresh files.

`--input-json` is repeatable and accepts shell globs. Report the printed count line and anything under `errors`.

## What the parser does

1. **Doc name → time.** `Title - YYYY/MM/DD HH:MM TZ - Notes by Gemini` gives the title, the local start, and a timezone abbreviation. Common abbreviations map to fixed offsets; anything else resolves through the configured IANA timezone (`{{timezone}}` in the config layer, stdlib `zoneinfo`). The UTC result lands in `timestamp`, the original in `timestamp_local`. A title that itself contains ` - ` is fine; the match is anchored on the date.
2. **Header block.** The `Jul 10, 2026` line (fallback date when the name lacks one), the `## Title` line, the `Invited [Name](mailto:…)` roster, `Attachments` (calendar event + agenda docs), and `Meeting records [Transcript](…)`.
3. **Sections.** `### Summary`, `### Decisions`, `### Next steps`, `### Details`. Gemini nests *Aligned* / *Shelved* under Decisions as H2; they are demoted to H3 so the vault file has one H2 spine. Vertical tabs (`\x0b`), which Gemini uses as soft line breaks inside the Summary, become newlines. Unknown `###` sections are kept under their own H2, never dropped.
4. **Boilerplate stripped.** "You should review Gemini's notes…", the quality survey, "We've updated the Decisions section…", "Want to see more?", and the transcript disclaimer. Nothing else is removed.
5. **Quick notes.** Topics become `### Topic` under `## Quick notes`. Its `Next steps` topic is dropped when Full notes already carries the same list.
6. **Transcript.** Parsed to count segments and derive `duration` from `Transcription ended after`. Written to disk **only** with `--include-transcript`; by default the file ends with a pointer block naming the doc id and the fetch call, mirroring the Fireflies summary record (DEC-032).

## Cross-linking

Matches the nearest `{{folders.raw}}/fireflies/*.md` (`date` + `meeting_id` + `meeting_note`) and the nearest `{{folders.raw}}/wispr/meetings/*.md` (`timestamp` + `wispr_meeting_id`) whose start is within **10 minutes** of the Gemini start. All three store UTC.

The window is wider than the Wispr ingest's 5 minutes because Gemini's timestamp is when someone clicked *Take notes*, which lags the call start. In practice that lag runs up to about 5 minutes against a bot recorder like Fireflies and up to about 9 against a native notetaker like Wispr, while the nearest false positive on a dense calendar sits past 11 minutes. Ten minutes covers every genuine pair with a small margin. If your calendar is packed with back-to-back calls, verify the first batch by hand before trusting the links. Titles are deliberately not compared: each tool titles the same call differently ("Pinecrest Lodge weekly sync" in Fireflies can be "Onboarding checklist review" in Wispr).

A match adds `fireflies_id` / `fireflies_note` and `wispr_meeting_id` / `wispr_note` frontmatter plus a callout line. Nothing is suppressed or deduplicated; a call captured by three tools yields three raw files that point at each other.

## Idempotency

Same deliberate deviation from strict append-only (DEC-009) as `atlas-wispr-meetings-ingest`: a Gemini doc is **mutable** after the meeting. Gemini rewrites the Decisions block when someone gives feedback, and attendees edit the doc. So each file records `source_modified_at` (Drive's `modifiedTime`, UTC) and on every run:

- file absent → **create**
- file present, `modifiedTime` unchanged → **skip**
- file present, `modifiedTime` newer → **rewrite in place**

The path is derived from the Drive file id, so an update overwrites rather than duplicating. `--incremental` additionally skips docs whose `modifiedTime` is at or before `cursor_ts`, so a nightly run only re-parses what changed.

## File contract

Path: `{{folders.raw}}/gemini/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md`. Body order: Summary → Decisions → Next steps → Details → Quick notes → Attachments → Transcript (pointer or text). Attendees and summary sit at the top because `atlas-synthesize` keyword-matches only the first ~2,500 characters.

```yaml
type: raw-gemini-meeting
gemini_doc_id: <Drive file id>       # stable PK
title: "<meeting title>"
date: YYYY-MM-DD                     # local date from the doc name
timestamp: YYYY-MM-DD HH:MM:SS       # UTC; empty when the name carried no time
timestamp_local: "YYYY-MM-DD HH:MM EDT"
source_modified_at: ...              # drives the update check — do not hand-edit
doc_url: https://docs.google.com/document/d/<id>/edit
duration: "1h 15m"                   # from the transcript tab; empty without one
attendee_emails: [...]               # sorted
participants: [...]                  # display names, roster order
has_quick_notes: true | false
has_decisions: true | false
next_steps: <int>
transcript_available: true | false
transcript_included: true | false    # only with --include-transcript
transcript_segments: <int>
fireflies_id / fireflies_note        # only when matched
wispr_meeting_id / wispr_note        # only when matched
ingested_at: <ISO>
```

`has_decisions: true` and `next_steps > 0` are the fields worth querying: they isolate the meetings where Gemini recorded a commitment.

## Operational notes

- **Run lock:** `.run.lock`, stale after 3600s, same pattern as the other ingests.
- **State:** `{{skills_root}}/engine/atlas-gemini-meetings-ingest/state.json` holds `cursor_ts` (max `modifiedTime` seen, UTC, `YYYY-MM-DD HH:MM:SS`), `last_run_iso`, `last_run_count`. Only `--execute` advances it.
- **Report:** `last-run.md` next to the script after every run; `--dry-run-report <path>` for a per-doc table.
- **Failure isolation:** a doc that fails to parse appends to `errors` and does not abort the run.
- **Non-Gemini docs** in the input (an agenda doc that matched the search) are counted under `not_gemini` and skipped, never written.
- **No routed meeting note.** Unlike Fireflies, this writes only the raw record. If a Gemini meeting also ran through Fireflies, the routed note already exists and is linked from `fireflies_note`.
- **Tests:** `python3 test_ingest.py` (stdlib, fictional fixture). Run it after touching the parser; Gemini changes its doc layout without notice, and the fixture is the record of the shapes seen so far.
- **Scale:** growth is a few docs per week; a full pass is seconds and the MCP fetches dominate.

## Relationship to other skills

- `atlas-fireflies-ingest` — writes the `{{folders.raw}}/fireflies/` records this skill cross-links against.
- `atlas-wispr-meetings-ingest` — sibling native-notetaker ingest; its `{{folders.raw}}/wispr/meetings/` records are the second cross-link target. Under `atlas-nightly` this skill runs after both so the links resolve against fresh files.
- `atlas-gmail-ingest` — mirrors Gemini's notification emails as ordinary messages; no special handling.
- `atlas-people-extract` — reads the `attendee_emails:` this skill writes.
- `atlas-emerge` / `atlas-synthesize` — consume `{{folders.raw}}/gemini/meetings/` like any other raw source.
