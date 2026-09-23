# Obsidian plugins the pipeline expects

Derived from the maintainer's vault configuration, with every folder path replaced by
its config key. Community plugins are installed by hand: Settings, Community plugins,
turn off Restricted mode, Browse, search the name, Install, Enable. Core plugins are
toggled under Settings, Core plugins. Neither can be automated from outside Obsidian;
the kickoff writes the checklist and the settings files, and the user does the clicks.

The scaffold ships `.obsidian/plugins/<id>/data.json` for each community plugin so the
settings below are already in place the moment the plugin is installed. Obsidian reads
`data.json` when the plugin loads; a file present before installation is picked up.

## Community plugins

| Plugin (id) | Required | Needed by | Why | Install |
|---|---|---|---|---|
| Templater (`templater-obsidian`) | required | morning briefing, weekly review, meeting ingest, distill, the vault templates | The scaffold templates use Templater syntax (`<% tp.date.now() %>`). Folder templates auto-apply the project and person templates on new notes. | Community plugins, Browse, "Templater" |
| Dataview (`dataview`) | required | dashboards, project and person templates, weekly review | Templates and dashboards embed Dataview queries (open tasks per project, recent meetings per person). Without it those blocks render as code. | Community plugins, Browse, "Dataview" |
| Periodic Notes (`periodic-notes`) | required | morning briefing, daily-note template | Owns the daily-note folder layout (`{{folders.daily}}/YYYY/YYYY-MM/`) and template so the briefing finds today's note where Obsidian would create it. | Community plugins, Browse, "Periodic Notes" |
| Tasks (`obsidian-tasks-plugin`) | optional | morning briefing (overdue tasks), weekly review (shipped tasks) | The briefing reads task lines in the Tasks emoji format (due and done dates). Without it the overdue and shipped lists are empty but nothing breaks. | Community plugins, Browse, "Tasks" |
| Tag Wrangler (`tag-wrangler`) | optional | graduate (thread tags) | Renaming `#thread/<slug>` tags across the vault when a graduated pattern gets a better name. Convenience only. | Community plugins, Browse, "Tag Wrangler" |
| Omnisearch (`omnisearch`) | optional | research (human side) | Full-text search with better ranking than the core search; useful when checking a research answer by hand. The pipeline never calls it. | Community plugins, Browse, "Omnisearch" |

## Core plugins

All of these are on in the scaffold's `core-plugins.json`. The ones the pipeline relies
on:

| Core plugin | Required | Needed by | Why | Toggle |
|---|---|---|---|---|
| Daily notes | required | morning briefing | Fallback creator of today's note when Periodic Notes is absent. Folder and template are set in `daily-notes.json`. | Core plugins, "Daily notes" |
| Templates | required | all templates | Core template folder pointer (`templates.json`) so the Insert template command finds `{{folders.meta}}/Templates/`. | Core plugins, "Templates" |
| Backlinks, Outgoing links | required | research, wiki | The wiki spine is navigated by wikilinks in both directions. | Core plugins |
| Properties | required | every generated note | Frontmatter editing; the engine writes frontmatter on every raw and wiki file. | Core plugins, "Properties view" |
| Bases | optional | dashboards | Table views over frontmatter; a lighter alternative to Dataview for simple lists. | Core plugins, "Bases" |
| Graph, Tag pane, Outline, Bookmarks, Word count, File recovery, Page preview, Slash commands, Canvas, Note composer, Command palette, Switcher, File explorer, Global search, Editor status | optional | none | On by default; harmless. | Core plugins |

Off in the scaffold: Markdown importer, ZK prefixer, Random note, Workspaces, Sync,
Publish, Footnotes, Slides, Audio recorder, Web viewer. Turn on what you like; nothing
generated depends on them.

## Settings the scaffold writes, in generic form

The kickoff rewrites each path to the user's real folder name when copying the scaffold.

### app.json

| Setting | Value | Why |
|---|---|---|
| `attachmentFolderPath` | `{{folders.attachments}}` | Pasted images land in one place. |
| `newFileLocation` / `newFileFolderPath` | `folder` / `{{folders.inbox}}` | New notes start in the inbox and get routed later. |
| `alwaysUpdateLinks` | `true` | Renames keep wikilinks valid; the wiki depends on it. |
| `useMarkdownLinks` | `false` | Wikilinks everywhere; the engine resolves `[[...]]`. |
| `newLinkFormat` | `shortest` | Links by basename; the engine's citation gate resolves basenames. |
| `trashOption` | `system` | Deletions go to the OS trash, recoverable. |
| `livePreview` / `defaultViewMode` | `true` / `source` | Editing defaults; personal preference, safe to change. |

### Templater (`templater-obsidian/data.json`)

| Setting | Value |
|---|---|
| `templates_folder` | `{{folders.meta}}/Templates` |
| `trigger_on_file_creation` | `false` (folder templates apply on demand) |
| `enable_folder_templates` | `true` |
| `folder_templates` | `{{folders.projects}}` → `Project-Note.md`; `{{folders.crm}}/People` → `Person-Note.md` |
| `enable_system_commands` | `false` (templates never shell out) |

### Periodic Notes (`periodic-notes/data.json`)

| Setting | Value |
|---|---|
| daily `format` | `YYYY-MM-DD` |
| daily `folder` | `{{folders.daily}}/YYYY/YYYY-MM` |
| daily `template` | `{{folders.meta}}/Templates/Daily-Note.md` |
| weekly | disabled in the scaffold; enable it and point `template` at a weekly template if you want one. The weekly review capability writes its own note under `{{folders.areas}}/Weekly-Reviews/` and does not need this. |

### Dataview (`dataview/data.json`)

Defaults plus: `enableDataviewJs: true`, `enableInlineDataview: true`,
`taskCompletionTracking: true` with `completion` as the field name and `yyyy-MM-dd` as
the date format, `renderNullAs: "-"`, `refreshInterval: 2500`. The task-completion
settings are what let the weekly review count shipped tasks.

### Tasks (`obsidian-tasks-plugin/data.json`)

`taskFormat: tasksPluginEmoji`, `setDoneDate: true`, `globalFilter` empty. The done-date
stamp is what the weekly review reads.

### Omnisearch, Tag Wrangler

Shipped with their defaults. Nothing in the pipeline reads their settings.

### Core: `daily-notes.json` and `templates.json`

`daily-notes.json`: `folder` = `{{folders.daily}}`, `template` =
`{{folders.meta}}/Templates/Daily-Note`, `format` = `YYYY-MM-DD`.
`templates.json`: `folder` = `{{folders.meta}}/Templates`.

## What is never copied from a real vault

`workspace.json` (open panes and recent files), `bookmarks.json`, `graph.json`,
`appearance.json`, and any plugin `data.json` that stores account tokens or history.
The scaffold contains none of these; a user's existing `.obsidian/` is never
overwritten (see the skill's Phase 4 rules).
