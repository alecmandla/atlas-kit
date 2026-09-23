# Atlas engine

The Python side of the Atlas suite: the scripts that mirror external sources into
an Obsidian vault's `raw/` layer, grow the wiki spine out of that layer, and
write the dashboards the briefing skills read. Each `atlas-<name>/` directory
holds the code for one skill; the matching `SKILL.md` that tells an agent when
and how to run it lives in `../exemplars/skills/<name>/`. Nothing here contains
the maintainer's data: every fixture, example, and default uses the fictional
world defined in `../docs/SCRUB-RULES.md`.

## Stdlib only

Every `.py` file imports only the Python standard library (decision DEC-021 in
`../exemplars/decisions/DECISIONS.md`). The scripts are launched by schedulers
and agent sessions under the system `python3`, with no virtualenv and no chance
to `pip install`; a third-party import fails silently in a headless run and the
nightly chain stops. So frontmatter, the routing YAML files, and Fireflies
payloads are all parsed with the small purpose-built readers inside each
script, not PyYAML. Two test files try `import yaml` inside `try/except
ImportError` purely as an oracle to compare output against; they skip the
comparison when it is absent.

The one sanctioned exception is `atlas-voice-memos-ingest/helper/`, a compiled
Swift binary (DEC-023): speech-to-text is not in the standard library and
cloud transcription is barred by the local-first rule (DEC-002). It is invoked
by absolute path through `subprocess`, never imported.

## Config layer

Every script resolves vault paths through `_shared/atlas_config.py`, imported
with:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config
CFG = atlas_config.load()
```

Resolution order, first hit wins:

1. `$ATLAS_CONFIG`: an explicit path to a JSON file. Set but missing is an
   error, never a silent fallback.
2. `~/.config/atlas/config.json`, read when present.
3. Built-in defaults: vault at `~/Vault`, with the folder names shown in
   `atlas.config.example.json`.

Copy `atlas.config.example.json` to `~/.config/atlas/config.json` and edit
`vault_root` and, if your layout differs, the `folders` map. The keys (`inbox`,
`daily`, `projects`, `areas`, `resources`, `archive`, `meta`, `attachments`,
`crm`, `clippings`, `raw`, `wiki`) are fixed; the values are the bare folder
names directly under `vault_root`. `no_nudge` is a list of vault-relative
folder paths and `#tags` that briefing and dashboard skills must never surface;
it defaults to empty. Subfolders below those (`Clients/`,
`People/`, `entities/`, `Dashboards/`) are engine conventions and are not
configurable. `crm` therefore names the parent folder: person notes are
written to `<crm>/People/`, so set it to `CRM` or `Contacts`, never to
`People`, which would nest the subfolder under a folder of the same name. Scripts that take `--vault` still do; the flag moves the root
for one run and does not rename the layout. The full contract, the public API,
and the validation rules are in `CONFIG-LAYER.md`.

## Files next to each script

Two kinds of file live beside a script and neither is shipped here:

- **Routing and seed configs**, which you write once from the examples in
  `../exemplars/configs/`: `atlas-gmail-ingest/mailbox-routing.yaml`,
  `atlas-slack-ingest/slack-routing.yaml`, `atlas-monday-ingest/monday-boards.yaml`,
  `atlas-github-ingest/github-repos.yaml`,
  `atlas-fireflies-ingest/meeting-routing.yaml`, and
  `atlas-wiki-materialize/entity_seeds.json`. `atlas-emerge/suppress.txt`
  (one slug per line) is optional.
- **Runtime state**, which the scripts create: `state.json`, `last-run.md`,
  `seen-ledger.json`, and the suite lock. These are git-ignored at the
  repository root.

Most ingest scripts default to a dry run that writes a `--dry-run-report`
markdown file and touches nothing in the vault; pass `--execute` to write.

## Running the scripts

Run each one from inside its own directory (or by absolute path; the config
import does not depend on the working directory). `python3 <script> --help`
prints the full flag list.

### Ingest (external source into `raw/`)

