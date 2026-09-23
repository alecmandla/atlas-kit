#!/usr/bin/env python3
"""atlas-voice-memos-ingest.

Mirror iOS / macOS Voice Memos into <vault_root>/raw/voice-memos/, one
markdown file per recording. Read-only, stdlib-only (DEC-021), local-only
(DEC-002). Transcripts come from Apple's stored transcript where present
(`transcript_source: native`), else the on-device Swift helper in helper/
(`on-device`), else a stub (`none`). Audio blobs are REFERENCED, never copied.

Voice Memos has no scriptable/MCP interface, so this reads the TCC-protected
`CloudRecordings.db` Core Data store directly. That requires Full Disk Access on
the runner (DEC-024). Without FDA the run **soft-skips**: it reports `TCC-blocked`,
writes nothing, and exits 0 — never a hard failure that aborts the nightly chain.

Before any transcription the helper's `--doctor` preflight runs once (DEC-033):
a blocked environment (Speech permission, missing on-device model, broken speech
stack) produces one precise machine-readable diagnostic in the run report instead
of a 45s watchdog stall per memo. Blocked runs still ingest memos (native
transcript or `none` stub) but never invoke the helper. Verdict policy: normal
runs fail OPEN on an `unknown` verdict (a helper too old to know --doctor keeps
working, with a rebuild note); `--retry-stubs` HELPER attempts require a strict
`ready` (a retry exists to confirm a fixed environment), though helper-free
native upgrades (DB transcript / Apple sidecar) apply on any verdict.
`python3 ingest.py --doctor` runs just the preflight, exactly as the ingest does.

The schema is introspected at runtime (sqlite_master + PRAGMA table_info) — column
names are NOT hardcoded — because the real schema is unknown until FDA is granted
and varies across macOS versions. The resolved table/column mapping is logged to
last-run.md.

Reuse: `open_db_ro`, state/incremental, and the text-only / no-blob discipline are
lifted from atlas-wispr-ingest/ingest.py; `redact()` is verbatim from
atlas-distill/distill.py (keep in sync).

Env overrides (testing / wiring):
  ATLAS_VM_DB             explicit CloudRecordings.db path (skips discovery)
  ATLAS_VM_CONTAINER      Voice Memos group-container root
  ATLAS_VM_RAW            output dir (default <vault_root>/raw/voice-memos)
  ATLAS_VM_AUDIO_ROOT     base dir for resolving relative audio paths
  ATLAS_TRANSCRIBE_BIN    transcription helper (default helper/atlas-transcribe next to this script)
  ATLAS_VM_MAX_TRANSCRIBE per-run helper-call cap (default 20)
  ATLAS_VM_LOCALE         transcription locale (default en-US)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()

# --- Core Data 2001 epoch → Unix epoch offset (seconds) ---------------------
COREDATA_EPOCH_OFFSET = 978307200  # 2001-01-01 00:00:00 UTC in Unix seconds

DEFAULT_CONTAINER = (
    Path.home() / "Library" / "Group Containers"
    / "group.com.apple.VoiceMemos.shared"
)
DEFAULT_RAW = CFG.folder("raw") / "voice-memos"
DEFAULT_HELPER = Path(__file__).resolve().parent / "helper" / "atlas-transcribe"
STATE_FILE = Path(os.environ["ATLAS_VM_STATE"]).expanduser() if os.environ.get("ATLAS_VM_STATE") \
    else Path(__file__).parent / "state.json"
LAST_RUN = Path(os.environ["ATLAS_VM_LASTRUN"]).expanduser() if os.environ.get("ATLAS_VM_LASTRUN") \
    else Path(__file__).parent / "last-run.md"


def env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


CONTAINER = env_path("ATLAS_VM_CONTAINER", DEFAULT_CONTAINER)
RAW_VM = env_path("ATLAS_VM_RAW", DEFAULT_RAW)
HELPER = env_path("ATLAS_TRANSCRIBE_BIN", DEFAULT_HELPER)
LOCALE = os.environ.get("ATLAS_VM_LOCALE", "en-US")
MAX_TRANSCRIBE = int(os.environ.get("ATLAS_VM_MAX_TRANSCRIBE", "20"))

# --- redact(): verbatim from atlas-distill/distill.py --------------
REDACTIONS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-REDACTED"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "ghp_REDACTED"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA_REDACTED"),
    (re.compile(r"xoxb-[A-Za-z0-9-]{30,}"), "xoxb-REDACTED"),
    (re.compile(r"Bearer [A-Za-z0-9._-]{30,}"), "Bearer REDACTED"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "password=REDACTED"),
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"), "api_key=REDACTED"),
]


def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, redactions_applied)."""
    if not text:
        return text, 0
    out = text
    applied = 0
    for pat, repl in REDACTIONS:
        new_out = pat.sub(repl, out)
        if new_out != out:
            applied += 1
            out = new_out
    return out, applied


