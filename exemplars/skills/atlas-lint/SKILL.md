---
name: atlas-lint
description: Wiki-spine health linter + synthesis work-list generator. Complements atlas-health (which owns orphans, broken links, frontmatter, inbox, stale-raw) by auditing the KNOWLEDGE SPINE: duplicate slugs, missing synthesis, and hollow pages. Surfaces a synthesis work-list the backfill consumes (staleness of existing pages defers to atlas-synthesize). Read-only on the spine; writes only its dashboard + last-run. Triggers on "atlas lint", "lint the wiki", "lint my vault", "what needs synthesizing", "find duplicate slugs", "wiki health", "/atlas-lint".
exemplar-of: atlas-lint
status: active
requires: [cli/python3]
---

# atlas-lint

You audit the **wiki spine** for quality issues and surface **what should be synthesized next**. You are read-only on the spine — you write only `{{folders.meta}}/Dashboards/Wiki-Lint.md` and `{{skills_root}}/engine/atlas-lint/last-run.md`. You never edit `{{folders.wiki}}/`, `{{folders.raw}}/`, or PARA notes.

This is the complement to **`atlas-health`**, not a replacement: `atlas-health` covers vault-wide hygiene (orphaned notes, broken wikilinks, frontmatter violations, inbox overflow, stale `{{folders.raw}}/`). `atlas-lint` covers what that pass doesn't — the *quality and coverage of the synthesized layer*.

## Checks (all mechanical, cheap)

| Check | What it flags |
|---|---|
| **duplicate-slugs** | Fuzzy-equal slugs across synthesis/concepts/entities/`#thread` (e.g. `portal-rewrite` / `PortalRewrite` / `portal_rewrite`), plus lower-confidence token-overlap pairs (e.g. `ledgerline` ~ `ledgerline-integration`). |
| **missing-synthesis** | `#thread/<slug>` with **≥ `MIN_EVIDENCE` (3) evidence files** and **no** `{{folders.wiki}}/synthesis/<slug>.md`. This is the **synthesis work-list**. *Evidence* counts tags outside admin/index files (AGENTS.md, dashboards, weekly reviews, daily notes — those only *list* threads). Threads below the threshold are shown as **tagged-but-thin**, not work. Variant slugs are merged; a possible existing duplicate is annotated `dup_of=`. |
| **hollow-pages** | synthesis/concept pages with `evidence_count <= 1` (placeholders / N=1 threads). |

> **Staleness of *existing* synthesis pages is deliberately NOT checked here** — that is `atlas-synthesize`'s content-fingerprint job, and an mtime/date heuristic false-positives against it. For authoritative new/changed status, run `python3 {{skills_root}}/engine/atlas-synthesize/synthesize.py --list`.

## Step 1 — Run the lint

```bash
python3 {{skills_root}}/engine/atlas-lint/lint.py report
```
This prints a stdout summary, (over)writes the `{{folders.meta}}/Dashboards/Wiki-Lint.md` snapshot, and updates `last-run.md`. Aggregate the counts; don't load the full dashboard into context unless asked.

## Step 2 — Present the report

Summarize per check with counts, then the **synthesis work-list** (the actionable part) and the **duplicate slugs** (the cleanup part). For each missing-synthesis row note its evidence count and any `dup_of`. Cap lists at ~8; point to the dashboard for the rest.

## Step 3 — Feed the synthesis backfill (the integration)

The synthesis work-list is machine-readable for the backfill:
```bash
python3 {{skills_root}}/engine/atlas-lint/lint.py worklist
# <slug> | evidence=<n> | sources=<csv> | status=missing [| dup_of=<slug>]   (actionable only)
```
Hand `status=missing` slugs (skipping any `dup_of` unless the user wants a separate page) to `atlas-synthesize` — by hand (`/atlas-synthesize <slug>`) or via the batched synthesis run. For re-synthesis of *existing* pages, run `synthesize.py --list` (its fingerprint decides what's stale). **Resolve duplicate slugs first** (pick the canonical, retag) so synthesis doesn't produce a duplicate page. An empty work-list is the healthy steady state.

## Step 4 — Recommend cleanup, don't perform it

Surfaces, never decides (mirrors `atlas-health`, DEC-007):
- **Duplicate slugs:** recommend the canonical slug + which to retag/merge. Retagging is an owner action (or a separate, explicitly-approved migration) — `atlas-lint` does not rewrite tags.
- **Missing synthesis:** offer to synthesize the work-list (actionable threads only).
- **Tagged-but-thin / staleness:** thin threads need more evidence before synthesis is worthwhile; for re-synthesis of existing pages, defer to `synthesize.py --list`.
- **Hollow pages:** flag for review — a concept page that never grew may be a candidate for synthesis or removal.

## Idempotency / contract
- Read-only on the spine. Only writes: `{{folders.meta}}/Dashboards/Wiki-Lint.md` (overwritten each run) + `last-run.md`.
- Re-running on an unchanged spine yields an identical dashboard (modulo `generated_at`).
- Stdlib-only (DEC-021); runs headless if ever scheduled.

## Edge cases
- **Tag vs. keyword evidence:** the work-list counts distinct *files carrying the `#thread/<slug>` tag*, not keyword hits. A thread with few tags can still have rich keyword evidence — `atlas-synthesize` gathers by keyword too, so a `files=3` thread can yield a large synthesis. Treat the count as "is this a real, curated thread," not "how much will synthesis find."
- **`dup_of` is a hint, not a verdict** (token overlap is fuzzy). `ledgerline` ~ `ledgerline-integration` is correct; a shared generic token could be a false positive — the owner confirms.
- **No threads/synthesis yet:** every count is 0; not an error.

## Relationship to other skills
- **Complements `atlas-health`** (vault hygiene) — no overlap by design.
- **Feeds `atlas-synthesize`** — the work-list is what to synthesize; re-synthesis of existing pages is `atlas-synthesize`'s own fingerprint job (`--list`).
- **Pairs with `atlas-research`** — when a query keeps drilling raw for a topic with no synthesis, that topic should already be on this work-list.
- **Downstream of `atlas-emerge`/`atlas-graduate`** — graduating an emerge candidate into a `#thread/<slug>` tag is what puts it on this list (which is why noisy emerge candidates such as a greeting phrase never appear here — they were never graduated).

## Anti-goals (NOT v1)
- No auto-retagging / auto-merging of duplicate slugs (too risky; surface the canonical instead).
- No LLM checks (contradictions / stale-claims prose analysis) — mechanical only in v1.
- No re-implementing `atlas-health`'s vault-wide orphan/broken-link/frontmatter passes.

## Invocation
On-demand (`/atlas-lint` or the trigger phrases). No schedule in v1 — run it before a synthesis batch, or when the spine feels messy.
