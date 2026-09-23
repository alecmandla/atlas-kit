# ATLAS-KICKOFF template

The parameterized skeleton for `docs/ATLAS-KICKOFF.md` in the user's target repo. The
skill fills every `{{placeholder}}` from the diagnostic and the interview, follows the
`<!-- AUTHOR: -->` notes, deletes them, and writes the result. Sections are never
omitted; an inapplicable section keeps its header and a one-line reason.

The invariant parts (section order, the constraint format, the config schema, the
generation rules) are not parameterized. Vaults differ in content, not in how the
kickoff works.

---

# TEMPLATE BEGINS

# ATLAS-KICKOFF.md — Bootstrap the knowledge pipeline for {{owner_name}}'s vault

> **How to use this file:** run `/atlas-kickoff --resume` from the root of this repo,
> or paste everything below the next rule into a fresh Claude Code session at the same
> location. It is a single self-contained brief: what to generate, where, and how to
> know it is done. Generated on {{date}} from a {{n_rounds}}-round interview.

---

# 1. Mission

{{mission_paragraph}}

<!-- AUTHOR: The user's own paragraph from Round 1, lightly edited for grammar only.
Keep the concrete question they named ("what did we agree with X across..."); that
question is the acceptance test for the whole pipeline. -->

# 2. Vault layout and config

- **Vault:** `{{vault_root}}` ({{vault_state}})
- **Scheme:** {{layout_scheme}}
- **Target repo:** `{{repo_root}}` (`{{skills_root}}` in the exemplars: `skills/<name>/SKILL.md` and `engine/<name>/` live under it)
- **Runtime:** {{runtime}} ({{claude_cli_state}})

<!-- AUTHOR: vault_state is one of "existing, N top-level folders" or "new, will be
created". runtime is "Claude desktop app" or "Claude Code CLI". repo_root comes from the
invocation (--repo, else the current directory) and is the value substituted for every
{{skills_root}} in the exemplars; it is never asked in the interview. -->

| Key | Folder | Exists | Notes |
|---|---|---|---|
{{folder_table_rows}}

<!-- AUTHOR: one row per key from the interview's folder table. "Exists" is yes / no /
has content. Rows marked "has content" are never written into without a question. -->

The config the engine reads, written to `~/.config/atlas/config.json` and committed as
`atlas.config.json`:

```json
{
  "vault_root": "{{vault_root}}",
  "owner_name": "{{owner_name}}",
  "timezone": "{{timezone}}",
  "folders": {
    "inbox": "{{folders.inbox}}",
    "daily": "{{folders.daily}}",
    "projects": "{{folders.projects}}",
    "areas": "{{folders.areas}}",
    "resources": "{{folders.resources}}",
    "archive": "{{folders.archive}}",
    "meta": "{{folders.meta}}",
    "attachments": "{{folders.attachments}}",
    "crm": "{{folders.crm}}",
    "clippings": "{{folders.clippings}}",
    "raw": "{{folders.raw}}",
    "wiki": "{{folders.wiki}}"
  }
}
```

<!-- AUTHOR: every key is present even when the name equals the default; the engine's
defaults exist for the maintainer's private checkout, not for users. vault_root may use
a leading ~ ; the engine expands it. Folder values are the top-level folder names only;
the subfolders below them (Clients/, People/, Dashboards/, Templates/, entities/) are
engine conventions. In particular `crm` names the parent folder and person notes live
at `<crm>/People/`, so `crm` must not itself be `People`. -->

# 3. Non-negotiable constraints

These were decided in the interview. Each becomes a `DEC-` entry in `docs/DECISIONS.md`
and a line in the `## Guardrails` section of every generated skill it touches.

{{constraints_list}}

<!-- AUTHOR: numbered; format per workflow-reference.md:

N. **<rule, imperative>** <rationale>
   Status: accepted | waived (<reason>)

Always include the five defaults (append-only raw, regenerable wiki, citation gate,
never delete, summary-only transcripts), then the no-nudge entry, then any user-added
rules from Round 3.4. The no-nudge entry reads:

N. **Nothing under {{no_nudge_list}} appears on any nudge surface.** Nudge surfaces
   are the morning report, weekly review, dashboards, and any scheduled output. Skills
   for these practices are on-demand only and never wired to a scheduler.
   Status: accepted (list: ...) | accepted (list: none)

A waived constraint stays in the list. Do not renumber around it. -->

# 4. Selected capabilities

| Capability | Exemplar | Prerequisites detected | Generates |
|---|---|---|---|
{{capability_rows}}

<!-- AUTHOR: one row per selected capability. Prerequisites column names the tool or
file that was found (e.g. "Gmail MCP: search_threads, list_labels"). Generates column is
the repo-relative file list: skills/<name>/SKILL.md plus any config. Group rows: ingest,
spine, query, briefing, orchestrator, capture, practice. -->

