---
name: atlas-graduate
description: Promote a pattern slug from atlas-emerge into a tracked surface. Takes a slug (e.g. `portal-rewrite`, `scheduled-tasks`) from `{{folders.meta}}/Dashboards/Emerging-Patterns.md`. Asks the owner one clarifying question — concept / project / area / resource — then materializes the destination page with auto-populated sections (first mention, current state, key contributors from CRM, open questions, related raw sources), backfills `#thread/<slug>` on every PARA note whose body/title matches the slug's keyword variants, and appends a row to `{{vault_root}}/AGENTS.md`'s canonical threads table. The slug becomes part of the deny-list, so atlas-emerge stops re-surfacing it on the next run. Triggers on "graduate", "promote pattern", "/atlas-graduate", "graduate <slug>", "promote <slug>", or any surface-promotion request keyed by a slug present in the emerge report.
exemplar-of: atlas-graduate
status: active
requires: [cli/python3]
---

# atlas-graduate

You promote an unnamed pattern (a row in `{{folders.meta}}/Dashboards/Emerging-Patterns.md`) into one of four tracked surfaces:

| Target | Destination | When to pick it |
|---|---|---|
| `concept` | `{{folders.wiki}}/concepts/<slug>.md` | Cross-cutting topic / technology / pattern that isn't a project (e.g. `scheduled-tasks`, `dataview-discipline`) |
| `project` | `{{folders.projects}}/Work/<slug>/<slug>-MOC.md` | Active work with a goal and deliverables (e.g. `portal-rewrite`, `ledgerline-integration`) |
| `area` | `{{folders.areas}}/<slug>/<slug>-MOC.md` | Ongoing responsibility (e.g. `client-success`, `dashboard-onboarding`) |
| `resource` | `{{folders.resources}}/<slug>/<slug>-MOC.md` | Reference material / topic-of-interest (e.g. `claude-skills`, `prompting-techniques`) |

This skill is the owner's primary interaction surface for idea-evolution tracking. Keep the prompt minimal: one question, then go.

## When invoked

1. **Find the pattern row.** Read `{{vault_root}}/{{folders.meta}}/Dashboards/Emerging-Patterns.md`. Locate the row whose `Slug` column matches the requested slug. If no match → list available slugs and stop.
2. **Confirm the target with the owner.** Ask one question:

   > Graduate `<slug>` as a **(1) concept**, **(2) project**, **(3) area**, or **(4) resource**? (default: concept)

   Wait for the owner's pick. Don't auto-decide.
3. **Execute the graduate.** Invoke `python3 graduate.py --slug <slug> --target <pick>`.
4. **Report.** Quote the new destination path, the count of PARA notes tagged, and the updated thread-table row in `{{vault_root}}/AGENTS.md`. Suggest running `atlas-emerge` again to confirm the slug is no longer surfaced.

## What the script does

### Step 1 — Read the pattern row

`graduate.py` parses the Emerging-Patterns.md table for the slug's row. It pulls: keyword variants, first/latest mention, mention count, contributing source types.

### Step 2 — Discover matching PARA notes

Walks every `**/*.md` under `{{vault_root}}/{{folders.inbox}}/`, `{{folders.projects}}/`, `{{folders.areas}}/`, and `{{folders.resources}}/`. A note "matches" the pattern if its title OR first 2000 chars of body contains any of the slug's keyword variants (case-insensitive, whole-word boundary). Raw sources (`{{folders.raw}}/`) are NOT modified — append-only invariant (DEC-009).

### Step 3 — Discover key contributors

For each matched note that's a meeting note (frontmatter has `participants:`), aggregate the wikilink-resolved person names. Sort by mention count; pick the top 10 as "key contributors".

### Step 4 — Write the destination page

Frontmatter + body, structured as:

```markdown
---
type: <wiki-concept | project-moc | area-moc | resource-moc>
canonical_id: <slug>
graduated_from: emerging-patterns
graduated_at: <YYYY-MM-DD>
first_mention: <YYYY-MM-DD>
last_mention: <YYYY-MM-DD>
mention_count: <N>
sources: [<source-list>]
tags: [<wiki-concept|project-moc|...>, thread/<slug>]
---

# <Title-cased slug>

> Graduated from `atlas-emerge` on <YYYY-MM-DD>. First mention: <YYYY-MM-DD>. <N> items across <M> sources.

## Current state

_<one-line placeholder — the owner fills in>_

## Key contributors

- [[Person-One]] — <N> meetings
- [[Person-Two]] — <N> meetings
- ...

## Open questions

- _<placeholder>_

## Related raw sources

- [[<raw-source-1>]] — <date>
- ...

## Thread query

```dataview
TABLE date as "Date", file.link as "Note"
FROM "{{folders.projects}}" OR "{{folders.areas}}" OR "{{folders.resources}}"
WHERE contains(file.tags, "thread/<slug>")
SORT date DESC
```

## Editable

<!-- atlas-graduate:editable-start -->
_Hand-add wikilinks, notes, and decisions here. This region is preserved across re-runs._
<!-- atlas-graduate:editable-end -->
```

The `## Editable` region is the **one editable region** in a graduated page. Re-running `atlas-graduate` on the same slug regenerates everything except the content between the markers.

### Step 5 — Backfill `#thread/<slug>` on matched PARA notes

For each matched PARA note (step 2):

- Parse its YAML frontmatter.
- If `tags:` is absent → add `tags: [thread/<slug>]`.
- If `tags:` exists and doesn't contain `thread/<slug>` → append `thread/<slug>`.
- If `tags:` already contains `thread/<slug>` → no-op (idempotent).

The tag goes inside the existing frontmatter block; the body is untouched. Format: `tags: [thread/<slug>, ...]` — kebab-case per the vault constitution.

### Step 6 — Append to the vault constitution's canonical threads table

`{{vault_root}}/AGENTS.md` has a threads table under its "Example threads" heading. Find the table; append a row:

```md
| `#thread/<slug>` | _Graduated <YYYY-MM-DD> — see [[<slug>]] for context._ |
```

Idempotent: if the row already exists for `<slug>`, no-op.

### Step 7 — Write `{{skills_root}}/engine/atlas-graduate/last-run.md`

Captures: timestamp, slug, target, destination path, PARA notes tagged (count), AGENTS.md row added (true/false), duration.

## Invocation

```bash
# Dry-run — preview what would happen, no writes
python3 graduate.py --slug scheduled-tasks --target concept --dry-run

# Execute
python3 graduate.py --slug scheduled-tasks --target concept

# Force-regenerate (re-runs preserve editable region)
python3 graduate.py --slug scheduled-tasks --target concept --force
```

CLI flags:

- `--slug <slug>` (required) — must exist in `{{folders.meta}}/Dashboards/Emerging-Patterns.md`.
- `--target {concept,project,area,resource}` — destination type. Without this, the script lists targets and exits (unless `--auto`).
- `--auto` — unattended mode (DEC-019): infer the target via `infer_target()` when `--target` is omitted (biases to `concept`), and stamp `graduation_mode: auto` on the page + last-run. Used by `auto_graduate.py`; safe to pass manually too.
- `--dry-run` — print plan; no writes.
- `--force` — re-graduate an already-graduated slug (regenerate everything outside the editable region).
- `--vault <path>` — override vault root (testing).
- `--report-path <path>` — override emerge-report path (testing).
- `--today <YYYY-MM-DD>` — override timestamp (testing).

## Safety contracts

- **`{{folders.raw}}/` is read-only** (DEC-009). Backfill never modifies a file under `{{folders.raw}}/`.
- **Existing frontmatter is preserved.** When adding to `tags:`, keep all other keys/values; preserve key order; preserve trailing whitespace.
- **Editable region is preserved on re-runs.** `<!-- atlas-graduate:editable-start -->` / `editable-end` markers fence owner-authored content.
- **Idempotent.** Running twice on the same slug must produce identical disk state (modulo `graduated_at:` timestamp inside the regenerated frontmatter).
- **Single-slug per run.** No batch mode in v1 — one slug per invocation. Forces the owner to make one decision at a time.
- **No deletion.** Graduate only adds. To un-graduate, the owner deletes the destination page and removes the AGENTS.md row by hand.

## Edge cases

- **Slug not in report** — list available slugs from the report's table; exit 2.
- **No PARA matches** — still write the destination page (it's a forward-looking projection); backfill count = 0. Surface in last-run.md.
- **Existing destination page** — without `--force`, refuse and exit 3. With `--force`, regenerate everything outside the editable region.
- **AGENTS.md table missing** — surface and exit 4. The vault constitution should always have the threads table; missing means something deeper is wrong.
- **Frontmatter is malformed** — skip the file and log to last-run.md; never trash a malformed-frontmatter file.

## Relationship to other skills

- `atlas-emerge` — produces the input report this skill consumes. The slug must exist there.
- `atlas-wiki-materialize` — writes `{{folders.wiki}}/entities/` (different namespace from `{{folders.wiki}}/concepts/`). The two never write to the same file.
- The next `atlas-emerge` run will naturally exclude the graduated slug because the slug becomes (a) a `{{folders.wiki}}/concepts/<slug>.md` filename stem in the deny-list, or (b) a `#thread/<slug>` tag found by the vault-wide grep.

