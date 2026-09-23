---
name: atlas-nightly
description: Nightly 10 PM orchestrator. Runs all eight ingest skills (fireflies, wispr, wispr-meetings, claude-history, github, gmail, slack, monday) in deterministic order, then atlas-wiki-materialize, atlas-emerge, then atlas-auto-graduate (DEC-019). Each sub-skill runs independently — failure is captured to the report but does NOT abort the chain. Appends an "Atlas nightly report" section to today's daily note (creating it if needed). Idempotent. Triggers on "atlas nightly", "nightly ingest", "run nightly", "/atlas-nightly", or any scheduled 22:00 run.
exemplar-of: atlas-nightly
status: active
requires: [mcp/scheduled-tasks, mcp/fireflies, mcp/gmail, mcp/slack, mcp/monday, cli/python3, cli/gh]
---

# atlas-nightly

You are the nightly orchestration agent. You run once per day at 22:00 local (DEC-013 / scheduled via `mcp__scheduled-tasks`). Your job:

1. Run the eight ingest skills in deterministic order.
2. After ingests, run `atlas-wiki-materialize`, `atlas-emerge`, then `atlas-auto-graduate` (DEC-019).
3. Capture each sub-skill's outcome (success / failure with stack trace).
4. Append an "Atlas nightly report" section to today's daily note.
5. Write a per-run summary at `{{skills_root}}/atlas-nightly/last-run.md`.

Sub-skill failures are **captured**, not propagated. The chain continues regardless. This invariant is load-bearing: failure of one sub-skill inside `atlas-nightly` does NOT block the other sub-skills.

`requires` lists the union of what the sub-skills need; the orchestrator itself only needs the scheduler and Python. A missing MCP server shows up as one failed row, never as an aborted chain.

## Run order (deterministic — do not reorder)

| # | Sub-skill | SKILL.md path | What it produces |
|---|---|---|---|
| 1 | `atlas-fireflies-ingest` | `{{skills_root}}/atlas-fireflies-ingest/SKILL.md` | Routes new Fireflies meeting markdown into PARA folders |
| 2 | `atlas-wispr-ingest` | `{{skills_root}}/atlas-wispr-ingest/SKILL.md` | `{{folders.raw}}/wispr/<id>.md` from Wispr SQLite |
| 3 | `atlas-wispr-meetings-ingest` | `{{skills_root}}/atlas-wispr-meetings-ingest/SKILL.md` | `{{folders.raw}}/wispr/meetings/<date>-<slug>-<id8>.md` — Wispr Notetaker meetings: the owner's notes + full transcript |
| 4 | `atlas-claude-history-ingest` | `{{skills_root}}/atlas-claude-history-ingest/SKILL.md` | `{{folders.raw}}/claude-history/<project>/<session>.md` |
| 5 | `atlas-github-ingest` | `{{skills_root}}/atlas-github-ingest/SKILL.md` | `{{folders.raw}}/github/<owner>/<repo>/<item>.md` |
| 6 | `atlas-gmail-ingest` | `{{skills_root}}/atlas-gmail-ingest/SKILL.md` | `{{folders.raw}}/gmail/<route>/<id>.md` |
| 7 | `atlas-slack-ingest` | `{{skills_root}}/atlas-slack-ingest/SKILL.md` | `{{folders.raw}}/slack/<channel>/<ts>.md` |
| 8 | `atlas-monday-ingest` | `{{skills_root}}/atlas-monday-ingest/SKILL.md` | `{{folders.raw}}/monday/<workspace>/<board>/<item>.md` |
| 9 | `atlas-wiki-materialize` | `{{skills_root}}/atlas-wiki-materialize/SKILL.md` | Regenerates `{{folders.wiki}}/entities/*.md` from CRM + raw/ |
| 10 | `atlas-emerge` | `{{skills_root}}/atlas-emerge/SKILL.md` | Regenerates `{{folders.meta}}/Dashboards/Emerging-Patterns.md` |
| 11 | `atlas-auto-graduate` | `{{skills_root}}/atlas-graduate/auto_graduate.py` | Auto-graduates high-confidence patterns (DEC-019); writes `{{folders.meta}}/Dashboards/Thread-Review-Queue.md` |

