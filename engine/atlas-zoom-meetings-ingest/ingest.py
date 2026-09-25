#!/usr/bin/env python3
"""atlas-zoom-meetings-ingest.

Turn Zoom meetings into one markdown file per meeting under
<vault_root>/raw/zoom/meetings/.

Three inputs, combinable in one run:
  --input-json   JSON a Claude session assembled from the official Zoom MCP
                 (search_meetings, recordings_list, get_meeting_assets,
                 get_recording_resource) or from the REST API: one meeting
                 object, a list, or {"meetings": [...]}. MCP envelopes
                 ({"result": "<json>"}) are unwrapped.
  .vtt files     transcripts downloaded from the Zoom web portal, or dropped
                 into a local-recording folder (--input-dir / --input, or
                 every export_dirs folder in zoom-sources.json).
  --fetch        the stdlib REST client in fetch.py (DEC-031), for headless
                 runs; it returns meetings in exactly the --input-json shape.

This script never touches the network itself. Only --fetch does, and only
through fetch.py. Stdlib only (DEC-021).

Transcript policy (DEC-032): a record with an AI Companion summary keeps the
summary and a pointer; the transcript stays in Zoom and is fetched on demand
(get_recording_resource via the Zoom MCP, or `fetch.py transcript <uuid>`).
Two exceptions write the transcript in full: an API record with no summary
(the transcript is its only content), and a file-based record (no API can
re-fetch a hand-downloaded file; the Teams ingest makes the same exception).
--include-transcript also writes it for a summary record (transcript_policy: full).

Idempotent: the file name ends in an 8-hex id derived from the meeting id, and
each file records a hash of the parsed content. Unchanged content is skipped;
changed content (a summary or transcript that landed after the first run)
rewrites in place.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
SKILL_DIR = Path(__file__).parent
SOURCES_FILE = SKILL_DIR / "zoom-sources.json"
STATE_FILE = SKILL_DIR / "state.json"
LAST_RUN = SKILL_DIR / "last-run.md"
RUN_LOCK = SKILL_DIR / ".run.lock"
RUN_LOCK_STALE_SECONDS = 3600

TRANSCRIPT_EXTS = (".vtt",)
NOTES_EXTS = (".md", ".txt")
# Files Zoom writes next to a local recording that are never a notes sidecar.
# Caption saving ended on 2026-05-18; the names stay here for older folders.
IGNORED_SIDECARS = {"chat.txt", "meeting_saved_chat.txt", "meeting_saved_closed_caption.txt"}

DEFAULT_LOOKBACK_DAYS = 7      # transcripts and summaries land after processing
FIRST_RUN_DAYS = 30

# A Zoom meeting and a Fireflies / Wispr / Gemini / Teams record are the same
# event when their starts fall within this window. Zoom's API start is the real
# call start, but Gemini's and Teams' starts are when note taking or
# transcription began, which lags the call start by up to about 9 minutes. Ten
# minutes covers every genuine pair. Titles are deliberately not compared.
MATCH_WINDOW_MIN = 10

# A downloaded transcript file and a JSON meeting describe the same meeting
# when their UTC starts are this close. Both come from Zoom's own clock.
FILE_MERGE_WINDOW_MIN = 2

UNKNOWN_SPEAKER = "Unknown speaker"

VTT_TIMING_RE = re.compile(
    r"^(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)
VTT_VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>(.*?)(?:</v>|$)", re.S)
TAG_RE = re.compile(r"<[^>]+>")

# Zoom cloud download: GMT20260910-185700_Recording.transcript.vtt (UTC).
NAME_GMT_RE = re.compile(r"GMT(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")
# Zoom local recording folder: "2026-09-10 14.57.00 Pinecrest Lodge weekly sync 81234567890" (local).
FOLDER_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})\s*(.*?)(?:\s+(\d{9,12}))?\s*$")
NAME_ISO_RE = re.compile(r"[-_ ]?(\d{4})-(\d{2})-(\d{2})(?:[ T_]+(\d{2})[:.h-]?(\d{2}))?")
NAME_NOISE_RE = re.compile(r"[-_ ]*(meeting recording|recording|transcript|closed captions?|captions?)\s*$", re.I)
VARIANT_SUFFIX_RE = re.compile(r"\.(transcript|cc)$", re.I)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BOLD_LINE_RE = re.compile(r"^\*\*(.+?)\*\*:?\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
NEXT_STEPS_RE = re.compile(r"next steps|action items", re.I)
SUMMARY_TITLE_PREFIX_RE = re.compile(r"^\s*meeting summary for\s+", re.I)
SUMMARY_TITLE_DATE_RE = re.compile(r"\s*\(\d{1,2}/\d{1,2}/\d{4}\)\s*$")


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    skipped_empty: int = 0
    skipped_duplicate_input: int = 0
    skipped_not_modified: int = 0
    merged: int = 0
    undated: int = 0
    with_notes: int = 0
    linked: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    max_mtime: str = ""
    max_start: str = ""


@dataclass
class Segment:
    offset_s: int
    speaker: str
    text: str


@dataclass
class Meeting:
    input: str                          # api | file
    zoom_id: str = ""                   # uuid, or zoom-<12hex>
    uuid: str = ""
    number: str = ""
    title: str = ""
    start_utc: Optional[dt.datetime] = None   # naive UTC; None when only a date is known
    local_date: str = ""                # YYYY-MM-DD in CFG.timezone
    date_source: str = ""               # api | filename-gmt | folder-name | filename | file-mtime
    duration_s: int = 0
    host_email: str = ""
    participants: list[str] = field(default_factory=list)
    attendee_emails: list[str] = field(default_factory=list)
    summary_md: str = ""                # rendered ## Summary body
    next_steps: list[str] = field(default_factory=list)   # rendered as ## Next steps
    next_steps_count: int = 0
    summary_modified: str = ""
    summary_doc_url: str = ""
    recording_url: str = ""
    segments: list[Segment] = field(default_factory=list)
    transcript_flag: bool = False       # JSON said a transcript exists even if not attached
    notes: str = ""
    source_file: str = ""
    path: Optional[Path] = None
    base_stem: str = ""
    mtime_iso: str = ""

    @property
    def has_summary(self) -> bool:
        return bool(self.summary_md.strip() or self.next_steps)


# --------------------------------------------------------------------------
# state / lock / sources
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "schema_version": 1,
            "cursor_mtime": None, "cursor_start": None}


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


def load_sources() -> dict:
    """zoom-sources.json: {"export_dirs": [...], "api": {"auth", "user_id",
    "redirect_uri", "lookback_days"}}. A missing file means no dirs, defaults."""
    if not SOURCES_FILE.exists():
        return {}
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {SOURCES_FILE} is not valid JSON: {exc}")
    return data if isinstance(data, dict) else {}


def load_source_dirs(sources: Optional[dict] = None) -> list[Path]:
    data = load_sources() if sources is None else sources
    dirs = data.get("export_dirs") or []
    return [Path(os.path.expanduser(str(d))) for d in dirs if str(d).strip()]


def lookback_days(sources: dict) -> int:
    api = sources.get("api") if isinstance(sources.get("api"), dict) else {}
    try:
        return max(0, int(api.get("lookback_days", DEFAULT_LOOKBACK_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_LOOKBACK_DAYS


def compute_window(state: dict, incremental: bool, since: Optional[str], lookback: int,
                   today: dt.date) -> tuple[dt.date, dt.date]:
    """The date window to ask Zoom for. Ends tomorrow (Zoom's `to` is inclusive
    and a late-evening meeting is already tomorrow in UTC)."""
    to = today + dt.timedelta(days=1)
    if since:
        frm = dt.date.fromisoformat(since)
    elif incremental and state.get("cursor_start"):
        frm = dt.date.fromisoformat(str(state["cursor_start"])[:10]) - dt.timedelta(days=lookback)
    else:
        frm = today - dt.timedelta(days=FIRST_RUN_DAYS)
    return frm, to


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def yaml_str(value: Optional[str]) -> str:
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


def to_utc(local: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Naive local time in CFG.timezone -> naive UTC."""
    if local is None:
        return None
    try:
        from zoneinfo import ZoneInfo  # stdlib, 3.9+
        aware = local.replace(tzinfo=ZoneInfo(CFG.timezone))
        return aware.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def to_local(utc: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Naive UTC -> aware datetime in CFG.timezone."""
    if utc is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return utc.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo(CFG.timezone))
    except Exception:
        return None


def parse_iso_utc(raw: Any) -> Optional[dt.datetime]:
    """2026-09-10T18:57:00Z (or with an offset) -> naive UTC. None on failure."""
    if not raw:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?", str(raw).strip())
    if not m:
        return None
    naive = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3) or '00'}", "%Y-%m-%d %H:%M:%S")
    tz = m.group(4)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        naive = naive - sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return naive


def clock_to_seconds(raw: str) -> int:
    raw = raw.replace(",", ".").split(".")[0]
    parts = [int(p) for p in raw.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


def fmt_offset(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def duration_human(seconds: int) -> str:
    mins = seconds // 60
    if mins <= 0:
        return ""
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


def id8_of(zoom_id: str) -> str:
    """8 hex for the file name. A Zoom UUID holds / + =, so it is hashed."""
    if zoom_id.startswith("zoom-"):
        return zoom_id[5:13]
    return hashlib.sha1(zoom_id.encode("utf-8")).hexdigest()[:8]


def local_id(title: str, key: str) -> str:
    return "zoom-" + hashlib.sha1(f"{slugify(title, 'untitled')}|{key}".encode("utf-8")).hexdigest()[:12]


def iso_mtime(path: Path) -> str:
    return dt.datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# transcripts
# --------------------------------------------------------------------------

def parse_vtt(text: str) -> tuple[list[Segment], int]:
    """WebVTT as Zoom emits it: numbered cues, `Name: text` inside the cue, no
    <v> tags; a cue without a name prefix is an unidentified speaker. <v Name>
    tags are accepted too. Returns (segments, duration_s from the last cue)."""
    segments: list[Segment] = []
    duration = 0
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = VTT_TIMING_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        start = clock_to_seconds(m.group("start"))
        duration = max(duration, clock_to_seconds(m.group("end")))
        i += 1
        payload: list[str] = []
        while i < len(lines) and lines[i].strip():
            payload.append(lines[i])
            i += 1
        body = "\n".join(payload)
        voices = VTT_VOICE_RE.findall(body)
        if voices:
            for speaker, spoken in voices:
                spoken = re.sub(r"\s+", " ", TAG_RE.sub("", spoken)).strip()
                if spoken:
                    segments.append(Segment(start, speaker.strip(), spoken))
            continue
        plain = re.sub(r"\s+", " ", TAG_RE.sub("", body)).strip()
        if not plain:
            continue
        speaker, _, rest = plain.partition(": ")
        if rest and len(speaker) <= 60 and len(speaker.split()) <= 6:
            segments.append(Segment(start, speaker.strip(), rest.strip()))
        else:
            segments.append(Segment(start, UNKNOWN_SPEAKER, plain))
    return segments, duration


def parse_plain_transcript(text: str) -> list[Segment]:
    """`Speaker: text` lines with no cue timings (what `fetch.py transcript`
    prints, and what some MCP responses carry)."""
    out: list[Segment] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s or s.upper() == "WEBVTT":
            continue
        speaker, _, rest = s.partition(": ")
        if rest and len(speaker) <= 60 and len(speaker.split()) <= 6:
            out.append(Segment(0, speaker.strip(), rest.strip()))
        else:
            out.append(Segment(0, UNKNOWN_SPEAKER, s))
    return out


def parse_transcript_text(text: str) -> tuple[list[Segment], int]:
    if not text or not text.strip():
        return [], 0
    segs, dur = parse_vtt(text)
    if segs or VTT_TIMING_RE.search(text) or "-->" in text:
        return segs, dur
    return parse_plain_transcript(text), 0


def render_transcript(segments: list[Segment]) -> str:
    """Zoom cues are 3 to 5 seconds long; consecutive cues by the same speaker
    collapse into one paragraph."""
    out: list[str] = []
    cur: Optional[str] = None
    cur_ts = 0
    buf: list[str] = []

    def flush() -> None:
        if buf and cur is not None:
            text = re.sub(r"\s+", " ", " ".join(buf)).strip()
            out.append(f"**{cur}** ({fmt_offset(cur_ts)}) {text}")
            out.append("")

    for seg in segments:
        if seg.speaker != cur:
            flush()
            buf = []
            cur, cur_ts = seg.speaker, seg.offset_s
        buf.append(seg.text)
    flush()
    return "\n".join(out).strip()


def speakers_of(segments: list[Segment]) -> list[str]:
    seen: list[str] = []
    for s in segments:
        if s.speaker not in seen and s.speaker != UNKNOWN_SPEAKER:
            seen.append(s.speaker)
    return seen


# --------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------

def demote_headings(markdown: str) -> str:
    """Shift headings so the shallowest one becomes ###, keeping the relative
    hierarchy. The vault file keeps a single H2 spine (## Summary, ## Next
    steps, ...), so `#` and `##` inside summary_content must not survive."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    levels: list[int] = []
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING_RE.match(line)
        if m:
            levels.append(len(m.group(1)))
    if not levels or min(levels) >= 3:
        return markdown.strip()
    shift = 3 - min(levels)
    out: list[str] = []
    fenced = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        m = None if fenced else HEADING_RE.match(line)
        if m:
            out.append("#" * min(6, len(m.group(1)) + shift) + " " + m.group(2).strip())
        else:
            out.append(line)
    return "\n".join(out).strip()


def count_next_steps(markdown: str) -> int:
    """Bullets under a heading (or a bold line) that reads next steps / action items."""
    count = 0
    level: Optional[int] = None
    fenced = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        h = HEADING_RE.match(line)
        b = BOLD_LINE_RE.match(line.strip())
        if h or b:
            lvl = len(h.group(1)) if h else 7
            title = h.group(2) if h else b.group(1)
            if NEXT_STEPS_RE.search(title):
                level = lvl
                continue
            if level is not None and lvl <= level:
                level = None
            continue
        if level is not None and BULLET_RE.match(line):
            count += 1
    return count


def _step_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("summary") or item.get("content") or "").strip()
    return str(item or "").strip()


