# Changelog

All notable changes to atlas-kit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `atlas-gemini-meetings-ingest`: exemplar skill and engine script for Google Meet's
  "Notes by Gemini" documents. Lists the docs in Google Drive through the Drive MCP (or
  reads a folder of markdown exports), and writes one `raw/gemini/meetings/` record per
  meeting with the summary, decisions, next steps, details, quick notes, and attendee
  emails. The transcript stays in the doc and is fetched on demand (DEC-032). Cross-links
  to Fireflies and Wispr records of the same call. Rewrites a record when the doc changes.
  Ships with a stdlib test suite. `atlas-nightly` runs it fourth, after the Wispr meetings
  ingest; the kickoff catalog, engine README, and raw-layer READMEs list it.
- `atlas-teams-meetings-ingest`: exemplar skill and engine script for Microsoft Teams
  meeting transcripts. Reads exported `.vtt` and `.docx` files from the folders listed in
  `teams-sources.json` (a download folder or a synced OneDrive Recordings folder), with no
  MCP required, because Graph access to transcripts is off by default in most tenants.
  Writes one `raw/teams/meetings/` record per meeting with the full transcript (an
  explicit exception to DEC-032, since no API can be relied on to re-fetch it) and an
  optional notes sidecar for a pasted Copilot recap. Cross-links to Fireflies, Wispr, and
  Gemini records. Ships `exemplars/configs/teams-sources.example.json` and a stdlib test
  suite. `atlas-nightly` runs it fifth.

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
- `exemplars/`: 24 scrubbed exemplar `SKILL.md` files (ingest, spine, query, briefing,
  orchestrator, capture), 6 vault document templates (`AGENTS.md`, `Guide.md`,
  `Getting-Started.md`, `Onboarding-Playbook.md`, raw-layer README, wiki index), 6 example
  routing and seed configs, and the design-decision register (`DECISIONS.md`) that the
  exemplars cite. Every generated skill traces back to one exemplar by name.
- `engine/`: the stdlib-only Python engine behind the spine (wiki materialize, emerge,
  graduate, synthesize, lint, health) and the file-based ingests, with a config layer
  (`engine/_shared/atlas_config.py`) that resolves `$ATLAS_CONFIG`, then
  `~/.config/atlas/config.json`, then built-in defaults, so the same scripts run against
  any vault layout. Includes `atlas.config.example.json`. The config's `no_nudge` list
  names the folders and tags that must never appear on a nudge surface; briefing and
  dashboard skills honor it (DEC-027).
- `vault-scaffold/`: a default PARA folder tree with numbered prefixes, `raw/` and `wiki/`
  layers with their invariant READMEs, an `.obsidian/` configuration with settings for
  each community plugin the pipeline expects, five note templates (daily, meeting,
  person, project, weekly review), and `PLUGIN-CHECKLIST.md` for the by-hand plugin installs.
- `docs/SCRUB-RULES.md`: the banned-string categories, placeholder vocabulary, fictional
  example world, and verification gate that everything in the kit and everything the
  kickoff generates must pass.
- `docs/LAYOUT-CONTRACT.md`: the repository layout and config-layer contract.
- `docs/RELEASE-CHECKLIST.md`: the pre-publication checklist, including the scrub gate
  and the whole-history identity check.
- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` so the repository
  installs as a Claude Code plugin from its own marketplace.

### Not included

- The maintainer's personal reflection-practice skill is not part of the kit and may
  appear later as a separate add-on plugin.