If the owner also runs `atlas-apple-notes-ingest` and `atlas-voice-memos-ingest`, they slot in after step 8 and before step 9, in that order; both are soft-skip ingests and follow the same capture-not-abort contract.

## Step 0 — Acquire the suite run lock (do this FIRST)

A desktop scheduler's wake-from-sleep behavior can fire this task many times in a
few seconds. Concurrent nightly chains race on state files and the daily-note
section edit, so the whole chain is mutual-exclusive:

```bash
python3 {{skills_root}}/atlas-nightly/lock.py acquire
```

- **Exit 0** — you hold the lock; proceed to Step 1.
- **Exit 1** — another nightly run is in progress. STOP immediately and report
  `skipped: suite lock held (duplicate-fire guard)`. Do not run any sub-skill,
  do not write any file.

A lock older than 180 minutes is presumed crashed and is stolen automatically.

## Step 1 — Initialize the run

```
TODAY = current date in YYYY-MM-DD format
RUN_LOG = []        # list of dicts: {skill, started_at, ended_at, status, files_written, error}
RUN_STARTED_AT = current timestamp
```

## Step 2 — Run each sub-skill in order

For each sub-skill in the table above:

1. Record `started_at = now()`.
2. Read its SKILL.md.
3. Execute the skill's documented invocation. **Flags are NOT uniform across skills — use the table in "Sub-skill invocation patterns" below, do not assume `--incremental` exists.** In brief: every Python ingest takes `--execute`; only `atlas-wispr-ingest` and `atlas-wispr-meetings-ingest` also accept `--incremental`; `wiki-materialize` defaults to dry-run and REQUIRES `--execute`; `emerge` writes by default and takes no flags; `atlas-auto-graduate` is `python3 {{skills_root}}/atlas-graduate/auto_graduate.py --execute`, which must run AFTER `emerge` since it consumes the freshly-written `Emerging-Patterns.md`.
4. Catch any error (subprocess non-zero exit, exception, missing dependency). On failure:
   - Capture the stack trace / stderr.
   - Set `status = "failed"`.
   - Continue to the next sub-skill — do NOT abort.
5. On success: capture `files_written` count (from the skill's last-run.md if it writes one, else 0).
6. Record `ended_at = now()`.
7. Append the row to `RUN_LOG`.

### Sub-skill invocation patterns

Each ingest skill defines its own invocation in its SKILL.md. **The flags differ per skill.** Keep this table verified against each script's `argparse` definitions. Passing a flag a script does not define makes it exit non-zero, which the chain records as a spurious failure.

| Skill | Nightly invocation | Notes |
|---|---|---|
| `atlas-wispr-ingest` | `ingest.py --execute --incremental` | accepts `--incremental` |
| `atlas-wispr-meetings-ingest` | `ingest.py --execute --incremental` | accepts `--incremental`; **defaults to dry-run** — without `--execute` it writes nothing. Runs AFTER `atlas-fireflies-ingest` so Fireflies cross-links resolve against fresh files |
| `atlas-claude-history-ingest` | `ingest.py --execute` | also takes `--limit`; **no** `--incremental` |
| `atlas-github-ingest` | `ingest.py --execute --days 7` | also takes `--repo`; **no** `--incremental`. Pin `--days`: an unbounded window can exceed the scheduler's timeout and leave a stale `.run.lock` |
| `atlas-gmail-ingest` | see its SKILL.md — needs `--labels-json` + `--input-json` captured via MCP | not a bare CLI run |
| `atlas-slack-ingest` | see its SKILL.md — needs `--input-json` per channel | not a bare CLI run |
| `atlas-monday-ingest` | see its SKILL.md — needs `--input-json` per board | not a bare CLI run |
| `atlas-fireflies-ingest` | `--execute` only; routing is performed per its SKILL.md | see its Monday-tier guard |
| `atlas-wiki-materialize` | `materialize.py --execute` | **defaults to dry-run** — without `--execute` it reports `created=0 updated=0` and writes nothing |
| `atlas-emerge` | `emerge.py` | writes by default; has `--dry-run` to suppress |

If a script rejects a flag, check its `argparse` block rather than retrying variants, and correct this table in the same run.

Where a skill doesn't have a Python entry point (e.g. it's instruction-only), follow the SKILL.md's documented invocation manually but capture the same `status` / `files_written` / `error` fields.

