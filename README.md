# atlas-kit

atlas-kit is a Claude Code plugin that turns an Obsidian vault into a second brain with an
automated knowledge pipeline. You run one command, answer an interview about your vault
and your sources, and it generates a set of skills, configs, and a scheduler tailored to
you, in your own repository. From then on your meetings, email, chat, boards, repositories,
and dictation land in the vault on a schedule, get distilled into a wiki, and can be
queried with citations back to the evidence.

```
sources ──ingest──▶ raw/ ──emerge / graduate──▶ wiki/ (entities, concepts, syntheses)
                                                   │
briefings (morning, weekly, health) read the vault ┘   research answers questions with citations
```

## Who it is for

People who already live in Obsidian and Claude Code, keep their working life across
several systems, and want one vault that ingests all of it without hand-copying notes. You
should be comfortable running a `python3` script and reading a generated `SKILL.md`. You
do not need to write either.

## What to expect

**Time.** Install takes two minutes. The kickoff interview and generation take twenty to
forty minutes, most of it answering questions. Working through the Obsidian plugin
checklist takes another ten. The first pipeline run is a few minutes on an empty vault.

**What changes in your vault.** New top-level folders `raw/` and `wiki/` appear, plus
whichever PARA folders you asked for that did not already exist. Existing folders with
content are never written into. A meta folder gains templates, a plugin checklist, and a
dashboards folder. Nothing generated ever deletes or edits a note you wrote.

**What changes on your machine.** A config file at `~/.config/atlas/config.json` with your
vault path, name, timezone, and folder names. A repository of your choosing gains
`skills/`, `engine/`, `docs/`, and a runbook. If you choose a scheduler, its job
definitions are written there too and you install them by hand.

**What it feels like after.** Each morning your daily note carries a briefing: calendar,
overdue tasks, active threads. Each night the ingests run, the wiki is regenerated, and
new recurring patterns are listed for you to promote or ignore. On Fridays a weekly review
is drafted. When you want an answer, `/atlas-research <question>` drills through the wiki
down to the raw evidence and cites every claim. When you want a thought captured,
`/atlas-distill` files the current conversation as a linked note.

**Honest tiers.** The spine (wiki, emerge, graduate, synthesize, lint, health, research,
distill) works on any vault with only `python3`. Each ingest is opt-in and needs one
connected service, listed below; missing ones are skipped, never faked. Voice memos and
Wispr dictation are macOS-only and experimental. Expect to babysit those two.

## Prerequisites

### Required

| What | Why |
|---|---|
| Obsidian with a vault, or a folder where one should be created | The pipeline writes into it |
| Claude Code, CLI or desktop app | Runs the kickoff and the session-driven skills |
| `python3` 3.10 or newer on your `PATH` | The engine. Standard library only, nothing installed with `pip` |
| A git repository for the generated skills, empty is fine | The kickoff writes there |

Required Obsidian community plugins. The kickoff writes their settings; you install them
from inside Obsidian:

| Plugin | Needed by |
|---|---|
| Templater | Daily, meeting, person, project, and weekly templates |
| Dataview | Dashboards, project and person pages, weekly review |
| Periodic Notes | Daily-note folder and template, morning briefing |

### Recommended

| What | Why |
|---|---|
| Tasks plugin | Overdue tasks in the morning briefing, shipped tasks in the weekly review |
| Tag Wrangler | Renaming thread tags across the vault when a pattern graduates |
| Omnisearch | Better full-text search when you research by hand |
| `rg` (ripgrep) | Faster research over large vaults; the engine falls back to Python without it |

### Per source, optional

Each unlocks one ingest. The kickoff detects what is connected in the session it runs in
and offers only those.