class TCCBlocked(Exception):
    """The Voice Memos container is present but unreadable (no Full Disk Access)."""


@dataclass
class ColMap:
    table: str
    uid: Optional[str] = None
    pk: str = "Z_PK"
    date: Optional[str] = None
    path: Optional[str] = None
    duration: Optional[str] = None
    title: Optional[str] = None
    transcript: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "table": self.table, "uid": self.uid, "pk": self.pk, "date": self.date,
            "path": self.path, "duration": self.duration, "title": self.title,
            "transcript": self.transcript,
        }


@dataclass
class Stats:
    seen: int = 0
    written: int = 0
    already_present: int = 0
    deferred: int = 0
    native: int = 0
    on_device: int = 0
    stub_none: int = 0
    stubs_upgraded: int = 0
    stub_retry_candidates: int = 0
    redactions: int = 0
    tcc_blocked: bool = False
    container_missing: bool = False
    colmap: Optional[ColMap] = None
    max_recording_date: Optional[str] = None  # max ISO date seen this run
    doctor_verdict: Optional[str] = None  # ready|blocked|unknown|absent|error
    doctor_reason: str = ""
    doctor_info: dict = field(default_factory=dict)
    helper_soft_skips: dict = field(default_factory=dict)  # exit code -> count (3=audio, 4=auth)
    notes: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


# --- state ------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": None, "last_run_count": 0, "last_recording_date": None,
            "schema_version": 1}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


# --- discovery + access (TCC soft-skip) -------------------------------------
def candidate_roots() -> list[Path]:
    """All standard places macOS may keep the Voice Memos store.

    DEC-024 assumed the group container, but that container can hold no recordings
    DB even with FDA granted — the real location varies by macOS version and
    by whether the store synced via iCloud. So search every known root. An explicit
    `ATLAS_VM_CONTAINER` override pins the search to just that root."""
    if os.environ.get("ATLAS_VM_CONTAINER"):
        return [CONTAINER]
    home = Path.home()
    return [
        DEFAULT_CONTAINER,  # ~/Library/Group Containers/group.com.apple.VoiceMemos.shared
        home / "Library" / "Containers" / "com.apple.VoiceMemos" / "Data"
             / "Library" / "Application Support" / "com.apple.voicememos",
        home / "Library" / "Application Support" / "com.apple.voicememos",
    ]


def discover_db() -> Optional[Path]:
    """Find CloudRecordings.db across the candidate roots.

    Raise TCCBlocked when a root exists but is unreadable and none was readable
    (the no-Full-Disk-Access signal). Return None when the roots are readable but
    hold no DB (container-missing — Voice Memos unused / not yet synced to this Mac)."""
    override = os.environ.get("ATLAS_VM_DB")
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else None

    saw_permission_error = False
    saw_readable_root = False
    for root in candidate_roots():
        try:
            if not root.exists():
                continue
        except PermissionError:
            saw_permission_error = True
            continue
        # Listing a TCC-protected dir raises PermissionError — that's the no-FDA signal.
        try:
            os.listdir(root)
        except PermissionError:
            saw_permission_error = True
            continue
        except FileNotFoundError:
            continue
        saw_readable_root = True
        for c in (root / "Recordings" / "CloudRecordings.db", root / "CloudRecordings.db"):
            try:
                if c.exists():
                    return c
            except PermissionError:
                saw_permission_error = True
        try:
            for p in root.rglob("CloudRecordings.db"):
                return p
        except PermissionError:
            saw_permission_error = True

    if saw_permission_error and not saw_readable_root:
        raise TCCBlocked("Voice Memos container(s) present but unreadable — grant Full Disk Access (DEC-024)")
    return None  # caller treats as container_missing soft-skip