### Output capture conventions

- **stdout**: capture last 200 lines per skill to `{{skills_root}}/atlas-nightly/logs/<TODAY>/<skill>.stdout.log`.
- **stderr**: full capture to `{{skills_root}}/atlas-nightly/logs/<TODAY>/<skill>.stderr.log`.
- **last-run.md from each sub-skill**: read and inspect for the `files_written:` / similar field; surface in the report.

`mkdir -p {{skills_root}}/atlas-nightly/logs/<TODAY>/` before running.

## Step 3 — Build the nightly report

After all sub-skills have run, build the report markdown:

```markdown
## Atlas nightly report

*Generated by `atlas-nightly` at <HH:MM> on <TODAY>. Total duration: <Xm Ys>.*

| # | Skill | Status | Duration | Files | Notes |
|---|---|---|---|---|---|
| 1 | `atlas-fireflies-ingest` | ✅ ok | 12s | 0 new | — |
| 2 | `atlas-wispr-ingest` | ✅ ok | 8s | 14 new | — |
| 3 | `atlas-wispr-meetings-ingest` | ✅ ok | 6s | 2 new, 1 updated | 1 carrying notes |
| 4 | `atlas-claude-history-ingest` | ✅ ok | 5s | 3 new | — |
| 5 | `atlas-github-ingest` | ⚠️ failed | 4s | — | `429 Too Many Requests` (see logs) |
| 6 | `atlas-gmail-ingest` | ✅ ok | 22s | 41 new | — |
| 7 | `atlas-slack-ingest` | ✅ ok | 9s | 12 new | — |
| 8 | `atlas-monday-ingest` | ✅ ok | 11s | 0 new | no board changes |
| 9 | `atlas-wiki-materialize` | ✅ ok | 18s | 32 regenerated | — |
| 10 | `atlas-emerge` | ✅ ok | 7s | 1 report | 12 patterns surfaced |
| 11 | `atlas-auto-graduate` | ✅ ok | 3s | 1 queue | 1 auto, 2 pending, 6 review |

### Failures

- `atlas-github-ingest` — `429 Too Many Requests` on commits endpoint. Stack trace at `{{skills_root}}/atlas-nightly/logs/<TODAY>/atlas-github-ingest.stderr.log`. Retry next run.

### Summary

- Total files written across ingest skills: <N>
- New emerging patterns: <N>
- Wiki pages regenerated: <N>
- Threads auto-graduated / pending / in review queue: <N> / <N> / <N>
- Sub-skills succeeded: <N> / 11
```

If all sub-skills succeed and produce zero new files (typical idempotent re-run on the same evening), the Summary becomes:

```markdown
### Summary

- No new changes — idempotent re-run.
- Sub-skills succeeded: 11 / 11
```

## Step 4 — Append to today's daily note (idempotent)

```
DAILY_NOTE_PATH = {{vault_root}}/{{folders.daily}}/<YEAR>/<MONTH>/<TODAY>.md
```