Blocked (nothing is generated for these until the prerequisite exists):

{{blocked_list}}

<!-- AUTHOR: "- `atlas-<name>`: needs <what>. Re-run `/atlas-kickoff --resume` after
connecting it." Write "- none" if nothing is blocked. -->

# 5. Sources and routing

{{routing_sections}}

<!-- AUTHOR: one subsection per selected source that has a config, holding the real
values that go into it. For each: the config file path, then the entries as the user
gave them, e.g. for meetings a list of "title keyword or attendee domain -> folder,
tags"; for email "domain or label -> folder"; for chat "channel -> folder"; for boards
"workspace or board -> folder"; for repos "owner, repo list". Unknown values are
written as {{fill-me}} and listed again in §9. Sources without a config get one line:
"- claude history: no routing; writes raw/claude-history/<project>/". -->

# 6. Scheduler

- **Mode:** {{schedule_mode}}
- **Scheduler:** {{scheduler_choice}}
- **Working directory for jobs:** `{{repo_root}}`
- **Python:** `{{python3_path}}` ({{python3_version}})

| Job | Capability | Cadence | Needs a Claude session |
|---|---|---|---|
{{job_rows}}

<!-- AUTHOR: schedule_mode is "scheduled" or "on-demand only". For on-demand, the table
still lists suggested cadences and the scheduler is "manual". "Needs a Claude session"
is yes for anything that calls an MCP or writes prose (ingests via MCP, morning, weekly,
health, synthesize, nightly); no for pure engine scripts (people-extract, materialize,
emerge, auto-graduate, lint, local-file ingests). Default cadences: nightly 22:00, synthesize 22:45,
morning 08:00, weekly Fri 18:00, health Sun 21:00. Practice capabilities never appear
here. -->

# 7. Generation plan

Files to create, relative to `{{repo_root}}` unless marked vault:

```
{{generation_tree}}
```

<!-- AUTHOR: the literal tree. Canonical shape:

atlas.config.json
docs/ATLAS-KICKOFF.md            (this file)
docs/DECISIONS.md
docs/RUNBOOK.md                  (manual scheduler, or always as a fallback)
README.md                        (new, or a section appended)
engine/                          (copied from the plugin; stdlib-only)
engine/<script-dir>/<routing config, one per selected source>
skills/<one per selected capability>/SKILL.md
schedulers/<plists or task prompts, per §6>
vault: <folders created>, .obsidian/ (only if absent), {{folders.meta}}/Templates/*,
       {{folders.meta}}/PLUGIN-CHECKLIST.md, {{folders.meta}}/Dashboards/,
       raw/README.md, wiki/index.md, AGENTS.md and the other vault templates (only if absent)

Rules applied during generation, restate them here so the executing session sees them:
- never write into a vault folder that has content without asking
- never overwrite; a diff and a question, or a sibling file with a date suffix
- every SKILL.md carries derived-from
- blocked capabilities generate nothing -->

# 8. Verification

Run after generation, from `{{repo_root}}`:

```bash
ATLAS_CONFIG=./atlas.config.json python3 -c "import sys; sys.path.insert(0, 'engine/_shared'); import atlas_config as c; cfg = c.load(); print(cfg.vault_root); [print(k, cfg.folder(k)) for k in {{folder_keys_list}}]"
{{verification_commands}}
grep -L '^derived-from:' skills/*/SKILL.md   # must print nothing
```

<!-- AUTHOR: verification_commands holds the engine's health or lint invocation in
read-only mode if those scripts exist (e.g. python3 engine/atlas-lint/lint.py
--vault "{{vault_root}}" report; the flag goes before the subcommand), else a comment saying the engine has not landed. -->

# 9. Manual steps

{{manual_steps}}

<!-- AUTHOR: numbered. Always includes:
1. Install the community plugins listed in {{folders.meta}}/PLUGIN-CHECKLIST.md
   (Settings, Community plugins, Browse). This cannot be automated.
2. Scheduler-specific steps from the chosen references/schedulers/ guide (create desktop
   tasks from this directory; launchctl load each plist; or nothing for manual).
3. Fill every {{fill-me}} in the routing configs under engine/<script-dir>/.
4. Grant Full Disk Access to the runner if voice memos was selected.
5. Run the first job by hand and check its artifacts (raw/ file count, last-run.md),
   not its timestamp.
-->

# 10. Tone for generated files

Plain, declarative, no filler, no emojis, American English. Headings for navigation,
tables for comparison, lists for enumeration. Code fenced with language tags. Paths
relative to the repo root or the vault root, never absolute.

# TEMPLATE ENDS
