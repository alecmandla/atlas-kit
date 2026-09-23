---
name: atlas-slack-ingest
description: Ingest Slack messages from all channels (public + private) the user is in, plus DMs, into raw/slack/<route>/<message_ts>.md via the Slack MCP. Per-message granularity. Reactions skipped per owner scope. File-upload metadata captured. Idempotent via message_ts dedup. Triggers on "sync slack", "ingest slack", "rebuild raw slack".
exemplar-of: atlas-slack-ingest
status: active
requires: [mcp/slack, cli/python3]
---

# atlas-slack-ingest

Mirror Slack messages into the vault's `{{folders.raw}}/slack/` layer. Same MCP-truncation runtime pattern as `atlas-gmail-ingest` — Claude session pages through the Slack MCP, JSON spills to auto-saved tool-results files, `ingest.py --input-json` reads them.

## Scope (owner decision)

- **Channels** — all public + all private channels the user is a member of.
- **DMs** — included.
- **Time window** — all-time (no date floor).
- **Granularity** — per-message.
- **Reactions** — skipped (not surfaced in frontmatter or body).
- **Files** — metadata only (filename, type, size); no file body content.

## Workflow (interactive)

### 1. Discover channels

In a Claude session with Slack MCP loaded, paginate `slack_search_channels` until no further cursor:

```text
slack_search_channels(query="*", limit=20, channel_types="public_channel,private_channel")
```

Each page lists ~20 channels with IDs. Save channel IDs to a temp file or keep in memory across the session.

For DMs: `slack_search_channels(query="*", channel_types="im")` (im = direct message).

### 2. Per-channel read

For each channel ID, paginate `slack_read_channel`:

```text
slack_read_channel(channel_id=<id>, limit=100)
```

Page through via `cursor`. Each call returns ~100 messages in reverse-chronological. Messages are spilled to auto-saved tool-results files if oversized.

### 3. Process pages

Feed each saved JSON file to `ingest.py`:

```bash
python3 ingest.py --input-json <path-to-slack-read-channel-response> \
                  --channel-id <C0...> \
                  --channel-name <name> \
                  --execute
```

`--channel-id` and `--channel-name` are required so the skill knows the routing context (since `slack_read_channel` JSON doesn't include the channel name itself in every shape).

### 4. Output shape

`{{folders.raw}}/slack/<route>/<message_ts>.md` (one file per message):

```markdown
---
type: raw-slack-message
message_ts: <ts>
channel_id: <C0…>
channel_name: <name>
sender_id: <U0…>
sender_name: <display name if available, else fallback to user_id>
date: <ISO derived from ts>
thread_ts: <parent ts if reply, else same as message_ts>
is_thread_reply: true | false
files: [{ name, mimetype, size, file_id }, …]   # metadata only, no body
permalink: <Slack permalink to the message>
route: <route>
ingested_at: <ISO>
---

# <channel_name> · <ISO date>

**From:** <sender_name>
**At:** <ISO>
**Channel:** #<channel_name>

> <message text — truncated to 1000 chars>

## Files

- `<filename>` (<mimetype>, <size> bytes) — file_id: `<file_id>`
- ...
```

Routing:

1. `channel_routes` exact match → that folder.
2. DM (channel_type == "im") → `dm_route` (default `_dms`).
3. Fallback → slugified channel name.

### 5. State + per-run summary

`state.json` tracks `last_run_iso`, `per_channel_oldest_ingested` (the oldest `ts` seen per channel, for backfill cursoring).

## Invocation

```bash
# Process a single channel's MCP response
python3 ingest.py --input-json /path/to/slack-read-channel.json \
                  --channel-id C0EXAMPLE001 --channel-name ops-feed --execute

# Dry-run
python3 ingest.py --input-json /path/to/slack-read-channel.json \
                  --channel-id ... --channel-name ... \
                  --dry-run-report /tmp/atlas-slack-ingest-dryrun.md
```

## Idempotency contract

`message_ts` is Slack's stable per-message PK. If `{{folders.raw}}/slack/<route>/<message_ts>.md` exists, skip. Re-running produces `written=0 already=N`. Glob-by-message_ts across all routes prevents duplication if a message is re-routed by a yaml change.

## Edge cases

- **Sender header shapes** — the MCP emits three (`parse_messages` handles all; covered by `test_ingest.py`):
  1. Attributed — `=== Message from <name> (<id>) at <date> ===`.
  2. Empty-sender bot — `=== Message from  (B…) at <date> ===` (no name). `sender_name` falls back to the bot id.
  3. Attribution-less — `=== Message at <date> ===` (no sender header; common for external users whose profiles don't resolve). Sender is derived from a leading `<@U…|Name>` mention in the body, else `sender_name: "unknown"`. NB: a leading mention is sometimes the *addressee*, not the author — attribution here is best-effort.
- **Bot messages** — included by default (Slack MCP doesn't exclude them). To skip, the owner adds a filter to `parse_messages` in `ingest.py`.
- **Edited messages** — Slack's API returns the current text. Re-running won't update an already-written file (append-only). Future improvement: optional `--refresh-edits` mode that detects edits and updates.
- **Messages with no text (file-only)** — body shows `(no text — see Files section)`.
- **Threads** — each reply is its own file; `is_thread_reply: true` and `thread_ts: <parent_ts>` link replies back to the parent.
- **Mentions** — Slack's API returns mentions as `<@U0XXX>` IDs. The skill leaves them as-is; future improvement could resolve to display names via `slack_read_user_profile`.

## Rate-limit handling

Slack's workspace tier rate-limits at ~1 request/sec for `conversations.history` family (which `slack_read_channel` wraps). The MCP runtime handles backoff. The skill itself doesn't need retry logic — it just processes whatever pages are fed via `--input-json`.

## Relationship to other skills

- `atlas-gmail-ingest` — sibling MCP-mediated ingest with the same JSON-bridge pattern.
- `atlas-emerge` — mines Slack as a thought-thread source per DEC-014's spirit (cross-source emerging patterns).
