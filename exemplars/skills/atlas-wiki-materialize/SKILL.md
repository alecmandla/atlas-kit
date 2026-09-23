---
name: atlas-wiki-materialize
description: Materialize the owner's LLM Wiki entity pages. Walk raw/fireflies/, CRM/People/, the project structure, and meeting frontmatter; emit one wiki/entities/<entity>.md per unique entity (client / org / product / technology / concept / area) with sourced mentions. Regenerate wiki/index.md as the master list. Idempotent. Triggers on "materialize wiki", "rebuild wiki", "regenerate entities", "wiki rebuild", or any scheduled materialization run.
exemplar-of: atlas-wiki-materialize
status: active
requires: [cli/python3]
---

# atlas-wiki-materialize

You materialize the LLM Wiki layer (`{{vault_root}}/{{folders.wiki}}/entities/` + `{{vault_root}}/{{folders.wiki}}/index.md`) from the structured signals already present in the vault: `{{folders.raw}}/fireflies/`, `{{folders.crm}}/People/`, the `{{folders.projects}}/Clients/` directory tree, the `{{folders.areas}}/` tree, and meeting-note frontmatter (`project:`, `participants:`).

The wiki layer is **freely rewriteable** (per the vault constitution `AGENTS.md` and DEC-004): every run regenerates `{{folders.wiki}}/entities/*.md` and `{{folders.wiki}}/index.md` from current sources. This is the opposite of `{{folders.raw}}/`'s append-only invariant: wiki pages are *projections*, not *source*. If you hand-edit a wiki page, your edit will be overwritten on the next materialization run.

## Mental model

The Karpathy LLM Wiki pattern (DEC-004): `{{folders.raw}}/` is the immutable source layer, `{{folders.wiki}}/` is the materialized projection layer. For v1, materialization is rule-based, not LLM-driven; the structured signals (project folders, CRM frontmatter, meeting `project:` field) already encode most of the entity graph. Future versions may add LLM-driven concept clustering.

The vault has six entity types in v1:

| Type | Source | Examples |
|---|---|---|
| `client` | `{{folders.projects}}/Clients/<client>/` directory | `Pinecrest-Lodge`, `Saltmarsh-Inn` |
| `area` | `{{folders.areas}}/<area>/` directory | `Client-Success`, `HQ` |
| `org` | `{{folders.crm}}/People/*.md` `company:` field (deduped against client list) | `Ledgerline` |
| `product` | Curated lexical extraction from meeting titles and summaries (v1: a hand-curated seed list of well-known products in the owner's domain) | `Ledgerline-Cloud`, `Northstar-BI` |
| `technology` | Curated lexical extraction (v1: same seed list) | `Dataview`, `Templater` |
| `concept` | Empty in v1; surfaces later via `atlas-emerge` clustering | — |

## Workflow

### 1. Walk the entity sources

For each entity source, derive (name, type, canonical_id) tuples:

- **Clients** — every immediate subdirectory of `{{vault_root}}/{{folders.projects}}/Clients/`. Name = directory name (kebab-case preserved).
- **Areas** — every immediate subdirectory of `{{vault_root}}/{{folders.areas}}/`. Name = directory name.
- **Orgs** — every unique `company:` value in `{{folders.crm}}/People/*.md`, **excluding** any that already maps to a client. The mapping is fuzzy: lower-cased alphanumeric-only comparison between `company:` value and the client directory name with hyphens removed (e.g. `pinecrestlodge` → matches `Pinecrest-Lodge` because `pinecrestlodge` starts with `pinecrestlodge`). De-fuzzed via a known-mapping table in the skill source (`client_aliases` in `entity_seeds.json`).
- **Products** and **technologies** — from a curated seed list in the skill source (see `entity_seeds.json`). Each seed has a canonical name, type, regex pattern for case-insensitive matching against meeting titles + summaries.

### 2. Count mentions per entity

For each entity, count meeting notes (`{{vault_root}}/**/meetings/*.md`) that reference it. Mention sources:

- **Clients** — meeting's `project:` frontmatter starts with `[[_<Client>]]` or `[[<Client>]]`, OR the meeting's path includes `{{folders.projects}}/Clients/<Client>/`.
- **Areas** — meeting's path includes `{{folders.areas}}/<Area>/`, OR `tags:` includes `area`.
- **Orgs** — any `participants:` wikilink's resolved person note has a matching `company:` field.
- **Products / technologies** — regex match against the meeting note's title (`# <title>`) or `## Summary` body.

Track `first_mention` (oldest meeting `date:`) and `last_mention` (newest) per entity.

### 3. Write entity pages

For each entity with `mention_count ≥ 1`:

```markdown
---
type: wiki-entity
entity_name: <name>
entity_type: <client | area | org | product | technology>
canonical_id: <kebab-case-id>
first_mention: <YYYY-MM-DD>
last_mention: <YYYY-MM-DD>
mention_count: <N>
generated_by: atlas-wiki-materialize
generated_at: <YYYY-MM-DD>
tags: [wiki-entity, entity-type/<type>]
---

# <name>

*<one-line description from entity-seeds metadata if available, else "<Entity-type> entity surfaced from <N> meeting references.">*

## Mentions

```dataview
TABLE date as "Date", file.link as "Meeting"
FROM "{{folders.projects}}" OR "{{folders.areas}}"
WHERE <type-specific predicate>
SORT date DESC
```

## Related

- *Hand-add wikilinks here. Survives re-materialization (the `## Related` section is the one editable region per `<!-- atlas-wiki-materialize:editable-start -->` markers).*
- (none yet)
```

The `## Related` section is the **one editable region** in a wiki entity page. Markers preserve owner edits across re-materialization:

```
<!-- atlas-wiki-materialize:editable-start -->
*Hand-add wikilinks here.*
- [[Some-Related-Entity]]
<!-- atlas-wiki-materialize:editable-end -->
```

When re-running, the skill reads the existing `## Related` section, captures content between the markers, regenerates the rest of the page, and re-inserts the captured content verbatim.

### 4. Write `{{folders.wiki}}/index.md`

After all entity pages are written, regenerate `{{folders.wiki}}/index.md`:

```markdown
---
type: wiki-index
generated_by: atlas-wiki-materialize
generated_at: <YYYY-MM-DD>
total_entities: <N>
---

# Wiki index

Last regenerated: <YYYY-MM-DD>

## Entities by type

### Client (<N>)

- [[<entity-name>]] — last mention <YYYY-MM-DD>, <N> mentions
- ...

### Area (<N>)
...

### Org (<N>)
...

### Product (<N>)
...

### Technology (<N>)
...

## Concepts

*Empty in v1. Surfaces via `atlas-emerge`.*

## Conventions

See `{{vault_root}}/AGENTS.md` for the rewriteable rule and the per-page `## Related` editable-region protocol.
```

### 5. Apply or report

- `--dry-run-report <path>` writes a structured plan report; no vault writes to `{{folders.wiki}}/`.
- `--execute` writes entity pages + `{{folders.wiki}}/index.md` + refreshes `last-run.md`.
- `--limit-entities N` processes only the first N entities (smoke test).

### 6. Write per-run summary

`{{skills_root}}/engine/atlas-wiki-materialize/last-run.md` captures:

- Timestamp + mode.
- Counts per entity_type.
- Entities created vs. updated (incremental run).
- Editable-region preservations (count of `## Related` sections preserved across re-run).

## Invocation

```bash
# Preview the plan
python3 materialize.py --dry-run-report /tmp/atlas-wiki-materialize-dryrun.md

# Apply
python3 materialize.py --execute

# Smoke test
python3 materialize.py --execute --limit-entities 10
```

**The script defaults to dry-run.** Without `--execute` it reports `created=0 updated=0` and writes nothing; `atlas-nightly` must pass `--execute`.

## Idempotency contract

Re-running on an already-materialized vault must:

- **Never create** duplicate entity files (the canonical_id is the dedup key).
- **Never overwrite** content inside `<!-- atlas-wiki-materialize:editable-start -->` / `editable-end` markers in any entity page.
- **Always refresh** `mention_count`, `last_mention`, `generated_at` per current corpus state.
- **Regenerate** `{{folders.wiki}}/index.md` from current entity files.

Verify mechanically: second `--execute` produces `created=0 updated=N`.

## Edge cases

- **Entity name collision** (e.g. two clients named `Saltmarsh-Inn` and `Saltmarsh-Inn-Group`): the canonical_id is `<directory-name>` so collisions resolve via the on-disk path. Disambiguation in `<name>` happens via the entity-seeds table if needed.
- **Empty mention count** — entities with zero mentions are NOT materialized (filtering criterion in step 3).
- **CRM company without a matching client folder** — that's the `org` branch. Common case: every Fireflies attendee's email-domain becomes a candidate org; only those whose mention count via `participants:` exceeds 0 are written.
- **`tags: [area]` ambiguity** — many meeting notes carry `area` as a tag for any area-related meeting, not specifically for the area they're filed under. The skill uses the **path predicate** primarily and the tag predicate as a fallback to avoid false-positive cross-area mentions.

## Relationship to other skills

- `atlas-people-extract` — builds the people CRM that this skill *consumes* (`{{folders.crm}}/People/*.md` `company:` field for the org branch).
- `atlas-fireflies-ingest` — writes the `{{folders.raw}}/fireflies/` summary records; a later version of this skill may extract entities from their bodies once LLM-driven extraction lands.
- `atlas-emerge` — mines for concept entities by clustering keywords across raw/. Concepts land in `{{folders.wiki}}/concepts/`, not in `{{folders.wiki}}/entities/`; the two skills never write to the same file.
- `atlas-graduate` — promotes a surfaced pattern to a wiki page; downstream of atlas-emerge.
- `atlas-nightly` — runs this skill after all ingests, before `atlas-emerge`.
