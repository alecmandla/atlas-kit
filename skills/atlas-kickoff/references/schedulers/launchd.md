# Scheduler: launchd (macOS, Claude Code CLI)

For users on macOS without the desktop app's scheduler. launchd runs a program on a
calendar; the program is either `python3` on an engine script (a **script job**) or the
`claude` CLI in non-interactive mode on a prompt (a **session job**). The kickoff
generates one plist per job under `<repo>/schedulers/launchd/` and the user loads them.

## Two kinds of job

| Kind | Runs | Needs | Examples |
|---|---|---|---|
| Script job | `python3 <repo>/engine/<name>/<script>.py --execute ...` | Python 3.10+, the config file | wiki-materialize, emerge, auto-graduate, lint, health, local-file ingests (dictation, Claude history, GitHub via `gh`) |
| Session job | `claude -p "<prompt>" --allowedTools ...` | the `claude` CLI, MCP servers configured for the CLI, permissions pre-approved | MCP-backed ingests (meetings, email, chat, boards), morning, weekly, synthesize, nightly orchestrator |

Prefer script jobs wherever a capability has a pure-engine path. A session job that
needs an MCP server the CLI does not have configured fails on every run; the kickoff
only generates a session job for a source whose MCP was detected in the CLI session it
ran in.

## The stdlib-only rule

Every engine script runs under launchd with whatever `python3` the plist names and no
virtual environment. Third-party imports (`yaml`, `requests`, `dateutil`) die at import
time in that context, and launchd reports nothing beyond an exit code. The engine is
stdlib-only for this reason, and any script you add to a job must stay stdlib-only.
YAML routing configs are parsed by the engine's own minimal reader; do not "fix" that by
adding PyYAML.

## Plist template

Every plist the kickoff writes has these properties: `python3` (or `claude`) at an
absolute path, an explicit working directory, `PATH` set explicitly, stdout and stderr
to files, and a `Label` in reverse-domain form. Replace `{{...}}` values from the
kickoff; the kickoff does this for you.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.atlas.{{job_name}}</string>

  <key>ProgramArguments</key>
  <array>
    <string>{{python3_path}}</string>
    <string>{{repo_root}}/engine/{{capability}}/{{script}}</string>
    <string>--execute</string>
  </array>

  <key>WorkingDirectory</key>
  <string>{{repo_root}}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>ATLAS_CONFIG</key>
    <string>{{repo_root}}/atlas.config.json</string>
    <key>TZ</key>
    <string>{{timezone}}</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{{hour}}</integer>
    <key>Minute</key>
    <integer>{{minute}}</integer>
    <!-- add <key>Weekday</key><integer>N</integer> for weekly jobs; 0 = Sunday -->
  </dict>

  <key>StandardOutPath</key>
  <string>{{repo_root}}/logs/{{job_name}}.out.log</string>
  <key>StandardErrorPath</key>
  <string>{{repo_root}}/logs/{{job_name}}.err.log</string>

  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
```

Notes on the template:

- `{{python3_path}}` is the absolute path from `command -v python3` in Phase 1, for
  example a Homebrew or Xcode path. Never `python3` bare; launchd's `PATH` is minimal
  and differs from your shell's.
- `WorkingDirectory` is the target repo so relative paths inside the engine resolve the
  same way they do when you run a script by hand.
- `ATLAS_CONFIG` points at the committed config so the job does not depend on
  `~/.config/atlas/config.json` existing on this machine.
- Logs go to `<repo>/logs/`; the kickoff creates that directory and adds it to
  `.gitignore`. Check the `.err.log` first when a job seems to do nothing.
- `RunAtLoad` is false so loading a plist does not fire the job immediately; run it once
  by hand with `launchctl kickstart` (below) to test.

A session job swaps `ProgramArguments` for:

```xml
<array>
  <string>{{claude_path}}</string>
  <string>-p</string>
  <string>Run the {{capability}} skill: execute every step in {{repo_root}}/skills/{{capability}}/SKILL.md unattended. Read {{repo_root}}/atlas.config.json for the vault path. Do not ask questions; skip ambiguous items and note them in last-run.md. Finish by writing {{repo_root}}/skills/{{capability}}/last-run.md.</string>
  <string>--allowedTools</string>
  <string>Read,Write,Edit,Bash,Glob,Grep,{{mcp_tool_patterns}}</string>
</array>
```

`{{claude_path}}` is the absolute path from `command -v claude`. `--allowedTools` must
list every tool the skill uses, including the MCP tool names, or the headless session
stops at the first permission prompt with nobody to answer it. Check the current CLI
documentation for the flag's exact form before relying on it; flags change between
releases.

## Default job set

| Job | Kind | Calendar |
|---|---|---|
| `nightly` | session (or script if every selected ingest is local) | 22:00 daily |
| `synthesize` | session | 22:45 daily |
| `morning` | session | 08:00 daily |
| `weekly` | session | 18:00 Friday (`Weekday` 5) |
| `health` | script | 21:00 Sunday (`Weekday` 0) |
| `materialize` | script | only if `nightly` is not generated; 22:00 daily |

Practices on the no-nudge list never get a plist.

## Setup steps for the user

```bash
mkdir -p ~/Library/LaunchAgents {{repo_root}}/logs
cp {{repo_root}}/schedulers/launchd/local.atlas.*.plist ~/Library/LaunchAgents/
for f in ~/Library/LaunchAgents/local.atlas.*.plist; do
  launchctl bootstrap gui/$(id -u) "$f"
done
launchctl list | grep local.atlas          # every job listed, exit status 0 or -
launchctl kickstart -k gui/$(id -u)/local.atlas.health   # run one job now
cat {{repo_root}}/logs/health.err.log      # empty or informational
```

On older macOS releases `launchctl load <plist>` replaces `bootstrap`. To remove a job:
`launchctl bootout gui/$(id -u)/local.atlas.<job>` then delete the plist.

Verify by artifacts: new files under `{{vault_root}}/{{folders.raw}}/`, a fresh
`skills/<name>/last-run.md`, a report section in today's daily note. `launchctl list`
shows the last exit status, which is more honest than a timestamp but still not proof
that the run wrote what you expected.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| Exit status 127 in `launchctl list` | program path wrong | use the absolute path from `command -v` |
| `ModuleNotFoundError` in `.err.log` | third-party import | remove it; stdlib only |
| Job runs but writes nothing | `ATLAS_CONFIG` unset or pointing at a moved repo | fix the plist, `bootout`, `bootstrap` again |
| Session job hangs then exits | permission prompt with no answerer | extend `--allowedTools` |
| Nothing ever runs | plist in the wrong directory or not bootstrapped | must be `~/Library/LaunchAgents/` and bootstrapped into `gui/<uid>` |
| Runs only when logged in | expected; LaunchAgents need a user session | acceptable for a personal vault; a LaunchDaemon would run as root and is not recommended |
