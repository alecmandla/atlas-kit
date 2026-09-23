---
type: raw-readme
status: invariant
---

# `raw/` — the append-only source layer

This folder holds source material ingested from outside the vault, one subfolder per
source, one file per item. Files here are written once by an ingest skill and never
changed. Everything else in the vault can be rebuilt from this folder.

## Subfolders

Each ingest creates its own subfolder on first run. Typical names:

| Subfolder | Source | Written by |
|---|---|---|
| `fireflies/` | meeting summary records | the meetings ingest |
| `gemini/meetings/` | Google Meet notes (summary, decisions, next steps) | the Gemini meetings ingest |
| `teams/meetings/` | Microsoft Teams transcripts | the Teams meetings ingest |
| `gmail/<route>/` | email messages, routed by sender or label | the email ingest |
| `slack/<route>/` | chat messages, routed by channel | the chat ingest |
| `monday/<workspace>/<board>/` | board items with column values | the boards ingest |
| `github/<owner>/<repo>/` | pull requests and issues | the repository ingest |
| `claude-history/<project>/` | one summary per coding session | the session-history ingest |
| `wispr/` | dictation transcripts | the dictation ingest |
| `apple-notes/<folder>/` | notes | the notes ingest |
| `voice-memos/` | recordings, transcript plus a reference to the audio | the voice-memo ingest |
| `distill/` | redacted source records for on-demand captures | the distill skill |

Every file has frontmatter with at least `type`, `source`, `date` (YYYY-MM-DD), and a
stable source id used for deduplication. Re-running an ingest never produces a second
copy of the same item.

## The append-only invariant

**Files in `raw/` are never edited or deleted once written.**

This is the load-bearing property of the whole vault. `wiki/` is regenerated from
`raw/`; the entity pages, the emerging-patterns dashboard, and the synthesis pages are
all derived. If `raw/` is ever mutated in place, re-running the materialization produces
something different from what it produced before, and the recovery property (delete
`wiki/`, re-run, get everything back) silently breaks.

### Corrections

If a source record is wrong (a name mis-transcribed, an item captured with the wrong
date), do not edit the file. Instead:

1. Write a new file with the corrected content.
2. Add `corrected_by: "[[<new-file>]]"` to the original's frontmatter. Adding this one
   forward pointer is the only permitted change to an existing raw file.
3. Add `corrects: "[[<original-file>]]"` to the new file's frontmatter.

The materialization layer follows the chain and prefers the newest link.

### Deletions

Do not. If an item should not have been captured (a meeting that was not yours, a
message that belongs to someone else), add `superseded: true` to its frontmatter and the
derived layers will skip it. Deletion forfeits the rebuild guarantee.

The one exception is a file containing a secret or personal data that should never have
been ingested. Delete it, then record the deletion and the reason in your repo's
`docs/DECISIONS.md` so the gap is explained the next time counts do not match.

## What the ingests do with edits at the source

If the original item changes after ingest (a note edited, a task's status updated), the
ingest does not rewrite the raw file. Depending on the source it either writes a fresh
record with a new id or surfaces the change in its `last-run.md` for you to decide. The
raw file is a record of what was captured on the day it was captured.

## See also

- `AGENTS.md` at the vault root for the full set of rules a session must follow.
- `wiki/index.md` for the materialized layer that reads this folder.
