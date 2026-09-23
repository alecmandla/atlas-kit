---
name: atlas-research
description: On-demand query/answer over the EXISTING Atlas vault — research a topic or answer a question by progressive drilldown (wiki/synthesis → concepts → entities → PARA → raw), citing every claim with a resolvable [[wikilink]]. Read-only on the knowledge spine; v1 never ingests. The pull counterpart to the nightly push pipeline. Triggers on "atlas research", "research <topic>", "what do I have on", "answer from my vault", "what does my vault say about", "ask atlas", "/atlas-research".
exemplar-of: atlas-research
status: active
requires: [cli/python3]
---

# atlas-research

You answer questions and assemble topic briefs **from what is already in the owner's vault** — you never invent facts, and in v1 you never ingest new sources. Every factual claim cites a specific vault note by its `[[id]]`, and a claim with no citation must be clearly framed as your inference. You are the *pull* counterpart to the nightly *push* pipeline: the engine already wrote `{{folders.raw}}/` and the wiki spine; your job is to make it answer.

`cli/ripgrep` is optional: the helper falls back to a pure-Python scan when it is absent. `mcp/fireflies` is optional too: it enables the capped transcript escalation in Step 2a; without it, answer from the summary records on disk.

## Mental model — read the synthesized layer first, drill to raw last

The vault is layered from most-synthesized to ground-truth. **Read top-down and stop the moment the question is answered.** Reaching straight for `{{folders.raw}}/` defeats the whole design.

| Layer | Where | Read it when |
|---|---|---|
| 0 — index/MOC | `{{folders.wiki}}/index.md`, `{{folders.meta}}/MOCs/` | to discover what exists |
| 1 — synthesis | `{{folders.wiki}}/synthesis/<slug>.md` | **first**, if one exists for the topic — already cited prose |
| 2 — concept | `{{folders.wiki}}/concepts/<slug>.md` | thread hub + the raw evidence set |
| 3 — entity | `{{folders.wiki}}/entities/<Name>.md` | to pivot from a name to its mentions |
| 4 — PARA | `{{folders.projects}}/`, `{{folders.areas}}/`, `{{folders.resources}}/`, `{{folders.crm}}/People/`, `{{folders.meta}}/` | meeting/person/project specifics the synthesis cites |
| 5 — raw | `{{folders.raw}}/<source>/<id>.md` | **last**, only to verify a cited claim or when no synthesis covers it |

Wikilinks resolve **by filename**: `[[01ABCDEF…]]` → `{{folders.raw}}/fireflies/01ABCDEF….md`, `[[2000001]]` → `{{folders.raw}}/monday/**/2000001.md`, `[[2026-05-28 — Pinecrest Lodge weekly sync]]` → the PARA meeting note. The `search` command hands you a drill map so you open by path instead of guessing.

## Step 0 — Classify intent (query is the default)

- A **question or topic** ("what do I have on X", "status of X", "summarize X", "how did X evolve") → **query mode** (this skill). This is the default even when the user says "research".
- A request to **add/ingest sources** (drops a URL/file, says "ingest/append/pull in") → **NOT this skill in v1.** Tell the user which ingest skill to run (`atlas-gmail-ingest`, `atlas-slack-ingest`, …) or `/atlas-nightly`. Ingest is opt-in by an explicit verb or a dropped source — never inferred.
- Ambiguous → default to query. Answering from existing material is cheap; ingesting is expensive and out of scope here.

## Step 1 — Build the read-first evidence map

```bash
python3 {{skills_root}}/atlas-research/research.py search --question "<the user's question, verbatim>"
```
Pass `--terms a,b,c` if you know the canonical names better than the question wording (e.g. the entity is "Ledgerline-Cloud" but the user typed "LL cloud"). The output gives you: **Direct hubs** (read first), **ranked candidates by layer**, and a **drill map** (`[[id]] -> path`).

## Step 2 — Read top-down, stop early

1. Read every **Direct hub** + the top `synthesis`/`concept` page **in full** — they are short and pre-cited. For many questions you can answer here.
2. Only if specifics are missing, open the top `para` notes, then specific `raw` files **via the drill map** — never bulk-read raw across many files.
3. Honor the context budget: stop as soon as you can answer. If the evidence is thin or absent, say so plainly (and offer to ingest) rather than padding.

### Step 2a — Transcript escalation (Fireflies API, capped)

Meeting evidence on disk is summary-only (DEC-032): the meeting note and its `{{folders.raw}}/fireflies/<id>.md` summary record never contain the full transcript. When the evidence bundle points at a meeting and the summary **cannot answer the question** (you need the exact wording, a number someone quoted, who committed to what, or detail the overview glosses over), escalate to the live transcript:

1. Take `meeting_id` from the meeting note's (or raw record's) frontmatter.
2. Fetch via the Fireflies MCP: `fireflies_get_transcript(transcriptId=<meeting_id>)` — returns speakers + sentences.
3. Quote what you need and cite **the meeting note** (`[[YYYY-MM-DD — Title]]`) as the source — the transcript itself has no vault file to link.