## Unattended graduation — `auto_graduate.py` (DEC-019)

`auto_graduate.py` (sibling script, runs as the last step of `atlas-nightly`) is the policy engine that decides which emerging patterns graduate **without an owner prompt**. It reads `Emerging-Patterns.md`, scores each pattern against the **Moderate** bar, and for each high-confidence pattern shells out to `graduate.py --slug <s> --auto`. Everything it won't auto-graduate is written to `{{folders.meta}}/Dashboards/Thread-Review-Queue.md` for a one-glance owner call.

- **Auto bar (Moderate):** passes all guards AND (`≥2 work sources` OR `≥5 items`) AND `persisted ≥2 nightly runs`. Work sources = `fireflies, wispr-meetings, gemini, teams, zoom, gong, gmail, slack, monday, github`; self sources = `claude-history, wispr`. `wispr` is the owner's dictations; `atlas-emerge` reports Wispr Notetaker records (`raw/wispr/meetings/`) as `wispr-meetings`. One call captured by several meeting ingests is one work source, not several: `atlas-emerge` groups the cross-linked records and writes a single source name for the group into `Emerging-Patterns.md`, which is all this script reads (see "Meeting grouping" in the `atlas-emerge` SKILL.md). A Zoom + Gong + Fireflies copy of one call arrives as `fireflies` alone and cannot meet the work-source rule by itself. A call the Wispr Notetaker also captured arrives as `wispr-meetings`.
- **Guards:** `person` (CRM stem / common-first-name), `known-entity` (existing wiki entity / client / area), `noise` (generic slugs + `suppress.txt`).
- **Persistence ledger:** `{{skills_root}}/engine/atlas-graduate/seen-ledger.json` (`{slug: [observation-dates]}`), advanced only on `--execute`.
- **Modes:** default = plan (classify + write the review-queue dashboard, no graduations, ledger untouched); `--execute` = apply graduations + persist ledger (this is how `atlas-nightly` calls it).
- **Per-run cap:** `MAX_AUTO_PER_RUN` (DEC-022) bounds how many slugs graduate in one night, so a burst of new evidence rolls out over several runs instead of tagging the vault all at once.

See DEC-019 for the full rationale and the reversal of the "no automatic tag application" anti-goal.

## Anti-goals (NOT v1)

- LLM-authored body content. The auto-populated sections are placeholders; the owner fills in `## Current state` and `## Open questions` themselves. Auto-summarization deferred to v2.
- Multi-slug batch mode *inside `graduate.py`*. This script still graduates one slug per invocation. Batched, policy-driven graduation lives in the sibling `auto_graduate.py` (DEC-019), which calls this script once per qualifying slug.
- Un-graduate. Reversal is a manual operation (delete the page, remove the table row, optionally strip the tag).
- Cross-vault graduation. Single-vault per DEC-001.
