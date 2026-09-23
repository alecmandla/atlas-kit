#!/usr/bin/env python3
"""atlas-wispr-ingest.

Read Wispr Flow's local SQLite History/Notes/CalendarEvents tables (read-only)
and emit one markdown file per row into <vault_root>/raw/wispr/.

Idempotent: skips rows whose raw file already exists (DEC-009 append-only).
Incremental: respects state.json's last_run_iso.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
WISPR_DB = Path.home() / "Library" / "Application Support" / "Wispr Flow" / "flow.sqlite"
RAW_WISPR = CFG.folder("raw") / "wispr"
RAW_NOTES = RAW_WISPR / "notes"
RAW_CAL = RAW_WISPR / "calendar"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"

APP_FRIENDLY = {
    "com.anthropic.claudefordesktop": "Claude Desktop",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.google.Chrome": "Chrome",
    "com.apple.Safari": "Safari",
    "com.apple.Terminal": "Terminal",
    "com.googlecode.iterm2": "iTerm",
    "com.microsoft.VSCode": "VS Code",
    "com.todesktop.230313mzl4w4u92": "Cursor",
    "notion.id": "Notion",
    "md.obsidian": "Obsidian",
    "com.apple.mail": "Mail",
    "com.apple.iCal": "Calendar",
    "com.electron.gather": "Gather",
    "com.linear": "Linear",
}


@dataclass
class Stats:
    history_seen: int = 0
    history_written: int = 0
    history_already_present: int = 0
    history_skipped_empty: int = 0
    notes_written: int = 0
    notes_already_present: int = 0
    calendar_written: int = 0
    calendar_already_present: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    max_ts: str = ""  # newest History.timestamp seen this run, in DB format


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_run_iso": (dt.datetime.utcnow() - dt.timedelta(days=30)).isoformat() + "Z",
        "last_run_count": 0,
        "schema_version": 1,
    }


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def to_db_ts(ts: str) -> str:
    """Normalize an ISO-ish timestamp into Wispr's DB shape for lexical comparison.

    The History.timestamp column stores `YYYY-MM-DD HH:MM:SS.fff +00:00` (space
    separator). A cursor carrying a `T` separator compares greater than every DB
    row on the same date ('T' > ' '), silently excluding them — so any cursor
    must be space-separated before it reaches the SQL.
    """
    return ts.replace("T", " ").rstrip("Z").strip()


def open_db_ro(retries: int = 3) -> sqlite3.Connection:
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            conn = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.2 * (i + 1))
    raise RuntimeError(f"Could not open Wispr DB read-only after {retries} attempts: {last_err}")


def app_friendly(bundle: str) -> str:
    if not bundle:
        return "Unknown app"
    if bundle in APP_FRIENDLY:
        return APP_FRIENDLY[bundle]
    if bundle == "unknown":
        return "Unknown app"
    # Best-effort: take the last segment, title-case
    segs = bundle.split(".")
    return segs[-1].replace("-", " ").title() if segs else bundle


def duration_human(secs: Optional[float]) -> str:
    if secs is None:
        return "—"
    if secs < 120:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s}s"


def normalize_timestamp(ts: str) -> tuple[str, str]:
    """Wispr stores `YYYY-MM-DD HH:MM:SS.fff +00:00`. Return (iso, yyyy-mm-dd)."""
    if not ts:
        return "", ""
    # Parse Wispr's timestamp shape
    m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?: ([+-]\d{2}:\d{2}))?", ts)
    if not m:
        return ts, ts[:10]
    date_part = m.group(1)
    time_part = m.group(2)
    tz = m.group(3) or "+00:00"
    iso = f"{date_part}T{time_part}{tz}"
    return iso, date_part


def pick_body(row: sqlite3.Row) -> tuple[str, str]:
    """Return (body, source_label)."""
    for col in ("editedText", "formattedText", "asrText"):
        v = row[col]
        if v:
            return v, col
    return "", "empty"


def write_history_file(row: sqlite3.Row, stats: Stats, ingested_at: str) -> bool:
    """Write one history row → markdown. Returns True if created, False if skipped."""
    wispr_id = row["transcriptEntityId"]
    if not wispr_id:
        stats.errors.append(("history-no-id", str(dict(row))[:100]))
        return False
    out = RAW_WISPR / f"{wispr_id}.md"
    if out.exists():
        stats.history_already_present += 1
        return False
    body, body_source = pick_body(row)
    if not body:
        stats.history_skipped_empty += 1
        body = "(no text content)"
    iso, date_part = normalize_timestamp(row["timestamp"])
    app = row["app"] or "unknown"
    url = row["url"] or ""
    duration = row["duration"]
    num_words = row["numWords"]
    status = row["status"] or ""
    language = row["language"] or ""

    yaml_lines = [
        "---",
        "type: raw-wispr",
        f"wispr_id: {wispr_id}",
        f"timestamp: {iso}",
        f"date: {date_part}",
        f"app: {app}",
        f"url: {url}",
        f"duration: {duration if duration is not None else 'null'}",
        f"numWords: {num_words if num_words is not None else 'null'}",
        f"status: {status}",
        f"language: {language}",
        f"body_source: {body_source}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# {date_part} — {app_friendly(app)} ({duration_human(duration)})",
        "",
        body,
    ]
    out.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    stats.history_written += 1
    return True


def write_note_file(row: sqlite3.Row, stats: Stats, ingested_at: str) -> bool:
    note_id = row["id"]
    if not note_id:
        return False
    out = RAW_NOTES / f"{note_id}.md"
    if out.exists():
        stats.notes_already_present += 1
        return False
    title = row["title"] or "(untitled)"
    content = row["content"] or ""
    created = row["createdAt"] or ""
    modified = row["modifiedAt"] or ""
    iso, date_part = normalize_timestamp(created or modified)
    yaml_lines = [
        "---",
        "type: raw-wispr-note",
        f"note_id: {note_id}",
        f"title: {title}",
        f"created_at: {iso}",
        f"modified_at: {modified}",
        f"date: {date_part}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# {title}",
        "",
        content,
    ]
    out.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    stats.notes_written += 1
    return True


def write_calendar_file(row: sqlite3.Row, stats: Stats, ingested_at: str) -> bool:
    external_id = row["externalId"]
    if not external_id:
        return False
    out = RAW_CAL / f"{external_id}.md"
    if out.exists():
        stats.calendar_already_present += 1
        return False
    title = row["title"] or "(untitled)"
    start_utc = row["startAtUtc"]
    end_utc = row["endAtUtc"]
    # Wispr renamed the event-body column (`summary` → `prereadSummary`); prefer the
    # summary, fall back to the fuller pre-read content when only that is populated.
    summary = row["prereadSummary"] or row["prereadContent"] or ""
    conf_url = row["conferenceUrl"] or ""
    start_iso = dt.datetime.utcfromtimestamp(start_utc / 1000).isoformat() + "Z" if start_utc else ""
    yaml_lines = [
        "---",
        "type: raw-wispr-calendar",
        f"event_id: {external_id}",
        f"title: {title}",
        f"start_utc: {start_iso}",
        f"end_utc_ms: {end_utc or 'null'}",
        f"conference_url: {conf_url}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# {title}",
        "",
        summary,
    ]
    out.write_text("\n".join(yaml_lines) + "\n", encoding="utf-8")
    stats.calendar_written += 1
    return True


def run_ingest(since_iso: str, execute: bool, stats: Stats, ingested_at: str, dry_run_rows: list[dict]) -> None:
    if not execute:
        RAW_WISPR.mkdir(parents=True, exist_ok=True)
        RAW_NOTES.mkdir(parents=True, exist_ok=True)
        RAW_CAL.mkdir(parents=True, exist_ok=True)
    else:
        RAW_WISPR.mkdir(parents=True, exist_ok=True)
        RAW_NOTES.mkdir(parents=True, exist_ok=True)
        RAW_CAL.mkdir(parents=True, exist_ok=True)

    conn = open_db_ro()
    try:
        cur = conn.cursor()
        # History
        cur.execute("""
            SELECT transcriptEntityId, asrText, formattedText, editedText, timestamp,
                   app, url, duration, numWords, status, language
            FROM History
            WHERE timestamp > ?
              AND COALESCE(isArchived, 0) = 0
            ORDER BY timestamp ASC
        """, (since_iso,))
        for row in cur.fetchall():
            stats.history_seen += 1
            if row["timestamp"] and row["timestamp"] > stats.max_ts:
                stats.max_ts = row["timestamp"]
            wispr_id = row["transcriptEntityId"]
            if not execute:
                # Plan only
                out_path = RAW_WISPR / f"{wispr_id}.md"
                will_write = not out_path.exists()
                if will_write:
                    stats.history_written += 1
                else:
                    stats.history_already_present += 1
                if len(dry_run_rows) < 50:
                    dry_run_rows.append({
                        "wispr_id": wispr_id,
                        "timestamp": row["timestamp"],
                        "app": row["app"],
                        "duration": row["duration"],
                        "numWords": row["numWords"],
                        "will_write": will_write,
                    })
            else:
                write_history_file(row, stats, ingested_at)

        # Notes
        cur.execute("SELECT id, title, contentPreview, content, createdAt, modifiedAt FROM Notes WHERE COALESCE(isDeleted, 0) = 0")
        for row in cur.fetchall():
            if execute:
                write_note_file(row, stats, ingested_at)
            else:
                stats.notes_written += 1

        # CalendarEvents (Wispr renamed `summary` → `prereadSummary`; also pull
        # `prereadContent` as a fallback body — see write_calendar_file).
        cur.execute("SELECT externalId, title, startAtUtc, endAtUtc, conferenceUrl, status, prereadSummary, prereadContent FROM CalendarEvents")
        for row in cur.fetchall():
            if execute:
                write_calendar_file(row, stats, ingested_at)
            else:
                stats.calendar_written += 1
    finally:
        conn.close()


def write_dry_run_report(report_path: Path, stats: Stats, since_iso: str, rows: list[dict]) -> None:
    today = dt.date.today().isoformat()
    lines = [
        f"# atlas-wispr-ingest dry-run — {today}",
        "",
        "Skill: `atlas-wispr-ingest`",
        f"Source: `~/Library/Application Support/Wispr Flow/flow.sqlite` (read-only).",
        f"Window: rows with `timestamp > {since_iso}` (30-day first-run window).",
        f"Output: `raw/wispr/<wispr_id>.md`, plus `raw/wispr/notes/<id>.md` and `raw/wispr/calendar/<id>.md` for those tables.",
        "",
        "## Summary",
        "",
        f"- History rows in window: **{stats.history_seen}**",
        f"  - Would write: {stats.history_written}",
        f"  - Already present (idempotent skip): {stats.history_already_present}",
        f"  - Empty body (would write stub): {stats.history_skipped_empty}",
        f"- Notes rows (full table; would all be written or skipped): {stats.notes_written + stats.notes_already_present}",
        f"- Calendar rows: {stats.calendar_written + stats.calendar_already_present}",
        f"- Errors: {len(stats.errors)}",
        "",
        "## Acceptance checks",
        "",
        "- `SKILL.md` with trigger phrases — see SKILL.md description; includes `sync wispr` and `wispr ingest`.",
        f"- first run pulls last 30 days — **{'PASS' if stats.history_seen > 0 else 'FAIL'}** ({stats.history_seen} rows in window).",
        "- stable `wispr_id` in frontmatter — `transcriptEntityId` PK used as filename + frontmatter `wispr_id`.",
        "- `state.json` with `last_run_iso` + `last_run_count` — written by skill on every execute.",
        "- idempotent re-run — file-exists check before write; verified inline by re-running execute.",
        "- read-only sqlite mode — connection opened with `?mode=ro`; retry-on-locked logic with 3x 200ms backoff.",
        "",
        "## Sample rows (first 50)",
        "",
        "| # | Timestamp | App | Duration | Words | Will write? |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        ts = r["timestamp"][:19] if r["timestamp"] else "—"
        app = r["app"] or "unknown"
        dur = f"{r['duration']:.1f}s" if r["duration"] else "—"
        words = r["numWords"] or 0
        ww = "yes" if r["will_write"] else "(already present)"
        lines.append(f"| {i} | {ts} | {app} | {dur} | {words} | {ww} |")
    if stats.history_seen > 50:
        lines.append("")
        lines.append(f"_(+ {stats.history_seen - 50} more rows in window.)_")
    lines += [
        "",
        "## Errors",
        "",
    ]
    if stats.errors:
        for ctx, msg in stats.errors[:20]:
            lines.append(f"- {ctx}: {msg}")
    else:
        lines.append("None.")
    lines += [
        "",
        "## Next action",
        "",
        "Re-invoke with `python3 ingest.py --execute` to write the planned files.",
    ]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    lines = [
        "# atlas-wispr-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **state.last_run_iso:** {state.get('last_run_iso')}",
        f"- **state.last_run_count:** {state.get('last_run_count')}",
        f"- **History rows seen:** {stats.history_seen}",
        f"- **History written:** {stats.history_written}",
        f"- **History already present:** {stats.history_already_present}",
        f"- **History empty-body stubs:** {stats.history_skipped_empty}",
        f"- **Notes written:** {stats.notes_written}",
        f"- **Calendar written:** {stats.calendar_written}",
        f"- **Errors:** {len(stats.errors)}",
    ]
    LAST_RUN.write_text("\n".join(lines) + "\n", encoding="utf-8")



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
    parser = argparse.ArgumentParser(description="Ingest Wispr Flow history into raw/wispr/")
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--incremental", action="store_true", help="Use state.json's last_run_iso as window start.")
    parser.add_argument("--since", type=str, help="Override window start, e.g. 2026-02-01.")
    args = parser.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2

    if not WISPR_DB.exists():
        print(f"error: Wispr DB not found at {WISPR_DB}", file=sys.stderr)
        return 2

    if args.execute:
        if not acquire_run_lock():
            print("skipped: another run of this skill is in progress (duplicate-fire guard)")
            return 0
        import atexit
        atexit.register(release_run_lock)

    state = load_state()
    if args.since:
        since_iso = to_db_ts(args.since)
    elif args.incremental:
        # Prefer the DB-format cursor; fall back to legacy last_run_iso (converted).
        since_iso = to_db_ts(state.get("cursor_ts") or state["last_run_iso"])
    else:
        # First-run default: 30 days back
        since_iso = to_db_ts((dt.datetime.utcnow() - dt.timedelta(days=30)).isoformat())

    stats = Stats()
    rows: list[dict] = []
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    try:
        run_ingest(since_iso, args.execute, stats, ingested_at, rows)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    mode = "execute" if args.execute else "dry-run"

    if args.execute:
        # Cursor advances only from timestamps actually seen in the scan, kept in
        # the DB's own format, with a 24h overlap (per-file dedup makes re-scans
        # free). A zero-row run leaves the cursor untouched.
        if stats.max_ts:
            iso, _ = normalize_timestamp(stats.max_ts)
            try:
                cursor_dt = dt.datetime.fromisoformat(iso) - dt.timedelta(hours=24)
                state["cursor_ts"] = cursor_dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                state["cursor_ts"] = to_db_ts(stats.max_ts)
        state["last_run_iso"] = ingested_at  # informational: when the run happened
        state["last_run_count"] = state.get("last_run_count", 0) + stats.history_written
        save_state(state)

    if args.dry_run_report:
        write_dry_run_report(args.dry_run_report, stats, since_iso, rows)

    write_last_run(stats, mode, state)

    print(
        f"history_seen={stats.history_seen} written={stats.history_written} "
        f"already={stats.history_already_present} empty={stats.history_skipped_empty} "
        f"notes={stats.notes_written} calendar={stats.calendar_written} "
        f"errors={len(stats.errors)}"
    )
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
