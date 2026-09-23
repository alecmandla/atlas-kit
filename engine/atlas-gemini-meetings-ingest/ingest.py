#!/usr/bin/env python3
"""atlas-gemini-meetings-ingest.

Turn Google Meet's "Notes by Gemini" documents into one markdown file per
meeting under <vault_root>/raw/gemini/meetings/.

Gemini writes its notes into a Google Doc in the organizer's Drive, named
"<Meeting title> - YYYY/MM/DD HH:MM TZ - Notes by Gemini". There is no local
database and no local file, so this ingest follows the Gmail/Slack pattern:
a Claude session (or any Drive client) fetches each doc as markdown and hands
it to this script as JSON, or a folder of markdown exports is pointed at with
--input-dir. The script itself never touches the network (DEC-021: stdlib only).

Two doc shapes exist and both are handled:
  - single tab:  date, "## Title", Invited ..., ### Summary / Decisions /
                 Next steps / Details
  - multi tab:   "# Quick notes", "# Full notes", "# Transcript" as H1 tabs,
                 each carrying the same header block

Transcript policy follows DEC-032: the summary record lives in the vault and
the transcript stays in the Google Doc, re-fetched on demand. The transcript
tab is parsed only to count segments and compute a duration; it is written to
disk only with --include-transcript.

Idempotent. A Gemini doc is MUTABLE after the meeting (Gemini itself edits the
Decisions section on feedback, attendees edit the doc), so a file is rewritten
when Drive's modifiedTime is newer than the source_modified_at recorded in the
existing file, exactly like atlas-wispr-meetings-ingest. See SKILL.md.
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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
SKILL_DIR = Path(__file__).parent
STATE_FILE = SKILL_DIR / "state.json"
LAST_RUN = SKILL_DIR / "last-run.md"
RUN_LOCK = SKILL_DIR / ".run.lock"
RUN_LOCK_STALE_SECONDS = 3600

DOC_NAME_SUFFIX = "Notes by Gemini"

# A Gemini meeting and a Fireflies / Wispr meeting are the same event when their
# start times fall within this window. Gemini's timestamp is when someone
# clicked "Take notes", which LAGS the call start: in practice the lag against
# a bot recorder runs up to about 5 minutes and against a native notetaker up
# to about 9, while the nearest false positive on a dense calendar sits past
# 11 minutes. Ten minutes covers every genuine pair with a small margin. Titles
# are deliberately not compared (each tool titles the same meeting differently).
MATCH_WINDOW_MIN = 10

# Timezone abbreviations Gemini writes into the doc name. Anything not listed
# falls back to the configured IANA timezone (zoneinfo, stdlib).
TZ_ABBREV_OFFSETS: dict[str, tuple[int, int]] = {
    "UTC": (0, 0), "GMT": (0, 0), "Z": (0, 0),
    "EST": (-5, 0), "EDT": (-4, 0), "CST": (-6, 0), "CDT": (-5, 0),
    "MST": (-7, 0), "MDT": (-6, 0), "PST": (-8, 0), "PDT": (-7, 0),
    "AKST": (-9, 0), "AKDT": (-8, 0), "HST": (-10, 0),
    "AST": (-4, 0), "ADT": (-3, 0), "NST": (-3, -30), "NDT": (-2, -30),
    "BST": (1, 0), "IST": (5, 30), "CET": (1, 0), "CEST": (2, 0),
    "EET": (2, 0), "EEST": (3, 0), "WET": (0, 0), "WEST": (1, 0),
    "AEST": (10, 0), "AEDT": (11, 0), "ACST": (9, 30), "ACDT": (10, 30),
    "AWST": (8, 0), "NZST": (12, 0), "NZDT": (13, 0), "JST": (9, 0),
    "KST": (9, 0), "SGT": (8, 0), "HKT": (8, 0), "SAST": (2, 0),
}

DOC_NAME_RE = re.compile(
    r"^(?P<title>.*?)\s*-\s*(?P<date>\d{4}/\d{2}/\d{2})\s+(?P<time>\d{1,2}:\d{2})"
    r"\s*(?P<tz>[A-Za-z]{1,5})?\s*-\s*" + re.escape(DOC_NAME_SUFFIX) + r"\s*$"
)
BODY_DATE_RE = re.compile(r"^([A-Z][a-z]{2}) (\d{1,2}), (\d{4})$")
MAILTO_RE = re.compile(r"\[([^\]]*)\]\(mailto:([^)]+)\)")
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
TRANSCRIPT_LINE_RE = re.compile(r"^\*\*(?P<speaker>[^*]+?):\s*\*\*\s*(?P<text>.*)$")
TRANSCRIPT_TS_RE = re.compile(r"^###\s+(\d{2}:\d{2}:\d{2})\s*$")
TRANSCRIPT_END_RE = re.compile(r"^###\s+Transcription ended after\s+(\d{2}):(\d{2}):(\d{2})")
CHECKBOX_RE = re.compile(r"^-\s+\[[ xX]\]\s+")

# Boilerplate Gemini appends to every tab. Any line starting with one of these
# is dropped before rendering.
FOOTER_PREFIXES = (
    "*You should review Gemini's notes",
    "*How is the quality of",
    "We've **updated the",
    "Let us know what you think",
    "**Want to see more?",
    "*Please ****rate",
    "*This editable transcript was computer generated",
)


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    skipped_not_gemini: int = 0
    skipped_empty: int = 0
    with_transcript: int = 0
    fireflies_linked: int = 0
    wispr_linked: int = 0
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


# --------------------------------------------------------------------------
# formatting helpers
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


def normalize_rfc3339(raw: Optional[str]) -> str:
    """Drive's 2026-07-10T15:34:33.583Z -> 2026-07-10 15:34:33 (UTC). Empty on failure."""
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", s)
    if not m:
        return ""
    date_part, time_part, tz = m.group(1), m.group(2), m.group(3)
    naive = dt.datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
        naive = naive - sign * offset
    return naive.strftime("%Y-%m-%d %H:%M:%S")