| Script | Run | Needs |
|---|---|---|
| `atlas-fireflies-ingest/summary_record.py` | `python3 summary_record.py apply --payload <meeting.json> [--dry-run]`; also `pending` and `mark-unavailable <id>` | `mcp/fireflies` fetches the payload |
| `atlas-fireflies-ingest/fireflies_state.py` | `python3 fireflies_state.py {guard,since,check-dup,advance,backfill-done}` | none |
| `atlas-fireflies-ingest/link_prep.py` | `python3 link_prep.py --folder <call folder> --call-basename <name> [--dry-run]` | none |
| `atlas-gmail-ingest/ingest.py` | `python3 ingest.py --input-json <threads.json> --labels-json <labels.json> --execute` | `mcp/gmail` fetches the JSON; `mailbox-routing.yaml` |
| `atlas-slack-ingest/ingest.py` | `python3 ingest.py --input-json <page.txt> --channel-id <id> --channel-name <name> --execute` | `mcp/slack` fetches the page; `slack-routing.yaml` |
| `atlas-monday-ingest/ingest.py` | `python3 ingest.py --input-json <items.json> --workspace-id <id> --workspace-name <n> --board-id <id> --board-name <n> --execute` | `mcp/monday` fetches the items; `monday-boards.yaml` |
| `atlas-github-ingest/ingest.py` | `python3 ingest.py --execute [--days 90] [--repo owner/name]` | `cli/gh` (authenticated); `github-repos.yaml` |
| `atlas-apple-notes-ingest/ingest.py` | `python3 ingest.py --input-json <notes.json> --execute --incremental` | `mcp/apple-notes` fetches the JSON (macOS) |
| `atlas-claude-history-ingest/ingest.py` | `python3 ingest.py --execute [--limit N]` | reads `~/.claude/projects/` |
| `atlas-wispr-ingest/ingest.py` | `python3 ingest.py --execute --incremental` | **macOS only**: reads the Wispr Flow SQLite store under `~/Library` |
| `atlas-wispr-meetings-ingest/ingest.py` | `python3 ingest.py --execute --incremental` | **macOS only**: same store, meetings table |
| `atlas-voice-memos-ingest/ingest.py` | `python3 ingest.py --doctor` first, then `python3 ingest.py --execute --incremental` | **macOS only**: Voice Memos container, Full Disk Access, and the built `helper/atlas-transcribe` |
| `atlas-distill/distill.py` | `python3 distill.py --input <exchange.md> --execute` | none |

### Spine (`raw/` into `wiki/`, `CRM/`, and dashboards)

| Script | Run | Needs |
|---|---|---|
| `atlas-people-extract/extract.py` | `python3 extract.py --execute` | none |
| `atlas-wiki-materialize/materialize.py` | `python3 materialize.py --execute [--limit-entities N]` | `entity_seeds.json` |
| `atlas-emerge/emerge.py` | `python3 emerge.py [--window-days 30] [--dry-run]` | none |
| `atlas-graduate/auto_graduate.py` | `python3 auto_graduate.py` (plan) or `--execute` | none |
| `atlas-graduate/graduate.py` | `python3 graduate.py --slug <slug> --target {concept,project,area,resource} [--dry-run]` | none |
| `atlas-synthesize/synthesize.py` | `python3 synthesize.py --list`, `--stale-only`, or `--thread <slug>` | none (the SKILL.md does the writing) |
| `atlas-lint/lint.py` | `python3 lint.py report` or `python3 lint.py worklist` | none |
| `atlas-frontmatter-migrate/migrate.py` | `python3 migrate.py` (dry run) then `--execute` | one-off migration of the legacy attendees email list to `participants:` wikilinks plus `attendee_emails:` |

### Briefing and notes

| Script | Run | Needs |
|---|---|---|
| `atlas-research/research.py` | `python3 research.py search <terms>`, `resolve`, `write` | `cli/ripgrep` speeds up search; falls back to pure Python without it |
| `atlas-book-summary/book_summary.py` | `python3 book_summary.py --input <book.json> --execute` | none |

