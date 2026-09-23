---
name: atlas-kickoff
description: >-
  Set up an Obsidian second brain with an automated knowledge pipeline. Diagnoses the
  machine and vault, interviews the user (at most three rounds), writes a tailored
  docs/ATLAS-KICKOFF.md, then executes it to generate vault-specific skills derived from
  the bundled exemplars, routing configs, an atlas config, the vault scaffold, a plugin
  checklist, and a scheduler setup. Use whenever the user runs /atlas-kickoff, says "set
  up my second brain", "bootstrap atlas", "kick off atlas", "set up the knowledge
  pipeline", or wants to start an Obsidian vault that ingests their sources automatically.
---

# /atlas-kickoff — interview, write the kickoff, generate the pipeline

Self-contained. The interview method and the kickoff model ship in `references/`; nothing
here depends on another skill being installed.

## Usage

```
/atlas-kickoff                                  # current repo is the target; vault detected or asked
/atlas-kickoff <vault-path>                     # vault given; target repo is the current directory
/atlas-kickoff --repo <path> <vault-path>       # both given
/atlas-kickoff --write-only                     # stop after writing docs/ATLAS-KICKOFF.md
/atlas-kickoff --resume                         # skip to Phase 4 using an existing docs/ATLAS-KICKOFF.md
```

The **target repo** is where generated skills, configs, and the kickoff document land. It
is the user's repo, never this plugin. The **vault** is the Obsidian folder. They are
different directories; keep them straight in every path you print.

## References — read at the right moment

| File | Read when |
|---|---|
| `references/workflow-reference.md` | Before anything else. The kickoff model: hierarchy, prompt structure, constraint discipline, the DECISIONS log. |
| `references/grilling.md` | Before Phase 2. How to interview: follow up on vague answers, batch, force tradeoffs, when to stop. |
| `references/interview.md` | During Phase 2. The question bank per round, with the config key each answer feeds, the follow-up, and the default. |
| `references/kickoff-template.md` | At Phase 3. The parameterized ATLAS-KICKOFF skeleton with authoring notes. |
| `references/obsidian-plugins.md` | At Phase 4 when writing the plugin checklist, and at Phase 5 for the manual steps. |
| `references/schedulers/<option>.md` | At Phase 4, only the one matching the chosen scheduler. |

Plugin paths below use `${CLAUDE_PLUGIN_ROOT}` for this plugin's own tree:
`exemplars/skills/<name>/SKILL.md`, `exemplars/configs/*.example.*`,
`exemplars/vault/*.md.template`, `engine/`, and `vault-scaffold/`.

## Capability catalog

Every generated skill is derived from exactly one exemplar. The authoritative list is
whatever exists under `exemplars/skills/*/SKILL.md`; read each one's frontmatter
(`exemplar-of`, `status`, `requires`) at Phase 1 and treat that as truth. The table below
is the expected shape so the diagnostic knows what to detect. Skip any exemplar whose
`status` is `retired`; flag `experimental` ones as such in the interview.