def render_summary(fields: dict) -> tuple[str, list[str], int]:
    """(summary markdown, next-step list for a separate section, next_steps count)."""
    content = str(fields.get("summary_content") or "").strip()
    edited = fields.get("edited_summary") if isinstance(fields.get("edited_summary"), dict) else {}
    raw_steps = (edited or {}).get("next_steps") or fields.get("next_steps") or []
    if isinstance(raw_steps, str):
        raw_steps = [raw_steps]
    steps = [t for t in (_step_text(x) for x in raw_steps) if t]

    if content:
        md = demote_headings(content)
        counted = count_next_steps(md)
        if counted:
            return md, [], counted
        return md, steps, len(steps)

    parts: list[str] = []
    overview = str(fields.get("summary_overview") or "").strip()
    if overview:
        parts.append(overview)
    details = (edited or {}).get("summary_details") or fields.get("summary_details") or []
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        label = str(d.get("label") or "").strip()
        text = str(d.get("summary") or "").strip()
        if not (label or text):
            continue
        parts.append((f"### {label}\n\n" if label else "") + text)
    return "\n\n".join(parts).strip(), steps, len(steps)


# --------------------------------------------------------------------------
# JSON input
# --------------------------------------------------------------------------

def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return str(v)
    return ""


def _unwrap(value: Any) -> Any:
    """Peel MCP envelopes: {"result": "<json>"}, {"content": [{"type": "text",
    "text": "<json>"}]}, and JSON serialized into a string."""
    for _ in range(5):
        if isinstance(value, str):
            s = value.strip()
            if s[:1] in ("{", "["):
                try:
                    value = json.loads(s)
                    continue
                except json.JSONDecodeError:
                    return value
            return value
        if isinstance(value, dict):
            if "result" in value and len(value) <= 2 and isinstance(value["result"], (str, dict, list)):
                value = value["result"]
                continue
            content = value.get("content")
            if isinstance(content, list) and content and all(isinstance(c, dict) and "text" in c for c in content) \
                    and not any(k in value for k in ("uuid", "meeting_uuid", "topic")):
                value = "".join(str(c.get("text", "")) for c in content)
                continue
        return value
    return value