`atlas-morning`, `atlas-weekly`, `atlas-health`, and `atlas-nightly` have no
Python of their own beyond the lock below; their exemplar `SKILL.md` files
orchestrate the scripts above through the scheduled-tasks MCP.

### Utility

| Script | Run | Needs |
|---|---|---|
| `atlas-nightly/lock.py` | `python3 lock.py acquire` (exit 1 means another run holds it), `release`, `status` | none |
| `atlas-fireflies-ingest/dry_run_monday_tier.py` | `python3 dry_run_monday_tier.py --out <report.md>` | `raw/monday/` already ingested; `meeting-routing.yaml` |
| `atlas-fireflies-ingest/convert_retained_to_summary.py` | `python3 convert_retained_to_summary.py --execute` | one-off DEC-032 conversion |
| `_shared/atlas_config.py` | imported, never run | none |

## Prerequisites by script

`mcp/<server>` means the agent session that drives the skill must have that
MCP server connected; the script itself only reads what the session fetched.
`cli/<tool>` must be on `PATH` for the script.

| Prerequisite | Scripts |
|---|---|
| `mcp/fireflies` | fireflies-ingest (`summary_record.py`); optional for research (transcript escalation) |
| `mcp/gmail` | gmail-ingest |
| `mcp/slack` | slack-ingest |
| `mcp/monday` | monday-ingest |
| `mcp/apple-notes` | apple-notes-ingest |
| `mcp/scheduled-tasks` | none as a hard requirement; the desktop scheduler is one of three ways to run the session skills (morning, weekly, health, nightly, synthesize) |
| `cli/gh` | github-ingest |
| `cli/ripgrep` | research (optional; pure-Python fallback) |
| `cli/python3` 3.9+ | everything |

macOS only: `atlas-wispr-ingest`, `atlas-wispr-meetings-ingest`, and
`atlas-voice-memos-ingest` read application stores under `~/Library`;
`atlas-apple-notes-ingest` depends on an MCP that exists only on macOS. The
voice-memo helper also needs Apple's Swift toolchain with a macOS 26 SDK to
build (`helper/build.sh`; see `helper/README.md` for the doctor runbook).
Everything else runs anywhere `python3` does.

## Defaults to tune for your own world

A few constants encode vocabulary rather than logic. They ship with fictional
values and are worth a look before the first real run:

- `atlas-fireflies-ingest/summary_record.py`: `JUNK_EMAIL_DOMAINS` is empty;
  add your CRM's BCC-to-logger domain so it is dropped from attendee lists.
- `atlas-fireflies-ingest/dry_run_monday_tier.py`: `INTERNAL_EMAIL_DOMAINS`
  (your organization's domains), `STOPWORDS` (industry words that should never
  match alone), and `TIER3_ATTACH_SCAN_DIRS` (your internal-meetings area).
- `atlas-lint/lint.py`: `GENERIC_TOKENS`, industry words ignored when comparing
  slugs for duplicates.
- `atlas-graduate/auto_graduate.py`: `COMMON_FIRST_NAMES` is a starter list for
  the person guard; add first names common in your network, coworkers first.

## Tests

Each test file runs from inside its own directory under the system `python3`,
with temp directories only, and never touches a real vault:

```bash
(cd _shared && python3 test_atlas_config.py)
(cd atlas-fireflies-ingest && python3 test_link_prep.py && python3 test_summary_record.py)
(cd atlas-frontmatter-migrate && python3 test_migrate.py)
(cd atlas-graduate && python3 test_graduate.py)
(cd atlas-people-extract && python3 test_extract.py)
(cd atlas-gmail-ingest && python3 test_ingest.py)
(cd atlas-slack-ingest && python3 -m pytest -q test_ingest.py)
(cd atlas-voice-memos-ingest && python3 -m pytest -q test_ingest.py)
```

The last two also run standalone (`python3 test_ingest.py`) when pytest is not
installed.