def open_db_ro(db_path: Path, retries: int = 3) -> sqlite3.Connection:
    """Open read-only with retry/backoff (lifted from atlas-wispr-ingest).

    A permission failure surfaces as TCCBlocked (the no-FDA soft-skip path)."""
    last_err: Optional[Exception] = None
    for i in range(retries):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1")  # force the file open now, not lazily
            return conn
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "authorization denied" in msg or "unable to open" in msg or "not permitted" in msg:
                raise TCCBlocked(str(e))
            last_err = e
            time.sleep(0.2 * (i + 1))
    raise RuntimeError(f"Could not open Voice Memos DB read-only after {retries} attempts: {last_err}")


# --- schema introspection (no hardcoded columns) ----------------------------
def _pick(cols: list[str], *needles: str, exact: Optional[str] = None) -> Optional[str]:
    up = {c.upper(): c for c in cols}
    if exact and exact.upper() in up:
        return up[exact.upper()]
    for needle in needles:
        for cu, c in up.items():
            if needle.upper() in cu:
                return c
    return None


def introspect(conn: sqlite3.Connection) -> ColMap:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    # Prefer a recordings table; fall back to the largest table with a date+path.
    table = None
    for t in tables:
        if "RECORDING" in t.upper():
            table = t
            break
    if table is None:
        best, best_rows = None, -1
        for t in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
            except sqlite3.Error:
                continue
            if _pick(cols, "DATE") and _pick(cols, "PATH"):
                n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if n > best_rows:
                    best, best_rows = t, n
        table = best
    if table is None:
        raise RuntimeError("no recordings table found in CloudRecordings.db")

    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    return ColMap(
        table=table,
        uid=_pick(cols, "UNIQUEID", exact="ZUNIQUEID"),
        pk="Z_PK" if "Z_PK" in cols else (_pick(cols, "PK") or "rowid"),
        date=_pick(cols, "DATE", exact="ZDATE"),
        path=_pick(cols, "PATH", exact="ZPATH"),
        duration=_pick(cols, "DURATION", exact="ZDURATION"),
        title=_pick(cols, "CUSTOMLABEL", "ENCRYPTEDTITLE", "LABEL", "TITLE", "NAME",
                    exact="ZCUSTOMLABEL"),
        transcript=_pick(cols, "TRANSCRIPT"),
    )


# --- field extraction -------------------------------------------------------
def zdate_to_iso(v) -> tuple[str, str]:
    """Core Data ZDATE (seconds since 2001) → (iso, yyyy-mm-dd)."""
    if v is None:
        return "", ""
    try:
        unix = float(v) + COREDATA_EPOCH_OFFSET
        d = dt.datetime.utcfromtimestamp(unix)
        iso = d.isoformat() + "Z"
        return iso, iso[:10]
    except (ValueError, OverflowError, OSError):
        return "", ""


def memo_id_for(row: sqlite3.Row, cm: ColMap) -> str:
    if cm.uid:
        v = row[cm.uid]
        if v:
            return re.sub(r"[^A-Za-z0-9._-]", "_", str(v))
    return f"pk-{row[cm.pk]}"


def resolve_audio_path(raw_path: Optional[str]) -> Optional[Path]:
    if not raw_path:
        return None
    p = Path(raw_path)
    if p.is_absolute():
        return p
    root = os.environ.get("ATLAS_VM_AUDIO_ROOT")
    base = Path(root).expanduser() if root else (CONTAINER / "Recordings")
    return base / raw_path


