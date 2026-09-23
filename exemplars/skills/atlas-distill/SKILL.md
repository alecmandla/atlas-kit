---
name: atlas-distill
description: On-demand — distill the current Claude chat exchange into an atomic, entity-linked note at "<resources>/<topic>/<title>.md", with a redacted append-only source record at raw/distill/<capture-id>.md. Human-in-the-loop — proposes topic/title/tags/entity-links/dedup target and confirms before writing. Idempotent via a deterministic capture-id. Redacts secrets. Triggers on "distill this", "capture this", "save this to the vault", "/atlas-distill".
exemplar-of: atlas-distill
status: active
requires: [cli/python3]
---

# atlas-distill

You turn a useful Claude chat exchange — a tool tutorial, a client's tech stack, a sharp explanation — into a **distilled, entity-linked** note in the owner's Atlas vault, on demand. This is the on-demand path that sits between `atlas-claude-history-ingest` (mirrors sessions to `{{folders.raw}}/` as mechanical metadata) and `atlas-emerge`→`atlas-graduate` (surface and promote patterns only once a topic recurs, in batch). One-off insights that never recur never surface through that batch flow; `atlas-distill` captures them the moment they're worth keeping.

Per **DEC-018**, this is a skill that writes directly into a PARA folder (`{{folders.resources}}/`). It is authoring, not ingest, so DEC-009's "ingest → raw first" rule doesn't bind it — but to preserve the wiki rebuildability invariant (DEC-004) it **also** writes a redacted, append-only source record to `{{folders.raw}}/distill/<capture-id>.md`, and the Resources note carries a `source_capture_id` backpointer. The raw record is the durable source; the Resources note is its promotion.

**You drive the distillation; `distill.py` handles the side effects.** The script cannot read the live chat — you read it, distill it, resolve the entity links against the vault, and hand the script a JSON "capture spec." The script does redaction, file writes, dedup, and state deterministically.

## Distill, don't dump

The Resources note is an **atomic note capturing the durable fact**, not a transcript. Write what the owner will want to re-find: the steps, the config, the decision, the stack — in their words, tightened. The full (redacted) exchange is preserved in `{{folders.raw}}/distill/` for provenance; it does not belong in the Resources note.

## Workflow

### 1. Select the exchange

The owner points at a span of the current chat (or pastes it). Identify the single durable thing worth keeping. If the exchange covers two unrelated topics, propose two captures.

### 2. Propose metadata (human-in-the-loop — confirm before writing)

Resolve these against the **real vault**, then show the owner the proposal and let them edit:

- **topic** — kebab-case folder slug under `{{folders.resources}}/` (e.g. `cli-tools`, `client-stacks`). Reuse an existing topic folder if one fits (`ls "{{vault_root}}/{{folders.resources}}/"`).
- **title** — kebab-case note name, **no date prefix** (e.g. `zellij-basics`).
- **tags** — applied **at write time** (vault rule: "tag at write time, not retroactively"). Include a `#thread/<slug>` and a tool/tech tag; add `#client/<x>` when a client is involved. In the spec, write them **without** the leading `#` (frontmatter array form: `thread/zellij`, `tool/zellij`). The script auto-adds a `distilled` tag.
- **entity links** — `[[Entity]]` wikilinks inline in the body. People are `[[First-Last]]` (resolve against `{{folders.crm}}/People/`); clients/technologies resolve against `{{folders.wiki}}/entities/`. Check existence before linking — the script flags any `[[link]]` that doesn't resolve, and never invents entities. Mixing client and personal data is fine.
- **dedup target** — if `{{folders.resources}}/<topic>/<title>.md` already exists, this is an **update**: the script preserves the note's editable region and (with `--force`) regenerates the rest. Pick the same topic/title to update; pick a new title to create.

### 3. Build the capture spec

A JSON object:

```json
{
  "topic": "cli-tools",
  "title": "zellij-basics",
  "tags": ["thread/zellij", "tool/zellij"],
  "source_session": "<claude session id if known, else omit>",
  "body": "## What\n\nZellij is a terminal multiplexer... [[{{owner_slug}}]] uses it for...",
  "exchange": "<the full raw exchange text — the script redacts it>"
}
```

