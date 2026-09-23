# Scheduler: desktop scheduled tasks

For users running the Claude desktop app, where the `scheduled-tasks` MCP is available
(tool names contain `create_scheduled_task`, `list_scheduled_tasks`,
`update_scheduled_task`). Each task fires a fresh Claude Code session on a cron cadence
with a prompt; that session has the same MCP servers and skills as the app.

Use this option when the pipeline includes jobs that need a Claude session (MCP-backed
ingests, the morning briefing, weekly review, synthesize, the nightly orchestrator).
Pure engine scripts also run fine here; the session just calls `python3`.

## The pinned working directory, read this before creating anything

**A scheduled task is pinned to the working directory of the session that created it,
and that directory cannot be changed afterward.** There is no `cwd` parameter on
`update_scheduled_task`. If the pinned directory is later moved, renamed, or deleted,
every run fails at startup. The scheduler still records a last-run timestamp, because it
logs the dispatch, not the outcome. No transcript, no log, no output. The maintainer's
own pipeline failed this way for over two weeks after a directory rename, with every
task showing a fresh last-run time the whole while.

Consequences:

1. Create every task from a session whose working directory is the target repo root
   (`{{repo_root}}`), the directory the generated skills and `engine/` live in. The
   kickoff checks `pwd` before creating tasks and refuses if it does not match.
2. Never move or rename the target repo after tasks exist. If you must, delete the tasks
   and recreate them from a session started in the new location.
3. **Verify runs by artifacts, never by last-run timestamps.** After the first scheduled
   run, and any time you doubt the pipeline, check: new files under
   `{{vault_root}}/{{folders.raw}}/<source>/`, the mtime of `skills/<name>/last-run.md`
   in the repo, and today's daily note for the morning or nightly report section. A
   timestamp with no artifact means the run failed silently.

## Wake-from-sleep storms

The desktop scheduler can fire a missed task many times in a row when the machine wakes
from sleep. Every generated job that writes must be idempotent and lock-guarded. The
exemplars already are (suite lock in the nightly orchestrator, per-skill run locks,
cursor-based ingest state). Do not remove those guards when adapting a skill, and do not
write a new job here without one.

## What the kickoff generates

`<repo>/schedulers/desktop-tasks.md`, one block per job, ready to paste into the
`create_scheduled_task` call or to hand to the session that will create them. Each block:

```
### <job name>

- **cron:** <expression>   (<plain-language cadence>, timezone {{timezone}})
- **working directory:** {{repo_root}}   (create the task from a session started here)
- **prompt:**

  Run the `<capability>` skill: execute every step in
  `{{repo_root}}/skills/<capability>/SKILL.md` unattended. Read
  `{{repo_root}}/atlas.config.json` for the vault path and folder names. Do not ask
  questions; on any ambiguity, skip the item, note it in `last-run.md`, and continue.
  Write nothing outside the paths that SKILL.md names. Finish by writing
  `{{repo_root}}/skills/<capability>/last-run.md` with counts and any errors.
```

Default job set and cadence (adjust per the interview):

| Job | cron | Cadence |
|---|---|---|
| `atlas-nightly` | `0 22 * * *` | daily 22:00; runs every selected ingest, then materialize, emerge, auto-graduate |
| `atlas-synthesize` | `45 22 * * *` | daily 22:45, after nightly |
| `atlas-morning` | `0 8 * * *` | daily 08:00 |
| `atlas-weekly` | `0 18 * * 5` | Friday 18:00 |
| `atlas-health` | `0 21 * * 0` | Sunday 21:00 |

Jobs for practices on the no-nudge list are never generated. On-demand-only
capabilities (research, distill, graduate by hand, book summaries) get no task.

## Setup steps for the user

1. Open a Claude Code session in the desktop app with the working directory set to
   `{{repo_root}}`. Confirm with `pwd`.
2. For each block in `schedulers/desktop-tasks.md`, ask the session to create the
   scheduled task with that cron, that prompt, and a name matching the job. The session
   calls `create_scheduled_task`; you approve.
3. Open the Scheduled section of the app sidebar and confirm each task is listed with the
   right cadence.
4. Trigger `atlas-nightly` once by hand (ask the session to run it now) and check the
   artifacts listed above before trusting the schedule.
5. Add a weekly habit: glance at `{{folders.raw}}/` file counts or the health dashboard.
   If the counts stop moving while last-run times keep updating, the pinned directory
   has probably moved; delete and recreate the tasks.

## Managing tasks later

- Change a cadence or prompt: `update_scheduled_task` from any session.
- Change the working directory: not possible; delete and recreate from the right
  directory.
- Pause everything: disable each task in the sidebar, or delete them; the manual
  runbook (`docs/RUNBOOK.md`) still lets you run any job by hand.
