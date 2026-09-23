#!/usr/bin/env python3
"""atlas-wispr-meetings-ingest.

Read Wispr Flow's local Meetings table (read-only) plus the per-meeting
transcript NDJSON on disk, and emit one markdown file per meeting into
<vault_root>/raw/wispr/meetings/.

Sources (all local, no network, no auth):
  - ~/Library/Application Support/Wispr Flow/flow.sqlite  -> Meetings table
      title, notes (the owner's live notes), summary, participantNames,
      speakerMap, createdAt, endedAt, calendarEventExternalId, refineStatus
  - ~/Library/Application Support/Wispr Flow/meetings/<id>/refined.ndjson
      cleaned, timestamped, speaker-labelled transcript (preferred)
  - .../live.ndjson  -> fallback when refinement has not completed

Idempotent. Unlike the other raw ingests, a Wispr meeting is MUTABLE after
it ends: refinement completes asynchronously and the owner adds notes later. So a
file is rewritten when the source row's modifiedAt is newer than the
source_modified_at recorded in the existing file. See SKILL.md "Idempotency".
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
WISPR_DIR = Path.home() / "Library" / "Application Support" / "Wispr Flow"
WISPR_DB = WISPR_DIR / "flow.sqlite"
MEETINGS_DIR = WISPR_DIR / "meetings"
RAW_MEETINGS = CFG.folder("raw") / "wispr" / "meetings"
RAW_FIREFLIES = CFG.folder("raw") / "fireflies"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"
RUN_LOCK = Path(__file__).parent / ".run.lock"
RUN_LOCK_STALE_SECONDS = 3600

# A Wispr meeting and a Fireflies meeting are treated as the same event when
# their start times fall within this window of each other. In practice genuine
# matches cluster within about 3 minutes (most under 1 minute) while the nearest
# false positives sit past 10 minutes, so 5 minutes separates the two
# populations with margin on both sides. Titles are
# deliberately NOT compared -- Wispr auto-titles from content while Fireflies
# uses the calendar name, so the same meeting often carries two valid titles
# ("Portal roadmap review" vs "Jordan and the owner (weekly)").
FIREFLIES_MATCH_WINDOW_MIN = 5


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    skipped_deleted: int = 0
    skipped_no_transcript: int = 0
    with_notes: int = 0
    fireflies_linked: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    max_ts: str = ""


# --------------------------------------------------------------------------
# state / lock
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "schema_version": 1, "cursor_ts": None}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


def acquire_run_lock() -> bool:
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
        stale = True
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


def open_db_ro(retries: int = 3) -> sqlite3.Connection:
    """Read-only connection. mode=ro is load-bearing: Wispr writes concurrently."""
    last = None
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(f"file:{WISPR_DB}?mode=ro", uri=True, timeout=20)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as exc:  # pragma: no cover
            last = exc
            time.sleep(1 + attempt)
    raise SystemExit(f"cannot open Wispr DB read-only: {last}")


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def normalize_dt(raw: Optional[str]) -> tuple[str, str]:
    """Wispr DATETIME text -> (ISO-ish, YYYY-MM-DD). Empty on failure."""
    if not raw:
        return "", ""
    s = str(raw).strip().replace("T", " ")
    s = re.sub(r"\s*[+-]\d{2}:?\d{2}$", "", s)
    s = s.rstrip("Z").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ ]?(\d{2}:\d{2}:\d{2})?", s)
    if not m:
        return s, ""
    date_part, time_part = m.group(1), m.group(2) or "00:00:00"
    return f"{date_part} {time_part}", date_part


def parse_dt(raw: Optional[str]) -> Optional[dt.datetime]:
    iso, _ = normalize_dt(raw)
    if not iso:
        return None
    try:
        return dt.datetime.strptime(iso, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def yaml_str(value: Optional[str]) -> str:
    """Quote a scalar for YAML frontmatter."""
    if value is None:
        return '""'
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ").strip()
    return f'"{s}"'


def yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(yaml_str(v) for v in values) + "]"


def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s or fallback


def duration_human(start: Optional[dt.datetime], end_epoch_ms: Optional[int]) -> str:
    if not start or not end_epoch_ms:
        return ""
    try:
        end = dt.datetime.utcfromtimestamp(end_epoch_ms / 1000.0)
    except (OverflowError, OSError, ValueError):
        return ""
    mins = int((end - start).total_seconds() // 60)
    if mins <= 0 or mins > 24 * 60:
        return ""
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


# --------------------------------------------------------------------------
# speaker resolution
# --------------------------------------------------------------------------

def build_speaker_names(speaker_map_raw: Optional[str], participants: list[str]) -> dict[str, str]:
    """speakerMap JSON -> {speaker_id_str: display name}.

    speakerMap looks like:
      {"people": {"<uuid>": {"name": "<owner name>", ...}},
       "assignments": {"1": {"consensus": "<uuid>", "mic": "<uuid>", ...}}}
    Only some speakers are ever resolved (usually just self), so unresolved
    ids fall back to "Speaker N" and the participant roster is surfaced in
    frontmatter for manual disambiguation.
    """
    names: dict[str, str] = {}
    if not speaker_map_raw:
        return names
    try:
        data = json.loads(speaker_map_raw)
    except (json.JSONDecodeError, TypeError):
        return names
    people = data.get("people") or {}
    assignments = data.get("assignments") or {}
    for speaker_id, assign in assignments.items():
        if not isinstance(assign, dict):
            continue
        person_uuid = assign.get("consensus") or assign.get("user") or assign.get("mic") or assign.get("llm")
        if not person_uuid:
            continue
        person = people.get(person_uuid) or {}
        name = (person.get("name") or "").strip()
        if name:
            names[str(speaker_id)] = name
    return names


def speaker_label(speaker: dict, names: dict[str, str]) -> str:
    sid = speaker.get("id")
    if sid is None:
        return "Speaker ?"
    key = str(sid)
    if key in names:
        return names[key]
    inline = (speaker.get("name") or "").strip()
    if inline:
        return inline
    # Wispr uses 1..n for mic-side speakers and 1000+ for system-audio speakers.
    source = speaker.get("source") or ""
    if isinstance(sid, int) and sid >= 1000:
        return f"Speaker {sid - 999} (remote)"
    if source == "mic":
        return f"Speaker {sid} (mic)"
    return f"Speaker {sid}"


# --------------------------------------------------------------------------
# transcript loading
# --------------------------------------------------------------------------

def load_transcript(meeting_id: str) -> tuple[list[dict], str]:
    """Return (segments, source) preferring refined.ndjson over live.ndjson."""
    base = MEETINGS_DIR / meeting_id
    for filename, source in (("refined.ndjson", "refined"), ("live.ndjson", "live")):
        path = base / filename
        if not path.exists():
            continue
        segments: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # The first line of live.ndjson is a {"meta": ...} header.
                    if "meta" in obj and "text" not in obj:
                        continue
                    if not (obj.get("text") or "").strip():
                        continue
                    segments.append(obj)
        except OSError:
            continue
        if segments:
            return segments, source
    return [], "none"


def render_transcript(segments: list[dict], names: dict[str, str]) -> str:
    """Collapse consecutive same-speaker segments into readable paragraphs."""
    lines: list[str] = []
    cur_speaker: Optional[str] = None
    cur_ts: str = ""
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        text = " ".join(part.strip() for part in buf if part.strip())
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(f"**{cur_speaker}** ({cur_ts}) {text}")
            lines.append("")

    for seg in segments:
        label = speaker_label(seg.get("speaker") or {}, names)
        ts = str(seg.get("timestamp") or "").strip()
        if label != cur_speaker:
            flush()
            buf = []
            cur_speaker = label
            cur_ts = ts
        buf.append(seg.get("text") or "")
    flush()
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# fireflies cross-linking
# --------------------------------------------------------------------------

def load_fireflies_index() -> list[tuple[dt.datetime, str, str]]:
    """Scan raw/fireflies/*.md frontmatter -> [(start_dt, meeting_id, note_link)]."""
    index: list[tuple[dt.datetime, str, str]] = []
    if not RAW_FIREFLIES.is_dir():
        return index
    date_re = re.compile(r"^date:\s*(.+)$")
    id_re = re.compile(r"^meeting_id:\s*(.+)$")
    note_re = re.compile(r"^meeting_note:\s*(.+)$")
    for path in RAW_FIREFLIES.glob("*.md"):
        raw_date = meeting_id = note = ""
        try:
            with path.open("r", encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i > 40 or line.startswith("---") and i > 0:
                        if i > 40:
                            break
                    if m := date_re.match(line):
                        raw_date = m.group(1).strip()
                    elif m := id_re.match(line):
                        meeting_id = m.group(1).strip()
                    elif m := note_re.match(line):
                        note = m.group(1).strip().strip('"')
                    if raw_date and meeting_id and note:
                        break
        except OSError:
            continue
        if not (raw_date and meeting_id):
            continue
        started = parse_dt(raw_date)
        if started:
            index.append((started, meeting_id, note))
    return index


def match_fireflies(
    start: Optional[dt.datetime], index: list[tuple[dt.datetime, str, str]]
) -> Optional[tuple[str, str]]:
    """Nearest Fireflies meeting within the match window, or None.

    Wispr stores createdAt in UTC and Fireflies stores date in UTC, so these
    are directly comparable.
    """
    if not start or not index:
        return None
    best: Optional[tuple[float, str, str]] = None
    for ff_start, ff_id, ff_note in index:
        delta = abs((ff_start - start).total_seconds()) / 60.0
        if delta <= FIREFLIES_MATCH_WINDOW_MIN and (best is None or delta < best[0]):
            best = (delta, ff_id, ff_note)
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def build_markdown(
    row: sqlite3.Row,
    segments: list[dict],
    transcript_source: str,
    names: dict[str, str],
    participants: list[str],
    ff_match: Optional[tuple[str, str]],
    ingested_at: str,
) -> str:
    meeting_id = row["id"]
    title = (row["title"] or "").strip() or "(untitled meeting)"
    created_iso, date_part = normalize_dt(row["createdAt"])
    modified_iso, _ = normalize_dt(row["modifiedAt"])
    start_dt = parse_dt(row["createdAt"])
    notes = (row["notes"] or "").strip()
    summary = (row["summary"] or "").strip()
    duration = duration_human(start_dt, row["endedAt"])

    fm = [
        "---",
        "type: raw-wispr-meeting",
        f"wispr_meeting_id: {meeting_id}",
        f"title: {yaml_str(title)}",
        f"date: {date_part}",
        f"timestamp: {created_iso}",
        f"source_modified_at: {modified_iso}",
        f"duration: {yaml_str(duration)}",
        f"participants: {yaml_list(participants)}",
        f"speakers_resolved: {yaml_list(sorted(set(names.values())))}",
        f"transcript_source: {transcript_source}",
        f"transcript_segments: {len(segments)}",
        f"refine_status: {yaml_str(row['refineStatus'] or '')}",
        f"has_notes: {'true' if notes else 'false'}",
        f"calendar_event_id: {yaml_str(row['calendarEventExternalId'] or '')}",
        f"audio_path: {yaml_str(str(MEETINGS_DIR / meeting_id))}",
    ]
    if ff_match:
        fm.append(f"fireflies_id: {ff_match[0]}")
        if ff_match[1]:
            fm.append(f"fireflies_note: {yaml_str(ff_match[1].strip())}")
    fm.append(f"ingested_at: {ingested_at}")
    fm.append("---")

    body: list[str] = ["", f"# {date_part} — {title}", ""]

    meta_bits = []
    if duration:
        meta_bits.append(f"**Duration:** {duration}")
    if participants:
        meta_bits.append(f"**Participants:** {', '.join(participants)}")
    if meta_bits:
        body.append("  \n".join(meta_bits))
        body.append("")

    if ff_match and ff_match[1]:
        body.append(f"> Also captured by Fireflies: {ff_match[1].strip()}")
        body.append("")

    # The owner's own notes lead — they are the scarcest and highest-signal content.
    if notes:
        body.append("## My Notes")
        body.append("")
        body.append(notes)
        body.append("")

    if summary:
        body.append("## Summary")
        body.append("")
        body.append(summary)
        body.append("")

    body.append("## Transcript")
    body.append("")
    if segments:
        body.append(render_transcript(segments, names))
    else:
        body.append("_(no transcript available for this meeting)_")
    body.append("")

    return "\n".join(fm + body) + "\n"


def existing_source_modified(path: Path) -> Optional[str]:
    """Read source_modified_at out of an already-written raw file."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                if line.startswith("source_modified_at:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------
# main ingest
# --------------------------------------------------------------------------

def run_ingest(
    since_iso: Optional[str],
    execute: bool,
    stats: Stats,
    ingested_at: str,
    preview: list[dict],
) -> None:
    conn = open_db_ro()
    ff_index = load_fireflies_index()

    sql = (
        "SELECT id, title, createdAt, modifiedAt, endedAt, notes, summary, "
        "       participantNames, speakerMap, calendarEventExternalId, "
        "       refineStatus, finalized, isDeleted "
        "FROM Meetings "
        "WHERE COALESCE(isDeleted, 0) = 0 "
    )
    params: dict[str, str] = {}
    if since_iso:
        sql += "AND modifiedAt > :since "
        params["since"] = since_iso
    sql += "ORDER BY createdAt ASC"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        stats.errors.append(("query", str(exc)[:200]))
        conn.close()
        return

    for row in rows:
        stats.seen += 1
        meeting_id = row["id"]
        if not meeting_id:
            stats.errors.append(("no-id", "meeting row without id"))
            continue

        modified_iso, _ = normalize_dt(row["modifiedAt"])
        if modified_iso > stats.max_ts:
            stats.max_ts = modified_iso

        segments, transcript_source = load_transcript(meeting_id)
        notes = (row["notes"] or "").strip()
        # A meeting with neither transcript nor notes carries nothing.
        if not segments and not notes:
            stats.skipped_no_transcript += 1
            continue

        try:
            participants = json.loads(row["participantNames"] or "[]")
            participants = [str(p).strip() for p in participants if str(p).strip()]
        except (json.JSONDecodeError, TypeError):
            participants = []

        names = build_speaker_names(row["speakerMap"], participants)
        start_dt = parse_dt(row["createdAt"])
        ff_match = match_fireflies(start_dt, ff_index)

        _, date_part = normalize_dt(row["createdAt"])
        title = (row["title"] or "").strip() or "untitled"
        filename = f"{date_part or 'undated'}-{slugify(title, meeting_id[:8])}-{meeting_id[:8]}.md"
        out = RAW_MEETINGS / filename

        prior = existing_source_modified(out) if out.exists() else None
        is_update = prior is not None and prior != modified_iso
        if prior is not None and not is_update:
            stats.already_present += 1
            continue

        if notes:
            stats.with_notes += 1
        if ff_match:
            stats.fireflies_linked += 1

        preview.append(
            {
                "id": meeting_id,
                "file": filename,
                "date": date_part,
                "title": title,
                "segments": len(segments),
                "source": transcript_source,
                "notes": bool(notes),
                "fireflies": ff_match[0] if ff_match else "",
                "action": "update" if is_update else "create",
            }
        )

        if not execute:
            continue

        try:
            markdown = build_markdown(
                row, segments, transcript_source, names, participants, ff_match, ingested_at
            )
            RAW_MEETINGS.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text(markdown, encoding="utf-8")
            os.replace(tmp, out)
            if is_update:
                stats.updated += 1
            else:
                stats.written += 1
        except OSError as exc:
            stats.errors.append((meeting_id, str(exc)[:200]))

    conn.close()


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    lines = [
        "# atlas-wispr-meetings-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **state.last_run_iso:** {state.get('last_run_iso')}",
        f"- **state.last_run_count:** {state.get('last_run_count')}",
        f"- **Meetings seen:** {stats.seen}",
        f"- **Written (new):** {stats.written}",
        f"- **Updated (source changed):** {stats.updated}",
        f"- **Already present, unchanged:** {stats.already_present}",
        f"- **Skipped (no transcript, no notes):** {stats.skipped_no_transcript}",
        f"- **Carrying the owner's notes:** {stats.with_notes}",
        f"- **Cross-linked to Fireflies:** {stats.fireflies_linked}",
        f"- **Errors:** {len(stats.errors)}",
        "",
    ]
    if stats.errors:
        lines.append("## Errors")
        lines.append("")
        for key, msg in stats.errors[:20]:
            lines.append(f"- `{key}` — {msg}")
        lines.append("")
    LAST_RUN.write_text("\n".join(lines), encoding="utf-8")


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict]) -> None:
    lines = [
        "# atlas-wispr-meetings-ingest — dry run",
        "",
        f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- Meetings seen: {stats.seen}",
        f"- Would create: {sum(1 for r in rows if r['action'] == 'create')}",
        f"- Would update: {sum(1 for r in rows if r['action'] == 'update')}",
        f"- Already present, unchanged: {stats.already_present}",
        f"- Skipped (no transcript, no notes): {stats.skipped_no_transcript}",
        f"- Carrying notes: {stats.with_notes}",
        f"- Cross-linked to Fireflies: {stats.fireflies_linked}",
        "",
        "| Action | Date | Title | Segs | Source | Notes | Fireflies |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['action']} | {r['date']} | {r['title'][:44]} | {r['segments']} | "
            f"{r['source']} | {'yes' if r['notes'] else ''} | {r['fireflies'][:12]} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Wispr Flow meetings into the vault.")
    ap.add_argument("--execute", action="store_true", help="write to the vault (default: dry run)")
    ap.add_argument("--incremental", action="store_true", help="only meetings modified since last run")
    ap.add_argument("--since", help="override the window, e.g. 2026-08-01")
    ap.add_argument("--dry-run-report", help="write a dry-run report to this path")
    args = ap.parse_args(argv)

    if not WISPR_DB.exists():
        print(f"error: Wispr DB not found at {WISPR_DB}", file=sys.stderr)
        return 1

    if not acquire_run_lock():
        print("error: another atlas-wispr-meetings-ingest run holds the lock", file=sys.stderr)
        return 1

    try:
        state = load_state()
        since_iso: Optional[str] = None
        if args.since:
            since_iso, _ = normalize_dt(args.since)
        elif args.incremental:
            since_iso = state.get("cursor_ts")

        stats = Stats()
        preview: list[dict] = []
        ingested_at = dt.datetime.now().isoformat(timespec="seconds")
        run_started = dt.datetime.utcnow().isoformat() + "Z"

        run_ingest(since_iso, args.execute, stats, ingested_at, preview)

        if args.dry_run_report:
            write_dry_run_report(Path(args.dry_run_report), stats, preview)

        mode = "execute" if args.execute else "dry-run"
        if args.execute:
            state["last_run_iso"] = run_started
            state["last_run_count"] = state.get("last_run_count", 0) + stats.written
            state["schema_version"] = 1
            if stats.max_ts:
                state["cursor_ts"] = stats.max_ts
            save_state(state)
        write_last_run(stats, mode, state)

        if args.execute:
            n_create, n_update = stats.written, stats.updated
        else:
            n_create = sum(1 for r in preview if r["action"] == "create")
            n_update = sum(1 for r in preview if r["action"] == "update")
        print(
            f"[{mode}] seen={stats.seen} created={n_create} updated={n_update} "
            f"unchanged={stats.already_present} skipped={stats.skipped_no_transcript} "
            f"with_notes={stats.with_notes} ff_linked={stats.fireflies_linked} "
            f"errors={len(stats.errors)}"
        )
        for key, msg in stats.errors[:10]:
            print(f"  error {key}: {msg}", file=sys.stderr)
        return 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