def sidecar_transcript(audio: Optional[Path]) -> Optional[str]:
    """Probe for an Apple-written sidecar transcript next to the .m4a."""
    if not audio:
        return None
    for ext in (".transcription", ".txt"):
        cand = audio.with_suffix(ext)
        try:
            if cand.exists():
                txt = cand.read_text(encoding="utf-8", errors="replace").strip()
                if txt:
                    return txt
        except OSError:
            pass
    plist = audio.with_suffix(".plist")
    try:
        if plist.exists():
            import plistlib
            data = plistlib.loads(plist.read_bytes())
            for key in ("transcription", "transcript", "text"):
                if isinstance(data, dict) and data.get(key):
                    return str(data[key]).strip()
    except Exception:
        pass
    return None


def run_doctor(stats: Stats) -> bool:
    """Preflight the transcription helper once per run (helper `--doctor`, DEC-033).

    Parses the doctor's stable key=value contract and returns True when
    transcription may proceed. A blocked environment costs this one fast check
    instead of one 45s watchdog stall per memo; the precise blocker lands in
    the run report (never an error — soft-skip philosophy, DEC-024).

    Verdicts: ready → transcribe. blocked/absent/error → memos still ingest
    (native transcript or `none` stub) but the helper is never invoked. A
    helper too old to know --doctor (`unknown`) fails OPEN for normal runs
    (proceeds as before the doctor existed, with a rebuild note); the caller
    holds --retry-stubs helper attempts to the stricter `ready`-only bar.
    """
    if not HELPER.exists():
        stats.doctor_verdict = "absent"
        stats.doctor_reason = f"helper not built at {HELPER}"
        stats.notes.append(f"doctor: helper not built at {HELPER} — memos ingest as `none` stubs; "
                           "build it via helper/build.sh")
        return False
    try:
        proc = subprocess.run(
            [str(HELPER), "--doctor", "--locale", LOCALE],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        stats.doctor_verdict = "error"
        stats.doctor_reason = str(e)
        stats.errors.append(("doctor", str(e)))
        return False
    info: dict = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            info[key.strip()] = value.strip()
    stats.doctor_info = info
    verdict = info.get("verdict")
    if verdict == "ready" and proc.returncode == 0:
        stats.doctor_verdict = "ready"
        return True
    if verdict == "blocked":
        stats.doctor_verdict = "blocked"
        stats.doctor_reason = info.get("reason") or proc.stderr.strip()[:200]
        stats.notes.append(
            f"doctor: transcription blocked by {info.get('blocked_by', '?')} — "
            f"{stats.doctor_reason} (runbook: helper/README.md)"
        )
        return False
    # No verdict line — a pre-doctor helper build (exits 2 on the unknown
    # flag). Proceed exactly as before the doctor existed rather than blocking
    # a previously-working setup.
    stats.doctor_verdict = "unknown"
    stats.doctor_reason = (proc.stderr.strip() or proc.stdout.strip())[:200]
    stats.notes.append("doctor: helper predates --doctor (rebuild via helper/build.sh) — "
                       "proceeding without preflight")
    return True


def call_helper(audio: Path, stats: Stats) -> Optional[str]:
    """Invoke the transcription helper BY ABSOLUTE PATH. Returns transcript or None.

    Exit 4 (auth absent) / 3 (audio unreadable) are soft no-transcript signals,
    not errors — they yield a `none` stub, never an aborted run."""
    if not HELPER.exists():
        stats.notes.append(f"helper not built at {HELPER} — emitting `none` stubs")
        return None
    try:
        proc = subprocess.run(
            [str(HELPER), str(audio), "--locale", LOCALE],
            capture_output=True, text=True, timeout=900,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        stats.errors.append(("helper-invoke", f"{audio.name}: {e}"))
        return None
    if proc.returncode == 0:
        return proc.stdout.strip()
    if proc.returncode in (3, 4):
        # No FDA / not authorized — expected pre-grant; soft none stub. The
        # doctor can't preflight per-file audio access (it runs before row
        # selection), so surface repeat occurrences in the run report instead
        # of hiding them entirely behind `none` stubs.
        stats.helper_soft_skips[proc.returncode] = stats.helper_soft_skips.get(proc.returncode, 0) + 1
        return None
    stats.errors.append(("helper", f"{audio.name}: exit {proc.returncode}: {proc.stderr.strip()[:120]}"))
    return None


def resolve_transcript(row: sqlite3.Row, cm: ColMap, audio: Optional[Path],
                       budget: list[int], stats: Stats,
                       helper_ready: bool) -> tuple[str, str]:
    """Return (transcript_text, source). source ∈ native|on-device|none|deferred."""
    # 1. DB-stored transcript (native).
    if cm.transcript:
        v = row[cm.transcript]
        if v and str(v).strip():
            return str(v).strip(), "native"
    # 2. Sidecar written by Apple (native).
    side = sidecar_transcript(audio)
    if side:
        return side, "native"
    # 3. On-device helper — only when the doctor preflight passed (a blocked
    #    environment stubs immediately instead of stalling per memo), subject
    #    to the per-run cap.
    if audio is not None and helper_ready:
        if budget[0] <= 0:
            return "", "deferred"
        budget[0] -= 1
        text = call_helper(audio, stats)
        if text:
            return text, "on-device"
    # 4. Nothing available.
    return "", "none"


# --- write one memo ---------------------------------------------------------
def render_memo(row: sqlite3.Row, cm: ColMap, ingested_at: str,
                budget: list[int], stats: Stats,
                helper_ready: bool) -> Optional[tuple[str, str, dict]]:
    """Return (memo_id, markdown, meta) or None if deferred."""
    memo_id = memo_id_for(row, cm)
    iso, date_part = zdate_to_iso(row[cm.date]) if cm.date else ("", "")
    title = (str(row[cm.title]).strip() if cm.title and row[cm.title] else "") or "(untitled memo)"
    dur = row[cm.duration] if cm.duration else None
    try:
        dur_s = int(round(float(dur))) if dur is not None else None
    except (ValueError, TypeError):
        dur_s = None
    raw_audio = str(row[cm.path]) if cm.path and row[cm.path] else None
    audio = resolve_audio_path(raw_audio)

    transcript, source = resolve_transcript(row, cm, audio, budget, stats, helper_ready)
    if source == "deferred":
        return None  # rolled out over subsequent runs; no file written

    body, n = redact(transcript) if transcript else ("", 0)
    stats.redactions += n
    if source == "native":
        stats.native += 1
    elif source == "on-device":
        stats.on_device += 1
    else:
        stats.stub_none += 1

    body_block = body if body else (
        "_(no transcript — recorded audio only. Run `python3 ingest.py --doctor` to see "
        "the exact blocker, fix it per helper/README.md, then "
        "`python3 ingest.py --execute --retry-stubs` to backfill.)_"
    )
    # Frontmatter: `date` first, then the remaining keys alphabetically.
    fm = [
        "---",
        f"date: {date_part}",
        f"audio_path: {audio if audio else ''}",
        f"duration_seconds: {dur_s if dur_s is not None else 'null'}",
        f"ingested_at: {ingested_at}",
        f"memo_id: {memo_id}",
        f"title: {title}",
        f"transcript_source: {source}",
        "type: raw-voice-memo",
        "---",
        "",
        f"# {title}" + (f" — {date_part}" if date_part else ""),
        "",
        body_block,
    ]
    return memo_id, "\n".join(fm) + "\n", {"date": date_part, "source": source, "title": title}


# --- main ingest ------------------------------------------------------------
def atomic_write(path: Path, content: str) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def run_ingest(execute: bool, stats: Stats, ingested_at: str,
               dry_rows: list[dict], incremental: bool = False,
               state: Optional[dict] = None, retry_stubs: bool = False) -> None:
    try:
        db = discover_db()
    except TCCBlocked as e:
        stats.tcc_blocked = True
        stats.notes.append(f"TCC-blocked: {e}")
        return
    if db is None:
        stats.container_missing = True
        stats.notes.append(f"No CloudRecordings.db found under {CONTAINER} (Voice Memos unused on this Mac?)")
        return

    try:
        conn = open_db_ro(db)
    except TCCBlocked as e:
        stats.tcc_blocked = True
        stats.notes.append(f"TCC-blocked opening {db}: {e}")
        return

    budget = [MAX_TRANSCRIBE]
    # Doctor preflight, once per run before any transcription (DEC-033): a
    # blocked environment yields one precise diagnostic instead of N identical
    # watchdog stalls. Memos still ingest (native or `none` stub) either way.
    # `helper_ok` fails open on an `unknown` verdict (pre-doctor helper build)
    # so a previously-working setup keeps working; stub RETRIES are stricter —
    # they exist to confirm a fixed environment, so `unknown` doesn't count.
    helper_ok = run_doctor(stats)
    retry_helper_ok = stats.doctor_verdict == "ready"
    try:
        cm = introspect(conn)
        stats.colmap = cm
        date_col = cm.date or cm.pk
        # --incremental limits the SCAN only (a perf bound); file-exists below is
        # the authoritative, bulletproof dedup, so nothing is ever orphaned even if
        # a deferred memo falls outside the window — a full run recovers it. A 30-day
        # overlap keeps recently-deferred memos in-window.
        where, params = "", ()
        if incremental and cm.date and state and state.get("last_recording_date"):
            try:
                floor_iso = state["last_recording_date"]
                floor_unix = dt.datetime.fromisoformat(floor_iso.rstrip("Z")).timestamp()
                floor_cd = floor_unix - COREDATA_EPOCH_OFFSET - 30 * 86400  # 30d overlap
                where, params = f' WHERE "{date_col}" >= ?', (floor_cd,)
            except Exception:
                where, params = "", ()
        try:
            rows = conn.execute(
                f'SELECT * FROM "{cm.table}"{where} ORDER BY "{date_col}" DESC', params).fetchall()
        except sqlite3.Error:
            rows = conn.execute(f'SELECT * FROM "{cm.table}"').fetchall()

        if execute:
            RAW_VM.mkdir(parents=True, exist_ok=True)

        for row in rows:
            stats.seen += 1
            if cm.date:
                _iso, _dp = zdate_to_iso(row[cm.date])
                if _dp and (stats.max_recording_date is None or _dp > stats.max_recording_date):
                    stats.max_recording_date = _dp
            memo_id = memo_id_for(row, cm)
            out = RAW_VM / f"{memo_id}.md"
            if out.exists():
                # --retry-stubs: memos frozen as `transcript_source: none` (e.g.
                # while the helper path was broken) get one more transcription
                # attempt; the file is rewritten only if a transcript appears.
                if retry_stubs:
                    try:
                        existing = out.read_text(encoding="utf-8")
                    except OSError:
                        existing = ""
                    if "transcript_source: none" in existing:
                        stats.stub_retry_candidates += 1
                        # Helper retry attempts require a strictly-ready doctor
                        # verdict (`retry_helper_ok`) — a still-broken or
                        # unverified environment must not burn helper calls.
                        # The render itself always runs: steps 1-2 of
                        # resolve_transcript (DB transcript, Apple sidecar) are
                        # helper-free, so a native transcript that appeared
                        # since the stub froze upgrades even while blocked.
                        # Provenance stays safe either way: no transcript →
                        # source "none" → no rewrite.
                        if execute:
                            rendered = render_memo(row, cm, ingested_at, budget, stats, retry_helper_ok)
                            if rendered is None:
                                stats.deferred += 1
                                continue
                            _mid, md, meta = rendered
                            if meta["source"] != "none":
                                atomic_write(out, md)
                                stats.stubs_upgraded += 1
                                continue
                stats.already_present += 1  # idempotent: re-run is a no-op
                continue
            if not execute:
                # Plan only — don't call the helper or write.
                iso, dp = zdate_to_iso(row[cm.date]) if cm.date else ("", "")
                if len(dry_rows) < 50:
                    dry_rows.append({"memo_id": memo_id, "date": dp,
                                     "has_db_transcript": bool(cm.transcript and row[cm.transcript])})
                stats.written += 1
                continue
            rendered = render_memo(row, cm, ingested_at, budget, stats, helper_ok)
            if rendered is None:
                stats.deferred += 1
                continue
            _mid, md, _meta = rendered
            atomic_write(out, md)
            stats.written += 1
    finally:
        conn.close()


# --- reports ----------------------------------------------------------------
def write_dry_run_report(path: Path, stats: Stats) -> None:
    today = dt.date.today().isoformat()
    lines = [
        f"# atlas-voice-memos-ingest dry-run — {today}",
        "",
        "Skill: `atlas-voice-memos-ingest`",
        f"Source: `{CONTAINER}/…/CloudRecordings.db` (read-only).",
        "Output: `raw/voice-memos/<memo-id>.md`, one per recording. Audio referenced, never copied.",
        "",
        "## Summary",
        "",
    ]
    if stats.tcc_blocked:
        lines += [
            "**TCC-blocked: 0 memos.** The Voice Memos container is present but unreadable — "
            "the runner lacks Full Disk Access (DEC-024). This is a soft-skip, not a failure: "
            "nothing written, exit 0. Grant FDA (see the runbook in `helper/README.md`) and re-run.",
        ]
    elif stats.container_missing:
        lines += ["**No Voice Memos container / DB found** — nothing to ingest (soft-skip, exit 0)."]
    else:
        cm = stats.colmap.as_dict() if stats.colmap else {}
        lines += [
            f"- Recordings seen: **{stats.seen}**",
            f"  - Would write: {stats.written}",
            f"  - Already present (idempotent skip): {stats.already_present}",
            f"- Transcription doctor: **{stats.doctor_verdict or 'not run'}**"
            + (f" — {stats.doctor_reason}" if stats.doctor_reason else ""),
            f"- Resolved table/column mapping: `{cm}`",
        ]
        if stats.notes:
            lines += ["", "### Notes", *[f"- {n}" for n in stats.notes]]
    lines += [
        "",
        "## Next action",
        "",
        "`python3 ingest.py --execute --incremental` to write the planned files "
        "(no-op if TCC-blocked; rolls transcription out under the per-run cap).",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    status = ("TCC-blocked (soft-skip — no Full Disk Access; DEC-024)" if stats.tcc_blocked
              else "container-missing (soft-skip)" if stats.container_missing
              else "ok")
    lines = [
        "# atlas-voice-memos-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **Status:** {status}",
        f"- **Recordings seen:** {stats.seen}",
        f"- **Transcription doctor (preflight):** {stats.doctor_verdict or 'not run'}"
        + (f" — {stats.doctor_reason}" if stats.doctor_reason else ""),
        f"- **Written:** {stats.written}",
        f"- **Already present:** {stats.already_present}",
        f"- **Deferred (over per-run transcription cap = {MAX_TRANSCRIBE}):** {stats.deferred}",
        f"- **Stub retries — candidates:** {stats.stub_retry_candidates}  **upgraded:** {stats.stubs_upgraded}",
        f"- **transcript_source — native:** {stats.native}  **on-device:** {stats.on_device}  **none:** {stats.stub_none}",
        f"- **Redactions applied:** {stats.redactions}",
        f"- **Errors:** {len(stats.errors)}",
    ]
    if stats.helper_soft_skips:
        parts = ", ".join(f"exit {c} ×{n}" for c, n in sorted(stats.helper_soft_skips.items()))
        lines.append(f"- **Helper soft no-transcript signals:** {parts} "
                     "(3 = audio unreadable, check FDA; 4 = Speech not authorized — memos stubbed as `none`)")
    if stats.colmap:
        lines.append(f"- **Resolved schema mapping:** `{stats.colmap.as_dict()}`")
    if stats.doctor_info:
        lines.append(f"- **Doctor detail:** `{stats.doctor_info}`")
    if stats.notes:
        lines += ["", "## Notes", *[f"- {n}" for n in stats.notes]]
    if stats.errors:
        lines += ["", "## Errors", *[f"- {c}: {m}" for c, m in stats.errors[:20]]]
    LAST_RUN.write_text("\n".join(lines) + "\n", encoding="utf-8")



# --- duplicate-fire guard (scheduler thundering-herd; suite lock is atlas-nightly/lock.py)
# Overridable so the test-bed's subprocess runs don't contend on the real
# source-dir lock (a killed test would otherwise poison --execute for 2h).
RUN_LOCK = Path(os.environ["ATLAS_VM_RUN_LOCK"]).expanduser() if os.environ.get("ATLAS_VM_RUN_LOCK") \
    else Path(__file__).parent / ".run.lock"
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
    parser = argparse.ArgumentParser(description="Ingest Voice Memos into raw/voice-memos/")
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--incremental", action="store_true",
                        help="Limit the scan using state.json (dedup is always file-exists).")
    parser.add_argument("--retry-stubs", action="store_true",
                        help="Re-attempt transcription for existing `transcript_source: none` "
                             "stubs; rewrites only when a transcript appears. Helper attempts "
                             "need a strict `ready` doctor verdict; helper-free native upgrades "
                             "apply regardless. Run WITHOUT --incremental so old stubs are in "
                             "the scan.")
    parser.add_argument("--doctor", action="store_true",
                        help="Run only the transcription preflight — exactly as the ingest "
                             "invokes it — and print the helper's key=value report. Exit 0 = "
                             "a normal run would attempt transcription (verdict ready, or "
                             "unknown from a pre-doctor helper build); exit 1 = blocked/absent. "
                             "Fix mapping: helper/README.md runbook.")
    args = parser.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2

    if args.doctor:
        stats = Stats()
        ready = run_doctor(stats)
        for k, v in stats.doctor_info.items():
            print(f"{k}={v}")
        if not stats.doctor_info:
            print(f"verdict={stats.doctor_verdict}")
            if stats.doctor_reason:
                print(f"reason={stats.doctor_reason}")
        for n in stats.notes:
            print(f"note: {n}", file=sys.stderr)
        return 0 if ready else 1

    if args.execute:
        if not acquire_run_lock():
            print("skipped: another run of this skill is in progress (duplicate-fire guard)")
            return 0
        import atexit
        atexit.register(release_run_lock)

    state = load_state()
    stats = Stats()
    dry_rows: list[dict] = []
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    try:
        run_ingest(args.execute, stats, ingested_at, dry_rows,
                   incremental=args.incremental, state=state,
                   retry_stubs=args.retry_stubs)
    except Exception as e:  # noqa: BLE001 — never abort the nightly chain
        print(f"error: {e}", file=sys.stderr)
        stats.errors.append(("fatal", str(e)))

    mode = "execute" if args.execute else "dry-run"
    if args.execute and not (stats.tcc_blocked or stats.container_missing):
        state["last_run_iso"] = ingested_at
        state["last_run_count"] = state.get("last_run_count", 0) + stats.written
        if stats.max_recording_date:
            prev = state.get("last_recording_date")
            state["last_recording_date"] = max(prev, stats.max_recording_date) if prev else stats.max_recording_date
        save_state(state)

    if args.dry_run_report:
        write_dry_run_report(args.dry_run_report, stats)
    write_last_run(stats, mode, state)

    print(
        f"seen={stats.seen} written={stats.written} already={stats.already_present} "
        f"deferred={stats.deferred} native={stats.native} on_device={stats.on_device} "
        f"none={stats.stub_none} tcc_blocked={stats.tcc_blocked} "
        f"container_missing={stats.container_missing} doctor={stats.doctor_verdict or 'n/a'} "
        f"errors={len(stats.errors)}"
    )
    # Soft-skip (TCC / missing container) is exit 0 by design (DEC-024).
    return 1 if stats.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