def parse_utc(raw: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def tz_offset_for(abbrev: Optional[str], local: dt.datetime) -> Optional[dt.timedelta]:
    """Offset (local - UTC) for an abbreviation, else the configured timezone."""
    if abbrev:
        key = abbrev.upper()
        if key in TZ_ABBREV_OFFSETS:
            h, m = TZ_ABBREV_OFFSETS[key]
            return dt.timedelta(hours=h, minutes=m)
    try:
        from zoneinfo import ZoneInfo  # stdlib, 3.9+
        aware = local.replace(tzinfo=ZoneInfo(CFG.timezone))
        return aware.utcoffset()
    except Exception:
        return None


def duration_human(seconds: int) -> str:
    mins = seconds // 60
    if mins <= 0:
        return ""
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


# --------------------------------------------------------------------------
# doc-name parsing
# --------------------------------------------------------------------------

@dataclass
class DocName:
    title: str
    date: str = ""            # YYYY-MM-DD, local
    local_ts: str = ""        # YYYY-MM-DD HH:MM, local
    tz: str = ""
    utc_ts: str = ""          # YYYY-MM-DD HH:MM:SS, UTC
    is_gemini: bool = False


def parse_doc_name(name: str) -> DocName:
    name = (name or "").strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    is_gemini = DOC_NAME_SUFFIX.lower() in name.lower()
    m = DOC_NAME_RE.match(name)
    if not m:
        title = re.sub(r"\s*-\s*" + re.escape(DOC_NAME_SUFFIX) + r"\s*$", "", name, flags=re.I).strip()
        return DocName(title=title or "(untitled meeting)", is_gemini=is_gemini)
    title = m.group("title").strip() or "(untitled meeting)"
    date = m.group("date").replace("/", "-")
    hh, mm = m.group("time").split(":")
    local = dt.datetime.strptime(f"{date} {int(hh):02d}:{mm}", "%Y-%m-%d %H:%M")
    tz = (m.group("tz") or "").upper()
    offset = tz_offset_for(tz or None, local)
    utc_ts = (local - offset).strftime("%Y-%m-%d %H:%M:%S") if offset is not None else ""
    return DocName(
        title=title, date=date, local_ts=local.strftime("%Y-%m-%d %H:%M"),
        tz=tz, utc_ts=utc_ts, is_gemini=is_gemini,
    )


# --------------------------------------------------------------------------
# doc-body parsing
# --------------------------------------------------------------------------

@dataclass
class ParsedDoc:
    title: str = ""
    body_date: str = ""                       # YYYY-MM-DD from the "Jul 10, 2026" line
    participants: list[str] = field(default_factory=list)
    attendee_emails: list[str] = field(default_factory=list)
    attachments: list[tuple[str, str]] = field(default_factory=list)   # (label, url)
    transcript_url: str = ""
    sections: dict[str, str] = field(default_factory=dict)             # Summary/Decisions/Next steps/Details
    quick_topics: list[tuple[str, str]] = field(default_factory=list)  # (topic, markdown)
    quick_blurb: str = ""
    transcript_segments: list[tuple[str, str, str]] = field(default_factory=list)  # (ts, speaker, text)
    transcript_duration_s: int = 0
    next_steps_count: int = 0


def clean_text(text: str) -> str:
    """Gemini uses vertical tabs (\\x0b) as soft line breaks inside paragraphs."""
    return text.replace("\x0b", "\n").replace("\r\n", "\n").replace("\r", "\n")


def is_footer(line: str) -> bool:
    s = line.strip()
    return any(s.startswith(p) for p in FOOTER_PREFIXES)


def split_tabs(markdown: str) -> list[tuple[str, list[str]]]:
    """Split on H1 tab headings. A doc without H1s is one 'Full notes' tab."""
    tabs: list[tuple[str, list[str]]] = []
    current_name = "Full notes"
    current: list[str] = []
    saw_h1 = False
    for line in clean_text(markdown).split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            if saw_h1 or any(l.strip() for l in current):
                tabs.append((current_name, current))
            current_name = line[2:].strip()
            current = []
            saw_h1 = True
            continue
        current.append(line)
    tabs.append((current_name, current))
    return tabs


def classify_tab(name: str) -> str:
    n = name.lower()
    if "transcript" in n:
        return "transcript"
    if "quick" in n:
        return "quick"
    return "full"


def month_line_to_iso(line: str) -> str:
    m = BODY_DATE_RE.match(line.strip())
    if not m:
        return ""
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_header(lines: list[str], doc: ParsedDoc) -> int:
    """Consume the header block (date, title, Invited, Attachments, Meeting
    records) and return the index of the first content line after it."""
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("### ") or (s.startswith("## ") and doc.title and i > 0 and not s.startswith("## " + doc.title)):
            # first section heading (### Summary) or first quick-notes topic
            break
        if not s or is_footer(s):
            i += 1
            continue
        iso = month_line_to_iso(s)
        if iso:
            doc.body_date = doc.body_date or iso
        elif s.startswith("## "):
            doc.title = doc.title or s[3:].strip()
        elif s.startswith("Invited ") or (MAILTO_RE.search(s) and not s.startswith("-")):
            for name, email in MAILTO_RE.findall(s):
                email = email.strip().lower()
                name = name.strip()
                if email and email not in doc.attendee_emails:
                    doc.attendee_emails.append(email)
                if name and name not in doc.participants:
                    doc.participants.append(name)
        elif s.startswith("Attachments"):
            for label, url in LINK_RE.findall(s):
                doc.attachments.append((label.strip(), url.strip()))
        elif s.startswith("Meeting records"):
            for label, url in LINK_RE.findall(s):
                if "transcript" in label.lower():
                    doc.transcript_url = url.strip()
                else:
                    doc.attachments.append((label.strip(), url.strip()))
        else:
            # Quick-notes tabs carry a one-line blurb under the participants.
            if not doc.quick_blurb and not s.startswith("#"):
                doc.quick_blurb = s
        i += 1
    return i


def parse_full_tab(lines: list[str], doc: ParsedDoc) -> None:
    start = parse_header(lines, doc)
    section: Optional[str] = None
    buf: list[str] = []

    def flush() -> None:
        if section is None:
            return
        text = "\n".join(buf).strip("\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            existing = doc.sections.get(section, "")
            doc.sections[section] = (existing + "\n\n" + text).strip() if existing else text

    for line in lines[start:]:
        s = line.rstrip()
        if is_footer(s):
            continue
        if s.startswith("### "):
            flush()
            section = s[4:].strip()
            buf = []
            continue
        if s.startswith("## ") and section is not None:
            # Gemini nests "Aligned" / "Shelved" under Decisions as H2; demote.
            buf.append("### " + s[3:].strip())
            continue
        if section is not None:
            buf.append(s)
    flush()

    steps = doc.sections.get("Next steps") or doc.sections.get("Suggested next steps") or ""
    doc.next_steps_count = sum(1 for l in steps.split("\n") if CHECKBOX_RE.match(l.strip()) or l.strip().startswith("- ["))


def parse_quick_tab(lines: list[str], doc: ParsedDoc) -> None:
    start = parse_header(lines, doc)
    topic: Optional[str] = None
    buf: list[str] = []

    def flush() -> None:
        if topic is None:
            return
        text = "\n".join(buf).strip()
        if text:
            doc.quick_topics.append((topic, text))

    for line in lines[start:]:
        s = line.rstrip()
        if is_footer(s):
            continue
        if s.startswith("## ") or s.startswith("### "):
            flush()
            topic = s.lstrip("#").strip()
            buf = []
            continue
        if topic is not None:
            buf.append(s)
    flush()


def parse_transcript_tab(lines: list[str], doc: ParsedDoc) -> None:
    ts = "00:00:00"
    for line in lines:
        s = line.rstrip()
        if not s or is_footer(s):
            continue
        m_end = TRANSCRIPT_END_RE.match(s)
        if m_end:
            h, m, sec = (int(x) for x in m_end.groups())
            doc.transcript_duration_s = h * 3600 + m * 60 + sec
            continue
        m_ts = TRANSCRIPT_TS_RE.match(s)
        if m_ts:
            ts = m_ts.group(1)
            continue
        m_line = TRANSCRIPT_LINE_RE.match(s)
        if m_line:
            text = m_line.group("text").strip()
            if text:
                doc.transcript_segments.append((ts, m_line.group("speaker").strip(), text))


def parse_doc(markdown: str) -> ParsedDoc:
    doc = ParsedDoc()
    tabs = split_tabs(markdown)
    # Parse full notes first so the title/participants come from the richest tab.
    for kind in ("full", "quick", "transcript"):
        for name, lines in tabs:
            if classify_tab(name) != kind:
                continue
            if kind == "full":
                parse_full_tab(lines, doc)
            elif kind == "quick":
                parse_quick_tab(lines, doc)
            else:
                parse_transcript_tab(lines, doc)
    doc.title = re.sub(r"\s*-\s*Transcript\s*$", "", doc.title).strip()
    return doc


def render_transcript(segments: list[tuple[str, str, str]]) -> str:
    """Collapse consecutive same-speaker lines into paragraphs, Wispr-style."""
    out: list[str] = []
    cur_speaker: Optional[str] = None
    cur_ts = ""
    buf: list[str] = []

    def flush() -> None:
        if buf and cur_speaker:
            text = re.sub(r"\s+", " ", " ".join(buf)).strip()
            if text:
                out.append(f"**{cur_speaker}** ({cur_ts}) {text}")
                out.append("")

    for ts, speaker, text in segments:
        if speaker != cur_speaker:
            flush()
            buf = []
            cur_speaker = speaker
            cur_ts = ts
        buf.append(text)
    flush()
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# cross-linking (Fireflies + Wispr meetings)
# --------------------------------------------------------------------------

def _frontmatter_fields(path: Path, wanted: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                for key in wanted:
                    if line.startswith(key + ":"):
                        found[key] = line.split(":", 1)[1].strip().strip('"')
                if len(found) == len(wanted):
                    break
    except OSError:
        pass
    return found


def load_fireflies_index(raw_root: Path) -> list[tuple[dt.datetime, str, str]]:
    """raw/fireflies/*.md -> [(start_utc, meeting_id, note_link)]."""
    index: list[tuple[dt.datetime, str, str]] = []
    folder = raw_root / "fireflies"
    if not folder.is_dir():
        return index
    for path in folder.glob("*.md"):
        fm = _frontmatter_fields(path, ("date", "meeting_id", "meeting_note"))
        raw_date, meeting_id = fm.get("date", ""), fm.get("meeting_id", "")
        if not (raw_date and meeting_id):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", raw_date)
        if not m:
            continue
        index.append((dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"),
                      meeting_id, fm.get("meeting_note", "")))
    return index


def load_wispr_index(raw_root: Path) -> list[tuple[dt.datetime, str, str]]:
    """raw/wispr/meetings/*.md -> [(start_utc, wispr_meeting_id, [[note]])]."""
    index: list[tuple[dt.datetime, str, str]] = []
    folder = raw_root / "wispr" / "meetings"
    if not folder.is_dir():
        return index
    for path in folder.glob("*.md"):
        fm = _frontmatter_fields(path, ("timestamp", "wispr_meeting_id"))
        started = parse_utc(fm.get("timestamp", ""))
        if started and fm.get("wispr_meeting_id"):
            index.append((started, fm["wispr_meeting_id"], f"[[{path.stem}]]"))
    return index


def match_nearest(
    start: Optional[dt.datetime], index: list[tuple[dt.datetime, str, str]]
) -> Optional[tuple[str, str]]:
    if not start or not index:
        return None
    best: Optional[tuple[float, str, str]] = None
    for other_start, other_id, note in index:
        delta = abs((other_start - start).total_seconds()) / 60.0
        if delta <= MATCH_WINDOW_MIN and (best is None or delta < best[0]):
            best = (delta, other_id, note)
    return (best[1], best[2]) if best else None


# --------------------------------------------------------------------------
# input loading
# --------------------------------------------------------------------------

@dataclass
class SourceDoc:
    doc_id: str
    name: str
    markdown: str
    modified_time: str = ""   # RFC3339 from Drive, or file mtime
    created_time: str = ""
    link: str = ""


def _first(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _coerce_markdown(value) -> str:
    """Accept a markdown string, or the raw MCP envelope {"result": "..."}."""
    if isinstance(value, dict):
        return str(value.get("result") or value.get("markdown") or value.get("content") or "")
    if value is None:
        return ""
    s = str(value)
    if s.lstrip().startswith("{"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and "result" in obj:
                return str(obj["result"])
        except json.JSONDecodeError:
            pass
    return s


def docs_from_json(path: Path, stats: Stats) -> list[SourceDoc]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        stats.errors.append((str(path), f"json parse failed: {exc}"))
        return []
    if isinstance(data, dict):
        entries = data.get("docs") or data.get("files") or data.get("items")
        if entries is None:
            entries = [data]
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    out: list[SourceDoc] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        doc_id = _first(entry, "id", "file_id", "document_id", "doc_id")
        link = _first(entry, "link", "web_link", "webViewLink", "url")
        if not doc_id and link:
            m = re.search(r"/d/([A-Za-z0-9_-]+)", link)
            doc_id = m.group(1) if m else ""
        name = _first(entry, "name", "title", "file_name")
        markdown = _coerce_markdown(entry.get("markdown") if "markdown" in entry else entry.get("content", entry.get("result")))
        if not doc_id:
            stats.errors.append((name or str(path), "entry without id/link"))
            continue
        if not link:
            link = f"https://docs.google.com/document/d/{doc_id}/edit"
        out.append(SourceDoc(
            doc_id=doc_id, name=name, markdown=markdown,
            modified_time=_first(entry, "modified_time", "modifiedTime", "modified"),
            created_time=_first(entry, "created_time", "createdTime", "created"),
            link=link,
        ))
    return out


def docs_from_dir(folder: Path, stats: Stats) -> list[SourceDoc]:
    """Markdown exports (Google Docs > File > Download > Markdown), one per doc.
    The filename is the doc name; the id is derived from it so re-exports of
    the same doc land on the same vault file."""
    out: list[SourceDoc] = []
    if not folder.is_dir():
        stats.errors.append((str(folder), "input dir not found"))
        return out
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            mtime = dt.datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OSError as exc:
            stats.errors.append((str(path), str(exc)[:200]))
            continue
        doc_id = "md-" + hashlib.sha1(path.stem.encode("utf-8")).hexdigest()[:16]
        out.append(SourceDoc(doc_id=doc_id, name=path.stem, markdown=text, modified_time=mtime))
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def build_markdown(
    src: SourceDoc, dn: DocName, doc: ParsedDoc, modified_iso: str,
    ff_match: Optional[tuple[str, str]], wispr_match: Optional[tuple[str, str]],
    include_transcript: bool, ingested_at: str,
) -> str:
    title = doc.title or dn.title
    date_part = dn.date or doc.body_date or normalize_rfc3339(src.created_time)[:10] or "undated"
    duration = duration_human(doc.transcript_duration_s)
    transcript_available = bool(doc.transcript_segments) or bool(doc.transcript_url)
    next_steps_text = doc.sections.get("Next steps") or doc.sections.get("Suggested next steps") or ""

    fm = [
        "---",
        "type: raw-gemini-meeting",
        f"gemini_doc_id: {src.doc_id}",
        f"title: {yaml_str(title)}",
        f"date: {date_part}",
        f"timestamp: {dn.utc_ts}",
        f"timestamp_local: {yaml_str(dn.local_ts + (' ' + dn.tz if dn.tz else ''))}",
        f"source_modified_at: {modified_iso}",
        f"doc_url: {src.link}",
        f"duration: {yaml_str(duration)}",
        f"attendee_emails: {yaml_list(sorted(doc.attendee_emails))}",
        f"participants: {yaml_list(doc.participants)}",
        f"has_quick_notes: {'true' if doc.quick_topics else 'false'}",
        f"has_decisions: {'true' if doc.sections.get('Decisions') else 'false'}",
        f"next_steps: {doc.next_steps_count}",
        f"transcript_available: {'true' if transcript_available else 'false'}",
        f"transcript_included: {'true' if include_transcript and doc.transcript_segments else 'false'}",
        f"transcript_segments: {len(doc.transcript_segments)}",
    ]
    if ff_match:
        fm.append(f"fireflies_id: {ff_match[0]}")
        if ff_match[1]:
            fm.append(f"fireflies_note: {yaml_str(ff_match[1])}")
    if wispr_match:
        fm.append(f"wispr_meeting_id: {wispr_match[0]}")
        fm.append(f"wispr_note: {yaml_str(wispr_match[1])}")
    fm.append(f"ingested_at: {ingested_at}")
    fm.append("---")

    body: list[str] = ["", f"# {date_part} — {title}", ""]
    meta_bits = []
    if duration:
        meta_bits.append(f"**Duration:** {duration}")
    if doc.participants:
        meta_bits.append(f"**Participants:** {', '.join(doc.participants)}")
    if doc.attendee_emails:
        meta_bits.append(f"**Attendees:** {', '.join(sorted(doc.attendee_emails))}")
    meta_bits.append(f"**Source:** [Notes by Gemini]({src.link})")
    body.append("  \n".join(meta_bits))
    body.append("")

    if ff_match and ff_match[1]:
        body.append(f"> Also captured by Fireflies: {ff_match[1]}")
        body.append("")
    if wispr_match:
        body.append(f"> Also captured by Wispr: {wispr_match[1]}")
        body.append("")

    for heading in ("Summary", "Decisions"):
        text = doc.sections.get(heading)
        if text:
            body += [f"## {heading}", "", text, ""]

    if next_steps_text:
        body += ["## Next steps", "", next_steps_text, ""]

    details = doc.sections.get("Details")
    if details:
        body += ["## Details", "", details, ""]

    # Anything Gemini adds that this script does not know by name is kept, not dropped.
    for heading, text in doc.sections.items():
        if heading in ("Summary", "Decisions", "Next steps", "Suggested next steps", "Details"):
            continue
        if text:
            body += [f"## {heading}", "", text, ""]

    if doc.quick_topics:
        body += ["## Quick notes", ""]
        if doc.quick_blurb:
            body += [f"_{doc.quick_blurb}_", ""]
        for topic, text in doc.quick_topics:
            if topic.lower() in ("next steps", "suggested next steps") and next_steps_text:
                continue
            body += [f"### {topic}", "", text, ""]

    if doc.attachments:
        body += ["## Attachments", ""]
        for label, url in doc.attachments:
            body.append(f"- [{label}]({url})")
        body.append("")

    body += ["## Transcript", ""]
    if include_transcript and doc.transcript_segments:
        body.append(render_transcript(doc.transcript_segments))
    elif transcript_available:
        where = doc.transcript_url or src.link
        body.append(
            f"The transcript stays in the Google Doc (DEC-032): [open transcript]({where}). "
            f"AI tools: fetch it on demand with `get_doc_as_markdown(document_id=\"{src.doc_id}\")` "
            "and read the `# Transcript` tab."
        )
    else:
        body.append("_(Gemini did not produce a transcript for this meeting)_")
    body.append("")
    return "\n".join(fm + body) + "\n"


def existing_source_modified(path: Path) -> Optional[str]:
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
    docs: list[SourceDoc], out_dir: Path, raw_root: Path, since_iso: Optional[str],
    execute: bool, include_transcript: bool, stats: Stats, ingested_at: str, preview: list[dict],
) -> None:
    ff_index = load_fireflies_index(raw_root)
    wispr_index = load_wispr_index(raw_root)

    for src in docs:
        stats.seen += 1
        dn = parse_doc_name(src.name)
        if not dn.is_gemini:
            stats.skipped_not_gemini += 1
            continue
        modified_iso = normalize_rfc3339(src.modified_time)
        if since_iso and modified_iso and modified_iso <= since_iso:
            stats.already_present += 1
            continue
        if modified_iso > stats.max_ts:
            stats.max_ts = modified_iso

        try:
            doc = parse_doc(src.markdown)
        except Exception as exc:  # a malformed doc must not abort the run
            stats.errors.append((src.doc_id, f"parse failed: {exc}"[:200]))
            continue
        has_content = any(doc.sections.values()) or doc.quick_topics or doc.transcript_segments
        if not has_content:
            stats.skipped_empty += 1
            continue

        start_dt = parse_utc(dn.utc_ts)
        ff_match = match_nearest(start_dt, ff_index)
        wispr_match = match_nearest(start_dt, wispr_index)

        title = doc.title or dn.title
        date_part = dn.date or doc.body_date or normalize_rfc3339(src.created_time)[:10] or "undated"
        filename = f"{date_part}-{slugify(title, src.doc_id[:8])}-{src.doc_id[:8]}.md"
        out = out_dir / filename

        prior = existing_source_modified(out) if out.exists() else None
        is_update = prior is not None and prior != modified_iso
        if prior is not None and not is_update:
            stats.already_present += 1
            continue

        if doc.transcript_segments or doc.transcript_url:
            stats.with_transcript += 1
        if ff_match:
            stats.fireflies_linked += 1
        if wispr_match:
            stats.wispr_linked += 1

        preview.append({
            "id": src.doc_id, "file": filename, "date": date_part, "title": title,
            "next_steps": doc.next_steps_count, "segments": len(doc.transcript_segments),
            "fireflies": ff_match[0] if ff_match else "", "wispr": wispr_match[0][:8] if wispr_match else "",
            "action": "update" if is_update else "create",
        })
        if not execute:
            continue
        try:
            markdown = build_markdown(src, dn, doc, modified_iso, ff_match, wispr_match, include_transcript, ingested_at)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text(markdown, encoding="utf-8")
            os.replace(tmp, out)
            if is_update:
                stats.updated += 1
            else:
                stats.written += 1
        except OSError as exc:
            stats.errors.append((src.doc_id, str(exc)[:200]))


def drive_query(cursor: Optional[str]) -> str:
    """The Drive search query a session should run to list new/changed notes."""
    q = f"name contains '{DOC_NAME_SUFFIX}' and mimeType = 'application/vnd.google-apps.document' and trashed = false"
    if cursor:
        q += f" and modifiedTime > '{cursor.replace(' ', 'T')}Z'"
    return q


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    lines = [
        "# atlas-gemini-meetings-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **state.last_run_iso:** {state.get('last_run_iso')}",
        f"- **state.last_run_count:** {state.get('last_run_count')}",
        f"- **state.cursor_ts:** {state.get('cursor_ts')}",
        f"- **Docs seen:** {stats.seen}",
        f"- **Written (new):** {stats.written}",
        f"- **Updated (source changed):** {stats.updated}",
        f"- **Already present, unchanged:** {stats.already_present}",
        f"- **Skipped (not a Gemini notes doc):** {stats.skipped_not_gemini}",
        f"- **Skipped (empty):** {stats.skipped_empty}",
        f"- **With transcript available:** {stats.with_transcript}",
        f"- **Cross-linked to Fireflies:** {stats.fireflies_linked}",
        f"- **Cross-linked to Wispr:** {stats.wispr_linked}",
        f"- **Errors:** {len(stats.errors)}",
        "",
    ]
    if stats.errors:
        lines += ["## Errors", ""]
        for key, msg in stats.errors[:20]:
            lines.append(f"- `{key}` — {msg}")
        lines.append("")
    LAST_RUN.write_text("\n".join(lines), encoding="utf-8")


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict]) -> None:
    lines = [
        "# atlas-gemini-meetings-ingest — dry run",
        "",
        f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- Docs seen: {stats.seen}",
        f"- Would create: {sum(1 for r in rows if r['action'] == 'create')}",
        f"- Would update: {sum(1 for r in rows if r['action'] == 'update')}",
        f"- Already present, unchanged: {stats.already_present}",
        f"- Skipped (not Gemini / empty): {stats.skipped_not_gemini} / {stats.skipped_empty}",
        f"- Cross-linked to Fireflies / Wispr: {stats.fireflies_linked} / {stats.wispr_linked}",
        "",
        "| Action | Date | Title | Next steps | Segs | Fireflies | Wispr |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| {r['action']} | {r['date']} | {r['title'][:44]} | {r['next_steps']} | "
            f"{r['segments']} | {r['fireflies'][:12]} | {r['wispr']} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Google Meet 'Notes by Gemini' docs into the vault.")
    ap.add_argument("--input-json", type=Path, action="append", default=[],
                    help="JSON with one doc or a list of docs: {id, name, modified_time, link, markdown}. Repeatable.")
    ap.add_argument("--input-dir", type=Path, help="folder of markdown exports, one .md per Gemini notes doc")
    ap.add_argument("--execute", action="store_true", help="write to the vault (default: dry run)")
    ap.add_argument("--incremental", action="store_true", help="skip docs not modified since state.json cursor_ts")
    ap.add_argument("--since", help="override the cursor, e.g. 2026-08-01")
    ap.add_argument("--include-transcript", action="store_true",
                    help="write the transcript tab into the vault file (default: pointer only, per DEC-032)")
    ap.add_argument("--dry-run-report", type=Path, help="write a dry-run report to this path")
    ap.add_argument("--print-query", action="store_true",
                    help="print the Drive search query for new/changed docs (honors the cursor) and exit")
    ap.add_argument("--vault", help="override the configured vault root for this run")
    args = ap.parse_args(argv)

    state = load_state()
    if args.print_query:
        cursor = args.since or (state.get("cursor_ts") if args.incremental else None)
        print(drive_query(cursor))
        return 0

    if not args.input_json and not args.input_dir:
        print("error: --input-json or --input-dir required (or --print-query)", file=sys.stderr)
        return 1

    vault = atlas_config.vault_from_arg(args.vault)
    raw_root = vault / CFG.folder_name("raw")
    out_dir = raw_root / "gemini" / "meetings"

    if not acquire_run_lock():
        print("error: another atlas-gemini-meetings-ingest run holds the lock", file=sys.stderr)
        return 1

    try:
        since_iso: Optional[str] = None
        if args.since:
            since_iso = normalize_rfc3339(args.since + ("T00:00:00Z" if len(args.since) == 10 else "")) or None
        elif args.incremental:
            since_iso = state.get("cursor_ts")

        stats = Stats()
        preview: list[dict] = []
        docs: list[SourceDoc] = []
        for path in args.input_json:
            docs.extend(docs_from_json(path, stats))
        if args.input_dir:
            docs.extend(docs_from_dir(args.input_dir, stats))

        ingested_at = dt.datetime.now().isoformat(timespec="seconds")
        run_started = dt.datetime.utcnow().isoformat() + "Z"

        run_ingest(docs, out_dir, raw_root, since_iso, args.execute, args.include_transcript,
                   stats, ingested_at, preview)

        if args.dry_run_report:
            write_dry_run_report(args.dry_run_report, stats, preview)

        mode = "execute" if args.execute else "dry-run"
        if args.execute:
            state["last_run_iso"] = run_started
            state["last_run_count"] = state.get("last_run_count", 0) + stats.written
            state["schema_version"] = 1
            if stats.max_ts and (not state.get("cursor_ts") or stats.max_ts > state["cursor_ts"]):
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
            f"unchanged={stats.already_present} not_gemini={stats.skipped_not_gemini} "
            f"empty={stats.skipped_empty} ff_linked={stats.fireflies_linked} "
            f"wispr_linked={stats.wispr_linked} errors={len(stats.errors)}"
        )
        for key, msg in stats.errors[:10]:
            print(f"  error {key}: {msg}", file=sys.stderr)
        return 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
