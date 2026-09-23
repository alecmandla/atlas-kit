#!/usr/bin/env python3
"""atlas-teams-meetings-ingest.

Turn Microsoft Teams meeting transcripts (.vtt or .docx, as downloaded from a
meeting's Recap tab or copied out of the organizer's OneDrive Recordings
folder) into one markdown file per meeting under
<vault_root>/raw/teams/meetings/.

Why a folder and not an API: Microsoft Graph can serve transcripts, but tenants
now gate Graph access to them behind an admin control that is off by default,
and Microsoft's own Teams MCP does not expose them. A folder of exported files
works in every tenant with nothing installed. Anything that can fetch a
transcript (a Graph MCP, a Power Automate flow, a person) only has to drop the
.vtt into the folder; Graph returns exactly that format.

Stdlib only (DEC-021): WebVTT is plain text and .docx is a zip of XML.

Transcript policy: unlike the Fireflies and Gemini ingests, the full transcript
IS written to disk. There is no API the query side can reliably call to fetch
it later, so a pointer-only record would be close to useless. This is the same
exception atlas-wispr-meetings-ingest makes for the same reason.

Idempotent: the file path derives from the meeting title + start, and each file
records a hash of the parsed content. Unchanged content is skipped, changed
content (a re-download after someone edited the transcript in Teams) rewrites
in place.
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
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
SKILL_DIR = Path(__file__).parent
SOURCES_FILE = SKILL_DIR / "teams-sources.json"
STATE_FILE = SKILL_DIR / "state.json"
LAST_RUN = SKILL_DIR / "last-run.md"
RUN_LOCK = SKILL_DIR / ".run.lock"
RUN_LOCK_STALE_SECONDS = 3600

TRANSCRIPT_EXTS = (".vtt", ".docx")
NOTES_EXTS = (".md", ".txt")

# A Teams transcript and a Fireflies / Wispr / Gemini record are the same event
# when their starts fall within this window. A Teams start comes from the file
# name or the .docx header, which reflect when transcription began, so it lags
# the call start the same way Gemini's does. Titles are not compared.
MATCH_WINDOW_MIN = 10

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

VTT_TIMING_RE = re.compile(
    r"^(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)
VTT_VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>(.*?)(?:</v>|$)", re.S)
TAG_RE = re.compile(r"<[^>]+>")
DOCX_SPEAKER_RE = re.compile(r"^(?P<name>[^\t]{1,80}?)(?:\t| {2,})(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*$")
DOCX_SPEAKER_LOOSE_RE = re.compile(
    r"^(?P<name>[A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,4}(?: \([^)]*\))?) (?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*$"
)
DOCX_DATE_RE = re.compile(
    r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:,?\s+(?P<time>\d{1,2}:\d{2})\s*(?P<ampm>[AaPp][Mm]))?"
)
DOCX_DURATION_RE = re.compile(r"^(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?$")
TRANSCRIPTION_EVENT_RE = re.compile(r"(started|stopped) transcription\s*$", re.I)

NAME_STAMP_RE = re.compile(r"[-_ ]?(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")
NAME_ISO_RE = re.compile(r"[-_ ]?(\d{4})-(\d{2})-(\d{2})(?:[ T_]+(\d{2})[:.h-]?(\d{2}))?")
NAME_NOISE_RE = re.compile(r"[-_ ]*(meeting recording|meeting transcript|transcript|recording)\s*$", re.I)


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    skipped_empty: int = 0
    skipped_duplicate_input: int = 0
    skipped_not_modified: int = 0
    undated: int = 0
    with_notes: int = 0
    linked: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    max_mtime: str = ""


@dataclass
class Segment:
    offset_s: int
    speaker: str
    text: str


@dataclass
class Meeting:
    path: Path
    title: str = ""
    local_start: Optional[dt.datetime] = None
    date_source: str = ""          # filename | docx-header | file-mtime
    duration_s: int = 0
    segments: list[Segment] = field(default_factory=list)
    notes: str = ""
    notes_path: Optional[Path] = None
    mtime_iso: str = ""


# --------------------------------------------------------------------------
# state / lock
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "schema_version": 1, "cursor_mtime": None}


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


def load_source_dirs() -> list[Path]:
    """teams-sources.json: {"export_dirs": ["~/Documents/Teams-Transcripts", ...]}."""
    if not SOURCES_FILE.exists():
        return []
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {SOURCES_FILE} is not valid JSON: {exc}")
    dirs = data.get("export_dirs") if isinstance(data, dict) else None
    return [Path(os.path.expanduser(str(d))) for d in (dirs or []) if str(d).strip()]


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
    if local is None:
        return None
    try:
        from zoneinfo import ZoneInfo  # stdlib, 3.9+
        aware = local.replace(tzinfo=ZoneInfo(CFG.timezone))
        return aware.astimezone(dt.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


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


def parse_filename(stem: str) -> tuple[str, Optional[dt.datetime]]:
    """Pull a local start and a clean title out of a file name.

    Recognized: OneDrive's recording stamp `Title-20260910_145700-Meeting Recording`,
    and an ISO date with an optional time, `2026-09-10 1457 Title` or `Title_2026-09-10`.
    """
    title, start = stem, None
    m = NAME_STAMP_RE.search(stem)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            start = dt.datetime(y, mo, d, h, mi, s)
            title = (stem[: m.start()] + " " + stem[m.end():]).strip()
        except ValueError:
            start = None
    if start is None:
        m = NAME_ISO_RE.search(stem)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            h = int(m.group(4)) if m.group(4) else 0
            mi = int(m.group(5)) if m.group(5) else 0
            try:
                start = dt.datetime(y, mo, d, h, mi)
                title = (stem[: m.start()] + " " + stem[m.end():]).strip()
            except ValueError:
                start = None
    title = NAME_NOISE_RE.sub("", title)
    title = re.sub(r"[_]+", " ", title)
    title = re.sub(r"\s*-\s*$|^\s*-\s*", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -_")
    return title, start


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

def parse_vtt(text: str) -> tuple[list[Segment], int]:
    """WebVTT as Teams and Graph emit it. Returns (segments, duration_s)."""
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
        else:
            plain = re.sub(r"\s+", " ", TAG_RE.sub("", body)).strip()
            if plain:
                speaker, _, rest = plain.partition(": ")
                if rest and len(speaker) <= 60 and len(speaker.split()) <= 6:
                    segments.append(Segment(start, speaker.strip(), rest.strip()))
                else:
                    segments.append(Segment(start, "Unknown speaker", plain))
    return segments, duration


def docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)
    paras: list[str] = []
    for p in root.iter(W_NS + "p"):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == W_NS + "t" and node.text:
                parts.append(node.text)
            elif node.tag == W_NS + "tab":
                parts.append("\t")
            elif node.tag in (W_NS + "br", W_NS + "cr"):
                parts.append("\n")
        paras.append("".join(parts).strip("\n"))
    return paras


def parse_docx(path: Path) -> tuple[str, Optional[dt.datetime], int, list[Segment]]:
    """Teams' .docx transcript: title, date line, duration line, then
    `Speaker<tab or spaces>m:ss` lines each followed by what they said."""
    paras = [p for p in docx_paragraphs(path)]
    title = ""
    start: Optional[dt.datetime] = None
    duration = 0
    segments: list[Segment] = []
    speaker: Optional[str] = None
    offset = 0
    buf: list[str] = []

    def flush() -> None:
        if speaker is not None:
            text = re.sub(r"\s+", " ", " ".join(buf)).strip()
            if text:
                segments.append(Segment(offset, speaker, text))

    header_done = False
    for raw in paras:
        line = raw.strip()
        if not line:
            continue
        m = DOCX_SPEAKER_RE.match(line) or DOCX_SPEAKER_LOOSE_RE.match(line)
        if m:
            flush()
            header_done = True
            speaker = m.group("name").strip()
            offset = clock_to_seconds(m.group("ts"))
            buf = []
            continue
        if TRANSCRIPTION_EVENT_RE.search(line) and len(line.split()) <= 8:
            continue
        if not header_done:
            dm = DOCX_DATE_RE.match(line)
            if dm and start is None:
                try:
                    day = dt.datetime.strptime(dm.group("date"), "%B %d, %Y")
                    if dm.group("time"):
                        t = dt.datetime.strptime(f"{dm.group('time')} {dm.group('ampm').upper()}", "%I:%M %p")
                        day = day.replace(hour=t.hour, minute=t.minute)
                    start = day
                except ValueError:
                    pass
                continue
            um = DOCX_DURATION_RE.match(line)
            if um and any(um.groups()):
                h, mi, s = (int(x) if x else 0 for x in um.groups())
                duration = h * 3600 + mi * 60 + s
                continue
            if not title:
                title = line
            continue
        buf.append(line)
    flush()
    return title, start, duration, segments


# --------------------------------------------------------------------------
# cross-linking
# --------------------------------------------------------------------------

def _fm(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 40:
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
    out: dict[str, list[tuple[dt.datetime, str, str]]] = {"fireflies": [], "wispr": [], "gemini": []}
    ff = raw_root / "fireflies"
    if ff.is_dir():
        for p in ff.glob("*.md"):
            f = _fm(p, ("date", "meeting_id", "meeting_note"))
            start = _utc(f.get("date", ""))
            if start and f.get("meeting_id"):
                out["fireflies"].append((start, f["meeting_id"], f.get("meeting_note", "")))
    for source, sub, key in (("wispr", "wispr/meetings", "wispr_meeting_id"), ("gemini", "gemini/meetings", "gemini_doc_id")):
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
# load + render
# --------------------------------------------------------------------------

def iso_mtime(path: Path) -> str:
    return dt.datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def load_meeting(path: Path) -> Meeting:
    meeting = Meeting(path=path, mtime_iso=iso_mtime(path))
    name_title, name_start = parse_filename(path.stem)
    if path.suffix.lower() == ".vtt":
        segs, dur = parse_vtt(path.read_text(encoding="utf-8-sig", errors="replace"))
        meeting.segments, meeting.duration_s = segs, dur
        meeting.title = name_title
    else:
        doc_title, doc_start, dur, segs = parse_docx(path)
        meeting.segments, meeting.duration_s = segs, dur
        meeting.title = doc_title or name_title
        if doc_start is not None and name_start is None:
            meeting.local_start, meeting.date_source = doc_start, "docx-header"
    if meeting.local_start is None and name_start is not None:
        meeting.local_start, meeting.date_source = name_start, "filename"
    if meeting.local_start is None:
        local = dt.datetime.fromtimestamp(path.stat().st_mtime)
        meeting.local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        meeting.date_source = "file-mtime"
    meeting.title = meeting.title or "(untitled Teams meeting)"
    for ext in NOTES_EXTS:
        sidecar = path.with_suffix(ext)
        if sidecar.exists():
            meeting.notes = sidecar.read_text(encoding="utf-8", errors="replace").strip()
            meeting.notes_path = sidecar
            meeting.mtime_iso = max(meeting.mtime_iso, iso_mtime(sidecar))
            break
    return meeting


def meeting_id(m: Meeting) -> str:
    key = f"{slugify(m.title, 'untitled')}|"
    if m.date_source == "file-mtime":
        key += m.path.stem
    else:
        key += m.local_start.strftime("%Y-%m-%d %H:%M")
    return "teams-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def content_hash(m: Meeting) -> str:
    h = hashlib.sha1()
    for s in m.segments:
        h.update(f"{s.offset_s}|{s.speaker}|{s.text}\n".encode("utf-8"))
    h.update(("notes:" + m.notes).encode("utf-8"))
    return h.hexdigest()[:16]


def render_transcript(segments: list[Segment]) -> str:
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


def speakers_of(m: Meeting) -> list[str]:
    seen: list[str] = []
    for s in m.segments:
        if s.speaker not in seen and s.speaker != "Unknown speaker":
            seen.append(s.speaker)
    return seen


def build_markdown(m: Meeting, mid: str, chash: str, links: dict[str, tuple[str, str]], ingested_at: str) -> str:
    date_part = m.local_start.strftime("%Y-%m-%d")
    utc = to_utc(m.local_start) if m.date_source != "file-mtime" else None
    has_time = m.date_source != "file-mtime" and (m.local_start.hour or m.local_start.minute)
    participants = speakers_of(m)
    duration = duration_human(m.duration_s)

    fm = [
        "---",
        "type: raw-teams-meeting",
        f"teams_meeting_id: {mid}",
        f"title: {yaml_str(m.title)}",
        f"date: {date_part}",
        f"timestamp: {utc.strftime('%Y-%m-%d %H:%M:%S') if (utc and has_time) else ''}",
        f"date_source: {m.date_source}",
        f"source_file: {yaml_str(m.path.name)}",
        f"source_hash: {chash}",
        f"duration: {yaml_str(duration)}",
        f"participants: {yaml_list(participants)}",
        f"transcript_segments: {len(m.segments)}",
        f"has_notes: {'true' if m.notes else 'false'}",
    ]
    for source, (oid, note) in links.items():
        key = {"fireflies": "fireflies_id", "wispr": "wispr_meeting_id", "gemini": "gemini_doc_id"}[source]
        fm.append(f"{key}: {oid}")
        if note:
            fm.append(f"{source}_note: {yaml_str(note)}")
    fm.append(f"ingested_at: {ingested_at}")
    fm.append("---")

    body = ["", f"# {date_part} — {m.title}", ""]
    meta = []
    if duration:
        meta.append(f"**Duration:** {duration}")
    if participants:
        meta.append(f"**Speakers:** {', '.join(participants)}")
    meta.append(f"**Source:** Microsoft Teams transcript (`{m.path.name}`)")
    body += ["  \n".join(meta), ""]
    for source, (_, note) in links.items():
        if note:
            body += [f"> Also captured by {source.capitalize()}: {note}", ""]
    if m.date_source == "file-mtime":
        body += ["> Start time unknown: the file name carried no date, so the date is the file's and cross-linking was skipped. Rename the file with a `YYYYMMDD_HHMMSS` stamp to fix.", ""]
    if m.notes:
        body += ["## Notes", "", m.notes, ""]
    body += ["## Transcript", ""]
    body.append(render_transcript(m.segments) if m.segments else "_(transcript file had no speech)_")
    body.append("")
    return "\n".join(fm + body) + "\n"


def existing_hash(path: Path) -> Optional[str]:
    return _fm(path, ("source_hash",)).get("source_hash") if path.exists() else None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

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
            stats.errors.append((str(p), "not a .vtt or .docx file"))
    return out


def run_ingest(paths: list[Path], out_dir: Path, raw_root: Path, cursor: Optional[str],
               execute: bool, stats: Stats, ingested_at: str, preview: list[dict]) -> None:
    indexes = load_link_indexes(raw_root)
    chosen: dict[str, Meeting] = {}
    for path in paths:
        stats.seen += 1
        try:
            m = load_meeting(path)
        except Exception as exc:  # one bad file must not abort the run
            stats.errors.append((path.name, f"parse failed: {exc}"[:200]))
            continue
        if m.mtime_iso > stats.max_mtime:
            stats.max_mtime = m.mtime_iso
        if cursor and m.mtime_iso <= cursor:
            stats.skipped_not_modified += 1
            continue
        if not m.segments and not m.notes:
            stats.skipped_empty += 1
            continue
        mid = meeting_id(m)
        # The same meeting downloaded as both .vtt and .docx: keep the richer one.
        prior = chosen.get(mid)
        if prior is not None:
            stats.skipped_duplicate_input += 1
            if len(m.segments) <= len(prior.segments):
                continue
        chosen[mid] = m

    for mid, m in chosen.items():
        if m.date_source == "file-mtime":
            stats.undated += 1
        utc = to_utc(m.local_start) if m.date_source != "file-mtime" else None
        links: dict[str, tuple[str, str]] = {}
        for source, index in indexes.items():
            hit = match_nearest(utc, index)
            if hit:
                links[source] = hit
        chash = content_hash(m)
        filename = f"{m.local_start:%Y-%m-%d}-{slugify(m.title, 'teams-meeting')}-{mid[6:14]}.md"
        out = out_dir / filename
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
            "file": filename, "date": f"{m.local_start:%Y-%m-%d}", "title": m.title,
            "segments": len(m.segments), "date_source": m.date_source,
            "links": ",".join(sorted(links)), "action": "update" if is_update else "create",
        })
        if not execute:
            continue
        try:
            text = build_markdown(m, mid, chash, links, ingested_at)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, out)
            if is_update:
                stats.updated += 1
            else:
                stats.written += 1
        except OSError as exc:
            stats.errors.append((m.path.name, str(exc)[:200]))


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    lines = [
        "# atlas-teams-meetings-ingest — last run", "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **state.cursor_mtime:** {state.get('cursor_mtime')}",
        f"- **Files seen:** {stats.seen}",
        f"- **Written (new):** {stats.written}",
        f"- **Updated (content changed):** {stats.updated}",
        f"- **Already present, unchanged:** {stats.already_present}",
        f"- **Skipped (not modified since cursor):** {stats.skipped_not_modified}",
        f"- **Skipped (empty):** {stats.skipped_empty}",
        f"- **Skipped (same meeting in two formats):** {stats.skipped_duplicate_input}",
        f"- **Undated (no date in file name or header):** {stats.undated}",
        f"- **With notes sidecar:** {stats.with_notes}",
        f"- **Cross-linked:** {stats.linked}",
        f"- **Errors:** {len(stats.errors)}", "",
    ]
    if stats.errors:
        lines += ["## Errors", ""] + [f"- `{k}` — {v}" for k, v in stats.errors[:20]] + [""]
    LAST_RUN.write_text("\n".join(lines), encoding="utf-8")


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict]) -> None:
    lines = [
        "# atlas-teams-meetings-ingest — dry run", "",
        f"- Files seen: {stats.seen}",
        f"- Would create: {sum(1 for r in rows if r['action'] == 'create')}",
        f"- Would update: {sum(1 for r in rows if r['action'] == 'update')}",
        f"- Already present, unchanged: {stats.already_present}",
        f"- Undated: {stats.undated}", "",
        "| Action | Date | Title | Segs | Date from | Linked to |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r['action']} | {r['date']} | {r['title'][:44]} | {r['segments']} | {r['date_source']} | {r['links']} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Microsoft Teams transcripts (.vtt/.docx) into the vault.")
    ap.add_argument("--input-dir", type=Path, action="append", default=[],
                    help="folder of exported transcripts (repeatable); default: export_dirs in teams-sources.json")
    ap.add_argument("--input", type=Path, action="append", default=[], help="a single .vtt or .docx file (repeatable)")
    ap.add_argument("--execute", action="store_true", help="write to the vault (default: dry run)")
    ap.add_argument("--incremental", action="store_true", help="skip files not modified since state.json cursor_mtime")
    ap.add_argument("--dry-run-report", type=Path, help="write a dry-run report to this path")
    ap.add_argument("--vault", help="override the configured vault root for this run")
    args = ap.parse_args(argv)

    dirs = [Path(os.path.expanduser(str(d))) for d in args.input_dir] or ([] if args.input else load_source_dirs())
    if not dirs and not args.input:
        print(f"error: no input. Pass --input-dir, or list export_dirs in {SOURCES_FILE.name}", file=sys.stderr)
        return 1

    vault = atlas_config.vault_from_arg(args.vault)
    raw_root = vault / CFG.folder_name("raw")
    out_dir = raw_root / "teams" / "meetings"

    if not acquire_run_lock():
        print("error: another atlas-teams-meetings-ingest run holds the lock", file=sys.stderr)
        return 1
    try:
        state = load_state()
        cursor = state.get("cursor_mtime") if args.incremental else None
        stats = Stats()
        preview: list[dict] = []
        paths = collect_files(dirs, args.input, stats)
        ingested_at = dt.datetime.now().isoformat(timespec="seconds")
        run_ingest(paths, out_dir, raw_root, cursor, args.execute, stats, ingested_at, preview)
        if args.dry_run_report:
            write_dry_run_report(args.dry_run_report, stats, preview)
        mode = "execute" if args.execute else "dry-run"
        if args.execute:
            state["last_run_iso"] = dt.datetime.utcnow().isoformat() + "Z"
            state["last_run_count"] = state.get("last_run_count", 0) + stats.written
            if stats.max_mtime and (not state.get("cursor_mtime") or stats.max_mtime > state["cursor_mtime"]):
                state["cursor_mtime"] = stats.max_mtime
            save_state(state)
        write_last_run(stats, mode, state)
        n_create = stats.written if args.execute else sum(1 for r in preview if r["action"] == "create")
        n_update = stats.updated if args.execute else sum(1 for r in preview if r["action"] == "update")
        print(
            f"[{mode}] seen={stats.seen} created={n_create} updated={n_update} "
            f"unchanged={stats.already_present} not_modified={stats.skipped_not_modified} "
            f"empty={stats.skipped_empty} dup_format={stats.skipped_duplicate_input} "
            f"undated={stats.undated} linked={stats.linked} errors={len(stats.errors)}"
        )
        for key, msg in stats.errors[:10]:
            print(f"  error {key}: {msg}", file=sys.stderr)
        return 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
