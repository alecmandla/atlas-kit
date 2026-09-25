---
name: atlas-emerge
description: Surface unnamed patterns from the last N days of the owner's raw/ corpus. Walk raw/fireflies/, raw/wispr/, raw/gemini/, raw/teams/, raw/zoom/, raw/gong/, raw/claude-history/, raw/github/, raw/gmail/, raw/slack/, raw/monday/, raw/distill/ for items dated within the window; extract candidate concept slugs from titles and bodies; dedupe against existing wiki/entities/, wiki/concepts/, and #thread/<slug> tags; rank by mention count and source diversity; emit a regenerated report at {{folders.meta}}/Dashboards/Emerging-Patterns.md. Idempotent (always overwrite). Triggers on "emerge", "surface patterns", "what's been recurring", "find new threads", "/atlas-emerge", or any scheduled emerge run.
exemplar-of: atlas-emerge
status: active
requires: [cli/python3]
---

# atlas-emerge

You surface **unnamed patterns** — recurring keyword clusters and entity-pair mentions that appear across the last N days of `{{folders.raw}}/` content but don't yet have a `#thread/<slug>` tag, a `{{folders.wiki}}/concepts/<slug>.md` page, or a matching `{{folders.wiki}}/entities/<slug>.md` page.

This skill is the v1 implementation of the owner's stated long-term goal: **track how a project idea changes over time**. It's the upstream half of idea-evolution tracking. Its output is reviewed by the owner; promising patterns get promoted via `/atlas-graduate <slug>` into either a `#thread/<slug>` tag applied retroactively, or a freshly-materialized `{{folders.wiki}}/concepts/<slug>.md` page.

## Mental model

`{{folders.raw}}/` is append-only immutable source material (DEC-009). `{{folders.wiki}}/entities/` is the rule-based projection of known entities (`atlas-wiki-materialize`). The gap between them — recurring topics that aren't yet known entities — is what `atlas-emerge` surfaces.

Three categories of pattern v1 cares about:

| Category | Signal | Example |
|---|---|---|
| Cross-project topic | The same kebab phrase shows up in 2+ source types over the window | `portal-rewrite`, `northstar-migration` |
| Recurring entity pair | Two known entities co-occur in 3+ items but aren't yet linked in either's wiki page | `Ledgerline` + `Guest-Portal` |
| New noun-phrase cluster | A capitalized multi-word phrase appears in 3+ items, doesn't match any existing entity canonical_id, doesn't match a documented `#thread/` slug | `Room Block`, `Rate Plan Audit` |

Quality matters more than quantity. A noisy `Emerging-Patterns.md` is worse than a quiet one — tune thresholds in subsequent iterations rather than padding the report.

## Workflow

### 1. Walk raw/ within the window

For each `{{vault_root}}/{{folders.raw}}/<source>/**/*.md`:

- Parse YAML frontmatter; extract `date:` (or `timestamp:`/`created_at:` fallback for sources without `date:`).
- If the parsed date is older than `--window-days` (default 30), skip.
- Record: source type (parent dir of `{{folders.raw}}/`), item path, item date, title (first `# <title>` line), and body (everything after the first blank line following the title).