def entries_from_data(data: Any) -> list[dict]:
    data = _unwrap(data)
    if isinstance(data, dict):
        entries = data.get("meetings")
        if entries is None:
            entries = [data]
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    out: list[dict] = []
    for e in entries:
        e = _unwrap(e)
        if isinstance(e, dict):
            out.append(e)
    return out


def meeting_from_entry(entry: dict) -> Meeting:
    """One --input-json / fetch.py meeting dict -> Meeting. Raises ValueError
    when the entry has no usable start time."""
    summary = _unwrap(entry.get("summary"))
    if isinstance(summary, str):
        summary_fields: dict = {"summary_content": summary}
    elif isinstance(summary, dict):
        summary_fields = summary
    else:
        summary_fields = {}
    # Entry keys win; summary keys fill the gaps (the REST meeting_summary
    # response carries meeting_topic, meeting_start_time, ... itself).
    c: dict = dict(summary_fields)
    for k, v in entry.items():
        if k != "summary" and v not in (None, "", [], {}):
            c[k] = v

    uuid = _first(c, "uuid", "meeting_uuid")
    number = _first(c, "id", "meeting_id")
    title = _first(c, "topic", "meeting_topic", "title")
    if not title:
        st = _first(c, "summary_title")
        title = SUMMARY_TITLE_DATE_RE.sub("", SUMMARY_TITLE_PREFIX_RE.sub("", st)).strip()
    title = title or "Zoom meeting"

    start = parse_iso_utc(_first(c, "start_time", "meeting_start_time", "summary_start_time"))
    if start is None:
        raise ValueError("entry has no start_time")
    end = parse_iso_utc(_first(c, "end_time", "meeting_end_time", "summary_end_time"))

    m = Meeting(input="api", uuid=uuid, number=number, title=title, start_utc=start, date_source="api")
    local = to_local(start)
    m.local_date = (local or start).strftime("%Y-%m-%d")
    m.zoom_id = uuid or local_id(title, start.strftime("%Y-%m-%d %H:%M"))

    m.host_email = _first(c, "host_email", "meeting_host_email").lower()
    emails: list[str] = [m.host_email] if m.host_email else []
    for p in c.get("participants") or []:
        if isinstance(p, str):
            if "@" in p:
                emails.append(p.strip().lower())
            elif p.strip() and p.strip() not in m.participants:
                m.participants.append(p.strip())
        elif isinstance(p, dict):
            name = _first(p, "name", "user_name", "display_name").strip()
            email = _first(p, "email", "user_email").strip().lower()
            if name and name not in m.participants:
                m.participants.append(name)
            if email:
                emails.append(email)
    m.attendee_emails = sorted(set(e for e in emails if e))

    m.summary_md, m.next_steps, m.next_steps_count = render_summary(c)
    m.summary_modified = _first(c, "summary_last_modified_time")
    m.summary_doc_url = _first(c, "summary_doc_url")
    m.recording_url = _first(c, "share_url", "recording_url", "play_url")

    raw_t = _unwrap(c.get("transcript_vtt") or c.get("transcript") or "")
    if isinstance(raw_t, str):
        m.segments, cue_end = parse_transcript_text(raw_t)
    else:
        cue_end = 0
    m.transcript_flag = bool(c.get("has_transcript"))

    if end and end > start:
        m.duration_s = int((end - start).total_seconds())
    elif _first(c, "duration").isdigit():
        m.duration_s = int(_first(c, "duration")) * 60
    else:
        m.duration_s = cue_end
    if not m.participants:
        m.participants = speakers_of(m.segments)
    return m


