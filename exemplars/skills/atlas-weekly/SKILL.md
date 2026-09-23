---
name: atlas-weekly
description: Friday 6 PM weekly review agent. Generates a narrative weekly review at {{folders.areas}}/Weekly-Reviews/<ISO-week>.md from the Weekly-Review.md template, populated with: this week's meetings the owner attended, shipped tasks (completed this week), open action items still unticked, threads with new activity this week, and threads gone quiet for ≥ 14 days. Idempotent — re-runs the same Friday overwrite the file (it's a snapshot). Triggers on "atlas weekly", "weekly review", "/atlas-weekly", or any scheduled Friday 18:00 run.
exemplar-of: atlas-weekly
status: active
requires: [cli/python3]
---

# atlas-weekly

You are the weekly review agent. You run once per week on Friday at 18:00 in `{{timezone}}` (scheduled via `mcp__scheduled-tasks`). Your job:

1. Compute the current ISO week.
2. Build five evidence lists from the vault's current state.
3. Render the weekly review using the `Weekly-Review.md` template, populated with synthesized prose + lists.
4. Write the review to `{{folders.areas}}/Weekly-Reviews/<ISO-week>.md`.
5. Write a per-run summary at `{{skills_root}}/atlas-weekly/last-run.md`.

This skill is **narrative-first**. The review should read like a coherent weekly recap, not a dump of lists. Use prose for the "What got done this week" and "What's blocked or stuck" sections, with bullets only where the data is genuinely list-shaped (meetings, action items, threads).

## Path choice — why `{{folders.areas}}/Weekly-Reviews/` and not a life-area subfolder

It is tempting to file weekly reviews under one area (for example a personal-finance area). Weekly reviews span every area of life (work, projects, health, threads), not just one. A top-level `{{folders.areas}}/Weekly-Reviews/` keeps them discoverable and avoids miscategorizing them under a specific life-area.

If the owner prefers a different location, change the `OUTPUT_DIR` constant in Step 4 and update the relevant tests.

## Step 1 — Compute the ISO week

```python
from datetime import date
TODAY = date.today()
ISO_YEAR, ISO_WEEK, ISO_WEEKDAY = TODAY.isocalendar()
ISO_WEEK_STR = f"{ISO_YEAR}-W{ISO_WEEK:02d}"   # e.g. "2026-W21"

# Week boundaries (Monday → Sunday, ISO convention)
from datetime import timedelta
MONDAY = TODAY - timedelta(days=ISO_WEEKDAY - 1)
SUNDAY = MONDAY + timedelta(days=6)
```

The `<ISO-week>` filename always reflects the ISO week of `TODAY` (the day the skill runs). Friday at 18:00 means the week is captured at end-of-business Friday — meetings still happening Friday evening or scheduled for Saturday/Sunday won't appear until next week's review. That's fine; weekly reviews are end-of-workweek snapshots.

## Step 2 — Build the five evidence lists

### 2a — This week's meetings (where the owner attended)

Walk every `.md` file under `{{vault_root}}/{{folders.projects}}/`, `{{vault_root}}/{{folders.areas}}/`, and any other PARA folder containing meeting notes. For each:

- Parse YAML frontmatter; require `type: meeting` (or `tags: [meeting]`).
- Require `date:` in the range `[MONDAY, SUNDAY]`.
- Require `participants:` to contain `[[{{owner_slug}}]]` (per DEC-003 — attendance is wikilink-encoded, not boolean).

Collect: title (or filename stem), date, participants list, project frontmatter, file path.

Sort by date ascending. This list grounds the "what happened" narrative.

### 2b — Shipped tasks this week (completed in the last 7 days)

Walk every `.md` under `{{vault_root}}/{{folders.projects}}/` and `{{vault_root}}/{{folders.areas}}/`. For each, extract lines matching:

```
^- \[x\] .*✅ (\d{4}-\d{2}-\d{2})
```
or the plain-text Tasks plugin format:
```
^- \[x\] .* completion:(\d{4}-\d{2}-\d{2})
```

Filter to completion dates in the range `[MONDAY, SUNDAY]`.

If neither token exists, fall back to: any `^- \[x\] ` line in a file whose **mtime** is within the last 7 days (rough heuristic; surface the heuristic-fallback count in `last-run.md`).

Collect: task text, file path, completion date. Cap at 50; surface the cap in the review if exceeded.

### 2c — Open action items still unticked

Walk PARA. For each, extract:

```
^- \[ \] .+
```

