#!/usr/bin/env python3
"""atlas-gong-meetings-ingest.

Turn Gong calls into one markdown file per call under
<vault_root>/<raw>/gong/meetings/<YYYY-MM-DD>-<title-slug>-<id8>.md.

Three inputs, combinable in one run:
  --fetch          Gong's REST API through fetch.py (the only network path).
                   Needs a Gong API key, which a Gong technical admin creates.
  --input-json P   JSON someone else fetched: an /v2/calls/extensive response,
                   a /v2/calls/transcript response, one call, a list of calls,
                   or {"call": {...}, "transcript": {...}} per call; an MCP
                   envelope {"result": "<json string>"} is unwrapped.
  transcript files Hand-downloaded transcripts (any Gong user can download one
                   call's transcript from its call page: More actions ->
                   Download transcript) from --input-dir / --input, or from
                   export_dirs in gong-sources.json.

PRIVACY INVARIANT. A Gong API key sees every call in the company. A call from
--fetch or --input-json is kept only when one of its parties has an email in
owner_emails (gong-sources.json), and that filter runs before anything is
written anywhere. Filtered calls are only counted (not_owner), never logged by
title. With no owner_emails, --fetch and --input-json refuse to run.
--no-owner-filter exists only for --input-json payloads the owner already
filtered, and is rejected with --fetch.

Transcript policy (DEC-032). When Gong produced AI content (brief, key points,
outline, or highlights) the record keeps that content and a pointer to the
transcript: the call page for people, `python3 fetch.py transcript <callId>`
for AI tools. --include-transcript writes it anyway. A call with no AI content
but a transcript has nothing else worth keeping, so its transcript is written
in full. A hand-downloaded file is written in full: there is no key to
re-fetch it with.

Idempotent. Each file records source_hash over its rendered content. Gong fills
its AI content in over the first days after a call, so a changed hash rewrites
the file in place; an unchanged one is skipped. Stdlib only (DEC-021).
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
SOURCES_FILE = SKILL_DIR / "gong-sources.json"
STATE_FILE = SKILL_DIR / "state.json"
LAST_RUN = SKILL_DIR / "last-run.md"
RUN_LOCK = SKILL_DIR / ".run.lock"
RUN_LOCK_STALE_SECONDS = 3600

TRANSCRIPT_EXTS = (".txt",)
NOTES_EXT = ".md"
DEFAULT_LOOKBACK_DAYS = 3        # Gong's AI content can land days after the call
NO_CURSOR_DAYS = 30
SLUG_MAX = 60

# A Gong call and a Fireflies / Wispr / Gemini / Teams / Zoom record are the
# same event when their starts fall within this window. Gong's `started` is the
# real recording start; Gemini's and Teams' starts lag the call by up to ~9
# minutes (someone clicks "Take notes" or "Start transcription"), so ten
# minutes covers every genuine pair. Titles are never compared.
MATCH_WINDOW_MIN = 10

NEXT_STEP_RE = re.compile(r"next step|action item", re.I)
UNKNOWN_SPEAKER = "Unknown speaker"

# --- hand-downloaded transcript shapes (see parse_transcript_text) ---
TS = r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)"
NAME = r"(?P<name>[^\t:|\[\]()]{1,80}?)"
TURN_RES = [
    re.compile(rf"^{NAME}(?:\t| {{2,}}){TS}\s*$"),                          # Name  0:05 / Name<TAB>0:05
    re.compile(rf"^{TS}\s*\|\s*{NAME}\s*$"),                                # 0:05 | Name
    re.compile(rf"^\[{TS}\]\s*{NAME}(?::\s*(?P<text>.*))?$"),               # [0:05] Name: text
    re.compile(rf"^{NAME}\s*\({TS}\)\s*:?\s*(?P<text>.*)$"),                # Name (0:05): text
    re.compile(rf"^{TS}\s+{NAME}:\s*(?P<text>.*)$"),                        # 0:05 Name: text
]
COLON_TURN_RE = re.compile(r"^(?P<name>[^:]{1,60}?):\s+(?P<text>\S.*)$")
HEADER_KEYS = {"date", "title", "duration", "participants", "attendees", "call", "meeting", "time", "recorded", "company", "account"}
MONTH_DATE_RE = re.compile(
    r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4})"
    r"(?:,?\s+(?:at\s+)?(?P<time>\d{1,2}:\d{2})\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?)?"
)
ISO_DATE_RE = re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[ T](?P<h>\d{2}):(?P<mi>\d{2}))?")
NAME_ISO_RE = re.compile(r"[-_ ]?(\d{4})-(\d{2})-(\d{2})(?:[ T_]+(\d{2})[:.h-]?(\d{2}))?")
NAME_NOISE_RE = re.compile(r"[-_ ]*(gong transcript|call transcript|transcript)\s*$", re.I)


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    updated: int = 0
    already_present: int = 0
    not_owner: int = 0
    private_skipped: int = 0
    not_modified: int = 0
    empty: int = 0
    orphan_transcript: int = 0
    undated: int = 0
    linked: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    max_started: str = ""
    max_mtime: str = ""
    fetch_failed: bool = False


@dataclass
class Segment:
    offset_s: int
    speaker: str
    text: str


@dataclass
class Record:
    gong_id: str
    input: str                                  # api | file
    title: str = ""
    local_start: Optional[dt.datetime] = None   # naive, CFG.timezone
    utc_start: Optional[dt.datetime] = None     # naive UTC; None when the time is unknown
    date_source: str = ""                       # api | filename | header | file-mtime
    duration_s: int = 0
    url: str = ""
    direction: str = ""
    scope: str = ""
    system: str = ""
    is_private: bool = False
    attendee_emails: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    internal: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    external_domains: list[str] = field(default_factory=list)
    brief: str = ""
    key_points: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    highlights: list[tuple[str, list[str]]] = field(default_factory=list)
    outline: list[tuple[str, int, list[str]]] = field(default_factory=list)
    outcome: str = ""
    topics: list[str] = field(default_factory=list)
    trackers: list[str] = field(default_factory=list)
    crm_accounts: list[str] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    transcript_available: bool = False
    notes: str = ""
    source_file: str = ""
    mtime_iso: str = ""

    @property
    def has_ai(self) -> bool:
        return bool(self.brief or self.key_points or self.outline or self.highlights or self.next_steps)


# --------------------------------------------------------------------------
# state / lock / config
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "schema_version": 1,
            "cursor_started": None, "cursor_mtime": None}


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
    """gong-sources.json next to this script. Keys starting with "_" are ignored.

    owner_emails (list, required for --fetch/--input-json), api_base,
    workspace_id, crm_context (bool), lookback_days (int), skip_private (bool),
    export_dirs (list of folders of hand-downloaded transcripts).
    """
    if not SOURCES_FILE.exists():
        return {}
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {SOURCES_FILE.name} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"error: {SOURCES_FILE.name} must hold a JSON object")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def owner_set(sources: dict) -> set[str]:
    raw = sources.get("owner_emails") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(e).strip().lower() for e in raw if str(e).strip()}


def export_dirs(sources: dict) -> list[Path]:
    return [Path(os.path.expanduser(str(d))) for d in (sources.get("export_dirs") or []) if str(d).strip()]


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
    s = s[:SLUG_MAX].strip("-")
    return s or fallback


def _zone():
    try:
        from zoneinfo import ZoneInfo  # stdlib, 3.9+
        return ZoneInfo(CFG.timezone)
    except Exception:
        return None


def to_utc(local: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if local is None:
        return None
    zone = _zone()
    if zone is None:
        return None
    return local.replace(tzinfo=zone).astimezone(dt.timezone.utc).replace(tzinfo=None)


def to_local(utc: dt.datetime) -> dt.datetime:
    zone = _zone()
    if zone is None:
        return utc
    return utc.replace(tzinfo=dt.timezone.utc).astimezone(zone).replace(tzinfo=None)


def tz_abbrev(local: dt.datetime) -> str:
    zone = _zone()
    if zone is None:
        return "UTC"
    return local.replace(tzinfo=zone).tzname() or CFG.timezone


def parse_iso_utc(raw: Any) -> Optional[dt.datetime]:
    """ISO-8601 with offset or Z -> naive UTC. Python 3.10's fromisoformat
    rejects 'Z' and odd fraction lengths, so both are normalized first."""
    s = str(raw or "").strip()
    if not s:
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def clock_to_seconds(raw: str) -> int:
    parts = [int(p) for p in raw.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3:]
    return h * 3600 + m * 60 + s


def fmt_offset(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def fmt_mmss(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def duration_human(seconds: int) -> str:
    mins = int(seconds) // 60
    if mins <= 0:
        return ""
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60:02d}m"


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _uniq(values) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


# --------------------------------------------------------------------------
# privacy filter (kept identical to fetch.keep_reason)
# --------------------------------------------------------------------------

def keep_reason(call: dict, owners: Optional[set[str]], skip_private: bool) -> Optional[str]:
    """None to keep; "not_owner" or "private" to drop. owners=None disables the
    owner check (only for --no-owner-filter with --input-json)."""
    if owners is not None:
        emails = {
            str(p.get("emailAddress") or p.get("email") or "").strip().lower()
            for p in (call.get("parties") or []) if isinstance(p, dict)
        }
        if not (emails & owners):
            return "not_owner"
    meta = call.get("metaData") or {}
    if skip_private and meta.get("isPrivate") is True:
        return "private"
    return None


# --------------------------------------------------------------------------
# JSON input
# --------------------------------------------------------------------------

def call_id(call: dict) -> str:
    meta = call.get("metaData") or {}
    return str(meta.get("id") or call.get("id") or call.get("callId") or "")


def _is_call(obj: dict) -> bool:
    return isinstance(obj.get("metaData"), dict) or ("parties" in obj and ("id" in obj or "callId" in obj))


def monologues_of(value: Any, cid: str = "") -> Optional[list]:
    """Accept a monologue list, a CallTranscript {callId, transcript}, or a
    transcript response {callTranscripts: [...]} (the entry for cid)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("callTranscripts"), list):
            for entry in value["callTranscripts"]:
                if isinstance(entry, dict) and (not cid or str(entry.get("callId")) == cid):
                    return entry.get("transcript") or []
            return None
        if isinstance(value.get("transcript"), list):
            return value["transcript"]
    return None