def meetings_from_entries(entries: list, stats: Stats, label: str) -> list[Meeting]:
    out: list[Meeting] = []
    for i, entry in enumerate(entries_from_data(entries)):
        stats.seen += 1
        try:
            out.append(meeting_from_entry(entry))
        except Exception as exc:  # one bad entry must not abort the run
            key = _first(entry, "uuid", "meeting_uuid", "topic") or f"{label}[{i}]"
            stats.errors.append((key, f"entry skipped: {exc}"[:200]))
    return out


def meetings_from_json(path: Path, stats: Stats) -> list[Meeting]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        stats.errors.append((str(path), f"json parse failed: {exc}"[:200]))
        return []
    return meetings_from_entries(data, stats, path.name)


# --------------------------------------------------------------------------
# file input
# --------------------------------------------------------------------------

def base_stem(path: Path) -> str:
    """`X.transcript.vtt` and `X.cc.vtt` both -> `X`."""
    return VARIANT_SUFFIX_RE.sub("", path.stem)


def clean_title(raw: str) -> str:
    t = NAME_NOISE_RE.sub("", raw)
    t = re.sub(r"[_]+", " ", t)
    t = re.sub(r"\s*-\s*$|^\s*-\s*", "", t)
    return re.sub(r"\s{2,}", " ", t).strip(" -_")


def parse_folder_name(name: str) -> Optional[tuple[dt.datetime, str, str]]:
    """Local recording folder -> (local start, title, meeting number)."""
    m = FOLDER_RE.match(name.strip())
    if not m:
        return None
    try:
        start = dt.datetime(*(int(x) for x in m.groups()[:6]))
    except ValueError:
        return None
    return start, m.group(7).strip(), m.group(8) or ""


def parse_iso_name(stem: str) -> tuple[str, Optional[dt.datetime], bool]:
    """`2026-09-10 1457 Portal roadmap review` -> (title, local start, has_time)."""
    m = NAME_ISO_RE.search(stem)
    if not m:
        return clean_title(stem), None, False
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    has_time = bool(m.group(4))
    try:
        start = dt.datetime(y, mo, d, int(m.group(4) or 0), int(m.group(5) or 0))
    except ValueError:
        return clean_title(stem), None, False
    title = clean_title((stem[: m.start()] + " " + stem[m.end():]).strip())
    return title, start, has_time


def find_sidecar(path: Path) -> Optional[Path]:
    for name in dict.fromkeys((base_stem(path), path.stem)):
        for ext in NOTES_EXTS:
            cand = path.parent / (name + ext)
            if cand.name.lower() in IGNORED_SIDECARS or cand == path:
                continue
            if cand.is_file():
                return cand
    return None


