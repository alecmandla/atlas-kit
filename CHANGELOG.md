# Changelog

All notable changes to atlas-kit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 - 2026-09-22

First public release.

### Added

- `skills/atlas-kickoff`: the one shipped skill. Diagnoses the machine and vault without
  asking, interviews the user in at most three rounds, writes a tailored
  `docs/ATLAS-KICKOFF.md` in the user's repo, then executes it to generate vault-specific
  skills, routing configs, an `atlas.config.json`, a decisions register, a plugin
  checklist, and a scheduler setup. Ships its own interview method, kickoff template,
  workflow reference, Obsidian plugin guide, and three scheduler guides (desktop
  scheduled tasks, launchd, manual runbook) under `references/`.
- `exemplars/`: 25 scrubbed exemplar `SKILL.md` files (ingest, spine, query, briefing,
  orchestrator, capture, practice), 6 vault document templates (`AGENTS.md`, `Guide.md`,
  `Getting-Started.md`, `Onboarding-Playbook.md`, raw-layer README, wiki index), 6 example
  routing and seed configs, and the design-decision register (`DECISIONS.md`) that the
  exemplars cite. Every generated skill traces back to one exemplar by name.
- `engine/`: the stdlib-only Python engine behind the spine (wiki materialize, emerge,
  graduate, synthesize, lint, health) and the file-based ingests, with a config layer
  (`engine/_shared/atlas_config.py`) that resolves `$ATLAS_CONFIG`, then
  `~/.config/atlas/config.json`, then built-in defaults, so the same scripts run against
  any vault layout. Includes `atlas.config.example.json`.
- `vault-scaffold/`: a default PARA folder tree with numbered prefixes, `raw/` and `wiki/`
  layers with their invariant READMEs, an `.obsidian/` configuration with settings for
  each community plugin the pipeline expects, four note templates (daily, meeting,
  person, project), and `PLUGIN-CHECKLIST.md` for the by-hand plugin installs.
- `docs/SCRUB-RULES.md`: the banned-string categories, placeholder vocabulary, fictional
  example world, and verification gate that everything in the kit and everything the
  kickoff generates must pass.
- `docs/LAYOUT-CONTRACT.md`: the repository layout and config-layer contract.
- `docs/RELEASE-CHECKLIST.md`: the pre-publication checklist, including the scrub gate
  and the whole-history identity check.
- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` so the repository
  installs as a Claude Code plugin from its own marketplace.
