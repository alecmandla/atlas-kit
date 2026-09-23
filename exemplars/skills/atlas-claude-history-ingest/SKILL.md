---
name: atlas-claude-history-ingest
description: Ingest Claude Code session JSONL files at ~/.claude/projects/*/ into raw/claude-history/<project>/<session_id>.md. One markdown summary per session — first user prompt, last assistant turn, files touched, turn count, timing. Idempotent. Redacts API keys + secrets before writing. Triggers on "sync claude history", "claude history ingest", "rebuild raw claude-history".
exemplar-of: atlas-claude-history-ingest
status: active
requires: [cli/python3]
---

# atlas-claude-history-ingest

You mirror the owner's Claude Code session history into the vault's `{{folders.raw}}/claude-history/` layer (per DEC-009 and DEC-014). Each JSONL file at `~/.claude/projects/<project>/<session_id>.jsonl` becomes one summary markdown at `{{vault_root}}/{{folders.raw}}/claude-history/<project>/<session_id>.md`, extracting:

- First user prompt (truncated to 200 chars).
- Last assistant turn (one-line preview).
- Files the session touched (parsed from tool-use events: `Read`, `Write`, `Edit`, `NotebookEdit`).
- Turn count + timing (`started`, `ended` from first / last line's `timestamp`).

The full JSONL stays in place at `~/.claude/projects/` (Claude Code's home directory). The markdown summary is the projection into the vault.

## Privacy

The skill **redacts** suspected secrets before writing. Patterns covered:

| Pattern | Redaction |
|---|---|
| `sk-[A-Za-z0-9]{20,}` (OpenAI / Anthropic-style keys) | `sk-REDACTED` |
| `ghp_[A-Za-z0-9]{36}` (GitHub PATs) | `ghp_REDACTED` |
| `AKIA[0-9A-Z]{16}` (AWS access keys) | `AKIA_REDACTED` |
| `xoxb-[A-Za-z0-9-]{30,}` (Slack bot tokens) | `xoxb-REDACTED` |
| `Bearer [A-Za-z0-9._-]{30,}` | `Bearer REDACTED` |
| `password\s*[=:]\s*\S+` (case-insensitive) | `password=REDACTED` |
| `api[_-]?key\s*[=:]\s*\S+` (case-insensitive) | `api_key=REDACTED` |

Redaction is per-string, applied to first-prompt and last-assistant-turn previews before they hit the raw file. This seven-pattern set is the shared redaction vocabulary for the whole suite (`atlas-distill` and `atlas-voice-memos-ingest` carry it verbatim); keep them in sync.

## Workflow

### 1. Walk `~/.claude/projects/*/`

Each subdirectory is one Claude Code project (path-encoded, e.g. `-home-user-code-guest-portal`). Each `*.jsonl` inside is one session.

### 2. Parse each session

Stream the JSONL line-by-line. Each line is one event. Event types include `user`, `assistant`, `attachment`, `last-prompt`, `queue-operation`, `tool-use`, `tool-result`. Capture:

- `started` = first line's `timestamp` (ISO).
- `ended` = last line's `timestamp` (ISO).
- `turn_count` = count of `user` + `assistant` events.
- `first_prompt` = first `user` event's `content` (truncated to 200 chars after redaction).
- `last_assistant` = last `assistant` event's `content` (truncated to 200 chars after redaction).
- `files_touched` = set of file paths seen in `tool-use` events whose tool name is `Read`, `Write`, `Edit`, or `NotebookEdit` (the `file_path` argument).

### 3. Skip in-progress sessions

A session is "in progress" if `now() - mtime(jsonl_file) < 5 minutes`. The 5-minute idle threshold is a practical cutoff: Claude Code writes to the JSONL throughout an active session; if it's quiet for 5+ min, the session has effectively closed. Skipped sessions land in `skipped_in_progress` and ingest on the next run.

### 4. Write or refresh the summary

`{{folders.raw}}/claude-history/<project_slug>/<session_id>.md`:

```markdown
---
type: raw-claude-history
session_id: <session_id>
project: <project_slug>
project_path: <decoded /path/to/project>
started: <ISO>
ended: <ISO>
duration_minutes: <int>
turn_count: <int>
first_prompt: "<200-char-truncated, redacted>"
files_touched: [list of file paths]
ingested_at: <ISO>
---

# <project_slug> · <YYYY-MM-DD HH:MM> session

## First prompt

> <first_prompt verbatim, redacted>

## Last assistant turn

> <last_assistant truncated, redacted>

## Files touched

- `<file_path>`
- ...

## Stats

- Turns: <turn_count>
- Duration: <int> minutes
- Started: <started>
- Ended: <ended>
```

Idempotency: if the raw file exists AND its `ended` frontmatter matches the JSONL's last-line timestamp, skip. Otherwise rewrite (rare; usually only happens if a session resumed after being initially treated as closed).

### 5. State + per-run summary

`{{skills_root}}/atlas-claude-history-ingest/state.json` tracks `last_run_iso`, `last_run_count`. Per-run summary at `last-run.md`.

## Invocation

```bash
python3 ingest.py --dry-run-report /tmp/atlas-claude-history-ingest-dryrun.md
python3 ingest.py --execute
python3 ingest.py --execute --limit 10   # smoke test
```

## Idempotency contract

Re-running:
- Skips raw files whose `ended` matches the JSONL's last-line timestamp.
- Updates raw files whose JSONL has grown (new lines, advanced `ended`).
- Never re-creates already-correct files.

Verify: 2x `--execute` → first run `written=N`, second run `written=0 already=N`.

## Edge cases

- **Empty JSONL** → log to `skipped_empty`, no file written.
- **JSONL with only `queue-operation` events** (no `user`/`assistant`) → log to `skipped_no_turns`.
- **Project slug starts with `-`** → strip leading `-` for filesystem-safe folder name, but keep original in `project:` frontmatter.
- **Session split across multiple JSONL files** (rare) → treated as separate sessions; each summary stands alone.
- **Malformed JSON line** → log and continue; line counter increments, content is skipped.

## Relationship to other skills

- `atlas-wispr-ingest` — sibling local-source skill; same state.json / dry-run pattern.
- `atlas-emerge` — consumes `{{folders.raw}}/claude-history/` per DEC-014 to mine thought-threads.
- `atlas-distill` — the on-demand counterpart: this skill mirrors session metadata mechanically; distill captures distilled content from a live exchange. Redaction patterns are shared.
- `atlas-wiki-materialize` — currently does NOT read this source; a later version may surface code-project entities.