def load_file_meeting(path: Path, roots: set[Path]) -> Meeting:
    m = Meeting(input="file", path=path, source_file=path.name, base_stem=base_stem(path), mtime_iso=iso_mtime(path))
    m.segments, m.duration_s = parse_vtt(path.read_text(encoding="utf-8-sig", errors="replace"))
    parent = path.parent
    generic_parent = parent.resolve() in roots
    folder = None if generic_parent else parse_folder_name(parent.name)

    gmt = NAME_GMT_RE.search(m.base_stem)
    start_utc: Optional[dt.datetime] = None
    local_start: Optional[dt.datetime] = None
    if gmt:
        try:
            start_utc = dt.datetime(*(int(x) for x in gmt.groups()))  # GMT stamp is already UTC
        except ValueError:
            start_utc = None
    if start_utc is not None:
        m.date_source = "filename-gmt"
        if folder:
            m.title, m.number = folder[1], folder[2]
        elif not generic_parent:
            m.title = parent.name.strip()
        m.title = m.title or "Zoom meeting"
    elif folder:
        local_start, m.title, m.number = folder
        m.date_source = "folder-name"
    else:
        title, iso_start, has_time = parse_iso_name(m.base_stem)
        m.title = title
        if iso_start is not None and has_time:
            local_start, m.date_source = iso_start, "filename"
        elif iso_start is not None:
            m.local_date, m.date_source = iso_start.strftime("%Y-%m-%d"), "filename"

    if local_start is not None:
        start_utc = to_utc(local_start)
        m.local_date = local_start.strftime("%Y-%m-%d")
    if start_utc is not None:
        m.start_utc = start_utc
        if not m.local_date:
            local = to_local(start_utc)
            m.local_date = (local or start_utc).strftime("%Y-%m-%d")
    if not m.date_source:
        mt = dt.datetime.fromtimestamp(path.stat().st_mtime)
        m.local_date, m.date_source = mt.strftime("%Y-%m-%d"), "file-mtime"
    m.title = m.title or clean_title(m.base_stem) or "Zoom meeting"

    if m.start_utc is not None:
        key = m.start_utc.strftime("%Y-%m-%d %H:%M")
    elif m.date_source == "file-mtime":
        key = m.base_stem
    else:
        key = m.local_date
    m.zoom_id = local_id(m.title, key)
    m.participants = speakers_of(m.segments)

    sidecar = find_sidecar(path)
    if sidecar is not None:
        m.notes = sidecar.read_text(encoding="utf-8", errors="replace").strip()
        m.mtime_iso = max(m.mtime_iso, iso_mtime(sidecar))
    return m


def collect_files(dirs: list[Path], files: list[Path], stats: Stats) -> list[Path]:
    out: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            stats.errors.append((str(d), "export dir not found"))
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in TRANSCRIPT_EXTS and not p.name.startswith(("~$", ".")):
                out.append(p)
    for p in files:
        if p.is_file() and p.suffix.lower() in TRANSCRIPT_EXTS:
            out.append(p)
        else:
            stats.errors.append((str(p), "not a .vtt file"))
    return out


def load_files(paths: list[Path], roots: set[Path], cursor: Optional[str], stats: Stats) -> list[Meeting]:
    chosen: dict[str, Meeting] = {}
    for path in paths:
        stats.seen += 1
        try:
            m = load_file_meeting(path, roots)
        except Exception as exc:  # one bad file must not abort the run
            stats.errors.append((path.name, f"parse failed: {exc}"[:200]))
            continue
        if m.mtime_iso > stats.max_mtime:
            stats.max_mtime = m.mtime_iso
        if cursor and m.mtime_iso <= cursor:
            stats.skipped_not_modified += 1
            continue
        # .transcript.vtt (audio transcript) and .cc.vtt (captions) of the same
        # meeting resolve to the same id: keep the one with more segments.
        prior = chosen.get(m.zoom_id)
        if prior is not None:
            stats.skipped_duplicate_input += 1
            if len(m.segments) <= len(prior.segments):
                continue
            m.notes = m.notes or prior.notes
        chosen[m.zoom_id] = m
    return list(chosen.values())


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def _fill(dst: Meeting, src: Meeting) -> None:
    for name in ("number", "host_email", "summary_md", "summary_modified", "summary_doc_url", "recording_url", "notes"):
        if not getattr(dst, name) and getattr(src, name):
            setattr(dst, name, getattr(src, name))
    if not dst.has_summary and src.has_summary:
        dst.summary_md, dst.next_steps, dst.next_steps_count = src.summary_md, src.next_steps, src.next_steps_count
    if not dst.segments and src.segments:
        dst.segments = src.segments
    dst.transcript_flag = dst.transcript_flag or src.transcript_flag
    dst.duration_s = dst.duration_s or src.duration_s
    for p in src.participants:
        if p not in dst.participants:
            dst.participants.append(p)
    dst.attendee_emails = sorted(set(dst.attendee_emails) | set(src.attendee_emails))


def merge_records(api: list[Meeting], files: list[Meeting], stats: Stats) -> list[Meeting]:
    """Same UUID twice (--input-json and --fetch) -> one record. A file whose
    UTC start is within FILE_MERGE_WINDOW_MIN of a JSON meeting -> that JSON
    meeting, which wins; the file supplies the transcript (and notes) only
    when the JSON lacked them."""
    by_id: dict[str, Meeting] = {}
    for m in api:
        prior = by_id.get(m.zoom_id)
        if prior is None:
            by_id[m.zoom_id] = m
        else:
            _fill(prior, m)
            stats.merged += 1
    out = list(by_id.values())
    api_recs = list(out)
    taken: set[str] = set()
    for f in files:
        best: Optional[tuple[float, Meeting]] = None
        if f.start_utc is not None:
            for a in api_recs:
                if a.start_utc is None or a.zoom_id in taken:
                    continue
                delta = abs((a.start_utc - f.start_utc).total_seconds()) / 60.0
                if delta <= FILE_MERGE_WINDOW_MIN and (best is None or delta < best[0]):
                    best = (delta, a)
        if best is None:
            out.append(f)
            continue
        a = best[1]
        taken.add(a.zoom_id)
        if not a.segments and f.segments:
            a.segments = f.segments
            if not a.duration_s:
                a.duration_s = f.duration_s
        a.notes = a.notes or f.notes
        a.source_file = f.source_file
        a.mtime_iso = f.mtime_iso
        if not a.participants:
            a.participants = speakers_of(a.segments)
        stats.merged += 1
    return out


# --------------------------------------------------------------------------
# cross-linking
# --------------------------------------------------------------------------

LINK_SOURCES = (
    # (source, subfolder under raw, id key in their frontmatter, our frontmatter id key)
    ("wispr", "wispr/meetings", "wispr_meeting_id", "wispr_meeting_id"),
    ("gemini", "gemini/meetings", "gemini_doc_id", "gemini_doc_id"),
    ("teams", "teams/meetings", "teams_meeting_id", "teams_meeting_id"),
)
LINK_KEYS = {"fireflies": "fireflies_id", **{s: k for s, _, _, k in LINK_SOURCES}}
LINK_LABELS = {"fireflies": "Fireflies", "wispr": "Wispr", "gemini": "Gemini", "teams": "Teams"}