| Group | Exemplar | Prerequisite and how to detect it | Config it needs |
|---|---|---|---|
| Ingest | `atlas-fireflies-ingest` | Fireflies MCP: a tool name containing `fireflies_get_transcripts` | `meeting-routing.yaml` |
| Ingest | `atlas-gmail-ingest` | Gmail MCP: tool names containing `search_threads` and `list_labels` | `mailbox-routing.yaml` |
| Ingest | `atlas-slack-ingest` | Slack MCP: `slack_search_channels` and `slack_read_channel` | `slack-routing.yaml` |
| Ingest | `atlas-monday-ingest` | Monday MCP: `get_board_items_page` | `monday-boards.yaml` |
| Ingest | `atlas-github-ingest` | `gh auth status` exits 0 | `github-repos.yaml` |
| Ingest | `atlas-claude-history-ingest` | `~/.claude/projects/` exists and is non-empty | none |
| Ingest | `atlas-wispr-ingest` | `~/Library/Application Support/Wispr Flow/flow.sqlite` exists | none |
| Ingest | `atlas-wispr-meetings-ingest` | same file as above | none |
| Ingest | `atlas-apple-notes-ingest` | Apple Notes MCP: `list_notes` and `get_note_content` | none |
| Ingest | `atlas-voice-memos-ingest` | macOS with Full Disk Access granted to the runner; cannot be detected, ask | none |
| Spine | `atlas-people-extract` | `python3` 3.10+; meeting notes carrying `attendee_emails:` (a meeting ingest, or notes written by hand from the meeting template). Materialize's org branch reads its output. | none |
| Spine | `atlas-wiki-materialize` | `python3` 3.10+ | `entity_seeds.json` |
| Spine | `atlas-emerge`, `atlas-graduate`, `atlas-lint`, `atlas-health` | `python3` 3.10+ | none |
| Spine | `atlas-synthesize` | `python3` 3.10+; a Claude session writes the prose | none |
| Query | `atlas-research`, `atlas-distill` | none (Fireflies MCP optional for transcript escalation) | none |
| Briefing | `atlas-morning` | a daily-note template in the vault; calendar MCP optional | none |
| Briefing | `atlas-weekly` | a weekly-review template in the vault (the scaffold ships one) | none |
| Orchestrator | `atlas-nightly` | at least one ingest selected and a scheduler chosen | none |
| Capture | `atlas-book-summary` | web search available | none |

If a prerequisite is missing: generate nothing for that capability, and tell the user
exactly what to install or connect. Do not generate a skill that will fail on first run.

## Phase 1 — Diagnose without asking

Gather all of this before the first question. Use `Bash` and the tool list; do not ask the
user anything the machine can answer.

1. **Vault path.** In order: the invocation argument; `$ATLAS_CONFIG` or
   `~/.config/atlas/config.json` if one exists (read `vault_root`); then
   `find "$HOME" "$HOME/Documents" -maxdepth 1 -mindepth 1 -type d \( -iname '*obsidian*' -o -iname '*vault*' \) 2>/dev/null`
   (a `find`, not a glob: under zsh an unmatched glob aborts the whole command, and
   `2>/dev/null` does not help). Zero candidates means
   "new vault" and the interview asks where to create it. More than one means the
   interview asks which.
2. **Obsidian installed.** macOS: `ls /Applications/Obsidian.app` or
   `mdfind "kMDItemCFBundleIdentifier == 'md.obsidian'"`. Linux: `which obsidian`,
   `flatpak list | grep -i obsidian`. Windows under WSL: assume installed on the host and
   say so.
3. **Vault state.** If a vault exists: read `.obsidian/community-plugins.json` and
   `.obsidian/core-plugins.json`; list top-level folders with a file count each
   (`find "<vault>" -maxdepth 1 -mindepth 1 -type d | while read d; do echo "$(find "$d" -type f | wc -l) $d"; done`).
   Never read note contents. A folder with files is "has content" for the guardrails below.
4. **Runtime.** Desktop app if tools named `mcp__scheduled-tasks__*` (or containing
   `create_scheduled_task`) are available; otherwise Claude Code CLI. This decides which
   scheduler options exist.
5. **CLIs.** `python3 --version` (need 3.10 or newer; record the absolute path from
   `command -v python3`), `gh --version && gh auth status`, `claude --version`,
   `launchctl version` (macOS only), `git --version`.
6. **MCP servers.** Scan available tool names for the substrings in the catalog. Record
   which of Gmail, Slack, Monday, Fireflies, Apple Notes, calendar, and scheduled-tasks are
   connected.
7. **Exemplars.** Read the frontmatter of every `exemplars/skills/*/SKILL.md`. Build the
   offer list: exemplar name, status, requires, and whether each requirement was detected.
8. **Target repo.** `git rev-parse --show-toplevel` from the target directory. Note an
   existing `docs/ATLAS-KICKOFF.md`, existing `skills/atlas-*`, and an existing
   `~/.config/atlas/config.json`; these change the overwrite questions later.
9. **Timezone.** `date +%Z` and, on macOS, `readlink /etc/localtime | sed 's|.*/zoneinfo/||'`.

Print a short diagnostic block (vault, Obsidian, runtime, CLIs, MCP servers, offerable
capabilities, blocked capabilities with the missing piece). Then move to Phase 2.