**Hard cap: ≤ 3 transcript fetches per question.** Transcripts are large; a broad question must not pull dozens into context. Choose the 1–3 meetings whose summaries look most likely to contain the answer; if the cap isn't enough, say which additional meetings look relevant and let the user ask a narrower question. This is a read-only API call — never write the fetched transcript into the vault, and never touch the fireflies ingest cursor (`state.json`).

## Step 3 — Compose the cited answer

Answer in chat in this shape:

```
**<one-line direct answer>**

<2–5 short paragraphs / bullets. Every factual claim ends with its source: [[id]].
Unsupported inferences are explicitly framed as "Inference:" / "Likely".>

**Confidence:** high | medium | low — <one line: high = multiple corroborating sources incl. a synthesis/PARA note; medium = a few sources, some gaps; low = sparse/old/single-source raw only>
**Counter-evidence / what would change this:** <anything that contradicts or would revise the answer; "none found" is a valid, useful answer>
**Open questions:** <what the vault does NOT answer>
**Sources:** [[id]] · [[id]] · [[id]]  (the notes you actually used)
```

Match the synthesis bar: a real thesis, explicit counter-evidence, calibrated confidence. Don't overclaim from sparse raw.

## Step 4 — Citation gate (always, before you finalize)

Every `[[link]]` in your answer must resolve to a real file. Write your draft answer to a temp file and check it:
```bash
python3 {{skills_root}}/atlas-research/research.py resolve --file /tmp/atlas-answer.md
```
Fix or drop any **UNRESOLVED** citation before presenting — never ship a broken `[[link]]`. (You may also pipe the draft on stdin.)

## Step 5 — Persist only if it earns it (optional)

Default is **answer-in-chat, no file**. Persist a research note only when the user asks to save it, OR the answer cited ≥2 sources AND the user bookmarks it:
```bash
python3 {{skills_root}}/atlas-research/research.py write \
  --slug ledgerline-integration --question "<q>" --answer-file /tmp/atlas-answer.md \
  --confidence high --sources 6 --layers synthesis,para,raw
```
This writes `{{folders.meta}}/Research/<date>-<slug>.md` (with a preserved `## Notes` editable region), appends a greppable line to `{{folders.meta}}/Research/_research-log.md`, runs the citation gate again (flagging any unresolved link in the note + `last-run.md`), and updates `{{skills_root}}/atlas-research/last-run.md`.

## Output template (persisted note)

```markdown
---
type: research-answer
generated_by: atlas-research
generated_at: <YYYY-MM-DD>
question: "<q>"
canonical_slug: <slug>
confidence: <high|medium|low>
sources_cited: <N>
layers_used: [synthesis, para, raw]
tags: [research, research/<slug>]
---

# Research — <question>
> Generated by atlas-research from existing vault material. Verify against the cited [[sources]].
<the answer>
<!-- atlas-research:editable-start -->
<your notes, preserved across regenerations>
<!-- atlas-research:editable-end -->
```

## Read-only / idempotency contract

- **Never mutates the knowledge spine** (`{{folders.raw}}/`, `{{folders.wiki}}/`, PARA). The only writes are under `{{folders.meta}}/Research/` and `last-run.md`, and only in Step 5.
- Re-running `write` for the same `<date>-<slug>` overwrites the generated region but **preserves** the `## Notes` editable region.
- Stdlib-only (DEC-021) — runs headless if ever scheduled.

## Edge cases
- **No synthesis/concept for the topic** (the common case — only a few exist): answer from PARA + raw via the drill map, and note that no synthesis page exists yet (a candidate for `/atlas-synthesize`).
- **Sparse/empty evidence:** say "the vault doesn't cover this" and offer to ingest — do not fabricate.
- **`ripgrep` missing:** the script falls back to a pure-python scan automatically (slower, same output).
- **Duplicate-slug topics** (e.g. `ledgerline` vs `ledgerline-integration`): surface both hubs; don't silently pick one.

## Relationship to other skills
- **Upstream:** `atlas-*-ingest` write `{{folders.raw}}/`; `atlas-wiki-materialize`/`atlas-emerge`/`atlas-graduate`/`atlas-synthesize` build the layers this reads. The better those run, the better this answers.
- **Sister (write side):** `atlas-synthesize` *creates* a thread's synthesis page; `atlas-research` *reads across the whole vault* to answer an arbitrary question. If a query keeps hitting a thread with no synthesis, that's the signal to run `/atlas-synthesize`.
- **Callable as a context loader:** other skills can run `search` to load relevant context before acting.

## Anti-goals (NOT v1)
- No ingestion / discovery / web fetch — query-only over what exists. (The Step 2a Fireflies transcript escalation is the one sanctioned external read, per DEC-032 — read-only, capped, never persisted.)
- No vector DB / embeddings / FTS index — `ripgrep` + layer-weighted ranking + reading the vault, by design; revisit only on a proven, repeated retrieval miss.
- No automatic spine mutation, no mass note-writing.

## Invocation
On-demand only (no schedule). Manual triggers: the phrases in the frontmatter `description`, or `/atlas-research`. There is no cron — this is the pull half of Atlas by design.
