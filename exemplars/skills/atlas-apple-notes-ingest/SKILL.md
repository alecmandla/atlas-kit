---
name: atlas-apple-notes-ingest
description: Ingest — mirror Apple Notes into the vault at raw/apple-notes/<folder-slug>/<note-id>.md, one markdown file per note, idempotent via the note's stable coredata id. The agent fetches notes through the Apple Notes MCP (list_notes + get_note_content) and dumps a JSON array; a stdlib-only ingest.py converts the HTML body to markdown, parses the human-formatted dates, redacts secrets, and writes the raw record. Append-only (DEC-009); a note edited after ingest is surfaced, not overwritten. Runs nightly under atlas-nightly. Triggers on "sync apple notes", "ingest apple notes", "/atlas-apple-notes-ingest".
exemplar-of: atlas-apple-notes-ingest
status: active
requires: [mcp/apple-notes, cli/python3]
---

# atlas-apple-notes-ingest

Mirrors Apple Notes into the vault's raw spine. One Apple note becomes one
`{{folders.raw}}/apple-notes/<folder-slug>/<note-id>.md` file. This is the low-friction half of
personal capture: the Apple Notes MCP is AppleScript-backed and needs **no Full Disk
Access** (DEC-025), and every note carries a stable `x-coredata://…/ICNote/p<NNNN>` id
that is the dedup key.

## Mental model

This skill is the same shape as `atlas-gmail-ingest` / `atlas-slack-ingest`: an MCP-driven
ingest split into two halves.

- **The agent (the orchestrating Claude session)** does the MCP fetching — it can reach
  the MCP both interactively and inside the headless `atlas-nightly` session — and writes
  a JSON array to a temp file.
- **`ingest.py` (stdlib-only, DEC-021)** does the deterministic transform: derive the
  `note_id`, parse the human-formatted dates, convert the HTML body to markdown, redact
  secrets, dedup, and write the raw record + `state.json` + `last-run.md`.

The script never calls the MCP itself (it can't read live Notes), and the agent never
writes vault files (it produces the JSON the script consumes).

## Workflow

1. **Enumerate folders + notes (agent, MCP).** Call `list_notes(folder=F, limit=N)` for
   each Notes folder you want to mirror (at minimum the default `Notes`; skip
   `Recently Deleted`). `list_notes` returns `{name, id, creation_date, modification_date}`
   but **not** the folder per item — so loop folders explicitly and stamp `folder` onto
   each item yourself. **Keep `limit` small (≤ 40):** `list_notes(limit=500)` times out
   (AppleScript can't enumerate that many at once). Page incrementally, newest first.
2. **Fetch bodies (agent, MCP).** For each note call `get_note_content(note_name, folder=F)`.
   The body comes back as **HTML**. **Caveat:** `get_note_content` resolves by *name*, and
   titleless notes report an ellipsis-truncated first line as their name (e.g.
   `"Remember to call the plumber about…"`) which won't resolve — tolerate those misses and rely on
   the nightly `--incremental` to keep retrying as notes are renamed/edited. Exhaustive
   by-id fetching would require the NoteStore.sqlite path (a DEC-025 follow-up).
3. **Dump JSON (agent).** Assemble one array of
   `{id, name, folder, creation_date, modification_date, html_body}` objects and write it
   to e.g. `/tmp/atlas-apple-notes-page-1.json`. Split across multiple files if large;
   `--input-json` is repeatable.
4. **Dry-run (script).** `python3 ingest.py --input-json … --dry-run-report /tmp/atlas-apple-notes-ingest-dryrun.md`
   — produces the planned-writes report, writes no vault files. Required before the first
   bulk write (> 5 files, per the vault constitution's dry-run gate).
5. **Execute (script).** `python3 ingest.py --input-json … --execute` — writes one
   `{{folders.raw}}/apple-notes/<folder-slug>/<note-id>.md` per new note and updates `state.json`.
6. **Incremental (nightly).** `--incremental` uses `state.json`'s `last_run_iso` as the
   `modification_date` floor so only new/changed notes are reconsidered.

## Idempotency contract

- Dedup key is the coredata `note_id` (`p<NNNN>`); the script globs
  `{{folders.raw}}/apple-notes/*/<note-id>.md` before writing and skips if present (DEC-009 append-only).
- A note **edited in Apple Notes after ingest** is **not** overwritten — its newer
  `modification_date` is detected against `state.json` and counted as "changed since
  ingest" in `last-run.md`. Corrections use the `corrected_by:` / `corrects:` chain, never
  an in-place rewrite. Use `--force` only for a deliberate correction pass.
- Re-running `--execute` on the same JSON is a no-op: `written=0 already=N`.

## Invocation

```bash
# Dry-run (no writes) — required before first bulk execute
python3 ingest.py --input-json /tmp/atlas-apple-notes-page-1.json \
                  --dry-run-report /tmp/atlas-apple-notes-ingest-dryrun.md

# First execute
python3 ingest.py --input-json /tmp/atlas-apple-notes-page-1.json --execute

# Nightly incremental
python3 ingest.py --input-json /tmp/atlas-apple-notes-latest.json --execute --incremental

# Backfill floor override / correction
python3 ingest.py --input-json … --execute --since 2026-01-01
python3 ingest.py --input-json … --execute --force      # rewrite existing (corrections)
```

## Frontmatter

```yaml
---
date: 2026-06-22                       # from creation_date
type: raw-apple-note
note_id: p2417                         # coredata id — dedup key + filename stem
title: "Call w/ Jordan Vale"
folder: Notes
folder_slug: notes
created_at: 2026-06-22T15:05:14
modified_at: 2026-06-22T15:06:40
note_uri: x-coredata://<store>/ICNote/p2417
ingested_at: 2026-06-23T...Z
---
```

## Edge cases

- **HTML→markdown** (`NotesHTMLConverter`): `<hN>`→`#`, `<ul>/<ol><li>`→`-`/`N.`,
  `<div><br></div>`→blank line, `<b>/<strong>`→`**`, `<i>/<em>`→`*`, `<a href>`→`[text](href)`,
  checklist `<li class="checked|unchecked">`→`- [x]`/`- [ ]`. Entities are decoded by the
  parser (`convert_charrefs=True`).
- **Attachments / tables / scanned docs** (out of scope): `get_note_content`
  returns only the text HTML; embedded images and tables don't round-trip — converted
  best-effort.
- **Date parse failure** (locale variance): falls back to the raw string and an empty
  `date:` rather than crashing.
- **No derivable `note_id`** (malformed uri): skipped and surfaced in the report.

## Relationship to other skills

- Downstream: a categorize step (future) walks `{{folders.raw}}/apple-notes/` and auto-files
  high-confidence notes into PARA with a review queue (DEC-022); `atlas-emerge` surfaces
  recurring patterns across this and every other raw source.
- Sibling: `atlas-voice-memos-ingest` is the other personal-capture source.
- Templates this skill was built from: `atlas-slack-ingest` (`--input-json` JSON
  processing, glob dedup), `atlas-wispr-ingest` (state/incremental), `atlas-distill`
  (`redact()`).
- Runs as a late step of `atlas-nightly`.
