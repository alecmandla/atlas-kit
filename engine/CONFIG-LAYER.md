# Config layer

This is the design note for the engine's config layer, carried over from the
private engine where it was written. `atlas-transcript-extract`, listed below,
is retired and is not shipped in the kit.

The Atlas Python engine reads one JSON file that says where the Obsidian vault
lives and what its top-level folders are called. Every script resolves vault
paths through `_shared/atlas_config.py`; nothing in `*.py` hardcodes a
home-relative vault path, an absolute home-directory path, or a literal
`"20 - Projects"`-style folder name as a filesystem path.

With no config file present, the built-in defaults give the reference layout
shown below (vault at `~/Vault`, numbered PARA folders). Stdlib only (DEC-021): the module imports `json`, `os`,
`dataclasses`, `pathlib`, `typing`, nothing else.

## Resolution order

First hit wins:

1. `$ATLAS_CONFIG` - explicit path to a JSON file. If it is set and the file
   is missing or unreadable, that is an error (the variable states intent; a
   silent fallback could write into the wrong vault). A blank value counts as
   unset.
2. `~/.config/atlas/config.json` - read when present, silently skipped when
   absent.
3. Built-in defaults.

`load()` resolves once per process and caches the result. `reset()` drops the
cache (tests only).

## Schema

Every key is optional. Unknown keys, and any key beginning with `_`, are
ignored, which is how `atlas.config.example.json` carries `_doc` strings.

```json
{
  "vault_root": "~/Vault",
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
  },
  "no_nudge": []
}
```

Rules enforced at load time (a violation raises `ConfigError` naming the file):

- The top level must be a JSON object; `vault_root`, `owner_name`, `timezone`
  must be strings; `folders` must be an object of strings.
- `vault_root` is `~`-expanded and must not be empty.
- Each folder value is a single directory name: non-empty, no `/` or `\`,
  not `.` or `..`. Subfolders below the top level (`Clients/`, `People/`,
  `entities/`, `Dashboards/`, ...) are engine conventions and are not
  configurable here.
- `no_nudge` is a list of non-empty strings, each a vault-relative folder path
  (`30 - Areas/Journal`) or a tag (`#journal`). Entries are trimmed and lose any
  leading or trailing `/`. The list is what must never appear on a nudge
  surface (morning report, weekly review, dashboards, scheduled output); the
  owner names it in the kickoff interview and it defaults to empty.
- Invalid JSON raises `ConfigError` with the path and the decoder's message.

`atlas.config.example.json` at the repo root shows every key at its default
with a `_doc` explanation. Copy it to `~/.config/atlas/config.json` and edit.

## Public API

```python
atlas_config.load() -> Config          # cached; reads the file at most once
atlas_config.reset() -> None           # drop the cache (tests)
atlas_config.vault_from_arg(v) -> Path # --vault value if given, else load().vault_root

Config.vault_root: Path                # absolute, ~ expanded
Config.owner_name: str
Config.timezone: str
Config.no_nudge: list[str]             # folders and tags never surfaced as nudges; [] by default
Config.source: Path | None             # file the values came from; None = defaults
Config.folder(key) -> Path             # absolute: vault_root / folders[key]
Config.folder_name(key) -> str         # bare name, e.g. "20 - Projects"
Config.rel(key, *parts) -> str         # "60 - Meta/Dashboards/X.md" style string
```

`folder()` is for filesystem paths. `folder_name()` and `rel()` are for text
that goes into notes - Dataview `FROM` clauses, `startswith(file.path, ...)`
predicates, wikilinks, frontmatter values - so that generated prose stays in
step with the folders on disk. Unknown keys raise `KeyError` listing the valid
ones.

## How a script consumes it

At the top of the script, before any other local import:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()

RAW_GMAIL = CFG.folder("raw") / "gmail"                 # filesystem path
PEOPLE_DIR = CFG.folder("crm") / "People"
PARA_DIRS = [CFG.folder_name(k) for k in ("inbox", "projects", "areas", "resources")]
QUEUE_REL = CFG.rel("meta", "Dashboards", "Thread-Review-Queue.md")
```

`parents[1]` is the repo root from any `atlas-*/script.py`, so the import works
from the engine directory, a worktree, or a fresh clone,
regardless of the current working directory.

Scripts that take `--vault` keep the flag; it overrides `vault_root` for that
run. The argparse default is `None` and the value is resolved at runtime:

```python
ap.add_argument("--vault", type=Path, default=None,
                help="vault root (default: vault_root from Atlas config)")
