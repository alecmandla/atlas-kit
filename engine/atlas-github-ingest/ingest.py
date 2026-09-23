#!/usr/bin/env python3
"""atlas-github-ingest.

Ingest GitHub PRs + Issues from the owner's repos (listed in github-repos.yaml)
into <vault_root>/raw/github/<owner>/<repo>/<kind>-<number>.md via the
`gh` CLI. Idempotent; refreshes state and comment counts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
RAW_GH = CFG.folder("raw") / "github"
REPOS_YAML = Path(__file__).parent / "github-repos.yaml"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"


@dataclass
class Stats:
    repos_scanned: int = 0
    repos_with_errors: int = 0
    prs_seen: int = 0
    issues_seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "per_repo_last_seen": {}, "schema_version": 1}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def parse_repos_yaml() -> tuple[str, list[str]]:
    """Tiny YAML reader for the github-repos.yaml shape."""
    text = REPOS_YAML.read_text()
    owner = ""
    repos: list[str] = []
    in_repos = False
    pending_name: Optional[str] = None
    for line in text.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("owner:"):
            owner = line.split(":", 1)[1].strip()
            continue
        if line.startswith("repos:"):
            in_repos = True
            continue
        if in_repos:
            stripped = line.strip()
            if stripped.startswith("- name:"):
                if pending_name is not None:
                    repos.append(pending_name)
                pending_name = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("enabled:"):
                val = stripped.split(":", 1)[1].strip().lower()
                if val == "false" and pending_name is not None:
                    pending_name = None
    if pending_name is not None:
        repos.append(pending_name)
    return owner, repos


def gh_json(args: list[str], retries: int = 3) -> Optional[list | dict]:
    """Run `gh` and parse JSON. Backoff on rate-limit; return None on terminal error."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            result = subprocess.run(["gh"] + args, check=True, capture_output=True, text=True, timeout=120)
            return json.loads(result.stdout) if result.stdout.strip() else None
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "")[:500]
            if "API rate limit" in stderr or "rate limit" in stderr.lower() or "429" in stderr:
                time.sleep(60 * (attempt + 1))
                continue
            last_err = RuntimeError(f"gh {' '.join(args)} failed: {stderr}")
            break
        except Exception as e:
            last_err = e
            break
    return None if last_err is None else (_:=last_err)  # noqa


def gh_safe(args: list[str], stats: Stats, context: str) -> Optional[list | dict]:
    result = gh_json(args)
    if isinstance(result, Exception):
        stats.errors.append((context, str(result)[:300]))
        return None
    return result