## Phase 2 — Interview (at most three rounds)

Read `references/grilling.md` and `references/interview.md`. Ask with `AskUserQuestion`,
up to four questions per round, options first and free text allowed. Every question names
its default so the user can accept it in one word. Follow up inside a round only when an
answer is vague enough that the config value cannot be written; otherwise move on. Three
rounds is the ceiling, not the target: skip any question the diagnostic already answered.

**Round 1 — Identity and shape.** Owner name (`owner_name`); the one-paragraph mission
(kickoff §Mission); vault layout scheme: PARA with numbered prefixes (default), PARA
without numbers, Zettelkasten, custom; timezone (default detected).

**Round 2 — Folders, sources, and silence.** Confirm every folder key's name from the
scheme chosen (present the full table, ask for edits, not a re-derivation); which sources
to ingest, offering only capabilities whose prerequisites were detected and listing the
blocked ones with what is missing, with a follow-up call for routing values and, when a
meeting or email source is picked, the optional owner email and employer name and domain
(blank is a valid answer; it becomes `{{fill-me}}` where used); which non-ingest
capabilities to enable (spine is on by default; briefings, research, capture are choices);
the **no-nudge question**: which practices, folders, or tags must never appear on any
nudge surface (morning report, weekly review, dashboards, scheduled output). The answer
becomes the config's `no_nudge` list; the kit ships no skill for any practice, only the
exclusion.

**Round 3 — Operation and non-negotiables.** On-demand only versus scheduled; scheduler
choice among the options the runtime allows (desktop scheduled tasks, launchd, manual);
the non-negotiables, each accepted or explicitly waived with a reason: raw is append-only,
wiki is regenerable from raw, every generated wiki claim carries a resolvable wikilink,
nothing generated ever deletes a note, meeting transcripts stay summary-only on disk;
and the things the user has been burned by that the pipeline must never do.

A waived non-negotiable is recorded in the kickoff as waived with the reason. It is not
silently dropped. If the user waives "raw is append-only", say plainly that the wiki can
no longer be rebuilt from raw and ask once more; then honor the answer.

Close the interview by restating every decision in one compact table and asking for a
single confirmation. Corrections here are cheap; corrections after generation are not.

## Phase 3 — Write the kickoff document

Read `references/kickoff-template.md`. Fill every `{{placeholder}}` from Phases 1 and 2,
follow the `<!-- AUTHOR: -->` notes, delete the notes, and write the result to
`docs/ATLAS-KICKOFF.md` in the target repo (create `docs/` if needed). If that file
already exists, write `docs/ATLAS-KICKOFF-<date>.md` and say so.

Quality bar for the document:

- §Non-negotiable constraints are enforceable rules with rationale, one per line, each
  marked accepted or waived. Each becomes a `DEC-` entry and a line in the generated
  skills' guardrails.
- §Selected capabilities names the exemplar for each, its detected prerequisites, and the
  files generation will write. Blocked capabilities appear in their own list with the
  missing prerequisite.
- §Sources and routing holds the user's real folder names and routing seeds (domains,
  channel names, board names, repo names) exactly as they will be written to the configs.
- §Generation plan is a literal file list, target-repo-relative, so the user can review
  what will be created before it is created.

Show the user a compact summary (vault, scheme, capability count, constraint list, file
count, where the kickoff lives). If invoked with `--write-only`, stop here and tell the
user to run `/atlas-kickoff --resume` when ready. Otherwise ask one question: execute now
(recommended) or pause to edit the kickoff first.

## Phase 4 — Execute the kickoff

Treat `docs/ATLAS-KICKOFF.md` as the brief. Do these in order; each step is independent
so a failure in one is reported, not fatal to the rest.

1. **Config.** Build the JSON from §Vault layout and config using the schema in
   `references/kickoff-template.md`. `no_nudge` holds the round 2 answer as a JSON list
   of vault-relative folder paths and `#tags` (`[]` when the answer was none). Write it
   to `<repo>/atlas.config.json` (committed copy) and to `~/.config/atlas/config.json`.
   If the latter exists, show a diff and ask before overwriting; never overwrite
   silently.
