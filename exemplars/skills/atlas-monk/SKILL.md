---
name: atlas-monk
description: Helper + shared conventions for the Monk Manual flows (an owner-driven, never-scheduled priority + reflection practice at <areas>/Monk-Manual/, governed by DEC-027). Backs the /atlas-monk-* slash commands. The Python helper monk_scaffold.py resolves paths, does ISO-week/Monday-Friday date math, instantiates the Monk-* templates, and writes notes idempotently (never clobbers a filled-in note). Triggers on "monk manual", "/atlas-monk-*", "set my month/week/today", "digitize my monk page", "weekly pattern read", "Sunday reflection".
exemplar-of: atlas-monk
status: active
requires: [cli/python3]
---

# atlas-monk

The deterministic half of the Monk Manual practice. The interactive ranking and
reflection live in the `/atlas-monk-*` slash commands (git-tracked in the owner's
`.claude/commands/` folder); this skill owns the **path resolution, date math,
template instantiation, and idempotent writes** so those mechanics are never
re-authored by hand each run (DEC-027 rationale).

**This practice never nudges (DEC-027).** Nothing here is scheduled. Every flow
is pull-only — the owner pastes a prompt or runs a command. No streaks, no
overdue-red, no accountability. Carry-over is quiet; patterns are neutral data.
Do **not** wire any Monk flow into a cron / `atlas-nightly` / scheduled agent —
that reverses the owner's explicit opt-out and needs a follow-up decision.

## The helper — `monk_scaffold.py`

Stdlib only (DEC-021, no PyYAML — frontmatter is carried verbatim from the
template with Templater date tokens substituted, never built from a dict).

```bash
python3 {{skills_root}}/atlas-monk/monk_scaffold.py <flow> [--date YYYY-MM-DD] [--force] [--print] [--vault PATH]
```

| flow | writes | from template |
|---|---|---|
| `month` | `Priorities/<label>-Month.md` | `Monk-Monthly.md` |
| `week` | `Priorities/<YYYY>-W<WW>-Week.md` | `Monk-Weekly.md` |
| `today` | `Priorities/daily/<date>-monk.md` | `Monk-Prepare.md` |
| `page` | `pages/<date>-page.md` | `Monk-Page-Capture.md` |
| `reflect` | `Reflections/<YYYY>-W<WW>-Sunday.md` | `Monk-Sunday-Reflection.md` |

Paths are under `<vault>/{{folders.areas}}/Monk-Manual/`. The **vault root is resolved
from the invocation cwd** (`--vault` → `MONK_VAULT` env → walk up for a `.obsidian/`
marker → `{{vault_root}}`), so running a command from a vault worktree writes into
that worktree; the Monk data + `Monk-*` templates ride along with the vault.
Filenames are name-suffixed (`-monk`, `-page`, `-Sunday`) to dodge the reserved bare
`YYYY-MM-DD.md` daily-note basename (DEC-027).

**month flow** takes the judgment inputs as flags so the helper still owns the
frontmatter (the orchestrating session decides the values, the helper writes them):

```bash
python3 {{skills_root}}/atlas-monk/monk_scaffold.py month \
  --label 2026-07 --start 2026-06-29 --end 2026-08-05 \
  --theme "Surrender" --title "Bridge month (Jun 29 - Aug 5, 2026) - Monthly focus"
```

`--label` is the book-cycle month (defaults to `--date`'s month). The Monk "month"
is a ~30-day book cycle, not a calendar month — a bridge month can span Jun 29 →
Aug 5, so pass `--start`/`--end`/`--title` explicitly when it's irregular.

**week flow** anchors on the **Monday** of `--date`'s ISO week, so a mid-week run
still produces the correct Mon→Fri period (the bare template encodes period_end as
`now+4`, which only holds if instantiated on Monday — the helper fixes that).

### Output contract (stdout)

```
STATUS=CREATED|EXISTS
PATH=<absolute path>
TEMPLATE=<template filename>
```

- `STATUS=CREATED` — the skeleton was written. The command then fills the body.
- `STATUS=EXISTS` — the note already exists; the helper wrote **nothing**. The
  command Edits the existing note (never re-scaffolds over filled content).
- `--force` overwrites with a fresh skeleton — **destroys** any filled-in content.
  Only for an explicit "reset this note" request.
- `--print` also dumps the rendered (or existing) note to stdout after a `---`.

### Idempotency contract

Re-running a flow whose note already exists is a no-op write (STATUS=EXISTS).
This is the core safety property: a daily Prepare or a digitized page that the
owner has filled in is never clobbered by a careless re-run. The helper on an
existing note leaves it byte-identical.

## Shared ranking procedure (used by month/week/today)

The owner stays in the decision — never just hand them a list. Score each
candidate priority across the three lenses they chose, surface each as
**High / Med / Low** with a one-line *why*, propose an order, and ask them to
confirm or reorder:

1. **Theme fit** — does it serve this month's theme + habit? (from `Themes.md`)
2. **Deadlines & stakes** — hard date? what breaks if it slips?
3. **Energy & season fit** — does it fit current capacity and the season?

Ties break toward **finishing something already started** (the last-10% edge)
over starting something new. A forced work/faith/family balance is **not** a
ranking lens (the owner's choice) — the Rule of Life (Surrender, process over
outcome) shapes *encouragement and reframes*, not rank order.

## Boundaries (DEC-027)

- **Never edit Daily Notes** (`{{folders.daily}}/`). Monk priorities reach the
  daily note only through its existing `due today` Tasks query — the `today` flow
  emits tasks tagged `📅 <date> #monk/priority`, nothing writes into the daily note.
- **Pages live in the Area, not `{{folders.raw}}/`.** `pages/` captures are freely-editable
  Area notes, not append-only ingest. A `{{folders.raw}}/monk-manual/` source would need its
  own decision entry.
- **`digitize` is multimodal-only.** It reads a photo of a handwritten page
  (vision) — run it in a Cowork / Claude-app / Claude Code session that can see
  images. There is no headless handwriting-OCR helper.
- **`#monk/` stays excluded** from `atlas-morning`'s overdue-task scan and the `Weekly-Review`
  template's vault-wide TASK query, so aged Monk priorities never resurface as
  "overdue".

## Fallback if this helper is missing

If `monk_scaffold.py` is absent, each command can
still scaffold by hand: read the mapped `Monk-*` template from
`{{folders.resources}}/Templates/`, substitute the Templater date tokens (`<% tp.date.now("YYYY-MM-DD") %>`
→ the date, week period_end = Monday+4 = Friday, `WW` = ISO week), and write to
the path in the table above — but only if the target does not already exist.