def _fm(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 50:
                    break
                for key in keys:
                    if line.startswith(key + ":"):
                        found[key] = line.split(":", 1)[1].strip().strip('"')
    except OSError:
        pass
    return found


def _utc(raw: str) -> Optional[dt.datetime]:
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", raw or "")
    if not m:
        return None
    return dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")


def load_link_indexes(raw_root: Path) -> dict[str, list[tuple[dt.datetime, str, str]]]:
    """source -> [(start_utc, id, note_link)] for every other meeting ingest present."""
    out: dict[str, list[tuple[dt.datetime, str, str]]] = {"fireflies": [], "wispr": [], "gemini": [], "teams": []}
    ff = raw_root / "fireflies"
    if ff.is_dir():
        for p in ff.glob("*.md"):
            f = _fm(p, ("date", "meeting_id", "meeting_note"))
            start = _utc(f.get("date", ""))
            if start and f.get("meeting_id"):
                out["fireflies"].append((start, f["meeting_id"], f.get("meeting_note", "")))
    for source, sub, key, _ in LINK_SOURCES:
        folder = raw_root / sub
        if not folder.is_dir():
            continue
        for p in folder.glob("*.md"):
            f = _fm(p, ("timestamp", key))
            start = _utc(f.get("timestamp", ""))
            if start and f.get(key):
                out[source].append((start, f[key], f"[[{p.stem}]]"))
    return out


def match_nearest(start: Optional[dt.datetime], index: list[tuple[dt.datetime, str, str]]) -> Optional[tuple[str, str]]:
    if not start:
        return None
    best: Optional[tuple[float, str, str]] = None
    for other, oid, note in index:
        delta = abs((other - start).total_seconds()) / 60.0
        if delta <= MATCH_WINDOW_MIN and (best is None or delta < best[0]):
            best = (delta, oid, note)
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def transcript_policy(m: Meeting, include_transcript: bool) -> tuple[str, bool]:
    """(policy, included). See the module docstring for why."""
    if m.input == "file":
        return "file", bool(m.segments)
    if m.has_summary:
        if include_transcript and m.segments:
            return "full", True
        return "pointer", False
    return "only-content", bool(m.segments)


def content_hash(m: Meeting, policy: str, included: bool) -> str:
    h = hashlib.sha1()
    h.update(("summary:" + m.summary_md + "\n").encode("utf-8"))
    for s in m.next_steps:
        h.update(("step:" + s + "\n").encode("utf-8"))
    for s in m.segments:
        h.update(f"{s.offset_s}|{s.speaker}|{s.text}\n".encode("utf-8"))
    h.update(("notes:" + m.notes).encode("utf-8"))
    # Toggling --include-transcript changes the file, so it changes the hash.
    h.update(f"policy:{policy}|{included}".encode("utf-8"))
    return h.hexdigest()[:16]


def transcript_pointer(m: Meeting) -> str:
    human = []
    if m.recording_url:
        human.append(f"[open the recording]({m.recording_url})")
    if m.summary_doc_url:
        human.append(f"[open the summary doc]({m.summary_doc_url})")
    lead = "The transcript stays in Zoom (DEC-032)"
    if not (m.segments or m.transcript_flag):
        lead = "No transcript was attached when this record was written; if the meeting was cloud-recorded it stays in Zoom (DEC-032)"
    lines = [lead + (": " + ", ".join(human) + "." if human else ".")]
    if m.uuid:
        lines.append(
            f"AI tools: fetch it on demand with `get_recording_resource` via the Zoom MCP (meeting UUID `{m.uuid}`), "
            f"or run `python3 fetch.py transcript '{m.uuid}'` from engine/atlas-zoom-meetings-ingest."
        )
    if m.source_file:
        lines.append(f"A downloaded copy was also found on disk: `{m.source_file}`.")
    return "\n\n".join(lines)


def build_markdown(m: Meeting, chash: str, policy: str, included: bool,
                   links: dict[str, tuple[str, str]], ingested_at: str) -> str:
    local = to_local(m.start_utc) if m.start_utc else None
    duration = duration_human(m.duration_s)
    participants = m.participants or speakers_of(m.segments)
    transcript_available = bool(m.segments) or m.transcript_flag

    fm = [
        "---",
        "type: raw-zoom-meeting",
        f"zoom_id: {m.zoom_id}",
        f"zoom_meeting_number: {m.number}",
        f"title: {yaml_str(m.title)}",
        f"date: {m.local_date}",
        f"timestamp: {m.start_utc.strftime('%Y-%m-%d %H:%M:%S') if m.start_utc else ''}",
        f"timestamp_local: {yaml_str(local.strftime('%Y-%m-%d %H:%M ') + (local.tzname() or '') if local else '')}",
        f"date_source: {m.date_source}",
        f"input: {m.input}",
    ]
    if m.source_file:
        fm.append(f"source_file: {yaml_str(m.source_file)}")
    fm += [
        f"source_hash: {chash}",
        f"duration: {yaml_str(duration)}",
        f"host_email: {m.host_email}",
        f"attendee_emails: {yaml_list(m.attendee_emails)}",
        f"participants: {yaml_list(participants)}",
        f"has_summary: {'true' if m.has_summary else 'false'}",
        f"next_steps: {m.next_steps_count}",
        f"summary_doc_url: {m.summary_doc_url}",
        f"recording_url: {m.recording_url}",
        f"transcript_available: {'true' if transcript_available else 'false'}",
        f"transcript_included: {'true' if included else 'false'}",
        f"transcript_policy: {policy}",
        f"transcript_segments: {len(m.segments)}",
    ]
    for source, (oid, note) in links.items():
        fm.append(f"{LINK_KEYS[source]}: {oid}")
        if note:
            fm.append(f"{source}_note: {yaml_str(note)}")
    fm.append(f"ingested_at: {ingested_at}")
    fm.append("---")

    body = ["", f"# {m.local_date} — {m.title}", ""]
    meta = []
    if duration:
        meta.append(f"**Duration:** {duration}")
    if participants:
        meta.append(f"**Participants:** {', '.join(participants)}")
    if m.attendee_emails:
        meta.append(f"**Attendees:** {', '.join(m.attendee_emails)}")
    if m.input == "api":
        src = "Zoom AI Companion summary" if m.has_summary else "Zoom cloud transcript"
        if m.source_file:
            src += f" + transcript file (`{m.source_file}`)"
    else:
        src = f"Zoom transcript file (`{m.source_file}`)"
    meta.append(f"**Source:** {src}")
    body += ["  \n".join(meta), ""]
    for source, (_, note) in links.items():
        if note:
            body += [f"> Also captured by {LINK_LABELS[source]}: {note}", ""]
    if m.date_source == "file-mtime":
        body += ["> Start time unknown: the file name carried no date, so the date is the file's and cross-linking "
                 "was skipped. Keep Zoom's `GMT<date>-<time>` download name, or put the file in a "
                 "`YYYY-MM-DD HH.MM.SS Title` folder, to fix.", ""]
    if m.summary_md:
        body += ["## Summary", "", m.summary_md, ""]
    if m.next_steps:
        body += ["## Next steps", ""] + [f"- [ ] {s}" for s in m.next_steps] + [""]
    if m.notes:
        body += ["## Notes", "", m.notes, ""]
    body += ["## Transcript", ""]
    if included:
        body.append(render_transcript(m.segments))
    elif policy == "pointer":
        body.append(transcript_pointer(m))
    else:
        body.append("_(transcript file had no speech)_")
    body.append("")
    return "\n".join(fm + body) + "\n"


def existing_hash(path: Path) -> Optional[str]:
    return _fm(path, ("source_hash",)).get("source_hash") if path.exists() else None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run_ingest(records: list[Meeting], out_dir: Path, raw_root: Path, execute: bool, include_transcript: bool,
               stats: Stats, ingested_at: str, preview: list[dict]) -> None:
    indexes = load_link_indexes(raw_root)
    # An existing file is found by its 8-hex id suffix, so a renamed meeting or
    # a corrected start rewrites the same file instead of adding a second one.
    existing: dict[str, Path] = {}
    if out_dir.is_dir():
        for p in out_dir.glob("*.md"):
            existing[p.stem.rsplit("-", 1)[-1]] = p

    for m in records:
        if not (m.has_summary or m.segments or m.notes):
            stats.skipped_empty += 1
            continue
        if m.start_utc is not None:
            stamp = m.start_utc.strftime("%Y-%m-%d %H:%M:%S")
            if stamp > stats.max_start:
                stats.max_start = stamp
        if m.date_source == "file-mtime":
            stats.undated += 1
        links: dict[str, tuple[str, str]] = {}
        for source, index in indexes.items():
            hit = match_nearest(m.start_utc, index)
            if hit:
                links[source] = hit
        policy, included = transcript_policy(m, include_transcript)
        chash = content_hash(m, policy, included)
        id8 = id8_of(m.zoom_id)
        out = existing.get(id8) or out_dir / f"{m.local_date}-{slugify(m.title, 'zoom-meeting')}-{id8}.md"
        prior_hash = existing_hash(out)
        if prior_hash == chash:
            stats.already_present += 1
            continue
        is_update = prior_hash is not None
        if m.notes:
            stats.with_notes += 1
        if links:
            stats.linked += 1
        preview.append({
            "file": out.name, "date": m.local_date, "title": m.title, "input": m.input,
            "summary": "yes" if m.has_summary else "no", "segments": len(m.segments),
            "policy": policy, "date_source": m.date_source,
            "links": ",".join(sorted(links)), "action": "update" if is_update else "create",
        })
        if not execute:
            continue
        try:
            text = build_markdown(m, chash, policy, included, links, ingested_at)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, out)
            existing[id8] = out
            if is_update:
                stats.updated += 1
            else:
                stats.written += 1
        except OSError as exc:
            stats.errors.append((m.source_file or m.zoom_id, str(exc)[:200]))


