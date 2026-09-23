# atlas-kit layout contract

This file is the agreement between the three build tracks. Each track writes only into the paths it owns. Nothing here is user-facing documentation; that lives in README.md once the tracks land.

## What atlas-kit is

A Claude Code plugin that helps someone set up an Obsidian second brain with an automated knowledge pipeline, modeled on the maintainer's private Atlas suite. It does not ship the maintainer's vault, routing rules, or identity. It ships:

1. A kickoff skill that interviews the user, writes a tailored kickoff prompt, and executes it to generate vault-specific skills.
2. Scrubbed exemplar skills and vault documents the kickoff reads as its design source.
3. The Python engine (ingest, emerge, graduate, synthesize, lint, health) with a config layer so it runs against any vault layout.
4. A vault scaffold: folder tree, Obsidian config, templates, plugin checklist.

## Repository layout

```
atlas-kit/
├── .claude-plugin/
│   └── plugin.json                  # manifest; written at export (step 5)
├── skills/
│   └── atlas-kickoff/               # TRACK C owns this tree
│       ├── SKILL.md
│       └── references/
│           ├── interview.md         # grill-style interview script and question bank
│           ├── kickoff-template.md  # parameterized AI-KICKOFF skeleton
│           ├── workflow-reference.md# bundled, trimmed dougs-workflow model
│           ├── grilling.md          # bundled, trimmed grill-me method
│           ├── obsidian-plugins.md  # required/optional plugins with one-line reasons
│           └── schedulers/
│               ├── desktop-scheduled-tasks.md
│               ├── launchd.md
│               └── manual.md
├── exemplars/                       # TRACK B owns this tree
│   ├── README.md                    # what an exemplar is; scrub rules applied
│   ├── skills/
│   │   └── <atlas-skill-name>/
│   │       └── SKILL.md             # scrubbed copy, second person, no real names
│   ├── vault/
│   │   ├── AGENTS.md.template       # vault constitution with {{placeholders}}
│   │   ├── Guide.md.template
│   │   ├── Getting-Started.md.template
│   │   └── Onboarding-Playbook.md.template
│   ├── configs/
│   │   ├── meeting-routing.example.yaml
│   │   ├── mailbox-routing.example.yaml
│   │   ├── slack-routing.example.yaml
│   │   ├── monday-boards.example.yaml
│   │   ├── github-repos.example.yaml
│   │   └── entity_seeds.example.json
│   └── decisions/
│       └── DECISIONS.md             # only the design invariants, scrubbed
├── engine/                          # TRACK A output; copied here at export
│   ├── _shared/
│   │   └── atlas_config.py          # the config layer module
│   ├── atlas.config.example.json
│   └── <atlas-skill-name>/*.py
├── vault-scaffold/                  # TRACK C owns this tree
│   ├── .obsidian/
│   │   ├── community-plugins.json
│   │   ├── core-plugins.json
│   │   └── app.json
│   ├── 00 - Inbox/.gitkeep          # default PARA tree; kickoff renames per interview
│   ├── ...
│   ├── raw/README.md
│   └── wiki/index.md
├── docs/
│   ├── LAYOUT-CONTRACT.md           # this file
│   └── SCRUB-RULES.md               # Track B writes; Track C's generated output must obey too
├── README.md                        # written at export
├── LICENSE                          # MIT, written at export
└── CHANGELOG.md                     # written at export
```

## Config layer contract (Track A)

The engine reads one JSON file. Stdlib only. If the file is absent, every default must reproduce the maintainer's current runtime behavior exactly, so the private `atlas` checkout keeps working with no config file present.

```
Resolution order:
  1. $ATLAS_CONFIG               explicit path
  2. ~/.config/atlas/config.json
  3. built-in defaults
```

Schema (all keys optional):

```json
{
  "vault_root": "~/Obsidian-Vault",
  "owner_name": "",
  "timezone": "America/Los_Angeles",
  "folders": {
    "inbox":       "00 - Inbox",
    "daily":       "10 - Daily Notes",
    "projects":    "20 - Projects",
    "areas":       "30 - Areas",
    "resources":   "40 - Resources",
    "archive":     "50 - Archive",
    "meta":        "60 - Meta",
    "attachments": "99 - Attachments",
    "crm":         "CRM",
    "clippings":   "Clippings",
    "raw":         "raw",
    "wiki":        "wiki"
  }
}
```

Public API of `_shared/atlas_config.py`:

```python
load() -> Config                 # cached; reads once
Config.vault_root: Path
Config.folder(key: str) -> Path  # absolute path under vault_root
Config.owner_name: str
Config.timezone: str
```

Scripts import it via `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))`. Every `Path.home() / "Obsidian-Vault"`, every hardcoded `/Users/...`, and every literal `"20 - Projects"`-style string in Python must go through this module. `--vault` CLI flags stay and override the config.

## Scrub rules (Track B writes docs/SCRUB-RULES.md; all tracks obey)

Nothing in atlas-kit may contain: the maintainer's name, employer, email addresses or domains, coworker or client names, real Monday/Slack/GitHub identifiers, real meeting titles, or file paths under `/Users/<anyone>`. Placeholders use `{{owner_name}}`, `{{vault_root}}`, `{{folders.projects}}` style. Example config entries use obviously fictional organizations.

## Kickoff output contract (Track C)

The generated skills land in the recipient's own repo, not in atlas-kit. The kickoff writes `skills/<name>/SKILL.md` files, an `atlas.config.json`, per-source routing configs, and a scheduler setup matching the chosen option. Each generated SKILL.md must be traceable to one exemplar by name in a `derived-from:` frontmatter field.

## Merge order

Tracks A, B, C land on separate branches. A merges into the private `atlas` repo. B and C merge into `atlas-kit` main. Step 4 (scratch-vault test) and step 5 (export: copy engine, write manifest, README, LICENSE, CHANGELOG) run serially after all three land.
