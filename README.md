# atlas-kit

atlas-kit is a Claude Code plugin that sets up an Obsidian second brain with an automated
knowledge pipeline. It interviews you about your vault, your sources, and your
non-negotiables, then generates a suite of skills, configs, and a scheduler setup tailored
to that vault, in your own repository. The pipeline it produces mirrors your sources into an
append-only raw layer, materializes a regenerable wiki on top of it, and answers questions
with citations back to the evidence.

## Who it is for

People who already live in Obsidian and Claude Code, keep their working life across
several systems (meetings, email, chat, boards, repositories, dictation), and want one
vault that ingests all of it on a schedule without anyone hand-copying notes. You should
be comfortable running `python3` scripts and reading a generated `SKILL.md`; you do not
need to write either.

## What you get

| Part | What it is |
|---|---|
| `skills/atlas-kickoff/` | The one skill this plugin installs. `/atlas-kickoff` diagnoses your machine, interviews you, writes a kickoff document, and executes it. Everything else below is material the kickoff reads or copies. |
| `exemplars/` | 25 scrubbed exemplar skills (ingest, spine, query, briefing, orchestrator, capture, practice), 6 vault document templates, 6 example routing configs, and the design-decision register they cite. Nothing here runs; it is the design source every generated skill is derived from. |
| `engine/` | A stdlib-only Python engine for the spine (wiki materialize, emerge, graduate, synthesize, lint, health) and the file-based ingests, with a config layer so the same scripts run against any folder layout. The kickoff copies it into your repo. |
| `vault-scaffold/` | A default PARA folder tree, `raw/` and `wiki/` layers with their invariant READMEs, an `.obsidian/` configuration with per-plugin settings, four note templates, and a plugin checklist. The kickoff copies it into your vault, renaming folders to match your answers. |

## Prerequisites

Required:

- **Obsidian**, installed, with a vault you can point at (or a folder where one should be created).
- **Claude Code** (CLI or the desktop app). The desktop app additionally offers its built-in scheduler as a scheduling option.
- **python3 3.10 or newer** on your `PATH`. The engine is standard-library only; nothing is installed with `pip`.

Optional, by source. Each unlocks one ingest; missing ones are skipped, not faked:

| Source | Prerequisite |
|---|---|
| Meeting transcripts | Fireflies MCP server |
| Email | Gmail MCP server |
| Chat | Slack MCP server |
| Boards | Monday.com MCP server |
| Apple Notes | Apple Notes MCP server (macOS) |
| Repositories | `gh` CLI, authenticated (`gh auth status` exits 0) |
| Claude Code session history | a non-empty `~/.claude/projects/` |
| Calendar in the morning briefing | a calendar MCP server |
| Scheduled runs from the desktop app | the app's scheduled-tasks MCP |

## Install

Two commands, inside Claude Code:

```
/plugin marketplace add alecmandla/atlas-kit
/plugin install atlas-kit@atlas-kit
```

The first registers this repository as a marketplace named `atlas-kit`; the second
installs the plugin of the same name from it. Restart the session or run
`/reload-plugins`, and `/atlas-kickoff` is available.

## First run

Open Claude Code in the repository where you want the generated skills to live (create an
empty one if you have none), then:

```
/atlas-kickoff
```

Give it a vault path as an argument if you want to skip detection:
`/atlas-kickoff ~/path/to/vault`.

**Diagnosis, no questions.** The skill looks for your vault, checks that Obsidian and
`python3` are present, detects which MCP servers and CLIs are connected, reads which
exemplars those unlock, and inspects the target repo. It reads folder names and file
counts in the vault, never note contents.

**Interview, at most three rounds.** Round one asks who the vault is for, its one-paragraph
mission, the folder scheme (numbered PARA by default), and confirms the timezone. Round
two shows the full folder table for edits, offers only the sources whose prerequisites were
detected, lets you choose the non-ingest capabilities, and asks which practices, folders,
or tags must never appear on any nudge surface. Round three settles on-demand versus
scheduled, the scheduler, and the non-negotiables: raw is append-only, the wiki is
regenerable from raw, every generated claim carries a resolvable wikilink, nothing
generated ever deletes a note, transcripts stay summary-only on disk. Each can be accepted
or explicitly waived with a reason; nothing is dropped silently. Every question names its
default so you can accept it in a word.

**Generation.** The kickoff writes `docs/ATLAS-KICKOFF.md` in your repo and, once you
confirm, produces:

- `atlas.config.json` in the repo and `~/.config/atlas/config.json` (asked before overwriting an existing one)
- `engine/` copied into the repo
- `skills/<name>/SKILL.md` for each selected capability, each carrying `derived-from: <exemplar>`
- routing configs (`github-repos.yaml`, `mailbox-routing.yaml`, and so on) and `entity_seeds.json`, each next to the engine script that reads it, seeded with your real routing values
- the vault scaffold in your vault, only into folders that are empty or missing
- `docs/DECISIONS.md` with one entry per decision made in the interview
- a scheduler setup: desktop task prompts, launchd plists, or a manual runbook
- a `README.md` index of everything generated, with the verification command

**Obsidian plugins are installed by hand.** Obsidian only installs community plugins from
inside the app. The kickoff writes each plugin's settings into `.obsidian/plugins/` and
leaves `PLUGIN-CHECKLIST.md` in your meta folder; you work through the checklist once
(Templater, Dataview, and Periodic Notes are the required ones), then run the first
pipeline job from your runbook.

## What works where

Be honest with yourself about the tiers before you commit to a scheduler.

- **Spine skills work on any vault.** Wiki materialize, emerge, graduate, synthesize, lint,
  and health need only `python3` and the folder layout from your config. They run on
  macOS, Linux, and Windows under WSL.
- **Ingests are opt-in and gated on prerequisites.** Each one needs the MCP server or CLI
  listed above, connected in the same Claude session the kickoff runs in. The kickoff
  generates nothing for a source it cannot detect, and tells you what to connect and how to
  re-run for just that source.
- **Voice memos and Wispr dictation are macOS-only and experimental.** They read local
  application data (a dictation database, the Voice Memos library), voice memos need Full
  Disk Access granted to whatever runs the job, and both depend on application internals
  that can change without notice. Expect to babysit them.
- **Scheduling depends on runtime.** Desktop scheduled tasks need the desktop app; launchd
  is macOS-only; the manual runbook works everywhere and is always generated, because every
  job is also runnable by hand.

## Updating

```
claude plugin marketplace update atlas-kit
claude plugin update atlas-kit@atlas-kit
```

Then restart the session. Updating the plugin never touches your repo or your vault: the
generated skills, the copied `engine/`, and your configs are yours and stay where they are.
When a new version changes an exemplar you care about, re-run `/atlas-kickoff --resume`
against your existing kickoff document to regenerate, and review the diff before keeping
it.

## Privacy

- The kit ships no vault content. Every exemplar, template, and example config was scrubbed
  to a fictional world (an analytics consultancy, two inns, a vendor) and gated against a
  banned-pattern list before it was committed. `docs/SCRUB-RULES.md` describes the rules.
- Generated configs stay in your repo. Routing rules, entity seeds, and the kickoff
  document contain your real domains, channels, boards, and names; they are written to your
  target repository and never sent anywhere by this plugin.
- The config file lives in your home config directory, at `~/.config/atlas/config.json`
  (or wherever `$ATLAS_CONFIG` points). It holds your vault path, display name, timezone,
  and folder names, nothing else.
- The kickoff never reads note bodies during diagnosis or generation, and no generated
  skill deletes a note.

## Design lineage

atlas-kit is a scrubbed export of a private, single-user pipeline that ran for long enough
to accumulate opinions. The ones that survived are encoded as invariants:

- **An append-only raw layer.** Every source is mirrored into `raw/<source>/`, one file per
  item, written once and never edited. Corrections are new files with a forward pointer;
  deletions forfeit the rebuild guarantee and are recorded as decisions.
- **A regenerable wiki spine.** Everything under `wiki/` (entities, graduated concepts,
  thread syntheses) is a projection of `raw/` plus your own notes. Delete it, re-run, get
  it back. A marked editable region on each page survives regeneration.
- **Citation gates.** A generated line that states a fact links to the file it came from,
  and the synthesize and research skills refuse to write a page whose wikilinks do not
  resolve. Auditors surface findings; they never decide.
- **No-nudge practices.** Reflection and other on-demand practices are never scheduled and
  never appear on a briefing, dashboard, or weekly review. The interview asks what must stay
  silent and encodes the answer in every briefing skill's guardrails.

The full register is in `exemplars/decisions/DECISIONS.md`; the kickoff copies it into
your repo as the starting `docs/DECISIONS.md`, and every decision you reject in the
interview is marked superseded there rather than removed.

## Contributing

Open an issue describing the vault shape or source you are working with before sending a
change; most fixes belong in an exemplar, and an exemplar change has to hold for every
vault the kickoff might generate. Before a pull request:

1. Read `docs/SCRUB-RULES.md`. Nothing under `exemplars/`, `engine/`, `skills/`, or
   `vault-scaffold/` may contain a real name, organization, identifier, or home-directory
   path; use the fictional world already established there.
2. Run the gate from section 7 of that file. It must print `gate clean`.
3. Run `claude plugin validate .` from the repository root.
4. Use American English spelling and a plain, declarative tone.

## License

MIT. See `LICENSE`.