For ranking, prefer items with a due date (`📅 YYYY-MM-DD` or `due:YYYY-MM-DD`). Among those, prefer items overdue or due this week (already on the owner's radar).

Collect: task text, file path, due date if any. Cap at 25; surface the cap.

### 2d — Threads with new activity this week

Walk the entire vault for files modified in the last 7 days. For each, extract every unique `#thread/<slug>` token.

Group by slug; per slug, capture: distinct file count, most-recent mtime, sample file paths (up to 3).

Filter: include only slugs appearing in ≥ 2 distinct files this week.

Sort by most-recent activity descending.

### 2e — Threads gone quiet ≥ 14 days

Walk the entire vault for every `#thread/<slug>` token. For each slug:

- Find the most-recent mtime across all files containing the slug.
- If `(TODAY - most-recent mtime) >= 14 days`, mark as stalled.

Cross-reference against the "active this week" set (2d): if a slug appears in both, prefer the active classification (something just touched it).

Sort stalled threads by stalled-duration descending (most-stalled first).

## Step 3 — Build the "Needs attention" alert

The review must contain ≥ 3 "needs attention" items. Synthesize from:

- Overdue action items (from 2c, items with `due_date < TODAY`).
- Stalled threads (from 2e).
- Any meeting from 2a that has unresolved action-item callouts in the note body.

If fewer than 3 items qualify under those rules, lower the bar in this order:

1. Add the top 3 oldest stalled threads regardless of overall count.
2. Add any unticked action item with no due date that is in a high-priority project (use the `tier:` frontmatter if present).
3. As a final fallback, surface "no needs-attention items found — review the dashboards manually" with one bullet pointing to `{{folders.meta}}/Dashboards/`.

The bar lowers automatically because the requirement is `≥ 3 items based on the data` — never fabricate items, but widen the net until enough genuine signal surfaces.

## Step 4 — Render the review

```
OUTPUT_DIR  = {{vault_root}}/{{folders.areas}}/Weekly-Reviews/
OUTPUT_PATH = <OUTPUT_DIR>/<ISO_WEEK_STR>.md
```

Create `OUTPUT_DIR` if needed. The file is **overwrite-safe** (re-running the same Friday replaces the snapshot).

Instantiate the `Weekly-Review.md` template by replacing Templater tokens:

- `<% tp.date.now("YYYY-MM-DD") %>` → `TODAY`

Then populate sections by replacing the template's placeholder bullets / Dataview blocks with synthesized content. Keep the section headings from the template; add an "## Atlas weekly synthesis" section at the top with the needs-attention alert, ISO-week banner, and metadata. Final structure:

```markdown
---
date: <TODAY>
type: weekly-review
iso_week: <ISO_WEEK_STR>
week_start: <MONDAY>
week_end: <SUNDAY>
tags: [weekly-review]
generated_by: atlas-weekly
generated_at: <YYYY-MM-DDTHH:MM:SS>
---

# Weekly Review — <ISO_WEEK_STR> (<MONDAY> → <SUNDAY>)

> **Needs attention this week** — <N> items
>
> 1. <highest-priority item with one-line context>
> 2. <next>
> 3. <next>
> *(see Needs attention section below for the full list)*

## Atlas weekly synthesis

*Generated by `atlas-weekly` at <HH:MM> on <TODAY>.*

**By the numbers:**
- Meetings attended: <N>
- Tasks shipped: <N>
- Open action items: <N> (cap: 25)
- Active threads: <N>
- Stalled threads: <N>

## What got done this week

<2–4 sentence narrative synthesizing the shipped tasks + meetings. Reference specific projects by name. If a project shipped multiple items, group them in one paragraph. Don't just list — synthesize.>

### Shipped tasks (<N>)

- [x] <task text> — [<file>](<vault-relative path>) <completion date>
- ...

## What's blocked or stuck

<2–4 sentence narrative on stalled threads + overdue items. If a thread has been quiet for > 30 days, flag it as a candidate for `{{folders.archive}}/`.>

### Stalled threads (≥ 14 days)

- `#thread/<slug>` — last activity <date> (<days> days ago, <N> files)
- ...

## Needs attention

<bulleted list, one per item, with file link + due date if applicable>

- [ ] <task text> — [<file>](<vault-relative path>) (due <date>, <X> days overdue)
- ...

## This week's meetings (you attended)

| Date | Title | Project | Path |
|---|---|---|---|
| <date> | <title> | <project> | [link](<path>) |
| ... | ... | ... | ... |

## Active threads (new activity this week)

- `#thread/<slug>` — <N> files updated, last activity <date>
- ...

## Open action items (<N>)

<grouped by file folder>

### `{{folders.projects}}/<project>/`

- [ ] <task> (due <date>)
- ...

### `{{folders.areas}}/<area>/`

- [ ] <task>
- ...

## Next week's focus

*(Hand-edit this section — the agent doesn't pick priorities.)*

1. 
2. 
3. 

## Notes / reflections

*(Hand-edit. Preserved across re-runs by the idempotent overwrite contract — see SKILL.md.)*
```

### Idempotent overwrite + handwritten preservation

`atlas-weekly` overwrites the whole file on re-run. To preserve hand-edited content in **"Next week's focus"** and **"Notes / reflections"**:

1. Before overwriting, read the existing file (if present).
2. Extract content between `## Next week's focus` and the next `## ` heading. Reuse it if non-trivial (more than just template scaffolding).
3. Same for `## Notes / reflections` (everything from that heading to EOF).
4. Splice the preserved content back into the freshly generated review.

Every other section is fully regenerated.

## Step 5 — Write last-run.md

`{{skills_root}}/atlas-weekly/last-run.md`:

```markdown
# atlas-weekly — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- iso_week: <ISO_WEEK_STR>
- week_start: <MONDAY>
- week_end: <SUNDAY>
- output_path: <OUTPUT_PATH>
- meetings_attended: <N>
- tasks_shipped: <N>
- tasks_shipped_heuristic_fallback: <N>   # subset of above counted via mtime heuristic
- open_action_items: <N> (cap 25)
- active_threads: <N>
- stalled_threads: <N>
- needs_attention_count: <N>
- preserved_handwritten_sections: [<"Next week's focus", "Notes / reflections">]
- duration_seconds: <float>
```

## Idempotency contract

- Re-running on the same Friday (same ISO week) **overwrites** the snapshot at `<ISO_WEEK_STR>.md`.
- Two sections — "Next week's focus" and "Notes / reflections" — are **preserved across re-runs** by the splice logic in Step 4.
- The skill never writes to `{{folders.raw}}/` (DEC-009).
- The skill never modifies meeting notes, person notes, or other PARA files — it only **reads** them.

Verified mechanically: two consecutive runs on the same Friday with no hand-edits produce byte-identical reviews modulo `generated_at:` timestamp.

## Edge cases

- **Run on a non-Friday** (manual invocation, or DST shift): proceed normally. The ISO-week filename is whatever week `TODAY` falls in. If two manual runs happen in the same week, they overwrite the same file.
- **Empty week** (no meetings, no shipped tasks, no thread activity): write the review anyway with the by-the-numbers showing zeros. The needs-attention bar lowers per Step 3's fallback rules.
- **Weekly-Review.md template missing**: hard-fail with a clear error. Surface: `"Weekly-Review.md template not found at {{vault_root}}/{{folders.meta}}/Templates/Weekly-Review.md"`.
- **`[[{{owner_slug}}]]` wikilink absent across the vault**: very unlikely once `atlas-people-extract` has run; the meeting-attendance filter returns empty. Surface the count = 0 and continue.
- **ISO-week edge cases around year boundary**: `date.isocalendar()` handles W52/W53 → W01 transitions correctly. Use the stdlib, don't reinvent.

## Invocation

When invoked manually (via Claude Code):
1. Read this SKILL.md.
2. Execute Steps 1–5 sequentially.
3. Report: output path, counts, needs-attention items, duration.

When invoked by the scheduled task:
- Cron: `0 18 * * 5` (Friday 18:00 in `{{timezone}}`).
- Registered via `mcp__scheduled-tasks__create_scheduled_task`.
- The scheduled task fires a Claude Code session with a self-contained prompt that points back to this SKILL.md.

## Relationship to other skills

- **Upstream**: `atlas-nightly` populates `{{folders.raw}}/` and runs `atlas-emerge` each evening — by Friday afternoon, the corpus + emerging-patterns dashboard reflect the week.
- **Sister scheduled agents**: `atlas-morning` and `atlas-health`.
- **Downstream**: the owner reads the review on Friday evening / Monday morning, then graduates patterns via `/atlas-graduate <slug>` and works the needs-attention list.

## Anti-goals (NOT v1)

- LLM-generated narrative prose. v1 synthesizes prose from rule-based templates ("Shipped N tasks across M projects. Largest cluster: ..."). LLM rewriting is a v2 concern.
- Email or Slack delivery of the review. Read-only into the vault.
- Multi-week trend charts. The review is a single-week snapshot; trend analysis is a v2 dashboard.
- Auto-triage of open action items (re-prioritizing, closing as stale). Surface, don't decide.
