# Scheduler: manual

For users who chose on-demand only, for platforms with neither the desktop scheduler
nor launchd, and as the fallback every other option gets too: the kickoff always writes
`<repo>/docs/RUNBOOK.md` from this guide, so any job can be run by hand regardless of
what else is set up.

## What the kickoff generates

`docs/RUNBOOK.md` with one section per selected capability: the exact command, what it
writes, how to verify it, and the suggested cadence. Commands are run from the target
repo root. Script jobs are plain shell; session jobs are a slash command or a prompt to
paste into a Claude Code session started in the repo.

Template for the runbook:

```markdown
# Runbook

Run from `{{repo_root}}`. Every job is idempotent: running it twice in a row is safe.
Verify by artifacts (the files each job writes), never by the fact that it exited.

## Suggested cadence

| When | Jobs |
|---|---|
| Every evening | nightly (all ingests, then materialize, emerge, auto-graduate), then synthesize |
| Every morning | morning |
| Friday evening | weekly |
| Sunday evening | health, lint |
| When you have a question | research |
| When a chat is worth keeping | distill |
| When the emerging-patterns dashboard shows something real | graduate <slug> |

## Jobs

### nightly
- **Run:** in a Claude Code session here, `/atlas-nightly`
- **Writes:** `{{folders.raw}}/<source>/` files, wiki pages, the emerging-patterns dashboard, a report section in today's daily note
- **Verify:** `find "{{vault_root}}/{{folders.raw}}" -newer engine/atlas-nightly/last-run.md -type f | wc -l` is greater than zero on a day with new source activity
- **Cadence:** every evening

### <each script job>
- **Run:** `python3 engine/<name>/<script>.py --execute`
- **Writes:** ...
- **Verify:** `cat engine/<name>/last-run.md` (every job, script or session, records its run there)
- **Cadence:** ...

### <each session job>
- **Run:** in a Claude Code session here, `/<skill-name>`
- ...
```

## One command per job, defaults

| Job | Command | Cadence |
|---|---|---|
| nightly | `/atlas-nightly` (session) | every evening |
| synthesize | `/atlas-synthesize` (session) | every evening, after nightly |
| morning | `/atlas-morning` (session) | every morning |
| weekly | `/atlas-weekly` (session) | Friday evening |
| health | `/atlas-health` (session; there is no engine script) | Sunday evening |
| lint | `python3 engine/atlas-lint/lint.py report` | Sunday evening or before a synthesis batch |
| people-extract | `python3 engine/atlas-people-extract/extract.py --execute` | evening, before materialize; only useful once meeting notes exist |
| materialize | `python3 engine/atlas-wiki-materialize/materialize.py --execute` | evening, if not using nightly |
| emerge | `python3 engine/atlas-emerge/emerge.py` | evening, after materialize |
| auto-graduate | `python3 engine/atlas-graduate/auto_graduate.py --execute` | evening, after emerge |
| local ingests (dictation, Claude history, GitHub) | `python3 engine/<name>/ingest.py --execute --incremental` (flags vary; the generated SKILL.md is authoritative) | evening |
| MCP ingests (meetings, email, chat, boards) | `/<skill-name>` (session; the script cannot call an MCP) | evening |
| research | `/atlas-research <question>` | on demand |
| distill | `/atlas-distill` | on demand |
| graduate | `/atlas-graduate <slug>` | on demand |

Exact script names and flags come from the exemplar each generated skill was derived
from; the kickoff copies them into the runbook verbatim. Do not assume every script
accepts `--incremental`.

## Making manual stick

A runbook nobody opens is the same as no pipeline. Two habits that work:

- Tie the evening jobs to an existing habit: the last thing before closing the laptop is
  `/atlas-nightly`. Put the line in the daily-note template so it is visible every day.
- Once a week, look at the health dashboard under `{{folders.meta}}/Dashboards/` and the
  file counts under `{{folders.raw}}/`. If they have not moved, you skipped a week; run
  nightly with a wider window (`--days 14` on the ingests that take it) to catch up.

## Cron on Linux

If you are on Linux and want the script jobs unattended, the runbook's script commands
work in a crontab entry with the same rules as the launchd guide: absolute `python3`,
`cd` to the repo, `ATLAS_CONFIG` set, output redirected to a log file, stdlib only.
Session jobs need the `claude` CLI with `-p` and pre-approved tools; see the launchd
guide's session-job notes, which apply unchanged.
