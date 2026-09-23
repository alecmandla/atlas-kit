---
name: atlas-synthesize
description: Cross-source thread synthesis agent (DEC-020). Separate scheduled agent firing ~22:45, after atlas-nightly. For each #thread/<slug> whose evidence has changed, weaves a living, cited narrative page at wiki/synthesis/<slug>.md — current state, how-it-evolved timeline, key decisions, open questions, key people — from a token-bounded evidence bundle gathered by synthesize.py. Change-detected (only re-writes changed threads). Idempotent. Triggers on "atlas synthesize", "synthesize threads", "rebuild synthesis", "/atlas-synthesize", or any scheduled 22:45 run.
exemplar-of: atlas-synthesize
status: active
requires: [mcp/scheduled-tasks, cli/python3]
---

# atlas-synthesize

You are the synthesis agent. You run once per day at ~22:45 local (after `atlas-nightly` has finished ingesting + graduating), scheduled via `mcp__scheduled-tasks`. Your job: for each thread whose underlying evidence has changed since its last synthesis, write a living, **cited** narrative page at `{{vault_root}}/{{folders.wiki}}/synthesis/<slug>.md`.

This skill is **narrative-first and hybrid**, exactly like `atlas-weekly`:

- **`synthesize.py` (deterministic)** gathers a ranked, token-bounded, citation-ready *evidence bundle* per thread, and detects which threads changed.
- **You (the orchestrating Claude session)** weave that bundle into prose.

**The cardinal rule: you never invent facts.** Every factual claim in a synthesis page cites a specific bundled source by its `[[id]]`. If the evidence doesn't support a claim, you don't make it. This is why the page carries a "verify against sources" banner — it's a generated projection, not ground truth.

## Mental model

- `{{folders.raw}}/` is append-only immutable source (DEC-009). `{{folders.wiki}}/` is a rewriteable projection (DEC-004). `{{folders.wiki}}/synthesis/<slug>.md` is regenerated whenever a thread's evidence changes — never hand-edit it outside the `## Notes` region.
- **Change detection:** `synthesize.py` computes an `evidence_fingerprint` over a thread's evidence files and stores it in the page frontmatter. On each run it compares the live fingerprint to the stored one, so you only re-synthesize `new` or `changed` threads — unchanged pages are left untouched. This is what keeps the nightly LLM cost bounded.
- **Search vocabulary (`thread-vocab.yml`):** the raw matcher is built from the slug plus `keywords:` on `{{folders.wiki}}/concepts/<slug>.md`. Hand-tagged threads have no concept page, so their only pattern is the literal slug phrase — and descriptive slugs (`ledgerline-integration`) never appear verbatim in source text, yielding **zero raw matches**. `thread-vocab.yml` in this skill dir supplies the terms those threads are actually written in; `thread_keywords()` reads it alongside the concept page. Two constraints matter: terms under 4 characters are silently dropped, and because `gather_raw()` ranks by **recency only** (20 items, 5 per source), a broad term *narrows* effective coverage rather than widening it — a specific phrase like `"Ledgerline sync"` can span years of evidence while the bare word `"ledgerline"` collapses the bundle to the last few days of chatter. Prefer the most specific phrase that still matches, and dry-run before trusting an entry:

  ```bash
  python3 - <<'EOF'
  import sys; sys.path.insert(0, '.')
  import synthesize as S, pathlib
  vault = pathlib.Path("{{vault_root}}")
  para, raw = S.evidence_for(vault, '<slug>')
  print(len(para), 'para +', len(raw), 'raw',
        (raw[-1]['date'], raw[0]['date']) if raw else '')
  EOF
  ```

  Changing vocabulary changes the evidence set, so the fingerprint gate re-flags the thread automatically — no manual invalidation needed.

- **Token bounding:** the bundle is capped (≤ 20 raw items, source-diversity balanced, 600-char excerpts). The raw matches are the *top* matches, not the full set — if a thread clearly has more evidence than the bundle shows, say so in the synthesis rather than implying completeness.

## Step 1 — Build the worklist

```bash
python3 {{skills_root}}/atlas-synthesize/synthesize.py --list --stale-only
```

This prints one slug per line for every thread that is `new` (no synthesis page yet) or `changed` (evidence moved since last run). Run without `--stale-only` to see the full status table while debugging.

- **Cap at `MAX_THREADS = 5` per run.** If more than 5 are stale, take the first 5; the rest surface again on the next run. Log the deferral in `last-run.md`.
- If the worklist is empty, write `last-run.md` with `threads_synthesized: 0` and exit. Nothing changed — nothing to do.

## Step 2 — Gather each thread's evidence

For each slug in the worklist:

```bash
python3 {{skills_root}}/atlas-synthesize/synthesize.py --thread <slug>
```

Read the bundle it prints:

- A **`## FRONTMATTER`** block with ready-to-use values (`evidence_fingerprint`, `evidence_count`, `sources`, `first_evidence`, `last_evidence`, `tags`). Copy these into the page verbatim — the fingerprint is what makes change-detection work next run.
- **`## Tagged PARA notes`** — authoritative, curated notes carrying `#thread/<slug>`. Weight these heavily.
- **`## Raw source matches`** — the top dated matches across sources, each with an `[[id]]` you cite.

## Step 3 — Write `{{folders.wiki}}/synthesis/<slug>.md`