Source-type detection: `raw/fireflies/` → `fireflies`, `raw/wispr/meetings/` → `wispr-meetings` (Wispr Notetaker records of calls), any other `raw/wispr/` file → `wispr` (the owner's dictations), `raw/gemini/meetings/` → `gemini`, `raw/teams/meetings/` → `teams`, `raw/zoom/meetings/` → `zoom`, `raw/gong/meetings/` → `gong`, `raw/claude-history/<...>/` → `claude-history`, `raw/github/<owner>/<repo>/` → `github`, `raw/gmail/<route>/` → `gmail`, `raw/slack/<channel>/` → `slack`, `raw/monday/<workspace>/<board>/` → `monday`, `raw/distill/` → `distill`.

Meeting grouping: the six meeting ingests record the same call as separate raw files that point at each other through frontmatter ids, a later ingest linking to earlier ones. Each record's own id is `meeting_id` (`raw/fireflies/`), `wispr_meeting_id`, `gemini_doc_id`, `teams_meeting_id`, `zoom_id`, or `gong_call_id`; a link to another record uses `fireflies_id` for Fireflies and the same key name for the rest. Treat each (source, id) pair a record carries, its own and every link, as a node, and union every record that shares a node (union-find), so a link counts in both directions and chains through records outside the window. Never compare titles: two calls with the same title on different days stay apart. Each group counts as **one item from one source type**, and it reports the first member's source type in the order `wispr-meetings`, `fireflies`, `gemini`, `teams`, `zoom`, `gong`. `wispr-meetings` leads because a Wispr Notetaker record carries the owner's own notes; the rest follow the nightly ingest order, so otherwise the group reports its earliest-ingested record. All six are work sources for `atlas-auto-graduate` (DEC-019); only the dictations (`wispr`) are a self source. One call captured by Zoom, Gong, and Fireflies is therefore one `fireflies` item and cannot meet `--min-sources 2` alone. Records outside the meeting folders, and meeting records with no id in common, are each their own item.

### 2. Build the known-tracked deny-list

Before surfacing candidates, build a set of identifiers that are **already tracked** and should be filtered out:

- Every `canonical_id:` value in `{{vault_root}}/{{folders.wiki}}/entities/*.md` frontmatter.
- Every filename stem in `{{vault_root}}/{{folders.wiki}}/concepts/*.md`.
- Every `#thread/<slug>` value found by grepping the whole vault (not just `{{folders.raw}}/`). Strip the `#thread/` prefix; keep the slug.
- A baseline English stopword list + Atlas-specific noise (e.g., "meeting", "discussion", "notes", "follow up", "next steps", "action items", "thanks").

All comparisons are lower-cased and hyphen-/underscore-normalized.

### 3. Extract candidate phrases per item

For each item retained in step 1:

- **Title tokens** — split the `# <title>` line on whitespace; drop date prefixes (`YYYY-MM-DD`, `2026-`, etc.) and parenthetical durations (`(3m 1s)`).
- **Hashtags** — every `#word` or `#word/sub` *that isn't* `#thread/`, `#client/`, `#status/` (those are governance tags, not concept signals). Strip the `#`.
- **Wikilinks** — every `[[Target]]` whose target isn't an entity already in the deny-list, isn't a CRM person page, and isn't a date stamp.
- **Capitalized multi-word phrases** in the title and the first 1000 chars of body — sequences of 2–4 consecutive Capitalized-Word tokens (Title Case heuristic), kebab-cased.
- **Existing kebab-case identifiers** — tokens already containing `-` (the owner will have been using kebab-case in URLs, filenames, slugs). Treat as candidate slugs directly.

Per phrase, store: `phrase`, `kebab_slug`, `source_type`, `item_path`, `item_date`.

### 4. Cluster + filter candidates

Cluster phrases by `kebab_slug`:

- For each cluster: distinct items (count, a meeting group counting once), distinct source types (count, a meeting group counting as its one reported source), date range (min/max), display variants seen.
- Apply filters:
  - Drop clusters whose `kebab_slug` is in the deny-list (step 2).
  - Drop clusters with fewer than 3 distinct items OR appearing in only 1 source type.
  - Drop clusters whose slug is a single token ≤ 4 chars (low signal).
  - Drop clusters whose slug is purely numeric (`2026`, `12345`).
  - Apply Atlas-noise stopwords as a final pass.

### 5. Rank + select top patterns

Rank surviving clusters by a simple score:

```
score = distinct_items × source_diversity_multiplier
source_diversity_multiplier = 1.0 + (distinct_source_types - 1) × 0.5
```

Pick the top N (default `--top 25`). If fewer than `--min-patterns` survive (default 1; raise it once the corpus is large enough that a low count means the thresholds are too strict), `last-run.md` carries a diagnostic with the candidate and dedup counts so the owner can tune thresholds.

### 6. Write the report

Regenerate `{{vault_root}}/{{folders.meta}}/Dashboards/Emerging-Patterns.md` **overwrite-safe** (never append):

```markdown
---
type: dashboard-emerging-patterns
generated_by: atlas-emerge
generated_at: <YYYY-MM-DD>
window_days: <N>
window_start: <YYYY-MM-DD>
window_end: <YYYY-MM-DD>
items_scanned: <N>
patterns_surfaced: <N>
tags: [dashboard, emerging-patterns]
---

# Emerging Patterns — last <N> days

Generated <YYYY-MM-DD> by `atlas-emerge`. Window: `<window_start>` → `<window_end>`. Scanned `<N>` raw items across `<M>` source types.

This is a regenerated projection of recurring topics in `raw/` that don't yet have a `#thread/<slug>` tag or a `wiki/concepts/<slug>.md` page. Promote a pattern with `/atlas-graduate <slug>`.

## Patterns

| # | Keywords | First | Latest | Items | Sources | Slug | Graduate |
|---|---|---|---|---|---|---|---|
| 1 | Portal rewrite; portal rebuild | 2026-04-23 | 2026-05-19 | 8 | fireflies, slack, monday | `portal-rewrite` | [Graduate](/atlas-graduate portal-rewrite) |
| 2 | ... | ... | ... | ... | ... | ... | ... |

## Methodology

- Window: last `<N>` days from generation date.
- Sources walked: <list of source dirs>.
- Deny-list size: <N> known entities + <N> known concepts + <N> known threads.
- Minimum thresholds: ≥ 3 distinct items, ≥ 2 source types, ≥ 5 chars per slug.
- Meeting records that cross-link through their frontmatter ids (one call captured by several meeting ingests) count as one item from one source type, reported as the first of wispr-meetings, fireflies, gemini, teams, zoom, gong among them. This run folded <N> such record(s) into another.

## Notes

- Hand-edit nothing in this file — it's regenerated on every run.
- To suppress a false-positive cluster permanently, add its slug to `{{skills_root}}/engine/atlas-emerge/suppress.txt` (one slug per line).
- To force-promote a pattern to a tracked surface, run `/atlas-graduate <slug>`.
```

The "Graduate" column is a clickable Markdown link of the form `[Graduate](/atlas-graduate <slug>)`. Obsidian renders these as plain links — the owner copies the slug or clicks through manually. (No clickable-from-vault command-bar wiring in v1; the link is documentation, not automation.)

### 7. Write per-run summary

`{{skills_root}}/engine/atlas-emerge/last-run.md` captures one run's metadata:

```markdown
# atlas-emerge — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- mode: <dry-run | execute>
- window_days: <N>
- items_scanned: <N>
- candidates_extracted: <N>
- candidates_after_dedup: <N>
- patterns_surfaced: <N>
- duration_seconds: <float>
- min_patterns: <N>  # the `--min-patterns` value for this run
- min_patterns_met: <true | false>
- report_path: {{folders.meta}}/Dashboards/Emerging-Patterns.md
```

If `min_patterns_met: false`, log the candidate count + dedup count in the body so the owner can see whether the gap is "not enough signal" vs "thresholds too strict".

## Invocation

```bash
# Default: 30-day window, write report + last-run.md
python3 emerge.py

# Custom window
python3 emerge.py --window-days 14

# Cap surfaced patterns
python3 emerge.py --top 15

# Dry-run: print summary, don't write any files
python3 emerge.py --dry-run

# Custom paths (testing)
python3 emerge.py --vault /tmp/test-vault --report-path /tmp/test-report.md
```

## Idempotency contract

Re-running on the same corpus must:

- **Always overwrite** `Emerging-Patterns.md` — no appended content; same generation produces the same report.
- **Never create** files outside `{{vault_root}}/{{folders.meta}}/Dashboards/Emerging-Patterns.md` and `{{skills_root}}/engine/atlas-emerge/last-run.md`.
- **Not write** to `{{folders.raw}}/` (read-only invariant per DEC-009).
- **Not touch** `{{folders.wiki}}/concepts/` or `{{folders.wiki}}/entities/` — promotion is `atlas-graduate`'s job.

Verified mechanically: two consecutive `--execute` runs against an unchanged corpus produce byte-identical `Emerging-Patterns.md` files (modulo `generated_at:` timestamp).

## Edge cases

- **Empty corpus** — too few items in the window to form a cluster: skill writes a report with a `0 patterns surfaced` summary and exits 0. Surfacing patterns needs non-trivial corpus content; the diagnostic in `last-run.md` tells the owner whether to widen the window or wait for more ingests.
- **Mono-source cluster** — a phrase appears 10× in `raw/fireflies/` and nowhere else: surface only if cross-source diversity adds a multiplier. Mono-source clusters often reflect transcription noise.
- **Known entity variants** — "Ledgerline Cloud" and "ledgerline-cloud" both deny-listed under canonical_id `ledgerline-cloud`. Fuzzy comparison: lowercase, drop hyphens/spaces, prefix match.
- **Stop-word collisions** — phrases that contain stopwords are kept if they're multi-token and the non-stopword part is meaningful (`rate plan audit` keeps `audit`; `the meeting today` is dropped).
- **Date parsing failure** — fall back to file mtime; never silently include an item with no timestamp signal.

## Suppression list

Maintain `{{skills_root}}/engine/atlas-emerge/suppress.txt`:

- One kebab-slug per line.
- Comments start with `#`.
- Loaded into the deny-list at step 2 before clustering.

The owner adds slugs here when they want a pattern to stop surfacing without graduating it (false positives that aren't worth a concept page).

## Relationship to other skills

- `atlas-wiki-materialize` — produces `{{folders.wiki}}/entities/` which this skill *consumes* as the deny-list anchor. Patterns that are already known entities don't re-surface.
- `atlas-graduate` — downstream skill that promotes an emerged pattern into either a `#thread/<slug>` tag-and-backfill or a `{{folders.wiki}}/concepts/<slug>.md` materialization.
- `atlas-nightly` — scheduled agent that runs this skill once per day at 22:00 in `{{timezone}}` (DEC-013).

## Anti-goals (NOT v1)

- LLM-driven entity extraction. v1 uses rule-based n-gram + capitalized-phrase extraction. LLM clustering is a v2 concern.
- Automatic tag application *by this skill*. `atlas-emerge` still only **surfaces** patterns into the dashboard — it never writes tags itself. (As of DEC-019, automatic graduation does happen downstream: `auto_graduate.py` reads this dashboard and auto-applies `#thread/` tags for high-confidence patterns. That is a separate skill with its own guards + policy; emerge's job is unchanged.)
- Cross-thread synthesis ("which threads touch each other"). Deferred to v2.