def unwrap_json(obj: Any, calls: list[dict], transcripts: dict[str, list], depth: int = 0) -> None:
    """Walk any supported payload shape, collecting call dicts and callId -> monologues."""
    if depth > 6:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in ("{", "["):
            try:
                unwrap_json(json.loads(s), calls, transcripts, depth + 1)
            except json.JSONDecodeError:
                pass
        return
    if isinstance(obj, list):
        for item in obj:
            unwrap_json(item, calls, transcripts, depth + 1)
        return
    if not isinstance(obj, dict):
        return
    if _is_call(obj):
        call = dict(obj)
        if "transcript" in call:
            mono = monologues_of(call["transcript"], call_id(call))
            if mono is not None:
                call["transcript"] = mono
            else:
                call.pop("transcript")
        calls.append(call)
        return
    if isinstance(obj.get("call"), dict):
        call = dict(obj["call"])
        if not isinstance(call.get("metaData"), dict) and call.get("id"):
            call = {"metaData": call, "parties": obj.get("parties") or call.get("parties") or []}
        if "transcript" in obj:
            mono = monologues_of(obj["transcript"], call_id(call))
            if mono is not None:
                call["transcript"] = mono
        calls.append(call)
        return
    if isinstance(obj.get("calls"), list):
        for item in obj["calls"]:
            unwrap_json(item, calls, transcripts, depth + 1)
    if isinstance(obj.get("callTranscripts"), list):
        for entry in obj["callTranscripts"]:
            if isinstance(entry, dict) and entry.get("callId") is not None:
                transcripts[str(entry["callId"])] = entry.get("transcript") or []
    if "callId" in obj and isinstance(obj.get("transcript"), list):
        transcripts[str(obj["callId"])] = obj["transcript"]
    if "result" in obj and not isinstance(obj.get("calls"), list):
        unwrap_json(obj["result"], calls, transcripts, depth + 1)
    content = obj.get("content")
    if isinstance(content, list):  # MCP tool-result envelope: [{"type": "text", "text": "..."}]
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                unwrap_json(part["text"], calls, transcripts, depth + 1)