2. **Engine.** Copy `${CLAUDE_PLUGIN_ROOT}/engine/` to `<repo>/engine/` if the target
   repo does not already have one (ask if it does). Generated skills and schedulers call
   `python3 <repo>/engine/<name>/<script>.py`, never a path inside the plugin cache, which
   moves on every plugin update. Note in the report that the engine is stdlib-only and must
   stay that way for headless runs.
3. **Skills.** For each selected capability, read its exemplar in full and write
   `<repo>/skills/<name>/SKILL.md` adapted to this vault: replace every folder placeholder
   with the config key reference or the real folder name, replace the owner placeholder,
   substitute `{{skills_root}}` with the target repo root (the exemplars already spell
   `{{skills_root}}/engine/<name>/<script or state file>` and
   `{{skills_root}}/skills/<name>/SKILL.md`, so scripts, state, and routing files
   resolve to `<repo>/engine/<name>/`, where the scripts read and write them, and
   skill references to `<repo>/skills/<name>/`), remove steps for sources the user did not
   select, and encode the accepted non-negotiables and the no-nudge list in a
   `## Guardrails` section. Frontmatter must carry `derived-from: <exemplar name>` and a
   `description` that keeps the exemplar's trigger phrases. Skip anything whose
   prerequisite was not detected. In every derived file (these skills, `DECISIONS.md`,
   the vault documents, the routing configs) write `{{vault_root}}` and `{{skills_root}}`
   in home-relative form (`~/Vault`, `~/atlas`) whenever the root lies under the home
   directory; only the config, the kickoff, the runbook, and scheduler files hold the
   expanded roots (template section 10).
4. **Routing configs.** For each selected source with a config, copy
   `exemplars/configs/<file>.example.<ext>` to `<repo>/engine/<script-dir>/<file>.<ext>`, next
   to the script that reads it (`atlas-github-ingest/github-repos.yaml`,
   `atlas-wiki-materialize/entity_seeds.json`, `atlas-gmail-ingest/mailbox-routing.yaml`,
   `atlas-slack-ingest/slack-routing.yaml`, `atlas-monday-ingest/monday-boards.yaml`,
   `atlas-fireflies-ingest/meeting-routing.yaml`; the engine has no config-path flag), and replace
   the fictional entries with the user's real values from §Sources and routing. Keep the
   file's comments; they document the format. `entity_seeds.json` gets the user's product
   and technology seeds, or the example's structure with an empty list.
