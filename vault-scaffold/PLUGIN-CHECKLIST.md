---
type: checklist
status: manual
---

# Obsidian plugin checklist

The kickoff cannot install plugins for you. Obsidian only installs community plugins
from inside the app, so this list is yours to work through once. The settings for each
plugin are already in place under `.obsidian/plugins/`; installing the plugin is enough
for them to take effect.

**How to install a community plugin:** Settings, Community plugins, turn off Restricted
mode (once), Browse, search the name, Install, then Enable.

**How to toggle a core plugin:** Settings, Core plugins, flip the switch.

## Required

These are needed by at least one generated skill. Without them, templates render as
raw code or the briefings cannot find today's note.

- [ ] **Templater** (`templater-obsidian`) — the daily, meeting, person, project, and
  weekly-review templates use its syntax; folder templates apply the project and person templates
  automatically.
- [ ] **Dataview** (`dataview`) — the project and person templates and the dashboards
  embed Dataview queries.
- [ ] **Periodic Notes** (`periodic-notes`) — decides where today's note lives so the
  morning briefing writes to the same file Obsidian would open.
- [ ] Core: **Daily notes** — on; fallback creator of today's note.
- [ ] Core: **Templates** — on; points the Insert template command at the templates
  folder.
- [ ] Core: **Backlinks** and **Outgoing links** — on; the wiki is navigated by links
  in both directions.
- [ ] Core: **Properties view** — on; every generated note has frontmatter.

## Optional

Nothing breaks without these. Each helps one capability or is a convenience.

- [ ] **Tasks** (`obsidian-tasks-plugin`) — lets the morning briefing list overdue tasks
  and the weekly review count shipped ones.
- [ ] **Tag Wrangler** (`tag-wrangler`) — rename a `#thread/<slug>` tag vault-wide when a
  graduated pattern gets a better name.
- [ ] **Omnisearch** (`omnisearch`) — better full-text search when checking a research
  answer by hand.
- [ ] Core: **Bases** — table views over frontmatter, a lighter alternative to Dataview
  for simple lists.

## After installing

- [ ] Open Settings, Templater, and confirm **Template folder location** shows the
  templates folder under your meta folder. If it is blank, the plugin loaded before its
  settings file existed; set it by hand.
- [ ] Open Settings, Periodic Notes, and confirm the daily-note folder and template
  paths point into your daily-notes and templates folders.
- [ ] Create a new daily note (Command palette, "Open today's daily note"). It should
  contain the Focus, Notes, Tasks, two Atlas report, and Scratch headings.
- [ ] Create a note inside the projects folder. Templater should apply the project
  template on its own; if not, Settings, Templater, Folder templates, check the mapping.

When every required box is checked, run the first pipeline job by hand from your repo's
runbook and check that files appear under `raw/`.
