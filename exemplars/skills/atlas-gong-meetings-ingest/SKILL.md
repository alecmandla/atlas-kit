---
name: atlas-gong-meetings-ingest
description: Ingest Gong calls into raw/gong/meetings/. Pulls the calls the owner took part in through Gong's REST API with a stdlib client (headless-capable, owner-filtered before anything touches disk), or reads JSON a session fetched, or transcripts downloaded by hand from a call page into the folders in gong-sources.json. Emits one vault file per call with Gong's brief, key points, next steps, highlights, outline, topics, trackers, call outcome, and internal and external attendees, plus a transcript pointer or the full transcript. Cross-links to Fireflies, Wispr, Gemini, Teams, and Zoom records of the same call. Idempotent, incremental. Triggers on "sync gong", "ingest gong calls", "gong transcripts", "gong briefs", "rebuild raw gong", or any scheduled nightly run.
exemplar-of: atlas-gong-meetings-ingest
status: active
requires: [cli/python3]
---

# atlas-gong-meetings-ingest

You ingest the owner's Gong calls into `{{vault_root}}/{{folders.raw}}/gong/meetings/`. One call becomes one markdown file.

This is the sixth meeting-capture ingest. Gong is a revenue tool: it records customer calls that run on Zoom, Teams, or Google Meet, and adds its own analysis on top (a brief, key points, an outline, next steps, topics, trackers, and a call outcome). The same call is often also captured by another ingest, so this skill runs last among the meeting ingests and links to whatever it finds, most often the Zoom record.

## Access, honestly

Gong does not offer a personal API path:

- **API keys** are created by a Gong technical admin, and a key sees **every call in the company**, not just the owner's.
- **OAuth apps** also need a technical admin, and Gong does not support user-level OAuth; the token is company-wide.
- **Gong's official MCP server** (`ask_account`, `ask_deal`, `generate_brief`) answers questions about accounts and deals. It returns no call list and no transcripts, so it cannot feed this ingest. Community Gong MCP servers need the same admin key.
- **Any user** can download one call's transcript from the call page (More actions → Download transcript) and can generate a follow-up email with an AI summary. There is no bulk self-export.

So the owner either obtains an API key from their admin (the good path) or downloads the transcripts that matter by hand.

## Privacy invariant (DEC-035)

A company-wide key must never mirror colleagues' calls into a personal vault. `gong-sources.json` lists the owner's addresses under `owner_emails`, and a call from the API or from JSON is kept only when one of those addresses is a party to it. The filter runs before anything is written anywhere, including the fetcher's debug output. Filtered calls are counted as `not_owner` and never named in a report. With `owner_emails` empty, the API and JSON paths refuse to run. `--no-owner-filter` exists only for a JSON payload the owner already filtered and is rejected together with `--fetch`.

## Transcript policy

Follows DEC-032 as generalized by DEC-035, recorded per file in `transcript_policy:`:

- `pointer`: the call has Gong AI content and an API key can re-fetch the transcript with `fetch.py transcript <callId>`. The file carries the analysis; the transcript stays in Gong.
- `only-content`: Gong produced no brief, key points, outline, or highlights, so the transcript is the record and is written in full.
- `file`: a hand-downloaded transcript, written in full because nothing can re-fetch it.

- `full`: `--include-transcript` was passed, so a call with AI content also carries its transcript; with that flag `--fetch` requests the transcript for every kept call.

An API call with neither AI content nor a transcript yet is counted as `empty` and not written; the next incremental run inside `lookback_days` picks it up once Gong has processed it.

## Workflow A: the REST client (headless)

One-time setup:

1. Ask the Gong technical admin for an API key (Admin center → Settings → Ecosystem → API → Get API key). The secret is shown once.
2. Export `GONG_ACCESS_KEY` and `GONG_ACCESS_KEY_SECRET`, or write them to `~/.config/atlas/gong-credentials.json` as `access_key` and `access_key_secret` with mode 600. A scheduler does not source the interactive shell, so a headless run needs the file or the scheduler's own environment block.
3. Copy `exemplars/configs/gong-sources.example.json` to `{{skills_root}}/engine/atlas-gong-meetings-ingest/gong-sources.json`, set `owner_emails`, and set `api_base` to the host shown on Gong's API page if it is not `https://api.gong.io`.
4. Verify:

   ```bash
   cd {{skills_root}}/engine/atlas-gong-meetings-ingest
   python3 fetch.py check
   ```

Then:

```bash
# Dry run (default — writes nothing) with a report table
python3 ingest.py --fetch --since 2026-08-01 --dry-run-report /tmp/atlas-gong-meetings-ingest-dryrun.md

# Nightly / incremental
python3 ingest.py --fetch --execute --incremental
```

`--fetch` pages `POST /v2/calls/extensive` over the window (the cursor minus `lookback_days`, because Gong fills in the brief after the call; 30 days on a first run), keeps the owner's calls, and requests `POST /v2/calls/transcript` only for kept calls without AI content. It throttles to under three requests a second and honors `Retry-After`: the 10,000-request daily quota is shared with every other Gong integration in the company.

## Workflow B: JSON from a session or another tool

Anything that already speaks Gong's API, such as a community MCP server with the admin's key or an export an admin ran, can hand its responses to the script. Save `calls/extensive` and `calls/transcript` responses as JSON files:

```bash
python3 ingest.py --input-json /tmp/atlas-gong/extensive-*.json --input-json /tmp/atlas-gong/transcript-*.json --execute
```

The script accepts a whole response (`{"calls": [...]}`, `{"callTranscripts": [...]}`), a single call, a list of calls, or `{"call": ..., "transcript": ...}`, and unwraps a raw `{"result": "..."}` MCP envelope. Transcripts are joined to calls by `callId`; a transcript with no matching call is counted as `orphan_transcript`. The owner filter applies.

## Workflow C: downloaded transcripts (no API key)

List a folder under `export_dirs` in `gong-sources.json`, then download a call's transcript from its Gong page into it. Name the file with the call's date so it can be cross-linked: `2026-09-10 Ledgerline retail kickoff.txt`. A `.md` with the same name is attached as `## Notes`; paste the Gong follow-up email or call spotlight there to keep the analysis the download leaves out.

```bash
python3 ingest.py --execute --incremental
python3 ingest.py --input-dir ~/Downloads --execute     # one-off
```

The export folders are scanned on every run, including `--fetch` runs. **The download's layout is not documented by Gong**, so the parser is lenient: it recognizes a title and date header, then speaker turns written as `Name  0:05`, `0:05 | Name`, `[0:05] Name:`, `Name (0:05):`, or `0:05 Name:`, then plain `Name: text` lines, and otherwise keeps the text as one unattributed block. If a real download parses badly, add it (scrubbed) to the test fixture and extend the parser.

## What the renderer does

1. **Parties.** Each party's `affiliation` splits them into **Internal** and **External** lines. All party emails go to `attendee_emails`; the domains of external parties go to `external_domains`, the routing signal for which client or prospect the call was with.
2. **Next steps.** Gong deprecated `actionItems` in January 2025; next steps now arrive as a highlight section. Highlight sections titled like "Next steps" or "Action items" become checkboxes under `## Next steps`; the other highlight sections render under `## Highlights`.
3. **Outline.** Each section becomes an H3 with its start offset, followed by its items.
4. **Transcript.** Speaker ids resolve to party names; consecutive monologues by one speaker collapse into one paragraph, `**Speaker** (hh:mm:ss) text`.

## Cross-linking

Matches the nearest `{{folders.raw}}/fireflies/*.md` (`date`), and `{{folders.raw}}/wispr/meetings/`, `gemini/meetings/`, `teams/meetings/`, and `zoom/meetings/` records (`timestamp`), whose start is within **10 minutes** of Gong's `started`. Gong's start is the real recording start; the window matches the Gemini and Teams ingests' because their starts lag. Titles are not compared. A match adds `<source>_id` and `<source>_note` frontmatter and a callout line; nothing is suppressed.

