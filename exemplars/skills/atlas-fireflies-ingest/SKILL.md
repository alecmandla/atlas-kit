---
name: atlas-fireflies-ingest
description: Pull recent Fireflies meeting summaries and file them as markdown notes into the Obsidian vault at {{vault_root}}, routed to the correct client/project/area folder based on meeting-routing.yaml. Full transcripts stay in Fireflies (DEC-032) — fetched via API on demand by the query side, never stored on disk. Triggers on "sync fireflies", "pull meetings", "fireflies sync", "sync my meetings", "update obsidian meetings", or any scheduled run. Also mirrors Claude skills into the vault reference library when "mirror skills" is mentioned.
exemplar-of: atlas-fireflies-ingest
status: active
requires: [mcp/fireflies, cli/python3]
---

# Fireflies meeting sync

You keep the owner's Obsidian vault up to date by pulling their Fireflies meeting summaries each morning and filing them into the right folder (full transcripts stay in Fireflies — DEC-032). You also nightly-mirror the owner's Claude skills into the vault's reference library.

## Mental model

The owner has one vault at `{{vault_root}}/` with a PARA structure. Their client and project folders each have a `meetings/` subfolder. You **never** decide where a meeting goes from scratch — routing rules live in `meeting-routing.yaml`. If no rule matches, the note goes to `{{vault_root}}/{{folders.inbox}}/needs-decision/` with a `needs-decision` tag (per DEC-016) and the owner triages it during their weekly review.

**Hard rule**: every meeting becomes exactly one meeting note. Never split, never duplicate. The filename is `YYYY-MM-DD — <meeting-title>.md`.

## Workflow — daily Fireflies sync

State, dedup, and the duplicate-fire guard are mechanical — always use the
helper `{{skills_root}}/atlas-fireflies-ingest/fireflies_state.py` for them
rather than reading/writing `state.json` or grepping by hand.

### 0. Duplicate-fire guard

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py guard
```

Exit 1 → STOP immediately and report the `skipped:` line it printed (a desktop
scheduler can fire this task many times in a burst on wake-from-sleep). Exit 0 → proceed.

### 1. Determine the time window

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py since
```

