---
name: atlas-github-ingest
description: Ingest GitHub PRs and Issues from the owner's repos into raw/github/<owner>/<repo>/<number>.md. Uses the `gh` CLI (authenticated as the owner) — not raw HTTP. Reads repo list from github-repos.yaml. Idempotent via state-aware writes; refreshes `state`, `merged_at`, and comment count without duplicating. Triggers on "sync github", "ingest github", "rebuild raw github".
exemplar-of: atlas-github-ingest
status: active
requires: [cli/gh, cli/python3]
---

# atlas-github-ingest

Mirror the owner's GitHub PRs and Issues into the vault's `{{folders.raw}}/github/` layer. One markdown file per PR or Issue at `{{vault_root}}/{{folders.raw}}/github/<owner>/<repo>/<number>.md`.

## Why `gh` CLI instead of the GitHub MCP

Python scripts (the `ingest.py` for this skill) can't invoke MCP tools — MCP is a Claude Code runtime feature, not a Python library. The `gh` CLI is the practical equivalent: same auth, same API access, callable via `subprocess`. What matters is the trigger phrases and the data shape produced, not which transport fetches it.

## Workflow

### 1. Read `github-repos.yaml`

Parse the file at `{{skills_root}}/engine/atlas-github-ingest/github-repos.yaml`. `owner` field + `repos[]` list. Skip entries with `enabled: false`.

### 2. For each repo, list PRs + Issues

Two `gh` commands per repo, both filtered by date:

```bash
gh pr list --repo <owner>/<repo> --state all --search "created:>=<since>" \
  --json number,title,state,author,createdAt,mergedAt,closedAt,labels,body,url \
  --limit 200

gh issue list --repo <owner>/<repo> --state all --search "created:>=<since>" \
  --json number,title,state,author,createdAt,closedAt,labels,body,url \
  --limit 200
```

`<since>` = state.json's `last_run_iso` for incremental, or 90 days back for first run.

### 3. For each PR, fetch detail (commits + review comments)

```bash
gh pr view <number> --repo <owner>/<repo> --json number,title,state,...,commits,reviews,comments
```

The full PR view includes commit list, review threads, and issue comments. Single call per PR.

### 4. For each Issue, fetch detail (comments)

```bash
gh issue view <number> --repo <owner>/<repo> --json number,title,state,...,comments
```

### 5. Write one file per PR/Issue

`{{folders.raw}}/github/<owner>/<repo>/<kind>-<number>.md` where `<kind>` is `pr` or `issue`:

```markdown
---
type: raw-github-<kind>
owner: <owner>
repo: <repo>
number: <number>
kind: <pr | issue>
state: open | closed | merged
author: <login>
created_at: <ISO>
merged_at: <ISO | null>
closed_at: <ISO | null>
labels: [name, ...]
url: <url>
ingested_at: <ISO>
---

# <#NNN> <title>

## Description

<body — first 2000 chars; "…" elision if longer>

## Commits (PRs only)

- `<short_sha>` <message>
- ...

## Comments

### <author> · <created_at>

<body — first 1000 chars per comment>

### <author> · <created_at>
...
```

### 6. State + per-run summary

`state.json` tracks `last_run_iso`, `last_run_count`, and a per-repo `last_seen_number` (in case the owner wants to backfill from N+1).

## Invocation

```bash
python3 ingest.py --dry-run-report /tmp/atlas-github-ingest-dryrun.md
python3 ingest.py --execute
python3 ingest.py --execute --days 30   # override 90-day default
python3 ingest.py --execute --repo guest-portal   # single-repo smoke test
```

## Rate-limit handling

`gh` queries are cheap individually but multiply quickly: 15 repos × (1 PR list + 1 Issue list + N PR details + M Issue details) = a few hundred API calls on a first run. GitHub allows ~5000/hr authenticated requests; well within budget.

If a 403 or 429 response appears, the skill backs off 60s and retries up to 3x. After 3 failures it logs the affected repo to `errors` and continues to the next repo — does not abort the whole run.

## Idempotency contract

For each file:
- If raw file doesn't exist → write fresh.
- If raw file exists AND its `state:` and `merged_at:` and comment count match the API's current view → no-op.
- Otherwise rewrite (state changed: open → closed, or merged_at advanced from null → ISO, or new comments).

Re-running `--execute` immediately on the same window produces `written=0 updated=0 already=N`.

## Edge cases

- **Empty repo** (no PRs, no issues) — skill creates the `{{folders.raw}}/github/<owner>/<repo>/` folder but writes nothing.
- **Body is null** — write the title + frontmatter; body section reads `(no description)`.
- **Author is null** (deleted user, bot) — `author: ghost`.
- **Long bodies (>2000 chars)** — truncated with `…` sentinel. The full text is in GitHub; this is a *summary* layer.
- **Cross-fork PRs** — `author: <forker>`, `url:` points to canonical PR URL on the upstream.
- **Unresolvable repo** (renamed, deleted, or transferred) — `gh` raises a non-fatal error on every run. Remove the entry (or set `enabled: false`) in `github-repos.yaml` rather than tolerating the noise; re-add if it returns.

## Relationship to other skills

- `atlas-claude-history-ingest` — sibling local-source skill; same dry-run + state.json pattern.
- `atlas-wiki-materialize` — currently doesn't read `{{folders.raw}}/github/`; future v2 may surface repo / org entities from this corpus.
- `atlas-emerge` — may mine cross-source thread connections (e.g. a Wispr voice journal that mentions a PR; the materializer links them).
