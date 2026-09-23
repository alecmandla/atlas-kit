#!/usr/bin/env python3
"""atlas-claude-history-ingest.

Walk ~/.claude/projects/*/<session_id>.jsonl. Emit one summary markdown
per session into <vault_root>/raw/claude-history/<project>/<session_id>.md.
Idempotent, secret-redacting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
RAW_CH = CFG.folder("raw") / "claude-history"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"

INPROGRESS_THRESHOLD_S = 300  # 5 minutes

REDACTIONS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-REDACTED"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "ghp_REDACTED"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA_REDACTED"),
    (re.compile(r"xoxb-[A-Za-z0-9-]{30,}"), "xoxb-REDACTED"),
    (re.compile(r"Bearer [A-Za-z0-9._-]{30,}"), "Bearer REDACTED"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "password=REDACTED"),
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"), "api_key=REDACTED"),
]


@dataclass
class SessionSummary:
    session_id: str
    project_slug: str
    project_path: str
    jsonl_path: Path
    started: str
    ended: str
    duration_minutes: int
    turn_count: int
    first_prompt: str
    last_assistant: str
    files_touched: list[str]


@dataclass
class Stats:
    scanned: int = 0
    summaries_written: int = 0
    already_present: int = 0
    skipped_in_progress: int = 0
    skipped_empty: int = 0
    skipped_no_turns: int = 0
    redactions_applied: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "schema_version": 1}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def redact(text: str, stats: Stats) -> str:
    if not text:
        return text
    out = text
    for pat, repl in REDACTIONS:
        new_out = pat.sub(repl, out)
        if new_out != out:
            stats.redactions_applied += 1
            out = new_out
    return out


def truncate(text: str, n: int = 200) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def project_slug(raw: str) -> str:
    """Convert a Claude Code project-path-encoded folder name into a clean slug."""
    s = raw.lstrip("-")
    return s.replace("/", "-")


def decode_project_path(raw: str) -> str:
    """Reverse the dash-encoding to recover the underlying filesystem path."""
    return "/" + raw.lstrip("-").replace("-", "/")


def parse_session(jsonl: Path, project_dirname: str, stats: Stats) -> Optional[SessionSummary]:
    try:
        text = jsonl.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        stats.errors.append((jsonl, f"read failed: {e}"))
        return None

    lines = text.strip().split("\n")
    if not lines or (len(lines) == 1 and not lines[0].strip()):
        stats.skipped_empty += 1
        return None

    started = ""
    ended = ""
    turn_count = 0
    first_prompt = ""
    last_assistant = ""
    files_touched: set[str] = set()

    for raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ts = obj.get("timestamp", "")
        if ts:
            if not started:
                started = ts
            ended = ts
        etype = obj.get("type", "")
        if etype == "user":
            turn_count += 1
            if not first_prompt:
                content = obj.get("content", "") or obj.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
                first_prompt = str(content)
        elif etype == "assistant":
            turn_count += 1
            content = obj.get("content", "") or obj.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = " ".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
            last_assistant = str(content)
        elif etype in ("tool-use", "tool_use"):
            tool_name = obj.get("name") or obj.get("tool_name", "")
            tool_input = obj.get("input") or obj.get("arguments", {}) or {}
            if tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
                fp = tool_input.get("file_path") or tool_input.get("path") or ""
                if fp:
                    files_touched.add(fp)
        elif etype == "last-prompt":
            content = obj.get("content", "")
            if not first_prompt and content:
                first_prompt = str(content)

    if turn_count == 0 and not first_prompt:
        stats.skipped_no_turns += 1
        return None

    # Duration
    duration_minutes = 0
    try:
        if started and ended:
            sd = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            ed = dt.datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration_minutes = int((ed - sd).total_seconds() / 60)
    except Exception:
        pass

    # Redact + truncate
    first_prompt = truncate(redact(first_prompt, stats), 200)
    last_assistant = truncate(redact(last_assistant, stats), 200)

    return SessionSummary(
        session_id=jsonl.stem,
        project_slug=project_slug(project_dirname),
        project_path=decode_project_path(project_dirname),
        jsonl_path=jsonl,
        started=started,
        ended=ended,
        duration_minutes=duration_minutes,
        turn_count=turn_count,
        first_prompt=first_prompt,
        last_assistant=last_assistant,
        files_touched=sorted(files_touched)[:30],  # cap to avoid mega-lists
    )


def in_progress(jsonl: Path) -> bool:
    try:
        mtime = jsonl.stat().st_mtime
        return (time.time() - mtime) < INPROGRESS_THRESHOLD_S
    except Exception:
        return False


def write_summary(s: SessionSummary, ingested_at: str) -> tuple[bool, bool]:
    """Return (created, updated)."""
    out_dir = RAW_CH / s.project_slug
    out = out_dir / f"{s.session_id}.md"
    is_new = not out.exists()
    if not is_new:
        # Check if `ended:` matches; if so, no-op
        try:
            existing = out.read_text(encoding="utf-8")
            m = re.search(r"^ended:\s*(.*)$", existing, re.MULTILINE)
            if m and m.group(1).strip() == s.ended:
                return False, False
        except Exception:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    title_dt = s.started[:16].replace("T", " ") if s.started else "unknown"
    files_yaml = "[]" if not s.files_touched else "\n  - " + "\n  - ".join(f'"{p}"' for p in s.files_touched)
    if s.files_touched:
        files_block = "\nfiles_touched:" + files_yaml
    else:
        files_block = "\nfiles_touched: []"

    body = (
        "---\n"
        "type: raw-claude-history\n"
        f"session_id: {s.session_id}\n"
        f"project: {s.project_slug}\n"
        f"project_path: {s.project_path}\n"
        f"started: {s.started}\n"
        f"ended: {s.ended}\n"
        f"duration_minutes: {s.duration_minutes}\n"
        f"turn_count: {s.turn_count}\n"
        f"first_prompt: \"{s.first_prompt.replace(chr(34), chr(39))}\""
        f"{files_block}\n"
        f"ingested_at: {ingested_at}\n"
        "---\n"
        "\n"
        f"# {s.project_slug} · {title_dt} session\n"
        "\n"
        "## First prompt\n"
        "\n"
        f"> {s.first_prompt}\n"
        "\n"
        "## Last assistant turn\n"
        "\n"
        f"> {s.last_assistant}\n"
        "\n"
        "## Files touched\n"
        "\n"
    )
    if s.files_touched:
        for fp in s.files_touched:
            body += f"- `{fp}`\n"
    else:
        body += "_(none)_\n"
    body += (
        "\n"
        "## Stats\n"
        "\n"
        f"- Turns: {s.turn_count}\n"
        f"- Duration: {s.duration_minutes} minutes\n"
        f"- Started: {s.started}\n"
        f"- Ended: {s.ended}\n"
    )
    out.write_text(body, encoding="utf-8")
    return is_new, not is_new



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
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2

    if not CLAUDE_PROJECTS.exists():
        print(f"error: {CLAUDE_PROJECTS} not found", file=sys.stderr)
        return 2

    if args.execute:
        if not acquire_run_lock():
            print("skipped: another run of this skill is in progress (duplicate-fire guard)")
            return 0
        import atexit
        atexit.register(release_run_lock)

    stats = Stats()
    summaries: list[SessionSummary] = []
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    jsonls = sorted(CLAUDE_PROJECTS.glob("*/*.jsonl"))
    if args.limit is not None:
        jsonls = jsonls[: args.limit]

    for jsonl in jsonls:
        stats.scanned += 1
        if in_progress(jsonl):
            stats.skipped_in_progress += 1
            continue
        project_dirname = jsonl.parent.name
        s = parse_session(jsonl, project_dirname, stats)
        if s is None:
            continue
        summaries.append(s)

    if args.execute:
        for s in summaries:
            try:
                created, updated = write_summary(s, ingested_at)
                if created:
                    stats.summaries_written += 1
                elif updated:
                    stats.summaries_written += 1
                else:
                    stats.already_present += 1
            except Exception as e:
                stats.errors.append((s.jsonl_path, f"write failed: {e}"))
        state = load_state()
        state["last_run_iso"] = ingested_at
        state["last_run_count"] = state.get("last_run_count", 0) + stats.summaries_written
        save_state(state)
    else:
        # Dry-run accounting
        for s in summaries:
            out_path = RAW_CH / s.project_slug / f"{s.session_id}.md"
            if out_path.exists():
                stats.already_present += 1
            else:
                stats.summaries_written += 1

    # last-run.md
    LAST_RUN.write_text(
        "\n".join([
            "# atlas-claude-history-ingest — last run",
            "",
            f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
            f"- **Mode:** `{'execute' if args.execute else 'dry-run'}`",
            f"- **Scanned:** {stats.scanned}",
            f"- **Summaries written:** {stats.summaries_written}",
            f"- **Already present:** {stats.already_present}",
            f"- **Skipped in-progress:** {stats.skipped_in_progress}",
            f"- **Skipped empty:** {stats.skipped_empty}",
            f"- **Skipped no-turns:** {stats.skipped_no_turns}",
            f"- **Redactions applied:** {stats.redactions_applied}",
            f"- **Errors:** {len(stats.errors)}",
        ]) + "\n"
    )

    if args.dry_run_report:
        today = dt.date.today().isoformat()
        lines = [
            f"# atlas-claude-history-ingest dry-run — {today}",
            "",
            "Skill: `atlas-claude-history-ingest`",
            f"Source: `~/.claude/projects/*/*.jsonl`",
            f"Output: `raw/claude-history/<project_slug>/<session_id>.md`",
            "",
            "## Summary",
            "",
            f"- JSONL sessions scanned: **{stats.scanned}**",
            f"- Summaries to write: **{stats.summaries_written}**",
            f"- Already present (idempotent skip): {stats.already_present}",
            f"- Skipped (in-progress, mtime < 5 min): {stats.skipped_in_progress}",
            f"- Skipped (empty file): {stats.skipped_empty}",
            f"- Skipped (no user/assistant turns): {stats.skipped_no_turns}",
            f"- Redactions that would apply: {stats.redactions_applied}",
            f"- Errors: {len(stats.errors)}",
            "",
            "## Acceptance checks",
            "",
            f"- ≥ 100 session summaries on first run: **{'PASS' if stats.summaries_written >= 100 else 'FAIL'}** ({stats.summaries_written} planned).",
            "- frontmatter shape: each summary carries `session_id`, `project`, `started`, `ended`, `turn_count`, `first_prompt` (≤200 chars), `files_touched` (array).",
            "- idempotent: file-exists + `ended:` match check before write.",
            f"- skip in-progress: {stats.skipped_in_progress} files skipped via 5-minute mtime threshold.",
            "",
            "## Project distribution",
            "",
        ]
        by_project: dict[str, int] = {}
        for s in summaries:
            by_project[s.project_slug] = by_project.get(s.project_slug, 0) + 1
        for proj, n in sorted(by_project.items(), key=lambda x: -x[1])[:20]:
            lines.append(f"- `{proj}`: {n} sessions")
        if len(by_project) > 20:
            lines.append(f"_(+ {len(by_project) - 20} more projects.)_")
        lines += [
            "",
            "## Next action",
            "",
            "Re-invoke with `--execute` to write the planned summaries.",
        ]
        args.dry_run_report.parent.mkdir(parents=True, exist_ok=True)
        args.dry_run_report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        f"scanned={stats.scanned} written={stats.summaries_written} "
        f"already={stats.already_present} in_progress={stats.skipped_in_progress} "
        f"empty={stats.skipped_empty} no_turns={stats.skipped_no_turns} "
        f"redactions={stats.redactions_applied} errors={len(stats.errors)}"
    )
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