| Source | Prerequisite |
|---|---|
| Meeting transcripts | Fireflies MCP server |
| Meeting notes from Google Meet (Gemini) | Google Drive MCP server, or markdown exports of the notes docs |
| Microsoft Teams meeting transcripts | a folder of exported `.vtt` or `.docx` transcripts, or a synced OneDrive Recordings folder |
| Zoom meeting summaries and transcripts | any of: the Zoom for Claude connector (attended runs); a Zoom Marketplace app for unattended runs, which may need an admin to grant you developer access; or a folder of `.vtt` transcripts downloaded from the Zoom portal. Summaries need AI Companion meeting summaries turned on |
| Gong call notes and transcripts | a Gong API key, which only a Gong technical admin can issue, plus your own email address so the ingest keeps only your calls; or a folder of transcripts you download from each call page |
| Email | Gmail MCP server |
| Chat | Slack MCP server |
| Boards | Monday.com MCP server |
| Repositories | `gh` CLI, authenticated |
| Claude Code session history | a non-empty `~/.claude/projects/` |
| Apple Notes | Apple Notes MCP server, macOS |
| Wispr dictation | Wispr Flow installed, macOS |
| Voice memos | macOS with Full Disk Access granted to the runner |
| Calendar in the morning briefing | a calendar MCP server |
| Scheduled runs from the desktop app | the app's scheduled-tasks MCP |

## Setup

### Step 1. Install the plugin

Inside Claude Code:

```
/plugin marketplace add alecmandla/atlas-kit
/plugin install atlas-kit@atlas-kit
```

Restart the session or run `/reload-plugins`. The `/atlas-kickoff` command is now
available.

### Step 2. Connect your sources first

Connect whichever MCP servers you want ingested before you run the kickoff, in the same
Claude session. Authenticate `gh` if you want repositories. The kickoff only offers sources
it can see. You can add a source later with `/atlas-kickoff --resume` after connecting it.

### Step 3. Run the kickoff

Open Claude Code in the repository where the generated skills should live, then:

```
/atlas-kickoff
/atlas-kickoff ~/path/to/vault       # skip vault detection
/atlas-kickoff --write-only          # stop after writing the kickoff document, execute later
/atlas-kickoff --resume              # execute an existing kickoff document
```

The interview runs in at most three rounds. Every question states its default so you can
accept it in a word.

1. **Identity and shape.** What generated notes should call you, the one-paragraph
   mission, your folder scheme (numbered PARA by default, or PARA without numbers,
   Zettelkasten, or custom), and your timezone.
2. **Folders, sources, and silence.** The full folder table for edits, which detected
   sources to ingest, which non-ingest capabilities to include, and which folders, tags, or
   practices must never appear in any briefing, dashboard, or scheduled output.
3. **Operation and non-negotiables.** Scheduled or on-demand, which scheduler, and five
   invariants to accept or explicitly waive with a reason: raw is append-only, the wiki is
   regenerable from raw, every generated claim carries a resolvable wikilink, nothing
   generated deletes a note, transcripts stay summary-only on disk. It also asks what a
   tool once did to your notes that you never want repeated; that becomes a guardrail in
   every generated skill.

### Step 4. Review the kickoff document, then let it execute

The kickoff writes `docs/ATLAS-KICKOFF.md` in your repo and shows it to you before doing
anything else. Read it. It is the contract for everything generated next.

### Step 5. Work through the Obsidian plugin checklist

Obsidian only installs community plugins from inside the app. Open the vault, open
`PLUGIN-CHECKLIST.md` in your meta folder, and install each listed plugin from Settings,
Community plugins, Browse. Their settings are already written, so each one works as soon
as it is enabled.

### Step 6. Run the first job and verify

Your repo's `docs/RUNBOOK.md` lists one command per job and what each writes. Run the
health check first, then one ingest, then wiki materialize. Verify by looking at the files
that appeared under `raw/` and `wiki/`, never by a scheduler's last-run timestamp.

## What the AI sets up, and what you do

| The kickoff does this | You do this |
|---|---|
| Detects your vault, Obsidian, `python3`, MCP servers, and CLIs | Connect the sources you want before running it |
| Interviews you and writes `docs/ATLAS-KICKOFF.md` | Answer the interview and read the document |
| Writes `~/.config/atlas/config.json`, asking before overwriting | Nothing, unless you want to edit folder names later |
| Copies the engine into your repo | Nothing |
| Generates one `SKILL.md` per selected capability, each traceable to its exemplar | Skim them; they are yours to edit |
| Writes routing configs seeded with your real domains, channels, boards, and repos | Fill any `{{fill-me}}` it could not infer, and edit after a misroute |
| Copies the vault scaffold into empty or missing folders, renamed to your scheme | Nothing; existing folders with content are left alone |
| Writes every Obsidian plugin's settings into `.obsidian/plugins/` | Install the plugins from inside Obsidian |
| Seeds `docs/DECISIONS.md` with your interview answers as decisions | Add a decision whenever you change an invariant |
| Writes the scheduler definitions for the option you chose | Install them: create the desktop tasks, load the launchd plists, or run the runbook by hand |
| Runs a read-only health check against the vault | Run the first real jobs from the runbook |

