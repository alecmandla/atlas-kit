# Scratch-vault test (Track F)

A first-time user installs atlas-kit and runs `/atlas-kickoff` against an empty vault under a
throwaway `HOME`. The run is non-interactive: the interview answers come from a fictional
profile, and `docs/scratch-test/generate.py` stands in for the Claude session that executes
Phases 3 and 4 of `skills/atlas-kickoff/SKILL.md`. Everything else (Phase 1 detection, the
engine runs in Phase 5, validation, the scrub gate) is executed as the skill instructs. Where
the skill's text disagreed with what exists in `exemplars/`, `engine/`, or `vault-scaffold/`,
that is a finding below.

## Rerun

```bash
sh docs/scratch-test/run.sh /path/to/an/empty/scratch/dir
```

Needs `python3` 3.10+, the `claude` CLI, and the git-ignored `docs/scrub-patterns.local.txt`.
The script creates `<scratch>/home/` (the test `HOME`), `<scratch>/home/Vault/` (the vault),
`<scratch>/home/sam-atlas/` (the target repo), two synthetic Claude Code sessions under
`<scratch>/home/.claude/projects/`, then generates, runs Phase 5, checks write scope,
validates the generated skills, and gates the output. It exits 1 on the first failure and
prints `scratch test passed` at the end. The real `HOME`, `~/.config`, and any real vault are
never read or written; the script proves it with a `find -newer` marker and a `git status` of
the kit tree.

`docs/scratch-test/generate.py --as-written` reproduces the pre-fix behavior (routing
configs under `configs/`, every vault template dropped at the vault root) for comparison.
The harness may run with uncommitted kit work; its kit-tree check compares against the
status taken before the run.

## Profile

| Item | Value |
|---|---|
| Owner, timezone | Sam Rivera, `America/Chicago` (detected zone differed; user corrected it) |
| Vault | `<scratch>/home/Vault`, empty directory, no `.obsidian/` |
| Layout | PARA without numbered prefixes: `Inbox`, `Daily`, `Projects`, `Areas`, `Resources`, `Archive`, `Meta`, `Attachments`, `CRM` (the `crm` key; it was `People` before F-15, which put the engine's fixed `People/` subfolder at `People/People/`), `Clippings`, `raw`, `wiki` |
| Mission | "A place where meeting notes, GitHub activity, and my own dictation accumulate into a wiki I can ask questions of." No acceptance question given. |
| Sources | GitHub via `gh` (no owner or repo list given); Claude Code session history (two synthetic sessions). Everything else declined or not connected. |
| Capabilities | spine on; morning, weekly, nightly on; book summaries off |
| No-nudge list | `Areas/Journal` and the tag `#journal`, written to the config's `no_nudge` key (the interview's round 2.4) |
| Schedule | on-demand only; scheduler = manual |
| Non-negotiables | all five accepted |
| Never again | "A tool once rewrote my daily note headers." |
| Target repo | `<scratch>/home/sam-atlas`, fresh `git init` |

The full question-by-question mapping, with the gaps it exposed, is summarized in F-14,
F-22, and F-23 below.

## Environment