**If the daily note does NOT exist** (e.g. weekend when atlas-morning didn't fire):

Create the note by instantiating `{{vault_root}}/{{folders.resources}}/Templates/Daily-Note.md` (same instantiation logic as atlas-morning Step 2). Use the freshly-instantiated note as the base.

**Inject the nightly report section:**

Locate the `## Atlas nightly report` heading in the daily note.

- **If absent:** append the section at end of file (after `## Scratch` if present).
- **If present:** replace everything from `## Atlas nightly report` to the next `## ` heading (or end of file). This is the idempotent overwrite: repeated runs on the same evening replace the previous nightly section without disturbing other sections (morning report, scratch, etc.).

## Step 5 — Release the suite lock, then write last-run.md

Release the lock from Step 0 **unconditionally**, including when sub-skills
failed (failures are captured, and a held lock would block the next run until
the stale timeout):

```bash
python3 {{skills_root}}/atlas-nightly/lock.py release
```

Then write last-run.md.

`{{skills_root}}/atlas-nightly/last-run.md`:

```markdown
# atlas-nightly — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- date: <TODAY>
- duration_seconds: <float>
- daily_note_path: <DAILY_NOTE_PATH>
- daily_note_created: <true | false>
- sub_skills_run: 11
- sub_skills_succeeded: <N>
- sub_skills_failed: <N>
- total_files_written: <N>
- failures:
  - skill: atlas-github-ingest
    error: "429 Too Many Requests"
    log: {{skills_root}}/atlas-nightly/logs/<TODAY>/atlas-github-ingest.stderr.log
```

## Idempotency contract

- **Daily note**: creates only if absent; never overwrites existing daily note wholesale.
- **Nightly report section**: always overwrites only the `## Atlas nightly report` section.
- **raw/ writes**: each ingest skill is itself idempotent; re-running atlas-nightly on the same evening produces a "no new changes" report if all upstream sources are quiescent.
- **Wiki/Emerge**: both are regeneration-overwrite by design (per their SKILL.md contracts).

Two consecutive nightly runs on the same date and unchanged corpora produce identical reports modulo timestamps.

## Failure isolation

The chain never aborts on a sub-skill failure. Implementation contract:

```python
for skill in run_order:
    try:
        result = run_skill(skill)
    except Exception as e:
        result = {"status": "failed", "error": traceback.format_exc()}
    run_log.append(result)
```

Failures surface in the report's Failures section + the per-skill stderr log. The owner reviews next morning via the daily note.

If `atlas-wiki-materialize` or `atlas-emerge` fail, that's still non-aborting; they regenerate on the next run.

## Edge cases

- **Sub-skill script missing** (e.g. `ingest.py` not found): log `status: skipped — script not found at <path>` in the report. Do not crash.
- **Sub-skill writes no last-run.md**: set `files_written: ?` in the report; do not infer.
- **Daily note exists but morning report is malformed**: still inject the nightly section; do not touch the morning section.
- **Logs directory creation fails**: fall back to in-memory capture only; report mentions "logs not persisted".
- **Total runtime > 30 min**: log a warning to the report's Summary section. Surface to the owner if consistent.

## Invocation

When invoked manually (via Claude Code):
1. Read this SKILL.md.
2. Execute Steps 0–5 sequentially.
3. Report: counts per sub-skill, any failures, daily note path.

When invoked by the scheduled task:
- Cron: `0 22 * * *` (22:00 local time, daily).
- Registered via `mcp__scheduled-tasks__create_scheduled_task` (DEC-008).
- The scheduled task fires a Claude Code session with a self-contained prompt that points back to this SKILL.md.

## Relationship to other skills

- **Upstream**: each of the eight ingest skills + `atlas-wiki-materialize` + `atlas-emerge`. This skill is purely an orchestrator; it owns no domain logic.
- **Sister scheduled agents**: `atlas-morning` reads what nightly wrote. `atlas-weekly` consumes nightly reports across the week. `atlas-health` audits idempotency of the corpus that nightly populates. `atlas-synthesize` fires ~45 minutes after nightly and consumes the newly graduated threads.
- **DEC-013**: this skill is the sole nightly trigger for all eight ingests. Any pre-existing standalone sync schedule is a legacy artifact; flag it for the owner if it conflicts.

## Anti-goals (NOT v1)

- Per-sub-skill schedule overrides. All eight run at 22:00, full stop (DEC-013).
- Parallel sub-skill execution. Determinism > speed; serial chain is the contract.
- Auto-retry on sub-skill failure. Retry happens on the next nightly run, not within the same invocation.
- Slack/email notifications on failure. Read-only into the vault. The daily-note report is the surface.