## Using it day to day

| Want to | Do |
|---|---|
| Answer a question from the vault, with citations | `/atlas-research <question>` |
| File the current conversation as a linked note | `/atlas-distill` |
| See what recurring patterns have emerged | Open the Emerging-Patterns dashboard in your meta folder |
| Promote a pattern into a wiki page | `/atlas-graduate <slug>` |
| Refresh a cross-source thread synthesis | `/atlas-synthesize <thread>` |
| Check vault hygiene | `/atlas-lint` or the health job from the runbook |
| Add or fix a routing rule | Edit the yaml next to that ingest's engine script |
| Add a source later | Connect it, then `/atlas-kickoff --resume` |

Scheduled jobs, if you chose them, run nightly at ten for ingests and the wiki, then
synthesis, a morning briefing at eight, a weekly review on Friday evening, and a health
audit on Sunday. All of these are also runnable by hand from the runbook.

## What is in this repository

| Path | What it is |
|---|---|
| `skills/atlas-kickoff/` | The one skill this plugin installs, with its interview, kickoff template, scheduler guides, and plugin reference |
| `exemplars/` | 28 scrubbed exemplar skills, 6 vault document templates, 9 example routing and source configs, and the design-decision register. Nothing here runs; it is what generated skills derive from |
| `engine/` | The stdlib-only Python engine with its config layer. Copied into your repo by the kickoff |
| `vault-scaffold/` | Default PARA tree, `raw/` and `wiki/` READMEs, `.obsidian/` settings, five note templates, the plugin checklist |
| `docs/` | The scrub rules, release checklist, and a reproducible scratch-vault test |

## Updating

```
claude plugin marketplace update atlas-kit
claude plugin update atlas-kit@atlas-kit
```

Restart the session. Updating never touches your repo or your vault; the generated
skills, the copied engine, and your configs stay where they are. To pick up a changed
exemplar, run `/atlas-kickoff --resume` and review the diff before keeping it.

## Privacy

- The kit ships no vault content. Every exemplar, template, and example config was
  scrubbed to a fictional world and checked against a banned-pattern gate before commit.
  `docs/SCRUB-RULES.md` describes the rules.
- Generated configs stay in your repo. Routing rules, entity seeds, and the kickoff
  document contain your real domains, channels, and names; this plugin never sends them
  anywhere.
- The config file holds your vault path, display name, timezone, folder names, and the
  no-nudge list. Nothing else.
- The kickoff reads folder names and file counts during diagnosis, never note bodies.

## Design lineage

atlas-kit is a scrubbed export of a private, single-user pipeline that ran long enough to
accumulate opinions. The ones that survived are invariants:

- **An append-only raw layer.** Every source is mirrored into `raw/<source>/`, one file
  per item, written once. Corrections are new files with a forward pointer.
- **A regenerable wiki.** Everything under `wiki/` is a projection of `raw/` plus your own
  notes. Delete it, re-run, get it back. A marked region on each page survives.
- **Citation gates.** A generated line that states a fact links to the file it came from,
  and the synthesize and research skills refuse to write a page whose links do not resolve.
- **A no-nudge list.** Folders, tags, and practices you name are never scheduled and never
  surface in a briefing, dashboard, or review.

The full register is in `exemplars/decisions/DECISIONS.md`. The kickoff copies it into
your repo as the starting `docs/DECISIONS.md`.

## Not included

The maintainer's personal reflection-practice skill is not part of the kit and may appear
later as a separate add-on. The kit keeps only the no-nudge exclusion.

## Contributing

Open an issue describing the vault shape or source you are working with before sending a
change; most fixes belong in an exemplar, and an exemplar change has to hold for every
vault the kickoff might generate. Before a pull request:

1. Read `docs/SCRUB-RULES.md`. Nothing under `exemplars/`, `engine/`, `skills/`, or
   `vault-scaffold/` may contain a real name, organization, identifier, or home-directory
   path.
2. Run the gate from section 7 of that file. It must print `gate clean`.
3. Run `claude plugin validate .` from the repository root.
4. Use American English spelling.

## License

MIT. See `LICENSE`.
