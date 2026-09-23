---
name: atlas-transcript-extract
description: "RETIRED (DEC-032) — do not run. Its purpose (move inline transcripts into raw/fireflies/) inverts under the transcript policy: full transcripts never live on disk; the vault keeps summaries and the query side fetches transcripts from the Fireflies API on demand. Its corpus is empty (every meeting note is already slim). Folder and tested script kept for history only."
exemplar-of: atlas-transcript-extract
status: retired
requires: [cli/python3]
---

# atlas-transcript-extract

> **RETIRED — DEC-032.** Full transcripts never live on
> disk anymore: `{{folders.raw}}/fireflies/<id>.md` is a summary record written by
> `atlas-fireflies-ingest`, and `atlas-research` fetches full transcripts from the
> Fireflies API when the summary isn't enough. The migration this skill performed is
> complete — there is nothing left for it to do, and running
> it would work against the policy. The script and its tests (including the
> never-destroy-a-transcript guard) are kept for history. The
> historical instructions below are preserved for reference.

You move the `## Full Transcript` `<details>` block from every meeting note into a per-meeting file under `{{folders.raw}}/fireflies/`. The meeting note keeps a slim link back to the raw file. The skill is **idempotent**: re-running it on an already-migrated meeting is a no-op.

## Mental model

Per DEC-004 (Karpathy LLM Wiki spine) and DEC-009 (ingest writes to `{{folders.raw}}/` first, promotion separate), source-of-truth transcript text lives at `{{folders.raw}}/fireflies/<meeting_id>.md`. The meeting note is the human-curated layer; the raw file is the immutable source. This migration moves existing transcripts (previously embedded in collapsible blocks) into that source-of-truth location.

In practice, every pre-existing meeting carried a `## Full Transcript` `<details>` block whose body was one of two short fireflies.ai placeholders — none had actual transcript content embedded. So every raw file written by this skill was a stub indicating "transcript not retained locally" plus the Fireflies URL. The skill still does the right thing if a meeting *does* carry real transcript text (handled by the `extracted_body` branch).

## Workflow

### 1. Walk the meeting corpus

Scan every `{{vault_root}}/**/meetings/*.md`. For each file:

- Parse YAML frontmatter. Extract `meeting_id` (required), `date`, `fireflies_url`.
- If `meeting_id` is missing → skip and log to the `skipped` bucket.

### 2. Detect the `## Full Transcript` block

Find the first `## Full Transcript` heading. The block runs from that heading to the next `##` heading (or EOF). Within the block, find a single `<details>...</details>` element. Strip the `<summary>...</summary>` child.

Three shapes:

| Body shape | Action |
|---|---|
| Empty (`<details>` with no body or whitespace only) | Write stub raw file (no-transcript). |
| Placeholder match (one of the two known fireflies.ai-link-only patterns — see `is_placeholder()` in `extract.py`) | Write stub raw file (transcript-not-retained). |
| Real content (anything else) | Extract content verbatim and write it as the raw-file body. |

### 3. Idempotency check (skip if already migrated)

A meeting is already migrated if the `## Full Transcript` block contains **no** `<details>` element — instead it holds a markdown link to `{{folders.raw}}/fireflies/<meeting_id>.md`. In that case → skip and log to the `already_migrated` bucket.

### 4. Plan the writes

For each non-skipped meeting:

- **New file:** `{{vault_root}}/{{folders.raw}}/fireflies/<meeting_id>.md` with the schema below.
- **Edit to meeting note:** replace the `<details>...</details>` element with a single-line link: `[Full transcript →](<relative-path>)`. The relative path is computed per-meeting (depth varies across the vault tree).

Raw file schema:

```markdown
---
type: raw-fireflies
meeting_id: <meeting_id>
meeting_note: [[<basename-without-ext>]]
fireflies_url: <fireflies_url-from-meeting>
date: <date-from-meeting>
transcript_status: <retained | not-retained-locally>
extracted_at: <ISO-date>
---

# <meeting note title (filename basename)>

<body — either the extracted transcript content, or the stub paragraph for not-retained-locally>
```

For `transcript_status: not-retained-locally`, the body is:

```
Transcript not retained locally.

This meeting's full transcript lives at Fireflies: [view](<fireflies_url>).

Original meeting note: [[<basename>]]
```

### 5. Apply the plan or report

- `--dry-run-report <path>` writes a structured report enumerating every planned change with byte counts; no vault writes.
- `--execute` writes raw files, edits meeting notes, and refreshes `last-run.md`.
- `--limit N` processes only the first N meetings (smoke-test mode). Combines with either dry-run or execute.

### 6. Write the per-run summary

After execution, write `{{skills_root}}/atlas-transcript-extract/last-run.md`:

- Timestamp.
- Mode (`execute` / `dry-run`).
- Counts: meetings scanned, raw files written, meeting notes edited, already-migrated (skipped), skipped-missing-id, real-transcript-extractions, stub-writes.
- Line-count delta: total transcript lines before = total raw file lines after (within ±5% tolerance).

### 7. Report

Print one-line summary: `migrated=<n> already=<n> skipped=<n> stub=<n> real=<n>`.

## Invocation

```bash
# Preview the plan, no vault writes
python3 extract.py --dry-run-report /tmp/atlas-transcript-extract-dryrun.md

# Smoke test on a small batch
python3 extract.py --execute --limit 10

# Full execution
python3 extract.py --execute
```

When invoked without arguments, run a dry-run preview and surface the planned counts. The owner reviews the dry-run before allowing `--execute`.

## Idempotency contract

Re-running on an already-migrated meeting is a no-op:

- The skill detects the absence of `<details>` in the `## Full Transcript` block → logs to `already_migrated` and skips.
- The corresponding `{{folders.raw}}/fireflies/<meeting_id>.md` is left untouched (append-only invariant — DEC-009).
- Second `--execute` run reports `migrated=0 already=N skipped=0`.

The append-only invariant is load-bearing. If a corrected transcript is ever needed, the owner writes `{{folders.raw}}/fireflies/<meeting_id>-corrected.md` and adds `corrected_by:` frontmatter to the original — never edits in place.

## Edge cases

- **Missing `meeting_id`** in frontmatter → skip, log to `skipped_missing_id`. The owner can backfill the ID and re-run.
- **Multiple `<details>` blocks** in a meeting note — only the first one inside the `## Full Transcript` section is migrated. Other `<details>` elements (e.g. inside Action Items) are left untouched.
- **No `## Full Transcript` heading** at all — skip, log to `skipped_no_section`. (Every meeting note in the original corpus had the heading; this branch is defensive.)
- **Empty `meeting_id`** (`null` or empty string) — treated as missing.
- **`<details>` without proper closing tag** — log as malformed, skip and surface.
- **Real transcript content** present in any meeting → the `extracted_body` branch handles it: writes content verbatim to raw file, leaves the meeting note's link in place.

## Relationship to other skills

- `atlas-frontmatter-migrate` — sibling skill; ran the email→wikilink frontmatter migration on the same meetings. The migration order is `atlas-frontmatter-migrate` first (participants done), `atlas-transcript-extract` second (this skill).
- `atlas-fireflies-ingest` — the ingest skill that originally wrote these meeting notes from the Fireflies API. Ingest runs now write `{{folders.raw}}/fireflies/<meeting_id>.md` directly (DEC-009) and skip the embedded `<details>` block entirely. This migration was the one-time bridge for the pre-DEC-009 corpus.
- `atlas-wiki-materialize` — consumes `{{folders.raw}}/fireflies/*.md` as one of its source-of-truth inputs.