def write_last_run(stats: Stats, mode: str, state: dict, window: Optional[tuple[dt.date, dt.date]]) -> None:
    lines = [
        "# atlas-zoom-meetings-ingest — last run", "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **Fetch window:** {f'{window[0]} to {window[1]}' if window else 'not fetched'}",
        f"- **state.cursor_mtime:** {state.get('cursor_mtime')}",
        f"- **state.cursor_start:** {state.get('cursor_start')}",
        f"- **Inputs seen:** {stats.seen}",
        f"- **Written (new):** {stats.written}",
        f"- **Updated (content changed):** {stats.updated}",
        f"- **Already present, unchanged:** {stats.already_present}",
        f"- **Skipped (not modified since cursor):** {stats.skipped_not_modified}",
        f"- **Skipped (empty):** {stats.skipped_empty}",
        f"- **Skipped (same meeting in two formats):** {stats.skipped_duplicate_input}",
        f"- **Merged (file into JSON, or duplicate JSON):** {stats.merged}",
        f"- **Undated (no date in file name or folder):** {stats.undated}",
        f"- **With notes sidecar:** {stats.with_notes}",
        f"- **Cross-linked:** {stats.linked}",
        f"- **Warnings:** {len(stats.warnings)}",
        f"- **Errors:** {len(stats.errors)}", "",
    ]
    if stats.warnings:
        lines += ["## Warnings", ""] + [f"- {w}" for w in stats.warnings[:20]] + [""]
    if stats.errors:
        lines += ["## Errors", ""] + [f"- `{k}` — {v}" for k, v in stats.errors[:20]] + [""]
    LAST_RUN.write_text("\n".join(lines), encoding="utf-8")


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict]) -> None:
    lines = [
        "# atlas-zoom-meetings-ingest — dry run", "",
        f"- Inputs seen: {stats.seen}",
        f"- Would create: {sum(1 for r in rows if r['action'] == 'create')}",
        f"- Would update: {sum(1 for r in rows if r['action'] == 'update')}",
        f"- Already present, unchanged: {stats.already_present}",
        f"- Merged: {stats.merged}",
        f"- Undated: {stats.undated}", "",
        "| Action | Date | Title | Input | Summary | Segs | Transcript | Date from | Linked to |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['action']} | {r['date']} | {r['title'][:44]} | {r['input']} | {r['summary']} | "
            f"{r['segments']} | {r['policy']} | {r['date_source']} | {r['links']} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Zoom meetings (AI Companion summaries and transcripts) into the vault.")
    ap.add_argument("--fetch", action="store_true", help="pull meetings from the Zoom REST API via fetch.py (needs credentials)")
    ap.add_argument("--input-json", type=Path, action="append", default=[],
                    help="JSON from the Zoom MCP or REST API: one meeting, a list, or {\"meetings\": [...]}. Repeatable.")
    ap.add_argument("--input-dir", type=Path, action="append", default=[],
                    help="folder of .vtt transcripts, searched recursively (repeatable); default: export_dirs in zoom-sources.json")
    ap.add_argument("--input", type=Path, action="append", default=[], help="a single .vtt file (repeatable)")
    ap.add_argument("--execute", action="store_true", help="write to the vault (default: dry run)")
    ap.add_argument("--incremental", action="store_true",
                    help="skip files not modified since state cursor_mtime; start the fetch window at cursor_start minus lookback_days")
    ap.add_argument("--since", help="start the fetch window at this date (YYYY-MM-DD), overriding the cursor")
    ap.add_argument("--include-transcript", action="store_true",
                    help="write the transcript even when a summary exists (default: pointer only, per DEC-032)")
    ap.add_argument("--print-window", action="store_true",
                    help="print the from=/to= dates a session should pass to the Zoom MCP, and exit")
    ap.add_argument("--dry-run-report", type=Path, help="write a dry-run report to this path")
    ap.add_argument("--vault", help="override the configured vault root for this run")
    args = ap.parse_args(argv)

    if args.since:
        try:
            dt.date.fromisoformat(args.since)
        except ValueError:
            print(f"error: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 1

    state = load_state()
    sources = load_sources()
    window = compute_window(state, args.incremental, args.since, lookback_days(sources), dt.date.today())
    if args.print_window:
        print(f"from={window[0].isoformat()} to={window[1].isoformat()}")
        return 0

    dirs = [Path(os.path.expanduser(str(d))) for d in args.input_dir] or ([] if args.input else load_source_dirs(sources))
    if not (dirs or args.input or args.input_json or args.fetch):
        print(f"error: no input. Pass --fetch, --input-json, --input-dir or --input, "
              f"or list export_dirs in {SOURCES_FILE.name}", file=sys.stderr)
        return 1

    vault = atlas_config.vault_from_arg(args.vault)
    raw_root = vault / CFG.folder_name("raw")
    out_dir = raw_root / "zoom" / "meetings"

    if not acquire_run_lock():
        print("error: another atlas-zoom-meetings-ingest run holds the lock", file=sys.stderr)
        return 1
    try:
        cursor = state.get("cursor_mtime") if args.incremental else None
        stats = Stats()
        preview: list[dict] = []

        api_recs: list[Meeting] = []
        for path in args.input_json:
            api_recs += meetings_from_json(path, stats)
        if args.fetch:
            try:
                import fetch  # only --fetch touches the network
                warnings: list[str] = []
                entries = fetch.pull(window[0], window[1], warnings=warnings)
                stats.warnings += warnings
                api_recs += meetings_from_entries(entries, stats, "fetch")
            except Exception as exc:
                stats.errors.append(("fetch", str(exc)[:300]))

        paths = collect_files(dirs, args.input, stats)
        roots = {d.resolve() for d in dirs if d.is_dir()} | {p.parent.resolve() for p in args.input}
        file_recs = load_files(paths, roots, cursor, stats)
        records = merge_records(api_recs, file_recs, stats)

        ingested_at = dt.datetime.now().isoformat(timespec="seconds")
        run_ingest(records, out_dir, raw_root, args.execute, args.include_transcript, stats, ingested_at, preview)
        if args.dry_run_report:
            write_dry_run_report(args.dry_run_report, stats, preview)

        mode = "execute" if args.execute else "dry-run"
        if args.execute:
            state["last_run_iso"] = dt.datetime.utcnow().isoformat() + "Z"
            state["last_run_count"] = state.get("last_run_count", 0) + stats.written
            state["schema_version"] = 1
            if stats.max_mtime and (not state.get("cursor_mtime") or stats.max_mtime > state["cursor_mtime"]):
                state["cursor_mtime"] = stats.max_mtime
            if stats.max_start and (not state.get("cursor_start") or stats.max_start > state["cursor_start"]):
                state["cursor_start"] = stats.max_start
            save_state(state)
        write_last_run(stats, mode, state, window if args.fetch else None)
        n_create = stats.written if args.execute else sum(1 for r in preview if r["action"] == "create")
        n_update = stats.updated if args.execute else sum(1 for r in preview if r["action"] == "update")
        print(
            f"[{mode}] seen={stats.seen} created={n_create} updated={n_update} "
            f"unchanged={stats.already_present} not_modified={stats.skipped_not_modified} "
            f"empty={stats.skipped_empty} dup_format={stats.skipped_duplicate_input} "
            f"merged={stats.merged} undated={stats.undated} linked={stats.linked} errors={len(stats.errors)}"
        )
        for w in stats.warnings[:10]:
            print(f"  warning: {w}", file=sys.stderr)
        for key, msg in stats.errors[:10]:
            print(f"  error {key}: {msg}", file=sys.stderr)
        return 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