Fetch all Fireflies transcripts with `date >=` the printed timestamp (it is
`state.json`'s cursor, or 30 days back on first run).

Use the Fireflies MCP's `fireflies_get_transcripts` — request at minimum: `id`, `title`, `date`, `participants`, `summary` (overview, action_items, keywords), `transcript_url`. This one call carries everything the sync needs (DEC-032) — do not fetch `sentences` or call `fireflies_get_transcript` per meeting.

### 2. Route each meeting

Load `{{skills_root}}/atlas-fireflies-ingest/meeting-routing.yaml`. For each transcript, apply rules in this priority order — **first match wins**:

1. **Attendee email match** (tier 1) — if any participant email is listed under a rule's `attendees:`, route there.
2. **Attendee email domain match** (tier 2) — if the domain portion (text after `@`) of any participant email is listed under a rule's `domains:`, route there. Compare lowercased.
3. **Title keyword match** (tier 3) — if the meeting title (case-insensitive) contains any string under `title_contains:`, route there.
4. **Monday lookup** (tier 4) — see step 2a below.
4.5. **Owner fallback** (tier 4.5) — if `maintainer_fallback.enabled` is true in `meeting-routing.yaml` and any attendee email appears in its `emails:` list, route to its `folder:` with its `tags:`. This exists because the owner attends every recorded meeting, so their address carries no routing signal and must never appear under a rule's tier-1 `attendees:` — there it matches 100% of meetings and starves tiers 2–4.
5. **Fallback** — `{{folders.inbox}}/needs-decision/` with a `needs-decision` tag (per DEC-016).

Evaluate all tiers in order across the full ruleset: try every rule's tier-1 (`attendees:`) match first, then every rule's tier-2 (`domains:`), then every rule's tier-3 (`title_contains:`), then tier-4 (Monday lookup), then tier-4.5 (`maintainer_fallback:`). A higher-tier match anywhere in the file wins over a lower-tier match. Within a single tier, the first rule to match (top-to-bottom in the YAML) wins.

**Record the deciding tier.** Write `routing_tier:` into the meeting note's frontmatter (`1`, `2`, `3`, `4a`, `4b`, `4.5`, or `5`). Routing is performed by reading these instructions rather than by a router module, so without this field a misroute cannot be audited after the fact.

### 2a. Monday lookup (tier 4)

Tier-4 consults the `{{folders.raw}}/monday/<workspace>/<board>/<item_id>.md` mirror produced by `atlas-monday-ingest`. **Critical: read only from `{{folders.raw}}/monday/`. Never call the Monday API directly from this skill** (per DEC-009). The mirror is refreshed nightly. If `{{folders.raw}}/monday/` is missing OR its newest mtime is more than 48h old, log a warning and skip tier 4 (fall through to tier 5).

Build an in-memory index at the start of each sync — O(N) over `{{folders.raw}}/monday/` (a few hundred items is typical). For each item:

- Extract emails from the entire item file (regex `[\w.%+-]+@[\w.-]+\.[a-z]{2,}`).
- **Skip emails on the owner's internal domain** (`@{{employer_domain}}`) — those are owners/teammates appearing in forwarded email threads inside `long_text` columns, never the customer contact. Matching on them produces massive false positives.
- Read `item_name`, `board_slug`, `workspace_slug`, `column_values` (for owner + stage extraction) from the YAML frontmatter.

Per meeting, attempt in order:

1. **Tier-4a — email-exact:** walk participant emails (from the `attendees:` field of the meeting note, plus emails the Fireflies summary inlines into the body). For each, look up the index. On match, route to the destination folder configured for that `board_slug` under `monday_routes:` in `meeting-routing.yaml`.
2. **Tier-4b — full-name substring:** normalize the meeting title to lowercase token form (strip stopwords; collapse). For each Monday item, normalize `item_name` the same way (drop stopwords like `the`, `inn`, `hotel`, `resort`, `llc`, etc.); require ≥ 2 tokens and ≥ 8 chars after stopword removal. If the normalized item-name is a substring of the normalized title, that's a match.

**Match policy guardrails** (learned from false-positive analysis of the name-substring tier):

- **Single-token matches REJECTED.** A single surname token in an item name ("Hale") matches any 1:1 meeting with a person of that name; a single place token ("Saltmarsh") matches every property that shares it. The full normalized item name must appear as a substring.
- Full normalized name length ≥ 8 chars after stopwords, and ≥ 2 tokens. Anything shorter is too generic.
- Email-exact is the high-precision tier. Full-name substring is the lower-precision tier. If email-exact matches, use it; only fall through to substring if no email match.

**Attach `monday_context:` to the routed note's frontmatter regardless of which tier was the deciding one.** When tier-1/2/3 fires and a Monday match exists for the same meeting, attach the metadata block but keep the tier-1/2/3 destination. When tier-4 is the deciding tier, attach it AND route to the Monday-derived destination.

```yaml
monday_context:
  board: dashboard-onboarding
  item_name: "Pinecrest Lodge"
  item_id: 3000001
  owner: "{{owner_name}}"
  stage: "Delivered"
  match_type: email-exact          # or name-substring
  match_value: "priya@pinecrestlodge.example"  # or the item_name that substring-matched
```

**Conflict with existing client routes.** If tier-3 already matched a client (e.g. "Pinecrest Lodge weekly sync" matches the existing Pinecrest-Lodge rule), tier-3 wins — tier-4 attaches metadata but does not re-route. This preserves the priority order and avoids spurious re-routes when a meeting is clearly about an existing tier-1/2/3-managed client.

### 3. Write the note (+ the raw summary record)

Each meeting produces **two** files, per DEC-004 (LLM Wiki spine), DEC-009 (ingest writes to `{{folders.raw}}/` first) and DEC-032 (summary in vault, transcript via API):

1. The **meeting note** at `<routed_folder>/YYYY-MM-DD — <sanitized-title>.md` — slim, human-curated. Its `## Full Transcript` section is a single link to the raw file, never an inline `<details>` block.
2. The **raw summary record** at `{{vault_root}}/{{folders.raw}}/fireflies/<meeting_id>.md` — the meeting's summary content plus the Fireflies link. The full transcript stays in Fireflies; nothing on disk retains it (DEC-032).

Meeting-note template (match the vault convention — `attendee_emails` + `participants` wikilinks + `keywords`):

```markdown
---
date: {{date_iso_full}}            # e.g. 2026-06-01 15:00:00.000000+00:00
attendee_emails:                   # sorted; omit / use `attendee_emails: []` when none
- {{email}}
participants:                      # one per attendee email; omit the block when none
- "[[{{Name-Slug}}]]"              # email local-part → Title-Case-hyphenated
meeting_id: {{fireflies_id}}
project: '[[{{project_moc}}]]'     # '' for needs-decision
prep_note: {{prep_note}}           # OMIT unless step 3a printed a prep_note line — see 3a
tags:
- meeting
- {{route_tags}}
fireflies_url: https://app.fireflies.ai/view/{{fireflies_id}}
meeting_link: {{meeting_link}}     # omit when absent
keywords:                          # omit the block when summary has none
- {{keyword}}
---

# {{date}} — {{meeting_title}}

## Summary
{{summary_overview}}

## Key Decisions
- _(extract from transcript on review)_

## Action Items
{{checkbox_action_items_grouped_by_**Name**}}

## Full Transcript
[Full transcript →]({{relative-path-to}}/{{folders.raw}}/fireflies/{{fireflies_id}}.md)
```

The `## Full Transcript` relative path depends on the note's folder depth (e.g. `../../` for `{{folders.inbox}}/needs-decision/`, `../../../` for `{{folders.areas}}/<area>/meetings/`, `../../../../` for `{{folders.projects}}/Clients/<client>/meetings/`).

Sanitization: filename title → strip `/\:?*"<>|`, collapse whitespace, truncate at 80 chars.

### 3a. Link a meeting-prep note if one exists

The owner hand-authors a **prep note** before some calls — named `YYYY-MM-DD — <title> (prep).md` in the same `meetings/` folder — and takes live notes in it during the call. Prep and call stay two separate files, linked. After you've decided the routed folder and the call-note basename (but **before** writing the note), run:

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/link_prep.py \
  --folder "<routed_folder>" --call-basename "YYYY-MM-DD — <sanitized-title>"
```

Join key is **date + folder**, never the title — so a title tweak or a router disambiguator suffix can't break the link. Policy: it links **only when exactly one** same-date prep exists in that folder; zero or two-plus → it prints a `skip:` reason to stderr and changes nothing (never guess).

- If it prints a `prep_note: "[[…]]"` line to **stdout** → set `prep_note:` in the meeting-note frontmatter to that value. The helper has already written the reciprocal `call_note:` into the prep's frontmatter (frontmatter only — it never touches the prep's body / live notes).
- If it prints nothing to stdout (skip) → **omit** the `prep_note:` line from the frontmatter entirely.

The helper is atomic, idempotent, and safe to re-run (a second sync over the same call reports `already-current`). It only ever edits the prep's frontmatter `call_note:` line; it never creates, deletes, or renames a file.

#### The summary record (do NOT retain the transcript — DEC-032 / DEC-034)

Full transcripts never live on disk (DEC-032): they stay in Fireflies, and the query side fetches them via the API when needed. The raw file is an AI-optimized **summary record** built entirely from what `fireflies_get_transcripts` (step 1) already returned — **never call `fireflies_get_transcript` from this skill**, and never write transcript sentences into the vault.

**Never hand-format this file.** The format lives in exactly one place — the writer module (DEC-034). Dump the step-1 meeting JSON (the whole object: `id`, `title`, `dateString`, `participants`, `meetingAttendees`, `summary`), add a `"meeting_note"` key, and pipe it through:

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/summary_record.py apply --payload - <<'EOF'
{ ...meeting JSON..., "meeting_note": "YYYY-MM-DD — <sanitized-title>" }
EOF
```

The `"meeting_note"` value must be the meeting note's **exact basename** — the same sanitized, ≤80-char filename you wrote in the step above (strip `/\:?*"<>|`, collapse whitespace, truncate at 80), **without** `.md` — not the raw Fireflies title. The writer uses it verbatim for the `meeting_note:` wikilink and the H1, so a mismatch produces a dangling backlink.

(`--payload` also takes a file path; a list of meeting objects processes as a batch.) The writer is atomic, guarded, and idempotent — it handles everything that used to be prose rules here:

- fresh write → the summary record (keywords + attendees + overview + action items at the top of the body, because `atlas-synthesize` keyword-matches only the first ~2,500 characters and `atlas-emerge` scans `{{folders.raw}}/*` bodies — this file is fireflies' entire contribution to pattern emergence);
- meeting with **no** summary content and no attendees → link-only stub (`transcript_status: not-retained-locally`), automatically;
- existing stub → upgraded in place, join keys (`meeting_note`, `date`, `fireflies_url`, `extracted_at`) preserved;
- existing summary record → re-rendered only if content actually changed (`already-current` otherwise);
- anything else on disk (e.g. a body it doesn't recognize) → `conflict`, file left untouched, non-zero exit — report it, never force.

The record body ends with the transcript pointer: the URL is for humans; AI tools are instructed to fetch the transcript by passing `meeting_id` to `fireflies_get_transcript`. Never remove or reword that block by hand — it is rendered by the writer.

### 4. Skip duplicates

Before writing each meeting, run:

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py check-dup {{fireflies_id}}
```

It searches **all eight** vault roots the filing/archival pipeline can land a
note in (`{{folders.inbox}}`, `{{folders.daily}}`, `{{folders.projects}}`,
`{{folders.areas}}`, `{{folders.resources}}`, `{{folders.archive}}`,
`{{folders.attachments}}`, `{{folders.raw}}`) — the completeness a hand-rolled
grep can silently miss. Output `duplicate: <path>` → skip, don't overwrite.
Output `new (...)` → write.

`check-dup` guards the **meeting note** (the human-curated file that must never
be duplicated). The `{{folders.raw}}/fireflies/<id>.md` summary record is a separate concern:
the writer (step 3) is independently idempotent and guarded, so re-running a sync
or the backfill over an existing record is safe — it reports `already-current` or
`no-content` and leaves the file byte-unchanged. A `duplicate:` here means "this
meeting already has a note, don't create a second one," not "leave the raw record
stale."

**Why the full list:** archived meetings (`{{folders.archive}}/`) and one-off MOC/dashboard files (`{{folders.resources}}/`, `{{folders.attachments}}/`) can carry a `meeting_id` that a narrower scope misses. A re-run with a rewound `state.json` must produce zero duplicates wherever a meeting note has ever lived.

### 5. Update state

After a successful run:

```bash
# fetched >= 1 meeting: cursor = newest meeting's timestamp + 1s
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py advance \
  --newest "<ISO timestamp of the newest meeting fetched>" --count <meetings filed>

# fetched 0 meetings: run it with NO --newest — it leaves the cursor untouched
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py advance
```

Never write `state.json` by hand, never write "now" or a date-only value as the
cursor. The helper writes atomically and preserves the `notes` field — append
run notes there afterwards if useful.

### 6. Report

Output a short summary to the user (or the scheduled-task log):
- `X meetings filed`, grouped by folder
- `Y meetings routed to needs-decision (needs-decision)` — list their titles so the owner can update `meeting-routing.yaml`
- Errors if any

#### Inbox-overflow warning

After the report above, count the `*.md` files directly in `{{vault_root}}/{{folders.inbox}}/` (depth 1 only — do **not** descend into `needs-decision/` or `from-apple-notes/`). Threshold: **20**.

```bash
# Equivalent shell check (depth 1, .md only):
find "{{vault_root}}/{{folders.inbox}}" -maxdepth 1 -type f -name "*.md" | wc -l
```

- **If count ≤ 20** → no warning. Continue.
- **If count > 20** → print this warning block on its own line, in addition to the regular report:

  ```
  ⚠️  INBOX OVERFLOW: {{folders.inbox}}/ root has <count> unreviewed files (threshold: 20).
      Review at: {{vault_root}}/{{folders.meta}}/Dashboards/Unreviewed-Inbox.md
      Either triage them or extend meeting-routing.yaml so future syncs route them automatically.
  ```

Substitute `<count>` with the actual integer. The warning is informational — do **not** block the sync, mutate state, or skip the state.json update. It only flags drift so the owner catches a regression early.

The dashboard at `{{folders.meta}}/Dashboards/Unreviewed-Inbox.md` lists Inbox files older than 7 days; the warning above directs the owner there for review.

## Workflow — mirror Claude skills (nightly)

Triggered on phrase "mirror skills" or as a second step in the scheduled daily run.

For each folder in `{{skills_root}}/` (and any other skill directories the owner maintains):

1. Skip folders starting with `archive-` or without a `SKILL.md` / `README.md`.
2. Read the frontmatter and first 500 chars of `SKILL.md` (or `README.md` fallback).
3. Write/overwrite `{{vault_root}}/{{folders.resources}}/Claude-Skills/<folder-name>.md` using the Skill-Reference template:

```markdown
---
type: skill-reference
source: {{skills_root}}/<folder-name>
status: active
last_modified: {{file_mtime_iso}}
tags: [claude-skill]
---

# {{skill-title-from-frontmatter-or-foldername}}

## Purpose
{{description-from-frontmatter}}

## Trigger phrases
{{extracted-from-description}}

## Source
`{{skills_root}}/<folder-name>/`

## Excerpt
{{first-500-chars-of-skill-md}}
```

## Routing file — how to edit

`meeting-routing.yaml` is the single source of truth for routing. If a meeting lands in the Inbox wrongly, open the file, add the rule, and it's fixed for next time. Format:

```yaml
routes:
  - folder: "{{folders.projects}}/Clients/Pinecrest-Lodge/meetings"
    project_moc: "_Pinecrest-Lodge"
    tags: [client, pinecrest-lodge]
    attendees:
      - "priya@pinecrestlodge.example"
    title_contains:
      - "pinecrest lodge"
      - "pinecrest"
```

## Error handling

- **Fireflies MCP fails** → report the error, do NOT update `state.json` (so next run retries the same window).
- **Write permission error on vault** → check the vault exists at `{{vault_root}}/`, report and stop.
- **Routing file malformed** → route everything to the Inbox with `routing-yaml-broken` tag, report.

## One-off backfill

If the owner asks to "backfill last N days", treat that as: ignore the cursor, fetch transcripts with `date >= today - N days`, run the rest of the workflow normally (including per-meeting `check-dup`). After completion:

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/fireflies_state.py backfill-done \
  --through "<full ISO timestamp of the END of the window you fetched>"
```

Pass the actual fetch-window end, not "today" — the helper rejects date-only values because midnight-vs-now ambiguity either loses the rest of the day's meetings or re-pulls them.

## Retroactive enrichment backfill (DEC-034)

One-time (re-runnable) pass that converges every unenriched file in `{{folders.raw}}/fireflies/` — link-only stubs **and** legacy summary conversions — onto the enriched summary record. **Attended run only** — the Fireflies connector doesn't auth headless (DEC-031). It never reads or writes `state.json` (the sync cursor is not involved), never deletes or renames files, and is safe to stop and resume mid-way (the writer is idempotent).

1. List what's left: `python3 {{skills_root}}/atlas-fireflies-ingest/summary_record.py pending` → `<meeting_id>\t<date>\t<stub|legacy-summary>` per row, oldest first, count (with a per-kind breakdown) on stderr. `legacy-summary` rows already carry content and re-render on `apply`; a fetch just refreshes them to the current Fireflies summary.
2. Work in date windows: pick a chunk of pending rows, call `fireflies_get_transcripts` with `fromDate`/`toDate` spanning them. **Page to exhaustion** — the tool returns at most 50 per call, so keep advancing `skip` (by `limit`) until a call returns a short (< `limit`) page. Dump the full returned list to a JSON file. Do **not** inject `meeting_note` — the writer preserves it from the existing record.
3. **Filter the fetched JSON to the current `pending` ids before applying.** `fireflies_get_transcripts` returns every meeting in the window, including ones that already have a full note; applying those would fresh-write orphan records (no `meeting_note`) that `check-dup` then blocks the real note against forever. Keep only entries whose `id` is in step-1's pending list:

   ```bash
   python3 {{skills_root}}/atlas-fireflies-ingest/summary_record.py pending | cut -f1 > pending.ids
   python3 -c 'import json; ids=set(open("pending.ids").read().split()); json.dump([m for m in json.load(open("fetched.json")) if m.get("id") in ids], open("filtered.json","w"))'
   ```

   Then `python3 {{skills_root}}/atlas-fireflies-ingest/summary_record.py apply --payload filtered.json`. It prints one line per meeting and a count summary (`enriched / already-current / no-content / conflict / error`).
4. A pending id is "aged out" only when a **fully-paged** window (step 2 exhausted) did not return it: `python3 {{skills_root}}/atlas-fireflies-ingest/summary_record.py mark-unavailable <id> ...` annotates the stub (`unavailable_at_source:`) so `pending` stops offering it. **Never** `mark-unavailable` off a truncated (50-cap) response — you'd annotate live meetings. Ids whose date sits exactly on a window boundary: don't mark them here — re-check them in the next window.
5. Append each batch's count line to the run ledger at `{{skills_root}}/atlas-fireflies-ingest/backfill-ledger.md`, then repeat from step 1. **Termination:** `pending: 0` is not always reachable — a meeting that is available but has no summary content (AI notes off, processing skipped) reports `no-content` and correctly stays a stub. Each pending id ends in exactly one terminal state: `enriched`/`already-current` (converged), `unavailable-marked` (aged out), or *present-in-an-exhausted-fetch-but-contentless* (leave as stub — record the id in the ledger). The loop ends when every pending id has reached one of the three, **not** when `pending` hits 0.
6. Final report to the owner: enriched / already-current / unavailable totals, the leave-as-stub (contentless) list, plus every `conflict` line for manual review.

**Steady state:** the nightly sync keeps minting stubs for new meetings, so `pending` is also the periodic re-check entry point — re-run this loop occasionally (or after any big backfill), not once-ever.

## Dry-run script: replay tier-4 against current Inbox

`dry_run_monday_tier.py` replays the Monday tier-4 logic against the current `{{folders.inbox}}/needs-decision/` corpus and emits a markdown report. Use when modifying tier-4 logic or after a Monday backfill to verify rescue counts.

```bash
python3 {{skills_root}}/atlas-fireflies-ingest/dry_run_monday_tier.py \
  --out /tmp/atlas-fireflies-ingest-monday-tier-dryrun.md
```

The script reads `{{folders.raw}}/monday/` (no API calls) and applies the same match policy described in step 2a. It also surfaces tier-3-routed files (in the owner's internal-meetings area, e.g. `{{folders.areas}}/HQ/meetings/`) where tier-4 would attach `monday_context:` metadata.