- `body` — the distilled markdown (atomic note), with inline `[[Entity]]` wikilinks. Goes into the Resources note above the `## Notes` editable region.
- `exchange` — the full exchange; the script redacts it and stores it in `{{folders.raw}}/distill/`.
- `capture_id` — omit; the script derives a deterministic id from `topic/title` (`distill-<sha1[:10]>`) so re-runs are idempotent. Pass one only to force a specific id.

### 4. Dry-run, then execute

```bash
cd {{skills_root}}/atlas-distill
python3 distill.py --input /tmp/capture.json --dry-run    # proposal + preview, writes nothing
python3 distill.py --input /tmp/capture.json --execute     # writes both files
python3 distill.py --input /tmp/capture.json --execute --force   # update an existing note
```

Show the owner the dry-run (topic, title, tags, resolved + unresolved entities, redaction counts, preview) and get their confirmation before `--execute`.

### 5. Report

After execute, surface the two written paths and any unresolved entities (from `last-run.md`) so the owner can create the missing person/entity pages if they want the links live.

## Outputs

**`{{folders.raw}}/distill/<capture-id>.md`** (append-only, redacted) — frontmatter order `date, id, type: raw-distill, tags`, then alphabetical (`captured_at`, `resource_note`, `source_session`, `topic`); body has the redacted exchange.

**`{{folders.resources}}/<topic>/<title>.md`** (the distilled note) — frontmatter order `date, id, type: distilled-resource, tags`, then alphabetical (`source_capture_id`, `source_session`, `topic`); body is the distilled content followed by a `## Notes` editable region fenced with `<!-- atlas-distill:editable-start/end -->`.

## Redaction

Secrets are redacted in **both** outputs before writing, using the verbatim patterns from `atlas-claude-history-ingest` (`sk-…`, `ghp_…`, `AKIA…`, `xoxb-…`, `Bearer …`, `password=…`, `api[_-]?key=…` → their `…REDACTED` forms). Keep `REDACTIONS` in `distill.py` in sync with that skill.

## Idempotency contract

- **Capture-id is deterministic** from `topic/title` — re-running the same capture targets the same files.
- **`{{folders.raw}}/distill/` is append-only** (DEC-009). If the raw record exists, the script leaves it byte-unchanged. Corrections use the `corrected_by:` / `corrects:` chain — never edit or delete a raw record.
- **The Resources note's editable region is preserved** across re-runs. Without `--force` an existing note is not overwritten; with `--force` everything outside the `## Notes` editable markers is regenerated and the owner's content between them is kept verbatim.
- **No deletion.** The skill only creates or updates.

## Edge cases

- **Unresolved entity** → the `[[link]]` is written as-is and flagged in `last-run.md`; the agent reports it. Don't invent a person/entity page.
- **New topic folder** → created on execute. Create a `_<Topic>-Index.md` (Dataview) only if the new topic warrants one; otherwise rely on the `distilled` tag + thread tags for auto-surfacing in MOCs.
- **Secret detected** → redacted in place; the redaction count appears in the dry-run and `last-run.md`.
- **Exchange spans multiple threads** → multiple `#thread/` tags is fine (vault rule).
- **Title collision under a topic** → treated as an update to that note (intended dedup), not a duplicate. Use a more specific title to keep them separate.

## Relationship to other skills

- `atlas-claude-history-ingest` — mechanical session metadata into `{{folders.raw}}/claude-history/`; `atlas-distill` captures distilled *content* on demand. Redaction patterns are shared.
- `atlas-emerge` / `atlas-graduate` — emergent + batch promotion of *recurring* patterns; `atlas-distill` is on-demand for one-off insights. `{{folders.raw}}/distill/` feeds `emerge` like any other raw source.
- `atlas-wiki-materialize` — consumes `{{folders.raw}}/distill/` so distilled captures surface as entity mentions in the wiki spine.