macOS (Darwin 25.2), zsh, `python3` 3.11.7 (anaconda), `gh` 2.96.0 authenticated (used only
to confirm the CLI exists; no repo was ever queried), `claude` 2.1.280, git 2.50.1, `launchctl`
present, Obsidian installed, `rg` present. No `mcp__scheduled-tasks__*` tools, so the runtime is
Claude Code CLI. The session's tool list did expose Fireflies, Gmail, and Slack connectors
(their tool names match the catalog's substrings), which the profile declines; Monday exposed
only an `authenticate` tool, so `get_board_items_page` was correctly absent. Repo state at the
start: commit `578a693`.

`claude plugin validate .` and `claude plugin validate skills --strict` both print
`Validation passed` before and after the fixes. The former reports "Validating marketplace
manifest" only.

## Phases

| Phase | Result | Notes |
|---|---|---|
| 1 Diagnose | PASS after F-01 | The vault glob in step 1 aborted under zsh (`no matches found`) before the fix. Every other command ran. The scratch `HOME` had no `~/.config/atlas/config.json`, so the vault came from the invocation argument. |
| 2 Interview | N/A (profile) | Mapped every question in `references/interview.md` to the profile. Gaps: F-14, F-22, F-23. |
| 3 Kickoff document | PASS | Every `{{placeholder}}` in `kickoff-template.md` resolved; only `{{fill-me}}` remains, by design. §Mission carries an `OPEN:` marker for the missing acceptance question, per `grilling.md`. |
| 4 Execute | PASS after F-03, F-05, F-06, F-08 | 14 skills generated after F-21 (2 ingest, 7 spine, 2 query, 2 briefing, 1 orchestrator), each with `derived-from` and a `## Guardrails` section encoding the five constraints, the no-nudge list, and the user's rule. Config written to the repo and to `<HOME>/.config/atlas/config.json`, with `no_nudge` as a list; the harness asserts the list is there and that the generated morning and weekly skills name it. Engine copied. Scaffold copied with the profile's folder names rewritten into every `.obsidian` JSON. Checklist, DECISIONS (DEC-035 to DEC-043), RUNBOOK, README written. |
| 5 Verify | PASS after F-06, F-07, F-09 | Config import prints the vault, all 12 folders, and the `no_nudge` list; `test_atlas_config.py` 15/15. `lint.py --vault <vault> report` exit 0. Claude-history ingest: dry run `scanned=2 written=2`, execute `written=2`, second execute `written=0 already=2` (idempotent). GitHub: `parse_repos_yaml()` reads the generated file (owner and one repo, both `{{fill-me}}`); no `gh` call made. `materialize.py --dry-run-report` exit 0 with empty seeds. `emerge.py --dry-run` and a real run exit 0 (2 items scanned, 0 patterns). `auto_graduate.py` plan mode exit 0, writes only the review-queue dashboard. |
| Write scope | PASS | Files newer than the marker exist only under the scratch repo, vault, logs, `<HOME>/.config/atlas/`, and the synthetic sessions. The real `~/.config` mtime is unchanged and has no `atlas/` entry. The kit worktree shows no changes after the runs. |
| Generated skills | PASS | `claude plugin validate <repo>/skills --strict` accepts the path and passes. `grep -L '^derived-from:'` prints nothing; every `derived-from` names an existing exemplar. |
| Scrub gate | PASS | Repo gate (section 7 script, scope widened to `exemplars/ engine/ skills/ vault-scaffold/`): `gate clean` after every commit. Generated output (python3 gate in `run.sh` since G-01): name and identifier rules over every file with the scratch directory's own name normalized; the home-path rule over the 40 derived files only, unnormalized, which pass because derived files carry `~/...` roots since F-12. |

## Findings

Severity: blocker = the kickoff cannot complete or its output cannot run; major = a selected
capability fails on first run or the user is misled; minor = cosmetic, or a gap with an easy
workaround.

| ID | Severity | Where | What | Status |
|---|---|---|---|---|
| F-01 | blocker | `SKILL.md` Phase 1 step 1 | `ls -d ~/*Obsidian* ...` aborts under zsh when any glob is unmatched; `2>/dev/null` does not help. Detection reported zero vaults on macOS. | fixed `cd3a2c2` |
| F-02 | major | `exemplars/skills/*/SKILL.md` frontmatter | `requires` listed `mcp/scheduled-tasks` on morning, weekly, health, synthesize, nightly (it only schedules them); nightly listed every ingest MCP and `gh`; research listed Fireflies and ripgrep, which its own prose calls optional. Since the kickoff gates on `requires`, a CLI user with a manual runbook was blocked from the spine and briefings. Contradicted the kickoff's own catalog and SCRUB-RULES section 6. | fixed `fce0f1f` |
| F-03 | major | morning, nightly, weekly, people-extract exemplars; `atlas.config.example.json` docs | Templates read from `{{folders.resources}}/Templates/`; the scaffold, Templater and Periodic Notes settings, `obsidian-plugins.md`, and the checklist put them under `{{folders.meta}}/Templates/`. Morning and nightly hard-fail on the missing daily template. The practice exemplar and its script, since removed under F-10, still read the resources folder. | fixed `dc87c3b` |
| F-04 | major | `SKILL.md` catalog; `interview.md` 2.3 | `atlas-synthesize` is part of the spine per README, the runbook, and the template cadences, but the catalog and the interview never offered it, so it was never generated. | fixed `d855978` |
| F-05 | major | `schedulers/manual.md`, `launchd.md`, `desktop-scheduled-tasks.md`; `SKILL.md` step 3 | Runbook invoked `engine/atlas-health/health.py`, which does not exist (health is prose-only). All three guides verified script jobs by `skills/<name>/last-run.md`; the engine writes `last-run.md` and `state.json` next to the script under `engine/<name>/`. Step 3 said nothing about where `{{skills_root}}/<name>/<state file>` maps. | fixed `e6c2d11` |
| F-06 | blocker | `SKILL.md` step 4; `kickoff-template.md` §7, §9; `README.md` | Routing configs were written to `<repo>/configs/`, but every engine script resolves its config as `Path(__file__).parent / <file>` with no flag. Reproduced: `github-repos.yaml not found`; `FileNotFoundError` on `entity_seeds.json`. | fixed `0613d1f` |
| F-07 | minor | `kickoff-template.md` §8 and §6 author note | Example `lint.py report --vault ...` is rejected by argparse (flag goes before the subcommand). Health listed as a pure-script job. | fixed `310fb02` |
| F-08 | minor | `SKILL.md` step 5 | "Copy `exemplars/vault/*.md.template` into the vault root" put `raw-README.md` and `wiki-index.md` at the root next to the scaffold's `raw/README.md` and `wiki/index.md` (the two sources also diverge: the template wiki index holds fictional populated entries). `.gitkeep` markers were copied into a vault that is not git-tracked (DEC-011). | fixed `648f614` |
| F-09 | major | `engine/atlas-wiki-materialize/materialize.py` | Crashed at startup when `entity_seeds.json` is absent; every consumer already uses `seeds.get(...)`. Now warns and runs with no seeds. | fixed `0480c30` |
| F-10 | major | the practice exemplar (since removed), interview 2.3 | The interview offered "a reflection practice folder that is never scheduled", but the only practice exemplar depended on templates and slash commands the kit does not ship, hardcoded its own folder and tag, and read templates from the resources folder. The profile's `Areas/Journal` could not be generated as a working skill; only the no-nudge exclusion could be encoded. | resolved by decision: practice skill not shipped; `no_nudge` config key added |
| F-11 | major | `atlas-weekly` exemplar; `vault-scaffold/` | Weekly hard-fails without `Weekly-Review.md`; the scaffold ships four templates and no weekly one. Listed as a manual step in the generated kickoff. | fixed `714ad0f` |
| F-12 | major | `SKILL.md` Phase 5 step 4; `SCRUB-RULES.md` section 7 rule 3 | The kickoff runs the gate over generated output "to catch maintainer identifiers", but rule 3 greps for macOS home-directory paths, and the generated `atlas.config.json`, `RUNBOOK.md`, and `ATLAS-KICKOFF.md` carry the user's absolute vault and repo paths, so the step fails for every macOS user. The harness normalizes the scratch path to show the content itself is clean. | fixed `164e1a1`, `89d5cb7` |
| F-13 | minor | `SCRUB-RULES.md` section 2; exemplars | `{{skills_root}}` means "the directory holding script, state, and SKILL.md" in the exemplars, but the kit splits scripts and state into `engine/<name>/` and SKILL.md into `skills/<name>/`. F-05 documents the mapping for script and state files; session skills still write `last-run.md` under `skills/<name>/`, so two conventions coexist. | fixed `94b9b24` |
| F-14 | minor | `interview.md`; `SCRUB-RULES.md` section 2 | No question feeds `{{owner_email}}`, `{{employer}}`, `{{employer_domain}}` (used by the meeting and email exemplars), and nothing states how `{{owner_slug}}` and `{{skills_root}}` are derived. This profile needed only the last two (derived as `Sam-Rivera` and `<repo>/skills`). | fixed `fe0df00` |
| F-15 | minor | config schema; engine conventions | `crm` renamed to `People` yields person notes at `People/People/` because the `People/` subfolder is a fixed engine convention; the Templater folder template is written as `People/People` accordingly. | fixed `e775ead`, `89d5cb7` (engine keeps the subfolder; `crm` names the parent) |
| F-16 | minor | `exemplars/vault/AGENTS.md.template`; `engine/atlas-lint` | On an empty vault, lint reports four "tagged but too thin" threads and one duplicate-slug group: the constitution's illustrative `#thread/portal-rewrite` examples count as real tags. Emerge and morning exclude `AGENTS.md`; lint does not. | fixed `2fc243d` |
| F-17 | minor | `engine/atlas-claude-history-ingest`, `engine/atlas-emerge` | Dry-run reports print acceptance checks from the maintainer's corpus ("≥ 100 session summaries on first run: FAIL", "AC ≥ 5 patterns unmet"); every new user fails them on day one. | fixed `cd46d51` |
| F-18 | minor | `vault-scaffold/.obsidian`, `SKILL.md` step 6 | Chronos ships in `community-plugins.json` with settings but is "needed by none" in `obsidian-plugins.md`, so step 6 prunes it from every checklist while its settings are still copied. | fixed `27c2752` |
| F-19 | minor | `vault-scaffold/60 - Meta/Templates/Meeting-Note.md`, `Person-Note.md` | Templates use `attendees:`; DEC-003, DEC-032, the engine, and `atlas-frontmatter-migrate` use `participants:`. The Person-Note Dataview query filters on `attendees`. | fixed `acf9ad4` |
| F-20 | minor | `kickoff-template.md` §10 vs §2, §6 | "Paths relative to the repo root or the vault root, never absolute" contradicts the absolute `vault_root` and `repo_root` the same template requires and the exemplars' `{{vault_root}}/...` usage. | fixed `bd1646d` |
| F-21 | minor | `SKILL.md` catalog | `atlas-people-extract` exists as an exemplar and an engine script, and materialize's org branch consumes its output, but the catalog and interview never mention it. | fixed `2cd025b` |
| F-22 | minor (profile) | interview 1.2 | The mission names meeting notes and dictation, both declined, and gives no acceptance question; the kickoff carries `OPEN:` in §Mission as `grilling.md` prescribes. | recorded |
| F-23 | minor (profile) | interview 2.2 | No GitHub owner or repos, so the config holds `{{fill-me}}` and a manual step. Phase 5's "confirm the dry run parses the repo list" can only be checked through `parse_repos_yaml()`: the script's dry run still calls `gh`. | recorded |
| F-24 | minor | `atlas-morning` exemplar | References a `## Scratch` section absent from the scaffold's daily template; harmless (falls through to end of file). | fixed `5b4ca1a` |
| G-01 | minor | `docs/scratch-test/run.sh` | The gate step called the system grep with `-P` for every local pattern; BSD grep has no `-P`, so macOS printed a usage message per pattern and tested nothing. The gate now runs in python3. | fixed `5387098` |

Observations that are not defects: `atlas_config.py` resolves `~/.config` through `Path.home()`
and ignores `XDG_CONFIG_HOME`, matching what README documents; the exemplars' DEC citations
all resolve in `exemplars/decisions/DECISIONS.md`; the `## Overview` / `## Action Items`
headings inside DEC-032 are within a fenced code block.

## Commits

| Commit | Finding |
|---|---|
| `cd3a2c2` fix(atlas-kickoff): make vault detection work under zsh | F-01 |
| `fce0f1f` fix(exemplars): declare only prerequisites a skill itself calls in requires | F-02 |
| `dc87c3b` fix(exemplars): read note templates from the meta folder | F-03 |
| `d855978` fix(atlas-kickoff): offer atlas-synthesize as part of the spine | F-04 |
| `e6c2d11` fix(atlas-kickoff): point scheduler guides at files that exist | F-05 |
| `0613d1f` fix(atlas-kickoff): write routing configs next to the engine script that reads them | F-06 |
| `310fb02` fix(atlas-kickoff): correct the lint invocation and health's job type in the template | F-07 |
| `648f614` fix(atlas-kickoff): name the vault templates that are copied and skip .gitkeep | F-08 |
| `0480c30` fix(engine): run materialize without entity_seeds.json instead of crashing | F-09 |
| `27c2752` fix(vault-scaffold): drop Chronos, which no capability needs | F-18 |
| `acf9ad4` fix(vault-scaffold): standardize note templates on participants | F-19 |
| `5b4ca1a` fix(vault-scaffold): add the Scratch section the morning exemplar expects | F-24 |
| `714ad0f` feat(vault-scaffold): ship the Weekly-Review template atlas-weekly needs | F-11 |
| `e775ead` fix(atlas-kickoff): name crm as the parent of the fixed People subfolder | F-15 |
| `2fc243d` fix(engine): skip the vault constitution in lint as morning does | F-16 |
| `cd46d51` fix(engine): replace maintainer-corpus acceptance checks with neutral run summaries | F-17 |
| `2cd025b` fix(atlas-kickoff): offer atlas-people-extract in the catalog and interview | F-21 |
| `94b9b24` fix(exemplars): define skills_root as the target repo root with engine/ and skills/ beneath it | F-13 |
| `fe0df00` feat(atlas-kickoff): ask for owner email and employer as optional follow-ups | F-14 |
| `bd1646d` docs(atlas-kickoff): say which generated files hold absolute roots | F-20 |
| `5387098` fix(scratch-test): make the generated-output gate portable off grep -P | G-01 |
| `164e1a1` fix(atlas-kickoff): apply the home-path rule only to files derived from exemplars | F-12 |
| `89d5cb7` fix: keep path literals out of the skill text and the engine README | F-12, F-15 |

Plus this document and `docs/scratch-test/` in the commit that follows the Track F rows.
F-22 and F-23 stay open for the maintainer: the two profile-specific gaps are design
decisions, not mechanical fixes. F-10 was closed by decision (Track H): the practice
exemplar and its engine script are gone, the interview no longer offers a practice
capability, and the round 2.4 answer lands in the config's `no_nudge` list, which the
engine reads (`Config.no_nudge`), the morning and weekly exemplars honor, and the harness
asserts.