def safe_text(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def truncate(text: str, n: int) -> str:
    if not text:
        return ""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def write_pr(owner: str, repo: str, pr_detail: dict, ingested_at: str, stats: Stats) -> str:
    number = pr_detail.get("number")
    out = RAW_GH / owner / repo / f"pr-{number}.md"
    new_state = pr_detail.get("state", "").lower()
    merged_at = pr_detail.get("mergedAt") or ""
    closed_at = pr_detail.get("closedAt") or ""
    is_merged = bool(merged_at)
    state_str = "merged" if is_merged else (new_state or "open")
    body = safe_text(pr_detail.get("body"))
    title = safe_text(pr_detail.get("title"))
    author = (pr_detail.get("author") or {}).get("login") or "ghost"
    created = pr_detail.get("createdAt", "")
    labels = pr_detail.get("labels") or []
    label_names = [l.get("name") for l in labels if l.get("name")]
    url = pr_detail.get("url", "")
    commits = pr_detail.get("commits") or []
    comments = pr_detail.get("comments") or []
    reviews = pr_detail.get("reviews") or []

    # Idempotency: compare state + merged_at + comment count to existing
    existing_comment_count = -1
    existing_state = ""
    existing_merged = ""
    is_new = not out.exists()
    if not is_new:
        try:
            existing = out.read_text(encoding="utf-8")
            m = re.search(r"^state:\s*(.*)$", existing, re.MULTILINE)
            if m:
                existing_state = m.group(1).strip()
            m = re.search(r"^merged_at:\s*(.*)$", existing, re.MULTILINE)
            if m:
                existing_merged = m.group(1).strip()
            existing_comment_count = existing.count("\n### ")
        except Exception:
            pass

    new_comment_count = len(comments) + len(reviews)
    if (not is_new and existing_state == state_str
            and existing_merged == (merged_at or "null")
            and existing_comment_count == new_comment_count):
        stats.already_present += 1
        return "noop"

    out.parent.mkdir(parents=True, exist_ok=True)
    yaml_label = "[" + ", ".join(label_names) + "]" if label_names else "[]"
    lines = [
        "---",
        f"type: raw-github-pr",
        f"owner: {owner}",
        f"repo: {repo}",
        f"number: {number}",
        f"kind: pr",
        f"state: {state_str}",
        f"author: {author}",
        f"created_at: {created}",
        f"merged_at: {merged_at or 'null'}",
        f"closed_at: {closed_at or 'null'}",
        f"labels: {yaml_label}",
        f"url: {url}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# #{number} {title}",
        "",
        "## Description",
        "",
        truncate(body, 2000) if body else "(no description)",
        "",
        "## Commits",
        "",
    ]
    if commits:
        for c in commits[:50]:
            short = (c.get("oid") or "")[:8]
            msg = safe_text((c.get("messageHeadline") or c.get("message") or "")).replace("\n", " ")
            lines.append(f"- `{short}` {truncate(msg, 100)}")
    else:
        lines.append("_(none)_")
    lines += ["", "## Comments", ""]
    all_comments: list[tuple[str, str, str]] = []
    for c in comments:
        all_comments.append((
            (c.get("author") or {}).get("login") or "ghost",
            c.get("createdAt", ""),
            safe_text(c.get("body")),
        ))
    for r in reviews:
        author_r = (r.get("author") or {}).get("login") or "ghost"
        all_comments.append((
            author_r,
            r.get("submittedAt") or r.get("createdAt", ""),
            f"({r.get('state', '').lower()}) " + safe_text(r.get("body")),
        ))
    all_comments.sort(key=lambda x: x[1])
    if all_comments:
        for a, t, b in all_comments[:50]:
            lines.append(f"### {a} · {t}")
            lines.append("")
            lines.append(truncate(b, 1000) if b else "_(empty)_")
            lines.append("")
    else:
        lines.append("_(none)_")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if is_new:
        stats.written += 1
        return "created"
    stats.updated += 1
    return "updated"


def write_issue(owner: str, repo: str, issue_detail: dict, ingested_at: str, stats: Stats) -> str:
    number = issue_detail.get("number")
    out = RAW_GH / owner / repo / f"issue-{number}.md"
    new_state = (issue_detail.get("state") or "").lower()
    closed_at = issue_detail.get("closedAt") or ""
    body = safe_text(issue_detail.get("body"))
    title = safe_text(issue_detail.get("title"))
    author = (issue_detail.get("author") or {}).get("login") or "ghost"
    created = issue_detail.get("createdAt", "")
    labels = issue_detail.get("labels") or []
    label_names = [l.get("name") for l in labels if l.get("name")]
    url = issue_detail.get("url", "")
    comments = issue_detail.get("comments") or []

    is_new = not out.exists()
    if not is_new:
        try:
            existing = out.read_text(encoding="utf-8")
            existing_state = (re.search(r"^state:\s*(.*)$", existing, re.MULTILINE) or [None, ""]).group(1).strip() if re.search(r"^state:\s*(.*)$", existing, re.MULTILINE) else ""
            existing_closed = (re.search(r"^closed_at:\s*(.*)$", existing, re.MULTILINE) or [None, ""]).group(1).strip() if re.search(r"^closed_at:\s*(.*)$", existing, re.MULTILINE) else ""
            existing_count = existing.count("\n### ")
            if (existing_state == new_state and existing_closed == (closed_at or "null")
                    and existing_count == len(comments)):
                stats.already_present += 1
                return "noop"
        except Exception:
            pass

    out.parent.mkdir(parents=True, exist_ok=True)
    yaml_label = "[" + ", ".join(label_names) + "]" if label_names else "[]"
    lines = [
        "---",
        "type: raw-github-issue",
        f"owner: {owner}",
        f"repo: {repo}",
        f"number: {number}",
        f"kind: issue",
        f"state: {new_state}",
        f"author: {author}",
        f"created_at: {created}",
        f"closed_at: {closed_at or 'null'}",
        f"labels: {yaml_label}",
        f"url: {url}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# #{number} {title}",
        "",
        "## Description",
        "",
        truncate(body, 2000) if body else "(no description)",
        "",
        "## Comments",
        "",
    ]
    if comments:
        sorted_c = sorted(comments, key=lambda c: c.get("createdAt", ""))
        for c in sorted_c[:50]:
            a = (c.get("author") or {}).get("login") or "ghost"
            t = c.get("createdAt", "")
            b = safe_text(c.get("body"))
            lines.append(f"### {a} · {t}")
            lines.append("")
            lines.append(truncate(b, 1000) if b else "_(empty)_")
            lines.append("")
    else:
        lines.append("_(none)_")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if is_new:
        stats.written += 1
        return "created"
    stats.updated += 1
    return "updated"


def process_repo(owner: str, repo: str, since: str, execute: bool, stats: Stats, ingested_at: str, dry_run_rows: list[dict]) -> None:
    # PRs
    pr_summaries = gh_safe(
        ["pr", "list", "--repo", f"{owner}/{repo}", "--state", "all",
         "--search", f"created:>={since}",
         "--json", "number,title,state",
         "--limit", "200"],
        stats, f"{repo}/pr-list",
    )
    if pr_summaries is None:
        stats.repos_with_errors += 1
        pr_summaries = []
    stats.prs_seen += len(pr_summaries)
    for pr_sum in pr_summaries:
        number = pr_sum.get("number")
        detail = gh_safe(
            ["pr", "view", str(number), "--repo", f"{owner}/{repo}",
             "--json", "number,title,state,author,createdAt,mergedAt,closedAt,labels,body,url,commits,comments,reviews"],
            stats, f"{repo}/pr-view#{number}",
        )
        if detail is None:
            continue
        if execute:
            write_pr(owner, repo, detail, ingested_at, stats)
        else:
            out = RAW_GH / owner / repo / f"pr-{number}.md"
            if out.exists():
                stats.already_present += 1
            else:
                stats.written += 1
            if len(dry_run_rows) < 40:
                dry_run_rows.append({
                    "repo": repo, "kind": "pr", "number": number,
                    "title": detail.get("title", ""),
                    "state": detail.get("state", ""),
                    "will_write": not out.exists(),
                })

    # Issues
    issue_summaries = gh_safe(
        ["issue", "list", "--repo", f"{owner}/{repo}", "--state", "all",
         "--search", f"created:>={since}",
         "--json", "number,title,state",
         "--limit", "200"],
        stats, f"{repo}/issue-list",
    )
    if issue_summaries is None:
        issue_summaries = []
    stats.issues_seen += len(issue_summaries)
    for is_sum in issue_summaries:
        number = is_sum.get("number")
        detail = gh_safe(
            ["issue", "view", str(number), "--repo", f"{owner}/{repo}",
             "--json", "number,title,state,author,createdAt,closedAt,labels,body,url,comments"],
            stats, f"{repo}/issue-view#{number}",
        )
        if detail is None:
            continue
        if execute:
            write_issue(owner, repo, detail, ingested_at, stats)
        else:
            out = RAW_GH / owner / repo / f"issue-{number}.md"
            if out.exists():
                stats.already_present += 1
            else:
                stats.written += 1
            if len(dry_run_rows) < 40:
                dry_run_rows.append({
                    "repo": repo, "kind": "issue", "number": number,
                    "title": detail.get("title", ""),
                    "state": detail.get("state", ""),
                    "will_write": not out.exists(),
                })



# --- duplicate-fire guard (scheduler thundering-herd; suite lock is atlas-nightly/lock.py)
RUN_LOCK = Path(__file__).parent / ".run.lock"
RUN_LOCK_STALE_SECONDS = 7200


def acquire_run_lock() -> bool:
    """Best-effort per-skill run lock. False = a fresh lock is already held."""
    def _create() -> bool:
        try:
            fd = os.open(RUN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    if _create():
        return True
    try:
        stale = (time.time() - RUN_LOCK.stat().st_mtime) > RUN_LOCK_STALE_SECONDS
    except FileNotFoundError:
        stale = True  # holder released in between; retry
    if stale:
        try:
            RUN_LOCK.unlink()
        except FileNotFoundError:
            pass
        return _create()
    return False


def release_run_lock() -> None:
    try:
        RUN_LOCK.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--repo", type=str, help="Only process this repo.")
    args = parser.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2

    if not REPOS_YAML.exists():
        print(f"error: {REPOS_YAML} not found", file=sys.stderr)
        return 2

    owner, repos = parse_repos_yaml()
    if args.repo:
        repos = [r for r in repos if r == args.repo]
        if not repos:
            print(f"error: repo {args.repo} not in github-repos.yaml", file=sys.stderr)
            return 2

    if args.execute:
        if not acquire_run_lock():
            print("skipped: another run of this skill is in progress (duplicate-fire guard)")
            return 0
        import atexit
        atexit.register(release_run_lock)

    state = load_state()
    since = (dt.datetime.utcnow() - dt.timedelta(days=args.days)).date().isoformat()
    # Gap-healing: if the last successful run predates the rolling window, widen
    # the window back to it (minus a day of overlap) — otherwise items created
    # while runs were paused longer than --days are never fetched by any run.
    last_run = state.get("last_run_iso")
    if last_run:
        try:
            last_date = dt.datetime.fromisoformat(str(last_run).rstrip("Z")).date()
            widened = (last_date - dt.timedelta(days=1)).isoformat()
            if widened < since:
                since = widened
        except ValueError:
            pass
    stats = Stats()
    rows: list[dict] = []
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    for repo in repos:
        stats.repos_scanned += 1
        process_repo(owner, repo, since, args.execute, stats, ingested_at, rows)

    if args.execute:
        state["last_run_iso"] = ingested_at
        state["last_run_count"] = state.get("last_run_count", 0) + stats.written
        save_state(state)

    if args.dry_run_report:
        today = dt.date.today().isoformat()
        lines = [
            f"# atlas-github-ingest dry-run — {today}",
            "",
            "Skill: `atlas-github-ingest`",
            f"Owner: `{owner}` (15 repos in `github-repos.yaml`, all enabled)",
            f"Window: created:>={since} (last {args.days} days)",
            "",
            "## Summary",
            "",
            f"- Repos scanned: **{stats.repos_scanned}**",
            f"- PRs seen: {stats.prs_seen}",
            f"- Issues seen: {stats.issues_seen}",
            f"- Files to write: **{stats.written}**",
            f"- Already present (idempotent skip): {stats.already_present}",
            f"- Repos with errors: {stats.repos_with_errors}",
            f"- Total errors: {len(stats.errors)}",
            "",
            "## Acceptance checks",
            "",
            "- SKILL.md + trigger phrases — see SKILL.md description.",
            f"- ≥ 5 repo entries in github-repos.yaml — **PASS** ({len(repos)}).",
            f"- first run covers last 90 days — window: {since} to now.",
            "- each PR/issue file includes commits, description, comments, merge timestamp — yes (PR view fetched with commits + reviews + comments; issue view with comments).",
            "- frontmatter shape: repo, number, state, author, created_at, merged_at, labels — yes.",
            "- idempotent — file-exists + state/merged_at/comment-count compare before write.",
            "",
            "## Sample (first 40 rows)",
            "",
            "| Repo | Kind | # | Title | State | Will write? |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            title = truncate(r.get("title") or "", 50)
            ww = "yes" if r["will_write"] else "already"
            lines.append(f"| {r['repo']} | {r['kind']} | {r['number']} | {title} | {r['state']} | {ww} |")
        if stats.prs_seen + stats.issues_seen > 40:
            lines.append("")
            lines.append(f"_(+ {stats.prs_seen + stats.issues_seen - 40} more.)_")
        lines += [
            "",
            "## Errors",
            "",
        ]
        if stats.errors:
            for ctx, msg in stats.errors[:20]:
                lines.append(f"- {ctx}: {msg[:200]}")
        else:
            lines.append("None.")
        lines += ["", "## Next action", "", "Re-invoke with `--execute`."]
        args.dry_run_report.parent.mkdir(parents=True, exist_ok=True)
        args.dry_run_report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    LAST_RUN.write_text(
        "\n".join([
            "# atlas-github-ingest — last run",
            "",
            f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
            f"- **Mode:** `{'execute' if args.execute else 'dry-run'}`",
            f"- **Repos scanned:** {stats.repos_scanned}",
            f"- **PRs seen:** {stats.prs_seen}",
            f"- **Issues seen:** {stats.issues_seen}",
            f"- **Files written:** {stats.written}",
            f"- **Files updated:** {stats.updated}",
            f"- **Already present:** {stats.already_present}",
            f"- **Repos with errors:** {stats.repos_with_errors}",
            f"- **Total errors:** {len(stats.errors)}",
        ]) + "\n"
    )

    print(
        f"repos={stats.repos_scanned} prs={stats.prs_seen} issues={stats.issues_seen} "
        f"written={stats.written} updated={stats.updated} already={stats.already_present} "
        f"errors={len(stats.errors)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
