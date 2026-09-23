---
name: atlas-monday-ingest
description: Ingest Monday.com items from workspaces in monday-boards.yaml into raw/monday/<workspace>/<board>/<item_id>.md. Per-item granularity with column values and updates. Subitems skipped per owner scope. Idempotent. Triggers on "sync monday", "ingest monday", "rebuild raw monday".
exemplar-of: atlas-monday-ingest
status: active
requires: [mcp/monday, cli/python3]
---

# atlas-monday-ingest

Mirror Monday.com items into the vault's `{{folders.raw}}/monday/` layer with per-item granularity, column values, and updates.

## Scope (owner decision)

- **Workspaces** — the workspaces listed in `monday-boards.yaml` (e.g. 1000001 "Client Onboarding", 1000002 "Company OKRs"). Configurable there.
- **Boards** — all boards in those workspaces, except names matching `^Subitems of ` (Monday auto-creates these per-board for subitem storage; subitems are explicitly out of scope).
- **Granularity** — per-item (not a per-board snapshot).
- **Window** — all-time (no `updated_at` floor).
- **Includes** — column values + updates (text comments on items).
- **Excludes** — subitems (per scope), files.

## Workflow (interactive)

### 1. Discover boards

Per workspace, paginate `workspace_info` (one call per workspace; up to 100 boards each):

```text
workspace_info(workspace_id=1000001)
workspace_info(workspace_id=1000002)
```

Filter out subitem boards via the `exclude_board_patterns` in `monday-boards.yaml`.

### 2. Paginate items per board

For each non-excluded board, paginate `get_board_items_page`:

```text
get_board_items_page(
  boardId=<id>,
  limit=100,
  includeColumns=true,
  includeItemDescription=true,
  includeSubItems=false
)
# subsequent pages: cursor=<nextCursor from previous response>
```

Response auto-spills to a tool-results file when oversized — `ingest.py --input-json` reads directly.

### 3. (Optional) Fetch updates per item

`get_updates` per item id gets the comment thread. For a first pass, skip updates and rely on the item's `description` field; a later backfill can add them.

### 3a. Nightly incremental path (use this, not a full re-page)

A full re-page of every board is wasteful once the mirror exists. Fan a
`__last_updated__` filter across every mirrored board id instead — the rule is
honored on the raw GraphQL path even though `get_board_items_page` ignores it:

```graphql
query ($ids: [ID!]) {
  boards(ids: $ids) {
    id name
    items_page(limit: 100, query_params: {rules: [{
      column_id: "__last_updated__", compare_value: ["TODAY","YESTERDAY"], operator: any_of
    }]}) { cursor items { id name updated_at } }
  }
}
```

Then fetch full bodies for only the ids that came back:

```graphql
query ($ids: [ID!]) { items(ids: $ids) { id name created_at updated_at column_values { id text } } }
```

**Two traps in that second call — both silent:**

1. **`items(ids:)` caps at 25 items per call, with no error and no cursor.** A
   44-id request returns 25 and looks successful. **Chunk to ≤25 ids and assert
   `len(returned) == len(requested)`** before trusting the result; the overflow
   items otherwise go stale with nothing in the log to say so.
2. **Normalize `column_values` to the `{column_id: text}` dict shape before
   `--input-json`.** The raw `[{id, text}]` list shape takes `ingest.py`'s list
   branch, which emits a `value:` line per column. The existing mirror carries
   no `value:` lines, so every file would re-render as changed. Drop empty/null
   texts while normalizing (`ingest.py` drops them anyway).

Responses over the MCP token cap **auto-spill to a `tool-results/*.txt` file**
that `ingest.py --input-json` reads directly — prefer that path, since the
payload then never passes through the session context. Request enough fields to
trigger the spill rather than paging a large result inline.

### 4. Process pages

```bash
python3 ingest.py --input-json <path> \
                  --workspace-id 1000001 \
                  --workspace-name "Client Onboarding" \
                  --board-id 2000001 \
                  --board-name "Dashboard Onboarding" \
                  --execute
```

### 5. Output shape

`{{folders.raw}}/monday/<workspace_slug>/<board_slug>/<item_id>.md`:

```markdown
---
type: raw-monday-item
item_id: <id>
item_name: <name>
workspace_id: <id>
workspace_name: <name>
workspace_slug: <slug>
board_id: <id>
board_name: <name>
board_slug: <slug>
created_at: <ISO>
updated_at: <ISO>
column_values:
  - column_id: status
    title: Status
    type: status
    text: <display text>
    value: <raw value>
  - ...
description: <plain-text item description if present>
ingested_at: <ISO>
---

# <item_name>

**Board:** [[<board_slug>]] (#<board_id>)
**Created:** <created_at>
**Updated:** <updated_at>

## Description

<description verbatim, or "(no description)" if empty>

## Column values

| Column | Type | Text | Value |
|---|---|---|---|
| Status | status | <text> | <value> |
| ...

## Updates

_(captured by the updates backfill when enabled; empty until then.)_
```

## Invocation

```bash
python3 ingest.py \
  --input-json /tmp/monday-items-page-N.json \
  --workspace-id 1000001 --workspace-name "Client Onboarding" \
  --board-id 2000001 --board-name "Dashboard Onboarding" \
  --execute
```

## Idempotency contract

Monday items mutate (status changes, column updates). Idempotency model:

- If `{{folders.raw}}/monday/.../<item_id>.md` doesn't exist → write fresh.
- If it exists AND its `updated_at:` frontmatter matches the API's current `updated_at` → no-op.
- Otherwise rewrite (item changed since last ingest).

This is *not* strict append-only — items are projections of Monday's mutable state, not raw immutable source like Fireflies summaries. Per DEC-009 spirit, this is acceptable for the Monday layer since the source-of-truth (Monday API) is authoritative; the vault mirror reflects current state.

Re-running `--execute` immediately after a fresh run produces `written=0 already=N updated=0`.

## Edge cases

- **Subitem boards** — auto-skipped by `^Subitems of ` prefix match. The `exclude_board_patterns` list in `monday-boards.yaml` makes this list-extensible.
- **Boards with > 500 items** — paginate via `nextCursor`. The skill processes one page per `--input-json` invocation.
- **Empty description** — body shows "(no description)".
- **Status column with multiple-select** — `text` shows comma-joined labels; `value` is the raw JSON.

## Relationship to other skills

- `atlas-fireflies-ingest` — wires `{{folders.raw}}/monday/` into the Fireflies routing brain as a routing tier. Critical: that tier reads only from `{{folders.raw}}/monday/` per DEC-009, never calls the Monday API directly.
- `atlas-gmail-ingest` + `atlas-slack-ingest` — sibling MCP-mediated ingest skills with the same JSON-bridge pattern.
- `atlas-emerge` — mines Monday item names + column values for project / task entities.