5. **Vault scaffold.** Copy `${CLAUDE_PLUGIN_ROOT}/vault-scaffold/` into the vault,
   renaming folders per the config. Rules: create folders that do not exist (the
   scaffold's `.gitkeep` markers are never copied); skip any
   folder that exists and has content; copy `.obsidian/` files only when the vault has no
   `.obsidian/` directory (otherwise write them to `<vault>/60 - Meta/atlas-obsidian-config/`
   equivalent under `{{folders.meta}}` and tell the user to merge by hand); write
   templates only if the destination file is absent. Rewrite the folder names inside
   `.obsidian/plugins/*/data.json` and `.obsidian/app.json` to the config values. Copy
   `exemplars/vault/{AGENTS,Guide,Getting-Started,Onboarding-Playbook}.md.template` into
   the vault root with placeholders filled, again only if absent. `raw-README.md.template`
   and `wiki-index.md.template` are not copied: the scaffold already placed `raw/README.md`
   and `wiki/index.md`, and the template versions show the populated shape the
   materializer produces, not first-run content.
6. **Plugin checklist.** Copy `vault-scaffold/PLUGIN-CHECKLIST.md` into
   `<vault>/{{folders.meta}}/PLUGIN-CHECKLIST.md`, marking each plugin already present in
   `community-plugins.json` as installed and pruning plugins no selected capability needs
   (per `references/obsidian-plugins.md`).
7. **Decisions.** Write `<repo>/docs/DECISIONS.md` seeded with one `DEC-` entry per
   non-negotiable (accepted or waived), one for the layout scheme, one for the scheduler,
   and one for the no-nudge list. Append-only from here on.
8. **Scheduler.** Read the one matching `references/schedulers/` guide and produce what it
   says: task prompts for desktop scheduled tasks, plist files for launchd, or
   `docs/RUNBOOK.md` for manual. On-demand-only users get the manual runbook regardless,
   because every job is also runnable by hand.
9. **Index.** Write `<repo>/README.md` (or append a section if one exists) listing every
   generated skill with its exemplar, every config, the scheduler choice, and the
   verification command.

Parallelize step 3 across capabilities with subagents when more than four skills are
selected; give each subagent the exemplar path, the config, the constraint list, and the
no-nudge list. Write the config and DECISIONS yourself first so subagents can read them.

## Phase 5 — Verify and report

1. If `<repo>/engine/atlas-health/` or `<repo>/engine/atlas-lint/` has a script, run it
   read-only against the vault (`--vault <path>` with `--dry-run` or its report mode) and
   summarize the counts. If neither exists yet, say the engine track has not landed and
   skip.
2. `python3 -c` import check of `<repo>/engine/_shared/atlas_config.py` with
   `ATLAS_CONFIG=<repo>/atlas.config.json`, printing `vault_root` and each folder path, to
   prove the config resolves.
3. Check every generated `SKILL.md` has `derived-from` naming an exemplar that exists.
4. Scrub self-check over the generated tree, adapted from section 7 of the plugin's
   `docs/SCRUB-RULES.md` (skip and say so if that file is absent). Two classes of file:
   **derived** files came from an exemplar, a vault template, or the scaffold
   (`skills/*/SKILL.md`, `docs/DECISIONS.md`, the routing configs under `engine/<dir>/`,
   the vault's `AGENTS.md` and guides, `{{folders.meta}}/Templates/`, the plugin
   checklist, `.obsidian/`); the **user's own** files hold their absolute roots on
   purpose (`atlas.config.json` and its `~/.config/atlas/` copy, `docs/ATLAS-KICKOFF.md`,
   `docs/RUNBOOK.md`, `schedulers/`, `README.md`). Check both classes against the
   banned-name and identifier categories (section 7 rules 1 and 2); the user's own name,
   employer, and domain are expected in their repo and are not banned. Apply the
   home-path rule (an expanded macOS or Linux home prefix, or a local-file URL) to
   derived files only: they carry `~/...` roots by construction, so a hit there is a leak from an exemplar or a
   substitution done wrong. The repo gate's maintainer-specific home-relative patterns
   and the user's own absolute paths never fail this step.
5. Print the report: files generated by category with paths; capabilities blocked and why;
   and the manual steps that remain. Installing Obsidian community plugins is always
   manual: say so plainly and point at the checklist in the vault. Scheduler steps that
   need the user (creating desktop tasks from the right working directory, `launchctl
   load`, granting Full Disk Access) go here too.

Do not run any generated skill or scheduled job yourself. The user runs the first job
after reviewing the output.

## Guardrails

- Never write into an existing vault folder that has content without asking first. Empty
  folders and missing folders are fair game; anything with files gets a question.
- Never delete or overwrite a note, a config, or a plugin setting. "Overwrite" is always
  a question with a diff, never a default.
- Never read note bodies during diagnosis or generation. Folder names and file counts are
  enough; the vault is personal data.
- Every generated skill traces to one exemplar by name in `derived-from`. No skill is
  invented from scratch during kickoff; a capability without an exemplar is a feature
  request, not a generation target.
- A capability whose prerequisite is missing generates nothing. Say what to install and
  how to re-run for just that capability (`/atlas-kickoff --resume` after connecting it).
- The no-nudge list lives in the config as `no_nudge` and is repeated in every briefing
  and dashboard skill's guardrails. A folder, tag, or practice the user asked to keep
  silent never appears in generated output, scheduled or not, and no scheduled job is
  ever generated on its behalf.
- Waived constraints are recorded as waived, with the reason, in the kickoff and in
  `DECISIONS.md`. Nothing decided in the interview is dropped silently.
- Plain, declarative tone in every generated file. No emojis, no filler, American English.