## Idempotency

The Gong call id is the key; a hand-downloaded file gets `gong-file-<12 hex>` from its title and date. A call renamed in Gong keeps its file: the existing record is found by id and rewritten under its new name. Path: `{{folders.raw}}/gong/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md`. Each file records `source_hash`; unchanged content is skipped and changed content rewrites the file in place. Gong adds and revises its analysis in the hours after a call, so early rewrites are expected (a deliberate deviation from strict append-only, DEC-009, like the other meeting ingests).

## File contract

```yaml
type: raw-gong-call
gong_call_id: <Gong call id, or gong-file-<12 hex>>
title: "<call title>"
date: YYYY-MM-DD                     # local
timestamp: YYYY-MM-DD HH:MM:SS       # UTC
timestamp_local: "YYYY-MM-DD HH:MM <tz>"
date_source: api | filename | header | file-mtime
input: api | file
source_file: "<file name>"           # file input only
source_hash: <16 hex>                # drives the update check — do not hand-edit
call_url: <Gong call link>
duration: "32m"
direction: Conference | Inbound | Outbound | Unknown
scope: Internal | External | Unknown
system: <conferencing system Gong recorded, e.g. Zoom>
is_private: true | false
attendee_emails: [...]               # sorted
participants: [...]
external_domains: [...]
topics: [...]
trackers: [...]                      # "Name (count)"
call_outcome: "<outcome>"
crm_accounts: [...]                  # empty unless crm_context: true
has_brief: true | false
next_steps: <int>
transcript_available: true | false
transcript_included: true | false
transcript_policy: pointer | only-content | file | full
transcript_segments: <int>
fireflies_id / fireflies_note        # only when matched
wispr_meeting_id / wispr_note        # only when matched
gemini_doc_id / gemini_note          # only when matched
teams_meeting_id / teams_note        # only when matched
zoom_id / zoom_note                  # only when matched
ingested_at: <ISO>
```

Body order: Brief → Key points → Next steps → Highlights → Outline → Notes → Transcript. The brief, key points, topics, and attendees sit at the top because `atlas-synthesize` keyword-matches only the first ~2,500 characters.

## Operational notes

- **Run lock:** `.run.lock`, stale after 3600s.
- **State:** `{{skills_root}}/engine/atlas-gong-meetings-ingest/state.json` holds `cursor_started` (newest call start seen, UTC), `cursor_mtime` (newest downloaded file seen), `last_run_iso`, `last_run_count`. Only `--execute` advances it.
- **Report:** `last-run.md` next to the script after every run; `--dry-run-report <path>` for a per-call table. Neither names a filtered-out call.
- **Failure isolation:** a call or file that fails to parse appends to `errors` and does not abort the run.
- **Unknown fields:** Gong adds response fields without notice; the parser ignores what it does not know.
- **Tests:** `python3 test_ingest.py` and `python3 test_fetch.py` (stdlib, fictional fixtures, no network).

## Relationship to other skills

- `atlas-zoom-meetings-ingest` — runs before this one; Gong's record of a Zoom call links to the Zoom record through `zoom_id`.
- `atlas-fireflies-ingest`, `atlas-wispr-meetings-ingest`, `atlas-gemini-meetings-ingest`, `atlas-teams-meetings-ingest` — the other cross-link targets.
- `atlas-people-extract` — reads the `attendee_emails:` this skill writes.
- `atlas-research` — may fetch a `pointer` record's transcript on demand with `python3 {{skills_root}}/engine/atlas-gong-meetings-ingest/fetch.py transcript <callId>`, within its cap (DEC-032, DEC-035).
- `atlas-emerge` / `atlas-synthesize` — consume `{{folders.raw}}/gong/meetings/` like any other raw source.
