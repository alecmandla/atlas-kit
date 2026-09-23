---
name: atlas-gmail-ingest
description: Ingest Gmail messages into raw/gmail/<route>/<message_id>.md using the Gmail MCP for fetching + a Python helper for parsing/writing. Per-message granularity. Filters out Promotions/Updates/Forums tabs and from:noreply@. Routes by sender domain first (mailbox-routing.yaml), then by Gmail triage label, resolving opaque label IDs to display names via a list_labels capture. Idempotent. Triggers on "sync gmail", "ingest gmail", "rebuild raw gmail".
exemplar-of: atlas-gmail-ingest
status: active
requires: [mcp/gmail, cli/python3]
---

# atlas-gmail-ingest

You mirror the owner's Gmail into the vault's `{{folders.raw}}/gmail/` layer with per-message granularity.

## Runtime model

This ingest skill depends on **MCP rather than a local CLI**. There's no `gh`-equivalent for Gmail without OAuth setup. Two execution paths:

| Path | When | How |
|---|---|---|
| **Interactive (Claude session)** | Initial backfill, on-demand sync, debugging | Claude invokes Gmail MCP tools, pipes JSON to `ingest.py --input-json <file>` |
| **Scheduled** | Nightly per DEC-013 | Requires either: (a) a stdlib connector fetcher (DEC-031), or (b) a scheduled Claude Code session that can invoke the MCP path. The owner chooses when this graduates to scheduled. |

The interactive path is canonical. The `ingest.py --input-json` mode accepts the JSON output of `search_threads` and converts it to markdown files.

## Filters (owner scope decision)

- **All-time** — no date floor on the query.
- **All primary threads** — no positive label filter beyond exclusions.
- **Excluded categories** — Promotions, Updates, Forums (Gmail's auto-categorization).
- **Excluded senders** — `from:noreply@*`, `from:no-reply@*`.

Final Gmail query string: `-category:promotions -category:updates -category:forums -from:noreply -from:no-reply`

## Workflow (interactive)

### 1. Pre-flight

Confirm `mailbox-routing.yaml` is present and parses. `ingest.py` reads it with a
stdlib YAML reader (DEC-021), and a malformed routing file fails fast on any
dry-run before a single vault write:

```bash
python3 ingest.py --labels-json /tmp/atlas-gmail-labels.json \
                  --input-json /tmp/atlas-gmail-page-1.json \
                  --dry-run-report /tmp/atlas-gmail-dryrun.md
```

### 2. Capture the label map (`list_labels`)

Gmail's `search_threads` returns **opaque label IDs** on each message (e.g.
`Label_1234567890123456789`), never display names. The routing YAML is written
against display names (e.g. `3. ADD TO TRIAGE`), so without a translation table the
label-routing tier can never fire. `list_labels` is that table.

In the Claude session with Gmail MCP loaded:

```text
list_labels()
```

Write its JSON to a temp file:

```text
/tmp/atlas-gmail-labels.json
```

This is captured **once per run** (labels change rarely), then passed to every
`ingest.py` invocation via `--labels-json`. If you skip this step, ingest still
works but only the domain-routing and `_unrouted` tiers are active.

### 3. Page through `search_threads`

In a Claude session with Gmail MCP loaded:

```text
search_threads(query="-category:promotions -category:updates -category:forums -from:noreply -from:no-reply", pageSize=50)
```

Page through subsequent responses via `pageToken`. Cap the first run at ≤500 threads (rate-limit safety) — that's 10 pages max.

Each call's JSON output is written to a temp file: `/tmp/atlas-gmail-page-N.json`.

### 4. Process each page

```bash
python3 ingest.py \
  --labels-json /tmp/atlas-gmail-labels.json \
  --input-json /tmp/atlas-gmail-page-N.json \
  --execute
```

Each invocation reads the label map plus one page's JSON, parses each thread's messages, applies routing, writes one markdown per message under `{{folders.raw}}/gmail/<route>/<message_id>.md`. `--labels-json` and `--input-json` are both repeatable, so a whole run's pages can be processed in a single invocation.

### 5. Dry-run preview

`--dry-run-report <path>` can also be used (with `--labels-json` + `--input-json`) to preview without writing. The report's **Routing distribution** block leads with a `label map: N label IDs resolved` line — if that reads `0`, the `--labels-json` capture was missed and the label tier is inert.

`N > 0` with `by label: 0` is the *other* failure: the map resolved fine, but no
routed label appears on any message in the sample. That is expected for a page
of pure client mail (domain wins) — but if it holds across a whole run, check
that `mailbox-routing.yaml`'s labels still exist in the mailbox.

## Tests

```bash
python3 test_ingest.py
```

Stdlib-only, no framework (same convention as `atlas-fireflies-ingest`). Covers
the `list_labels` response shapes, the domain-beats-label precedence, and a
guard that every `label:` in `mailbox-routing.yaml` exists in a real
`list_labels` capture (skipped when no capture is present).

## Per-file shape

`{{folders.raw}}/gmail/<route>/<message_id>.md`:

```markdown
---
type: raw-gmail-message
message_id: <message_id>
thread_id: <thread_id>
subject: <subject>
sender: <sender_email>
recipients: [<to>, ...]
date: <ISO>
labels: [...]
route: <route>
participants: [[[Person-Name]], ...]  # wikilinks via CRM where matched
ingested_at: <ISO>
---

# <subject>

**From:** <sender>
**To:** <recipients joined by ", ">
**Date:** <ISO>

> <snippet — truncated to 500 chars>

## Thread context

This message is part of thread `<thread_id>` (use the Gmail MCP's `get_thread` to fetch the full thread).
```

## Routing precedence

The two tiers sort on different axes. `domain_routes` answers **who** the mail
is from; the Gmail labels the owner maintains answer **what they plan to do about
it** (`3. ADD TO TRIAGE`, `4. NOTIFICATION`, …). Sender is the stabler folder
key — it never changes — so it goes first, and one folder stays one
correspondent. Labels then catch what no domain rule covers, rather than
yanking a client email out of its client folder the moment it gets triaged.

1. **Domain match** — if the sender's domain matches an entry in
   `domain_routes`, route to that folder, tier `domain`. Client routing lives
   entirely here and needs no labeling.
2. **Label match** — else, each of the message's `labelIds` is an opaque ID
   (`Label_1234567890123456789`). The skill resolves it to a display name via
   the `--labels-json` map (from `list_labels`), then matches that display name
   against `mailbox-routing.yaml`'s `label_routes`. Match is **exact** on the
   full nested path (Gmail returns nested labels as
   `Marketing Automation/UTMCreator`, verbatim against the YAML), with a
   last-segment fallback so a flat child label still matches a nested route.
   First matching `label_routes` entry (top-to-bottom) wins → tier `label`.
3. **Fallback** — `_unrouted/`, tier `unrouted`.

> **Why resolution is required:** `search_threads` returns IDs, not names, so
> matching the YAML's display names against raw `labelIds` can never fire — the
> label tier stays dead and everything falls through to domain/`_unrouted`.
> Resolving IDs → display names via `list_labels` is what makes the label tier
> work. The YAML's `label:` values must equal the Gmail label **display names**
> (including any `parent/child` nesting), not the IDs.
>
> **Two ways the label tier can die, both silent.** The first is a key-name
> bug: the Gmail MCP returns `{"labelId": …, "name": …}`; a map builder that
> reads only `id` produces an always-empty map. The second is a YAML that names
> labels which do not exist in the mailbox — routing on planned-but-never-created
> labels like `client/pinecrest-lodge` matches nothing. The dry-run's
> `label map: N label IDs resolved` line catches the first (reads `0`); only a
> `list_labels` capture catches the second. `test_ingest.py` pins both.

## Idempotency

`message_id` is Gmail's stable per-message PK. If `{{folders.raw}}/gmail/<route>/<message_id>.md` exists, skip (append-only per DEC-009).

If a message is re-labeled in Gmail (and re-routed by this skill), it could land in a different folder on re-run. The skill detects this by globbing `{{folders.raw}}/gmail/*/<message_id>.md` before writing — if a file with this id already exists *anywhere*, skip. The owner manually moves files between route folders if Gmail labels are updated retroactively; the skill never relocates.

## Edge cases

- **Multipart MIME body** — `search_threads` returns snippets, not full bodies. Per-message body excerpt = snippet (typically 100-200 chars; well within the 500-char limit). For full bodies, the owner can later call `get_thread` per thread.
- **Sender header parsing** — Gmail returns the sender header verbatim (may include display name). The skill extracts the email address via regex `<(.+@.+)>` or falls back to the whole string.
- **Multiple recipients** — joined by `, ` in the To: line; full list in the `recipients:` frontmatter array.
- **Snippet HTML entities** — Gmail returns `&#39;` (apostrophe) etc. The skill decodes via Python's `html.unescape()`.

## Invocation

```bash
# Process a single page's JSON (with the label map for tier-1 routing)
python3 ingest.py \
  --labels-json /tmp/atlas-gmail-labels.json \
  --input-json /tmp/atlas-gmail-page-1.json \
  --execute

# Process a whole run at once — both flags are repeatable
python3 ingest.py \
  --labels-json /tmp/atlas-gmail-labels.json \
  --input-json /tmp/atlas-gmail-page-1.json \
  --input-json /tmp/atlas-gmail-page-2.json \
  --execute

# Dry-run a page (no writes)
python3 ingest.py \
  --labels-json /tmp/atlas-gmail-labels.json \
  --input-json /tmp/atlas-gmail-page-1.json \
  --dry-run-report /tmp/atlas-gmail-ingest-dryrun.md
```

## Relationship to other skills

- `atlas-people-extract` — provides the CRM that this skill cross-references for `participants:` wikilinks.
- `atlas-wiki-materialize` — v2 may surface email-thread entities from this corpus.
- `atlas-emerge` — mines cross-source thread connections.