def load_json_inputs(paths: list[Path], stats: Stats) -> tuple[list[dict], dict[str, list]]:
    calls: list[dict] = []
    transcripts: dict[str, list] = {}
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            stats.errors.append((Path(path).name, f"json parse failed: {type(exc).__name__}"))
            continue
        before = (len(calls), len(transcripts))
        unwrap_json(data, calls, transcripts)
        if (len(calls), len(transcripts)) == before:
            stats.errors.append((Path(path).name, "no Gong calls or transcripts recognized"))
    return calls, transcripts


def merge_calls(calls: list[dict], transcripts: dict[str, list], stats: Stats) -> list[dict]:
    """Deduplicate by call id (later input wins, a transcript is never lost) and
    attach separately supplied transcripts. Unmatched transcripts are orphans."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for call in calls:
        cid = call_id(call)
        if not cid:
            stats.errors.append(("json", "call without an id"))
            continue
        prior = by_id.get(cid)
        if prior is None:
            order.append(cid)
        elif "transcript" in prior and "transcript" not in call:
            call = dict(call, transcript=prior["transcript"])
        by_id[cid] = call
    for cid, mono in transcripts.items():
        if cid in by_id:
            by_id[cid]["transcript"] = mono
        else:
            stats.orphan_transcript += 1
    return [by_id[c] for c in order]


# --------------------------------------------------------------------------
# call -> Record
# --------------------------------------------------------------------------

def segments_from_monologues(monologues: list, names: dict[str, str]) -> list[Segment]:
    out: list[Segment] = []
    for mono in monologues or []:
        if not isinstance(mono, dict):
            continue
        sentences = [s for s in (mono.get("sentences") or []) if isinstance(s, dict)]
        text = re.sub(r"\s+", " ", " ".join(str(s.get("text") or "") for s in sentences)).strip()
        if not text:
            continue
        speaker = names.get(str(mono.get("speakerId")), UNKNOWN_SPEAKER)
        out.append(Segment(_int(sentences[0].get("start")) // 1000, speaker, text))  # ms -> s
    return out


def crm_accounts_of(call: dict) -> list[str]:
    names: list[str] = []
    for ctx in call.get("context") or []:
        if not isinstance(ctx, dict):
            continue
        for obj in ctx.get("objects") or []:
            if not isinstance(obj, dict) or str(obj.get("objectType") or "").lower() != "account":
                continue
            for f in obj.get("fields") or []:
                if isinstance(f, dict) and str(f.get("name") or "").lower() == "name" and f.get("value"):
                    names.append(str(f["value"]).strip())
    return _uniq(names)


def record_from_call(call: dict, crm_context: bool) -> Record:
    meta = call.get("metaData") if isinstance(call.get("metaData"), dict) else call
    cid = call_id(call)
    rec = Record(gong_id=cid, input="api", date_source="api")
    rec.title = str(meta.get("title") or "").strip() or "(untitled Gong call)"
    rec.utc_start = parse_iso_utc(meta.get("started")) or parse_iso_utc(meta.get("scheduled"))
    if rec.utc_start is None:
        raise ValueError("call has no started or scheduled time")
    rec.local_start = to_local(rec.utc_start)
    rec.duration_s = _int(meta.get("duration"))
    rec.url = str(meta.get("url") or "")
    rec.direction = str(meta.get("direction") or "")
    rec.scope = str(meta.get("scope") or "")
    rec.system = str(meta.get("system") or "")
    rec.is_private = meta.get("isPrivate") is True

    names: dict[str, str] = {}
    emails: list[str] = []
    domains: list[str] = []
    for p in call.get("parties") or []:
        if not isinstance(p, dict):
            continue
        email = str(p.get("emailAddress") or p.get("email") or "").strip().lower()
        name = str(p.get("name") or "").strip()
        label = name or email
        if email:
            emails.append(email)
        if name:
            rec.participants.append(name)
        if p.get("speakerId") is not None:
            names[str(p["speakerId"])] = label or UNKNOWN_SPEAKER
        aff = str(p.get("affiliation") or "")
        if aff == "Internal" and label:
            rec.internal.append(label)
        elif aff == "External":
            if label:
                rec.external.append(label)
            if "@" in email:
                domains.append(email.split("@", 1)[1])
    rec.attendee_emails = sorted(set(emails))
    rec.participants = _uniq(rec.participants)
    rec.internal, rec.external = _uniq(rec.internal), _uniq(rec.external)
    rec.external_domains = sorted(set(domains))

    content = call.get("content") if isinstance(call.get("content"), dict) else {}
    rec.brief = str(content.get("brief") or "").strip()
    rec.key_points = [str(k.get("text") or "").strip() for k in content.get("keyPoints") or []
                      if isinstance(k, dict) and str(k.get("text") or "").strip()]
    for sec in content.get("highlights") or []:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip() or "Highlights"
        items = [str(i.get("text") or "").strip() for i in sec.get("items") or []
                 if isinstance(i, dict) and str(i.get("text") or "").strip()]
        if not items:
            continue
        if NEXT_STEP_RE.search(title):
            rec.next_steps.extend(items)
        else:
            rec.highlights.append((title, items))
    for sec in content.get("outline") or []:
        if not isinstance(sec, dict):
            continue
        items = [str(i.get("text") or "").strip() for i in sec.get("items") or []
                 if isinstance(i, dict) and str(i.get("text") or "").strip()]
        rec.outline.append((str(sec.get("section") or "").strip() or "Section", _int(sec.get("startTime")), items))
    outcome = content.get("callOutcome")
    if isinstance(outcome, dict):
        rec.outcome = str(outcome.get("name") or "").strip()
    rec.topics = _uniq(str(t.get("name") or "").strip() for t in content.get("topics") or []
                       if isinstance(t, dict) and ("duration" not in t or _int(t.get("duration")) > 0))
    rec.trackers = [f"{t.get('name')} ({_int(t.get('count'))})" for t in content.get("trackers") or []
                    if isinstance(t, dict) and t.get("name") and _int(t.get("count")) > 0]
    if crm_context:
        rec.crm_accounts = crm_accounts_of(call)

    rec.segments = segments_from_monologues(call.get("transcript") or [], names)
    # Gong's AI content is generated from the transcript, so its presence means one exists.
    rec.transcript_available = bool(rec.segments) or rec.has_ai
    return rec


# --------------------------------------------------------------------------
# hand-downloaded transcript files
# --------------------------------------------------------------------------

def _valid_name(name: str) -> bool:
    name = name.strip()
    return bool(name) and len(name.split()) <= 6 and (name[0].isalpha() or name[0] in "'\"")


def _match_turn(line: str) -> Optional[tuple[str, int, str]]:
    for rx in TURN_RES:
        m = rx.match(line)
        if m and _valid_name(m.group("name")):
            text = (m.groupdict().get("text") or "").strip()
            return m.group("name").strip(), clock_to_seconds(m.group("ts")), text
    return None


def parse_header_date(line: str) -> Optional[tuple[dt.datetime, bool]]:
    """`September 10, 2026` (optionally `, 2:57 PM`) or ISO `2026-09-10[ 14:57]`.
    Returns (local datetime, has_time)."""
    m = MONTH_DATE_RE.search(line)
    if m:
        raw = m.group("date").replace(".", "").replace("Sept ", "Sep ")
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                day = dt.datetime.strptime(raw, fmt)
                break
            except ValueError:
                day = None
        if day is not None:
            if m.group("time"):
                h, mi = (int(x) for x in m.group("time").split(":"))
                ampm = (m.group("ampm") or "").replace(".", "").lower()
                if ampm == "pm" and h < 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                if h < 24 and mi < 60:
                    return day.replace(hour=h, minute=mi), True
            return day, False
    m = ISO_DATE_RE.search(line)
    if m:
        try:
            day = dt.datetime(int(m.group("y")), int(m.group("m")), int(m.group("d")))
            if m.group("h"):
                return day.replace(hour=int(m.group("h")), minute=int(m.group("mi"))), True
            return day, False
        except ValueError:
            return None
    return None


def parse_transcript_text(text: str) -> tuple[str, Optional[tuple[dt.datetime, bool]], list[Segment]]:
    """Lenient parser for a hand-downloaded Gong transcript (.txt).

    THE DOWNLOAD FORMAT IS UNVERIFIED, so this accepts several shapes.

    Header block (everything before the first speaker turn): the first line that
    is not a date is the title; a date line may be `September 10, 2026`
    (optionally `, 2:57 PM`) or ISO `2026-09-10` (optionally ` 14:57`). Other
    header lines (duration, participants) are ignored.

    Speaker turns, each followed by text lines until the next turn:
        Name  0:05          (two or more spaces)
        Name<TAB>0:05
        0:05 | Name
        [0:05] Name:        (text may follow on the same line)
        Name (0:05):        (text may follow on the same line)
        0:05 Name:          (text may follow on the same line)
    Timestamps are m:ss or h:mm:ss offsets from the call start.

    If no turn line is found: `Name: text` lines (header keys such as `Date:` or
    `Participants:` excluded), continuation lines appended to the prior turn.
    If that finds nothing either: the body is one Unknown-speaker blob.

    Returns (title, (local start, has_time) or None, segments).
    """
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    first_turn = next((i for i, ln in enumerate(lines) if _match_turn(ln.strip())), None)

    def colon_turn(line: str) -> Optional[tuple[str, str]]:
        m = COLON_TURN_RE.match(line)
        if not m or not _valid_name(m.group("name")):
            return None
        if m.group("name").strip().lower() in HEADER_KEYS:
            return None
        return m.group("name").strip(), m.group("text").strip()

    mode = "turn"
    if first_turn is None:
        first_turn = next((i for i, ln in enumerate(lines) if colon_turn(ln.strip())), None)
        mode = "colon" if first_turn is not None else "blob"

    header = lines[:first_turn] if first_turn is not None else []
    body = lines[first_turn:] if first_turn is not None else lines

    title = ""
    when: Optional[tuple[dt.datetime, bool]] = None
    header_src = header if mode != "blob" else lines[:3]
    consumed = 0
    for i, raw in enumerate(header_src):
        line = raw.strip()
        if not line:
            continue
        d = parse_header_date(line) if when is None else None
        if d is not None and len(line) <= 60:
            when = d
            consumed = i + 1
            continue
        if not title:
            title = line.lstrip("# ").strip()
            consumed = i + 1
    if mode == "blob":
        body = lines[consumed:]

    segments: list[Segment] = []
    if mode == "blob":
        blob = re.sub(r"\s+", " ", " ".join(ln.strip() for ln in body if ln.strip())).strip()
        if blob:
            segments.append(Segment(0, UNKNOWN_SPEAKER, blob))
        return title, when, segments

    speaker: Optional[str] = None
    offset = 0
    buf: list[str] = []

    def flush() -> None:
        if speaker is not None:
            said = re.sub(r"\s+", " ", " ".join(buf)).strip()
            if said:
                segments.append(Segment(offset, speaker, said))

    for raw in body:
        line = raw.strip()
        if not line:
            continue
        hit = _match_turn(line) if mode == "turn" else None
        if hit:
            flush()
            speaker, offset, first = hit
            buf = [first] if first else []
            continue
        if mode == "colon":
            ct = colon_turn(line)
            if ct:
                flush()
                speaker, buf = ct[0], [ct[1]]
                continue
        buf.append(line)
    flush()
    return title, when, segments


def parse_filename(stem: str) -> tuple[str, Optional[tuple[dt.datetime, bool]]]:
    """An ISO date (optionally with HHMM / HH:MM / HH-MM) anywhere in the stem."""
    title, when = stem, None
    m = NAME_ISO_RE.search(stem)
    if m:
        try:
            day = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if m.group(4) and int(m.group(4)) < 24 and int(m.group(5)) < 60:
                when = (day.replace(hour=int(m.group(4)), minute=int(m.group(5))), True)
            else:
                when = (day, False)
            title = (stem[: m.start()] + " " + stem[m.end():]).strip()
        except ValueError:
            when = None
    title = NAME_NOISE_RE.sub("", title)
    title = re.sub(r"[_]+", " ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -_")
    return title, when


def iso_mtime(path: Path) -> str:
    return dt.datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def load_file_record(path: Path) -> Record:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    head_title, head_when, segments = parse_transcript_text(text)
    name_title, name_when = parse_filename(path.stem)
    rec = Record(gong_id="", input="file", source_file=path.name, mtime_iso=iso_mtime(path))
    rec.title = head_title or name_title or "(untitled Gong call)"
    has_time = False
    if name_when is not None:
        rec.local_start, has_time = name_when
        rec.date_source = "filename"
        # A header on the same day may carry the time the file name lacks.
        if not has_time and head_when and head_when[1] and head_when[0].date() == rec.local_start.date():
            rec.local_start, has_time = head_when
    elif head_when is not None:
        rec.local_start, has_time = head_when
        rec.date_source = "header"
    else:
        local = dt.datetime.fromtimestamp(path.stat().st_mtime)
        rec.local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        rec.date_source = "file-mtime"
    rec.utc_start = to_utc(rec.local_start) if has_time else None
    rec.segments = segments
    rec.transcript_available = bool(segments)
    rec.participants = _uniq(s.speaker for s in segments if s.speaker != UNKNOWN_SPEAKER)
    sidecar = path.with_suffix(NOTES_EXT)
    if sidecar.exists():
        rec.notes = sidecar.read_text(encoding="utf-8", errors="replace").strip()
        rec.mtime_iso = max(rec.mtime_iso, iso_mtime(sidecar))
    key = f"{slugify(rec.title, 'untitled')}|"
    key += path.stem if rec.date_source == "file-mtime" else rec.local_start.strftime("%Y-%m-%d %H:%M")
    rec.gong_id = "gong-file-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return rec


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
            stats.errors.append((str(p), "not a .txt transcript"))
    return out


# --------------------------------------------------------------------------
# cross-linking
# --------------------------------------------------------------------------

LINK_KEYS = {  # source -> (id key, note key)
    "fireflies": ("fireflies_id", "fireflies_note"),
    "wispr": ("wispr_meeting_id", "wispr_note"),
    "gemini": ("gemini_doc_id", "gemini_note"),
    "teams": ("teams_meeting_id", "teams_note"),
    "zoom": ("zoom_id", "zoom_note"),
}
LINK_LABELS = {"fireflies": "Fireflies", "wispr": "Wispr", "gemini": "Gemini", "teams": "Teams", "zoom": "Zoom"}


def _fm(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 60:
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
    out: dict[str, list[tuple[dt.datetime, str, str]]] = {k: [] for k in LINK_KEYS}
    ff = raw_root / "fireflies"
    if ff.is_dir():
        for p in ff.glob("*.md"):
            f = _fm(p, ("date", "meeting_id", "meeting_note"))
            start = _utc(f.get("date", ""))
            if start and f.get("meeting_id"):
                out["fireflies"].append((start, f["meeting_id"], f.get("meeting_note", "")))
    for source, sub, key in (
        ("wispr", "wispr/meetings", "wispr_meeting_id"),
        ("gemini", "gemini/meetings", "gemini_doc_id"),
        ("teams", "teams/meetings", "teams_meeting_id"),
        ("zoom", "zoom/meetings", "zoom_id"),
    ):
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

def transcript_policy(rec: Record, include_transcript: bool) -> tuple[str, bool]:
    """(policy, included). pointer | full | only-content | file."""
    if rec.input == "file":
        return "file", bool(rec.segments)
    if rec.has_ai:
        if include_transcript and rec.segments:
            return "full", True
        return "pointer", False
    return "only-content", bool(rec.segments)


def content_hash(rec: Record, policy: str, included: bool) -> str:
    payload = {
        "title": rec.title, "started": rec.utc_start.isoformat() if rec.utc_start else "",
        "duration": rec.duration_s, "url": rec.url, "private": rec.is_private,
        "brief": rec.brief, "key_points": rec.key_points, "next_steps": rec.next_steps,
        "highlights": rec.highlights, "outline": rec.outline, "outcome": rec.outcome,
        "topics": rec.topics, "trackers": rec.trackers, "crm": rec.crm_accounts,
        "emails": rec.attendee_emails, "participants": rec.participants,
        "internal": rec.internal, "external": rec.external,
        "policy": policy, "available": rec.transcript_available, "n_segments": len(rec.segments),
        "segments": [(s.offset_s, s.speaker, s.text) for s in rec.segments] if included else [],
        "notes": rec.notes,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def render_transcript(segments: list[Segment]) -> str:
    """`**Speaker** (hh:mm:ss) text`, consecutive monologues by one speaker merged."""
    out: list[str] = []
    cur: Optional[str] = None
    cur_ts = 0
    buf: list[str] = []

    def flush() -> None:
        if buf and cur is not None:
            out.append(f"**{cur}** ({fmt_offset(cur_ts)}) {' '.join(buf).strip()}")
            out.append("")

    for seg in segments:
        if seg.speaker != cur:
            flush()
            buf = []
            cur, cur_ts = seg.speaker, seg.offset_s
        buf.append(seg.text)
    flush()
    return "\n".join(out).strip()


def build_markdown(rec: Record, chash: str, policy: str, included: bool,
                   links: dict[str, tuple[str, str]], ingested_at: str) -> str:
    date_part = rec.local_start.strftime("%Y-%m-%d")
    ts_local = ""
    if rec.utc_start is not None:
        ts_local = f"{rec.local_start:%Y-%m-%d %H:%M} {tz_abbrev(rec.local_start)}"
    duration = duration_human(rec.duration_s)

    fm = [
        "---",
        "type: raw-gong-call",
        f"gong_call_id: {rec.gong_id}",
        f"title: {yaml_str(rec.title)}",
        f"date: {date_part}",
        f"timestamp: {rec.utc_start.strftime('%Y-%m-%d %H:%M:%S') if rec.utc_start else ''}",
        f"timestamp_local: {yaml_str(ts_local)}",
        f"date_source: {rec.date_source}",
        f"input: {rec.input}",
    ]
    if rec.input == "file":
        fm.append(f"source_file: {yaml_str(rec.source_file)}")
    fm += [
        f"source_hash: {chash}",
        f"call_url: {rec.url}",
        f"duration: {yaml_str(duration)}",
        f"direction: {rec.direction}",
        f"scope: {rec.scope}",
        f"system: {rec.system}",
        f"is_private: {'true' if rec.is_private else 'false'}",
        f"attendee_emails: {yaml_list(rec.attendee_emails)}",
        f"participants: {yaml_list(rec.participants)}",
        f"external_domains: {yaml_list(rec.external_domains)}",
        f"topics: {yaml_list(rec.topics)}",
        f"trackers: {yaml_list(rec.trackers)}",
        f"call_outcome: {yaml_str(rec.outcome)}",
        f"crm_accounts: {yaml_list(rec.crm_accounts)}",
        f"has_brief: {'true' if rec.brief else 'false'}",
        f"next_steps: {len(rec.next_steps)}",
        f"transcript_available: {'true' if rec.transcript_available else 'false'}",
        f"transcript_included: {'true' if included else 'false'}",
        f"transcript_policy: {policy}",
        f"transcript_segments: {len(rec.segments)}",
    ]
    for source, (oid, note) in links.items():
        id_key, note_key = LINK_KEYS[source]
        fm.append(f"{id_key}: {oid}")
        if note:
            fm.append(f"{note_key}: {yaml_str(note)}")
    fm += [f"ingested_at: {ingested_at}", "---"]

    body = ["", f"# {date_part} — {rec.title}", ""]
    meta: list[str] = []
    if duration:
        meta.append(f"**Duration:** {duration}")
    if rec.internal:
        meta.append(f"**Internal:** {', '.join(rec.internal)}")
    if rec.external:
        meta.append(f"**External:** {', '.join(rec.external)}")
    if rec.input == "file" and rec.participants:
        meta.append(f"**Speakers:** {', '.join(rec.participants)}")
    if rec.attendee_emails:
        meta.append(f"**Attendees:** {', '.join(rec.attendee_emails)}")
    if rec.topics:
        meta.append(f"**Topics:** {', '.join(rec.topics)}")
    if rec.outcome:
        meta.append(f"**Outcome:** {rec.outcome}")
    if rec.input == "file":
        meta.append(f"**Source:** Gong transcript download (`{rec.source_file}`)")
    elif rec.url:
        meta.append(f"**Source:** [Gong call]({rec.url})")
    else:
        meta.append("**Source:** Gong call")
    body += ["  \n".join(meta), ""]
    for source, (_, note) in links.items():
        if note:
            body += [f"> Also captured by {LINK_LABELS[source]}: {note}", ""]
    if rec.date_source == "file-mtime":
        body += ["> Start time unknown: neither the file name nor its header carried a date, so the date is "
                 "the file's and cross-linking was skipped. Put `YYYY-MM-DD HHMM` in the file name to fix.", ""]

    if rec.brief:
        body += ["## Brief", "", rec.brief, ""]
    if rec.key_points:
        body += ["## Key points", ""] + [f"- {k}" for k in rec.key_points] + [""]
    if rec.next_steps:
        body += ["## Next steps", ""] + [f"- [ ] {n}" for n in rec.next_steps] + [""]
    if rec.highlights:
        body += ["## Highlights", ""]
        for title, items in rec.highlights:
            body += [f"### {title}", ""] + [f"- {i}" for i in items] + [""]
    if rec.outline:
        body += ["## Outline", ""]
        for section, start, items in rec.outline:
            body += [f"### {section} ({fmt_mmss(start)})", ""]
            if items:
                body += [f"- {i}" for i in items] + [""]
    if rec.notes:
        body += ["## Notes", "", rec.notes, ""]

    body += ["## Transcript", ""]
    if included:
        body.append(render_transcript(rec.segments))
    elif policy == "pointer":
        where = f"[open the call in Gong]({rec.url})" if rec.url else "open the call in Gong"
        body.append(
            f"The transcript stays in Gong (DEC-032): {where}. AI tools: fetch it on demand with "
            f"`python3 fetch.py transcript {rec.gong_id}` (needs the Gong API key)."
        )
    else:
        body.append("_(no transcript)_")
    body.append("")
    return "\n".join(fm + body) + "\n"


def existing_file(out_dir: Path, filename: str, gong_id: str) -> Path:
    """The file for this call: the computed path, or an earlier file for the same
    id under a different title slug (Gong titles can be edited)."""
    target = out_dir / filename
    if target.exists() or not out_dir.is_dir():
        return target
    for p in out_dir.glob(f"*-{gong_id[-8:]}.md"):
        if _fm(p, ("gong_call_id",)).get("gong_call_id") == gong_id:
            return p
    return target


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def filter_calls(calls: list[dict], owners: Optional[set[str]], skip_private: bool, stats: Stats) -> list[dict]:
    """The privacy gate. Dropped calls are counted, never named."""
    kept: list[dict] = []
    for call in calls:
        reason = keep_reason(call, owners, skip_private)
        if reason == "not_owner":
            stats.not_owner += 1
        elif reason == "private":
            stats.private_skipped += 1
        else:
            kept.append(call)
    return kept


def run_records(records: list[Record], out_dir: Path, raw_root: Path, execute: bool,
                include_transcript: bool, stats: Stats, ingested_at: str, preview: list[dict]) -> None:
    indexes = load_link_indexes(raw_root)
    for rec in records:
        if rec.input == "api" and rec.utc_start is not None:
            started = rec.utc_start.strftime("%Y-%m-%d %H:%M:%S")
            stats.max_started = max(stats.max_started, started)
        policy, included = transcript_policy(rec, include_transcript)
        if not rec.has_ai and not rec.segments and not rec.notes:
            stats.empty += 1
            continue
        if rec.date_source == "file-mtime":
            stats.undated += 1
        links: dict[str, tuple[str, str]] = {}
        for source, index in indexes.items():
            hit = match_nearest(rec.utc_start, index)
            if hit:
                links[source] = hit
        chash = content_hash(rec, policy, included)
        filename = f"{rec.local_start:%Y-%m-%d}-{slugify(rec.title, 'gong-call')}-{rec.gong_id[-8:]}.md"
        out = existing_file(out_dir, filename, rec.gong_id)
        prior_hash = _fm(out, ("source_hash",)).get("source_hash") if out.exists() else None
        if prior_hash == chash:
            stats.already_present += 1
            continue
        is_update = prior_hash is not None
        if links:
            stats.linked += 1
        preview.append({
            "file": out.name, "date": f"{rec.local_start:%Y-%m-%d}", "title": rec.title,
            "policy": policy, "segments": len(rec.segments), "next_steps": len(rec.next_steps),
            "links": ",".join(sorted(links)), "action": "update" if is_update else "create",
        })
        if not execute:
            continue
        try:
            text = build_markdown(rec, chash, policy, included, links, ingested_at)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".md.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, out)
            if is_update:
                stats.updated += 1
            else:
                stats.written += 1
        except OSError as exc:
            stats.errors.append((rec.gong_id, str(exc)[:200]))


def write_last_run(stats: Stats, mode: str, state: dict, window: str) -> None:
    lines = [
        "# atlas-gong-meetings-ingest — last run", "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **Fetch window (UTC):** {window or 'none'}",
        f"- **state.cursor_started:** {state.get('cursor_started')}",
        f"- **state.cursor_mtime:** {state.get('cursor_mtime')}",
        f"- **Calls and files seen:** {stats.seen}",
        f"- **Written (new):** {stats.written}",
        f"- **Updated (content changed):** {stats.updated}",
        f"- **Already present, unchanged:** {stats.already_present}",
        f"- **Dropped (owner not a party):** {stats.not_owner}",
        f"- **Dropped (private):** {stats.private_skipped}",
        f"- **Skipped (file not modified since cursor):** {stats.not_modified}",
        f"- **Skipped (no content yet):** {stats.empty}",
        f"- **Orphan transcripts (no matching call):** {stats.orphan_transcript}",
        f"- **Undated files:** {stats.undated}",
        f"- **Cross-linked:** {stats.linked}",
        f"- **Errors:** {len(stats.errors)}", "",
    ]
    if stats.errors:
        lines += ["## Errors", ""] + [f"- `{k}` — {v}" for k, v in stats.errors[:20]] + [""]
    LAST_RUN.write_text("\n".join(lines), encoding="utf-8")


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict]) -> None:
    lines = [
        "# atlas-gong-meetings-ingest — dry run", "",
        f"- Calls and files seen: {stats.seen}",
        f"- Would create: {sum(1 for r in rows if r['action'] == 'create')}",
        f"- Would update: {sum(1 for r in rows if r['action'] == 'update')}",
        f"- Already present, unchanged: {stats.already_present}",
        f"- Dropped (owner not a party): {stats.not_owner}",
        f"- Dropped (private): {stats.private_skipped}",
        f"- No content yet: {stats.empty}", "",
        "| Action | Date | Title | Transcript | Segs | Next steps | Linked to |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r['action']} | {r['date']} | {r['title'][:44]} | {r['policy']} | "
                     f"{r['segments']} | {r['next_steps']} | {r['links']} |")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def fetch_window(args: argparse.Namespace, state: dict, lookback_days: int,
                 now: Optional[dt.datetime] = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.utcnow()
    if args.since:
        local = dt.datetime.strptime(args.since, "%Y-%m-%d")
        return (to_utc(local) or local), now
    cursor = _utc(state.get("cursor_started") or "") if args.incremental else None
    if cursor:
        return cursor - dt.timedelta(days=lookback_days), now
    return now - dt.timedelta(days=NO_CURSOR_DAYS), now


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Gong calls (API, JSON, or downloaded transcripts) into the vault.")
    ap.add_argument("--fetch", action="store_true", help="pull calls from the Gong API via fetch.py (needs a Gong API key)")
    ap.add_argument("--input-json", type=Path, action="append", default=[],
                    help="Gong JSON: an extensive or transcript response, a call, or a list of calls. Repeatable.")
    ap.add_argument("--no-owner-filter", action="store_true",
                    help="with --input-json only: the payload is already the owner's calls (never with --fetch)")
    ap.add_argument("--input-dir", type=Path, action="append", default=[],
                    help="folder of downloaded .txt transcripts (repeatable); default: export_dirs in gong-sources.json")
    ap.add_argument("--input", type=Path, action="append", default=[], help="one downloaded .txt transcript (repeatable)")
    ap.add_argument("--execute", action="store_true", help="write to the vault (default: dry run)")
    ap.add_argument("--incremental", action="store_true",
                    help="--fetch from state.json cursor_started minus lookback_days; skip files not modified since cursor_mtime")
    ap.add_argument("--since", help="with --fetch: pull calls from this local date (YYYY-MM-DD), overriding the cursor")
    ap.add_argument("--include-transcript", action="store_true",
                    help="write transcripts even when Gong AI content exists (default: pointer only, per DEC-032)")
    ap.add_argument("--dry-run-report", type=Path, help="write a dry-run report to this path")
    ap.add_argument("--vault", help="override the configured vault root for this run")
    args = ap.parse_args(argv)

    if args.no_owner_filter and args.fetch:
        print("error: --no-owner-filter is never allowed with --fetch: an API key sees every call in the company",
              file=sys.stderr)
        return 1
    if args.no_owner_filter and not args.input_json:
        print("error: --no-owner-filter only applies to --input-json", file=sys.stderr)
        return 1
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        print("error: --since takes YYYY-MM-DD", file=sys.stderr)
        return 1

    sources = load_sources()
    owners = owner_set(sources)
    if (args.fetch or (args.input_json and not args.no_owner_filter)) and not owners:
        print(f"error: owner_emails is empty or missing in {SOURCES_FILE.name}. A Gong API key sees every call "
              "in the company, so --fetch and --input-json keep only calls the owner was on. "
              'Add {"owner_emails": ["<your work email>"]}.', file=sys.stderr)
        return 1

    dirs = [Path(os.path.expanduser(str(d))) for d in args.input_dir]
    if not dirs and not args.input:
        dirs = export_dirs(sources)
    if not (args.fetch or args.input_json or dirs or args.input):
        print(f"error: no input. Pass --fetch, --input-json, --input-dir, or --input, "
              f"or list export_dirs in {SOURCES_FILE.name}", file=sys.stderr)
        return 1

    vault = atlas_config.vault_from_arg(args.vault)
    raw_root = vault / CFG.folder_name("raw")
    out_dir = raw_root / "gong" / "meetings"

    if not acquire_run_lock():
        print("error: another atlas-gong-meetings-ingest run holds the lock", file=sys.stderr)
        return 1
    try:
        state = load_state()
        stats = Stats()
        preview: list[dict] = []
        skip_private = bool(sources.get("skip_private", False))
        crm_context = bool(sources.get("crm_context", False))
        lookback = _int(sources.get("lookback_days", DEFAULT_LOOKBACK_DAYS)) or DEFAULT_LOOKBACK_DAYS
        run_started = dt.datetime.utcnow().isoformat() + "Z"
        window_label = ""

        raw_calls, transcripts = load_json_inputs(args.input_json, stats)
        json_ids = {call_id(c) for c in raw_calls}
        fetched: list[dict] = []
        if args.fetch:
            import fetch  # the only network path
            w_from, w_to = fetch_window(args, state, lookback)
            window_label = f"{w_from:%Y-%m-%d %H:%M} to {w_to:%Y-%m-%d %H:%M}"
            counts: dict = {}
            try:
                fetched = fetch.pull(w_from, w_to, sources, args.include_transcript, counts)
            except fetch.GongError as exc:
                stats.errors.append(("fetch", str(exc)[:300]))
                stats.fetch_failed = True
            stats.seen += counts.get("seen", 0)
            stats.not_owner += counts.get("not_owner", 0)
            stats.private_skipped += counts.get("private_skipped", 0)

        # The privacy gate runs on JSON calls before anything else touches them.
        stats.seen += len([c for c in raw_calls if call_id(c)])
        merged = merge_calls(raw_calls + fetched, transcripts, stats)
        json_owner_gate = None if args.no_owner_filter else owners
        kept: list[dict] = []
        from_json = [c for c in merged if call_id(c) in json_ids]
        from_api = [c for c in merged if call_id(c) not in json_ids]
        kept += filter_calls(from_json, json_owner_gate, skip_private, stats)
        kept += filter_calls(from_api, owners, skip_private, stats)  # already filtered by fetch.pull; cheap to re-check

        records: list[Record] = []
        for call in kept:
            try:
                records.append(record_from_call(call, crm_context))
            except Exception as exc:  # one bad call must not abort the run
                stats.errors.append((call_id(call) or "json", f"call skipped: {exc}"[:200]))

        cursor_mtime = state.get("cursor_mtime") if args.incremental else None
        for path in collect_files(dirs, args.input, stats):
            stats.seen += 1
            try:
                rec = load_file_record(path)
            except Exception as exc:
                stats.errors.append((path.name, f"parse failed: {exc}"[:200]))
                continue
            stats.max_mtime = max(stats.max_mtime, rec.mtime_iso)
            if cursor_mtime and rec.mtime_iso <= cursor_mtime:
                stats.not_modified += 1
                continue
            records.append(rec)

        ingested_at = dt.datetime.now().isoformat(timespec="seconds")
        run_records(records, out_dir, raw_root, args.execute, args.include_transcript, stats, ingested_at, preview)

        if args.dry_run_report:
            write_dry_run_report(args.dry_run_report, stats, preview)
        mode = "execute" if args.execute else "dry-run"
        if args.execute:
            state["last_run_iso"] = run_started
            state["last_run_count"] = state.get("last_run_count", 0) + stats.written
            state["schema_version"] = 1
            if stats.max_started and stats.max_started > (state.get("cursor_started") or ""):
                state["cursor_started"] = stats.max_started
            if stats.max_mtime and stats.max_mtime > (state.get("cursor_mtime") or ""):
                state["cursor_mtime"] = stats.max_mtime
            save_state(state)
        write_last_run(stats, mode, state, window_label)
        n_create = stats.written if args.execute else sum(1 for r in preview if r["action"] == "create")
        n_update = stats.updated if args.execute else sum(1 for r in preview if r["action"] == "update")
        print(
            f"[{mode}] seen={stats.seen} created={n_create} updated={n_update} "
            f"unchanged={stats.already_present} not_owner={stats.not_owner} "
            f"private_skipped={stats.private_skipped} not_modified={stats.not_modified} "
            f"empty={stats.empty} orphan_transcript={stats.orphan_transcript} "
            f"undated={stats.undated} linked={stats.linked} errors={len(stats.errors)}"
        )
        for key, msg in stats.errors[:10]:
            print(f"  error {key}: {msg}", file=sys.stderr)
        return 1 if stats.fetch_failed else 0
    finally:
        release_run_lock()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