...
vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()   # Path-typed flags
vault = atlas_config.vault_from_arg(args.vault)                # str-typed flags
```

so `--help` never prints a personal path. Folder names always come from the
config even when `--vault` points elsewhere: `--vault` moves the root, it does
not rename the layout.

Functions that receive `vault: Path` as a parameter compose it with
`CFG.folder_name(...)` (`vault / CFG.folder_name("wiki") / "entities"`) rather
than `CFG.folder(...)`, so a `--vault` override flows through.

## Testing

- `_shared/test_atlas_config.py` (unittest, temp dirs only): defaults match the
  reference layout, resolution order, `~` expansion, unknown/`_doc` keys
  ignored, the example file loads to the defaults, `no_nudge` loads as a
  trimmed list and rejects non-list or empty entries, malformed JSON and wrong
  types raise `ConfigError` naming the path, `$ATLAS_CONFIG` pointing at a
  missing file errors, blank `$ATLAS_CONFIG` means unset.
- Existing suites are unchanged and pass before and after (222 checks across
  8 files).
- Identical-behavior proof used for this track: import every non-test module
  with no config present and dump each module-level path value plus `--help`,
  before and after the refactor. The only differences are the intended ones:
  `--help` text for `--vault`, `SKILL_DIR`/`ROUTING_YAML` following `__file__`
  (identical when run from the live checkout), and new derived constants whose
  values equal the old literals. Rendered note text from `graduate`,
  `people-extract`, and `wiki-materialize` was compared old-vs-new
  byte-for-byte.

Never point a test at a real vault; use a temp directory and either
`$ATLAS_CONFIG` or `--vault`.

## Files changed in this track

New:

- `_shared/atlas_config.py` - the config module.
- `_shared/test_atlas_config.py` - its tests.
- `atlas.config.example.json` - every key at its default, documented with `_doc` strings.
- `docs/CONFIG-LAYER.md` - this file.

Changed (one line each):

- `atlas-apple-notes-ingest/ingest.py` - `RAW_APPLE_NOTES` and the `relative_to(vault)` call go through config.
- `atlas-book-summary/book_summary.py` - `--vault` default resolved from config; `--skills-dir` default follows `__file__`; Books dir uses the resources folder name.
- `atlas-claude-history-ingest/ingest.py` - `RAW_CH` goes through config.
- `atlas-distill/distill.py` - `VAULT_DEFAULT`, `RESOURCES_DIR`, `RAW_DISTILL_DIR`, CRM/wiki lookups, and `--vault` fallback go through config.
- `atlas-emerge/emerge.py` - `VAULT_DEFAULT`, raw/wiki/CRM walks, the report path, and `--vault` help/fallback go through config.
- `atlas-fireflies-ingest/convert_retained_to_summary.py` - absolute home-directory vault path replaced; raw folder and `SKIP_DIRS` from config.
- `atlas-fireflies-ingest/dry_run_monday_tier.py` - PyYAML removed (stdlib frontmatter + `monday_routes` parsers); vault, raw, inbox, areas paths from config; `meeting-routing.yaml` resolved next to the script.
- `atlas-fireflies-ingest/fireflies_state.py` - `VAULT` and `VAULT_ROOTS` from config.
- `atlas-fireflies-ingest/link_prep.py` - `DEFAULT_VAULT` from config; `--vault` defaults to `None`.
- `atlas-fireflies-ingest/summary_record.py` - `DEFAULT_VAULT`, the three `raw/fireflies` paths, and three `--vault` flags go through config.
- `atlas-frontmatter-migrate/migrate.py` - absolute home-directory vault path and `SKILL_DIR` replaced (config and `__file__`).
- `atlas-github-ingest/ingest.py` - `RAW_GH` goes through config.
- `atlas-gmail-ingest/ingest.py` - `RAW_GMAIL` and `PEOPLE_DIR` go through config.
- `atlas-graduate/auto_graduate.py` - `VAULT_DEFAULT`, `QUEUE_REL`, wiki/CRM/Clients/Areas lookups, report path, `--vault` fallback go through config.
- `atlas-graduate/graduate.py` - `VAULT_DEFAULT`, `PARA_DIRS`, `TARGET_PATHS`, area/client inference, raw walk, the Dataview `FROM` clause, report path, `--vault` fallback go through config.
- `atlas-lint/lint.py` - absolute home-directory vault path replaced; `EXCLUDE_DIRS`, `ADMIN_PREFIXES`, wiki dirs, raw check, dashboard path, `--vault` from config.
- `atlas-monday-ingest/ingest.py` - `RAW_MONDAY` goes through config.
- `atlas-people-extract/extract.py` - absolute home-directory vault path and `SKILL_DIR` replaced; `PEOPLE_DIR`, `SCAN_ROOTS`, and both Dataview `FROM` clauses derive from config.
- `atlas-research/research.py` - absolute home-directory vault path replaced; `EXCLUDE_DIRS`, `RESEARCH_SUBDIR`, every `LAYER_RULES` prefix, raw/wiki lookups, `--vault` from config.
- `atlas-slack-ingest/ingest.py` - `RAW_SLACK` goes through config.
- `atlas-synthesize/synthesize.py` - `VAULT_DEFAULT`, `PARA_DIRS`, the extra scan roots, wiki/raw paths, `--vault` fallback go through config.
- `atlas-transcript-extract/extract.py` - `VAULT` and `RAW_FIREFLIES` go through config.
- `atlas-voice-memos-ingest/ingest.py` - `DEFAULT_RAW` goes through config (the `ATLAS_VM_RAW` env override still wins).
- `atlas-wiki-materialize/materialize.py` - `VAULT`, `CLIENTS_DIR`, `AREAS_DIR`, `PEOPLE_DIR`, `WIKI_DIR` from config; path-match strings, Dataview predicates, and the index conventions line derive from the same values.
- `atlas-wispr-ingest/ingest.py` - `RAW_WISPR` goes through config.
- `atlas-wispr-meetings-ingest/ingest.py` - `RAW_MEETINGS` and `RAW_FIREFLIES` go through config.

Not changed, on purpose:

- `atlas-nightly/lock.py` - has no vault paths.
- Test files - none contained a home-relative or absolute vault path; their `import yaml` sites already gate on `ImportError`; fixture paths like `test_link_prep.CALL_FOLDER` live under temp dirs.
- `research.py`'s `layer == "raw"` comparisons - `"raw"` there is a layer label (the second element of each `LAYER_RULES` tuple), not a path.
- SKILL.md prose - another track owns it; no SKILL.md invokes a flag whose name changed.