**Preserve the editable region first.** If the page already exists, read it and capture the content between `<!-- atlas-synthesize:editable-start -->` and `<!-- atlas-synthesize:editable-end -->`. Re-insert it verbatim at the end of the regenerated page. Everything else is regenerated.

Write the page with this exact structure:

```markdown
---
type: wiki-synthesis
canonical_id: <slug>
generated_by: atlas-synthesize
generated_at: <YYYY-MM-DD>
evidence_fingerprint: <from bundle>
evidence_count: <from bundle>
sources: [<from bundle>]
first_evidence: <from bundle>
last_evidence: <from bundle>
tags: [wiki-synthesis, thread/<slug>]
---

> 🤖 Generated by `atlas-synthesize` on <YYYY-MM-DD> from <evidence_count> sources spanning <first_evidence> → <last_evidence>. **This is a projection — verify against the cited sources before relying on it.** Hand-edits belong only in `## Notes` at the bottom (preserved across regenerations).

# <Title-cased slug> — synthesis

## Current state

<2–5 sentence narrative: where this thread stands *right now*, grounded in the most recent evidence. Cite the latest items, e.g. "As of [[<id>]] (<date>), …". This is the section a reader checks first.>

## How it evolved

<Chronological narrative of how the idea/work changed over time — the whole point of threads. Walk first_evidence → last_evidence, citing the items that mark each shift. Prose, not a raw list; group by phase where natural.>

## Key decisions

- <decision> — <date>, per [[<id>]]
- ...
- _(or "No explicit decisions found in the current evidence." — don't manufacture them.)_

## Open questions

- <unresolved question surfaced by the evidence>
- ...

## Key people

- [[Person-Name]] — <their role in this thread, if the evidence shows it>
- _(derive from tagged PARA notes' `participants:` and named mentions; omit if none.)_

## Sources

<The cited items, most recent first — this is the audit trail.>

- [[<id>]] — <date> (<source>)
- ...

## Notes

<!-- atlas-synthesize:editable-start -->
_Hand-add corrections, context, and decisions here. Preserved across regenerations._
<!-- atlas-synthesize:editable-end -->
```

### Quality bar

- **Every factual sentence in Current state / How it evolved / Key decisions cites at least one `[[id]]`.** A sentence with no citation must be a clearly-framed inference ("This suggests…"), not an asserted fact.
- **Narrative, not a dump.** Current state and How it evolved are prose. If you find yourself listing the bundle back, you're not synthesizing — find the throughline.
- **Honesty about coverage.** If the bundle is thin (1–2 items) or skewed to one source (e.g. all `wispr` voice notes = the owner thinking out loud, not cross-system corroboration), say so explicitly rather than overstating certainty.
- **Don't cross threads.** Synthesize only this slug's evidence. Cross-thread relationships are a future concern.

## Step 4 — Write `last-run.md`

`{{skills_root}}/atlas-synthesize/last-run.md`:

```markdown
# atlas-synthesize — last run

- timestamp: <YYYY-MM-DDTHH:MM:SS>
- threads_stale: <N>
- threads_synthesized: <N>   # capped at MAX_THREADS
- threads_deferred: <N>      # stale beyond the cap, next run
- pages_written: [<slug>, ...]
- duration_seconds: <float>
```

## Idempotency contract

- A thread is re-synthesized **only when its `evidence_fingerprint` changes.** Two runs with no new evidence write zero pages (the worklist is empty).
- The `## Notes` region is **preserved verbatim** across regenerations via the editable markers.
- The skill **never writes to `{{folders.raw}}/`** (DEC-009) and never edits the tagged PARA notes — it only reads them.
- `{{folders.wiki}}/synthesis/<slug>.md` is fully regenerated (outside `## Notes`) when evidence changes — it's a projection, per DEC-004.

## Invocation

When invoked manually (via Claude Code):
1. Read this SKILL.md.
2. Execute Steps 1–4. Report: threads synthesized, pages written, any deferred.

When invoked by the scheduled task:
- Cron: `45 22 * * *` (22:45 local, daily — after `atlas-nightly` at 22:00).
- Registered via `mcp__scheduled-tasks__create_scheduled_task` (DEC-020).
- The scheduled task fires a Claude Code session with a self-contained prompt pointing back to this SKILL.md.

## Relationship to other skills

- **Upstream**: `atlas-nightly` ingests `{{folders.raw}}/` and runs `atlas-auto-graduate`, which creates the `#thread/<slug>` tags this skill synthesizes. Running after nightly means today's new evidence + newly-graduated threads are included.
- **Consumes**: tagged PARA notes + `{{folders.raw}}/` (read-only), and `{{folders.wiki}}/concepts/<slug>.md` keywords when a thread was graduated.
- **Sister narrative agent**: `atlas-weekly` — same hybrid pattern (deterministic evidence gathering + Claude-written prose).

## Anti-goals (NOT v1)

- Inventing facts or decisions not present in the evidence. Every claim is cited; thin evidence is flagged, not padded.
- Cross-thread synthesis ("which threads touch each other"). One slug per page; deferred by design.
- Synthesizing threads with zero tagged evidence. If `synthesize.py --thread` finds nothing, skip it.
- Editing `{{folders.raw}}/` or the tagged PARA notes. Read-only into source; the synthesis page is the only thing written.
- Re-synthesizing unchanged threads. The fingerprint gate is load-bearing for cost control — don't bypass it.
