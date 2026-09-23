#!/usr/bin/env python3
"""Contract tests for atlas-voice-memos-ingest's doctor preflight + stub retry.

Run standalone:  python3 test_ingest.py
Or with pytest:  pytest test_ingest.py

Asserts external behavior at the ingest ↔ helper contract only (no helper
internals): given a fake helper emitting a particular `--doctor` output / exit
code, the ingest soft-skips or proceeds, the run report carries the doctor's
reason, and stub upgrades happen only when a real transcript is produced.

Test-bed: a synthetic CloudRecordings.db (the real ZCLOUDRECORDING shape, as
resolved by the 2026-07 real-machine runs) + a scriptable fake helper whose
doctor output and per-file transcription behavior are set via a JSON config.
Every case runs ingest.py as a subprocess with the documented env overrides —
the same seam the nightly chain uses.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INGEST = HERE / "ingest.py"

COREDATA_EPOCH_OFFSET = 978307200

DOCTOR_READY = """\
doctor=v1
engine=analyzer
locale=en-US
speech_auth=authorized
model_supported=true
model_installed=true
probe=pass
verdict=ready
"""

DOCTOR_SPEECH_DENIED = """\
doctor=v1
engine=legacy
locale=en-US
speech_auth=denied
recognizer_present=true
recognizer_available=true
on_device_model=true
probe=skipped
verdict=blocked
blocked_by=speech-auth
reason=Speech Recognition is denied for this process context — enable it in System Settings
"""

DOCTOR_SPEECH_NOT_DETERMINED = """\
doctor=v1
engine=legacy
locale=en-US
speech_auth=notDetermined
recognizer_present=true
recognizer_available=true
on_device_model=true
probe=skipped
verdict=blocked
blocked_by=speech-auth
reason=Speech Recognition was never asked for this process context — run 'atlas-transcribe --doctor --request-auth' interactively
"""

DOCTOR_MODEL_MISSING = """\
doctor=v1
engine=analyzer
locale=en-US
speech_auth=authorized
model_supported=true
model_installed=false
probe=skipped
verdict=blocked
blocked_by=model
reason=on-device speech model for 'en-US' is not installed — run 'atlas-transcribe --doctor --install-model --locale en-US'
"""

FAKE_HELPER = '''#!/usr/bin/env python3
"""Scriptable stand-in for helper/atlas-transcribe (test-bed only).

Reads fake_helper_config.json next to this script:
  doctor_stdout / doctor_exit    what --doctor emits
  files: {basename: {stdout, stderr, exit}}   per-audio-file behavior
  default: same shape, for files not listed
Appends every invocation's argv to calls.log.
"""
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "fake_helper_config.json")) as f:
    cfg = json.load(f)
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")

if "--doctor" in sys.argv:
    sys.stdout.write(cfg.get("doctor_stdout", ""))
    sys.exit(cfg.get("doctor_exit", 0))

audio = sys.argv[1]
beh = cfg.get("files", {}).get(os.path.basename(audio),
                               cfg.get("default", {"exit": 7, "stderr": "stalled"}))
if beh.get("stdout"):
    sys.stdout.write(beh["stdout"])
if beh.get("stderr"):
    sys.stderr.write(beh["stderr"])
sys.exit(beh.get("exit", 0))
'''


def make_db(path: Path, memos: list[dict]) -> None:
    """Synthetic CloudRecordings.db matching the resolved real schema mapping."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ZCLOUDRECORDING ("
        "Z_PK INTEGER PRIMARY KEY, ZUNIQUEID TEXT, ZDATE REAL, ZPATH TEXT, "
        "ZDURATION REAL, ZCUSTOMLABEL TEXT)"
    )
    for m in memos:
        conn.execute(
            "INSERT INTO ZCLOUDRECORDING (ZUNIQUEID, ZDATE, ZPATH, ZDURATION, ZCUSTOMLABEL) "
            "VALUES (?, ?, ?, ?, ?)",
            (m["uid"], m["unix_date"] - COREDATA_EPOCH_OFFSET, m["path"],
             m.get("duration", 30.0), m.get("title", "test memo")),
        )
    conn.commit()
    conn.close()


class Bed:
    """One disposable test environment: db + audio + fake helper + output dirs."""

    def __init__(self, root: Path, memos: list[dict], helper_cfg: dict | None):
        self.root = root
        self.raw = root / "raw"
        self.raw.mkdir()
        self.audio_root = root / "audio"
        self.audio_root.mkdir()
        self.db = root / "CloudRecordings.db"
        for m in memos:
            (self.audio_root / m["path"]).write_bytes(b"\x00fakeaudio")
        make_db(self.db, memos)
        self.helper = root / "fake-helper"
        self.calls_log = root / "calls.log"
        if helper_cfg is not None:
            self.helper.write_text(FAKE_HELPER)
            self.helper.chmod(0o755)
            (root / "fake_helper_config.json").write_text(json.dumps(helper_cfg))
        self.last_run = root / "last-run.md"
        self.state = root / "state.json"

    def run(self, *args: str, locale: str = "en-US") -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            ATLAS_VM_DB=str(self.db),
            ATLAS_VM_RAW=str(self.raw),
            ATLAS_VM_AUDIO_ROOT=str(self.audio_root),
            ATLAS_TRANSCRIBE_BIN=str(self.helper),
            ATLAS_VM_STATE=str(self.state),
            ATLAS_VM_LASTRUN=str(self.last_run),
            ATLAS_VM_LOCALE=locale,
            # Isolate the duplicate-fire guard: without this, test subprocesses
            # contend on the real source-dir .run.lock (a killed test poisons
            # --execute runs for 2h).
            ATLAS_VM_RUN_LOCK=str(self.root / "run.lock"),
        )
        return subprocess.run(
            [sys.executable, str(INGEST), *args],
            capture_output=True, text=True, timeout=60, env=env, cwd=self.root,
        )

    def transcribe_calls(self) -> list[list[str]]:
        """Helper invocations that were transcription attempts (not --doctor)."""
        if not self.calls_log.exists():
            return []
        calls = [json.loads(l) for l in self.calls_log.read_text().splitlines()]
        return [c for c in calls if "--doctor" not in c]

    def doctor_calls(self) -> list[list[str]]:
        if not self.calls_log.exists():
            return []
        calls = [json.loads(l) for l in self.calls_log.read_text().splitlines()]
        return [c for c in calls if "--doctor" in c]


MEMOS = [
    {"uid": "AAAA-1111", "unix_date": 1600000000, "path": "a.m4a", "title": "memo a"},
    {"uid": "BBBB-2222", "unix_date": 1600100000, "path": "b.m4a", "title": "memo b"},
]


class DoctorPreflightTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def bed(self, helper_cfg, memos=MEMOS) -> Bed:
        return Bed(self.root, memos, helper_cfg)

    # --- ready path -----------------------------------------------------------

    def test_ready_verdict_transcribes(self):
        bed = self.bed({
            "doctor_stdout": DOCTOR_READY, "doctor_exit": 0,
            "default": {"stdout": "hello from the fake recognizer", "exit": 0},
        })
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("doctor=ready", proc.stdout)
        self.assertEqual(len(bed.doctor_calls()), 1)  # once per run, before the batch
        self.assertEqual(len(bed.transcribe_calls()), 2)
        for m in MEMOS:
            body = (bed.raw / f"{m['uid']}.md").read_text()
            self.assertIn("transcript_source: on-device", body)
            self.assertIn("hello from the fake recognizer", body)
        self.assertIn("**Transcription doctor (preflight):** ready", bed.last_run.read_text())

    # --- blocked paths --------------------------------------------------------

    def test_blocked_soft_skips_transcription_but_still_stubs(self):
        bed = self.bed({
            "doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4,
            "default": {"stdout": "should never be produced", "exit": 0},
        })
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)  # soft-skip: exit 0 (DEC-024)
        self.assertIn("doctor=blocked", proc.stdout)
        self.assertEqual(bed.transcribe_calls(), [])  # ONE diagnostic, zero stalls
        for m in MEMOS:  # capture still happens — as `none` stubs
            body = (bed.raw / f"{m['uid']}.md").read_text()
            self.assertIn("transcript_source: none", body)
        report = bed.last_run.read_text()
        self.assertIn("blocked", report)
        self.assertIn("Speech Recognition is denied", report)  # the doctor's reason

    def test_blocked_reasons_distinguish_notdetermined_from_denied(self):
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_NOT_DETERMINED, "doctor_exit": 4})
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("never asked", bed.last_run.read_text())
        self.assertIn("--request-auth", bed.last_run.read_text())

    def test_blocked_model_missing_reason_reaches_report(self):
        bed = self.bed({"doctor_stdout": DOCTOR_MODEL_MISSING, "doctor_exit": 5})
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not installed", bed.last_run.read_text())
        self.assertIn("--install-model", bed.last_run.read_text())

    def test_old_helper_without_doctor_proceeds_as_before(self):
        # A pre-doctor helper exits 2 on the unknown flag with no key=value
        # output. The ingest must not brick a previously-working setup.
        bed = self.bed({
            "doctor_stdout": "", "doctor_exit": 2,
            "default": {"stdout": "legacy transcript", "exit": 0},
        })
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("doctor=unknown", proc.stdout)
        self.assertEqual(len(bed.transcribe_calls()), 2)

    def test_helper_absent_stubs_everything(self):
        bed = Bed(self.root, MEMOS, helper_cfg=None)  # no helper script written
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("doctor=absent", proc.stdout)
        for m in MEMOS:
            self.assertIn("transcript_source: none", (bed.raw / f"{m['uid']}.md").read_text())

    # --- retry-stubs × doctor -------------------------------------------------

    def _freeze_stubs(self, bed: Bed):
        """First run with a blocked doctor → both memos land as `none` stubs."""
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for m in MEMOS:
            self.assertIn("transcript_source: none", (bed.raw / f"{m['uid']}.md").read_text())
        bed.calls_log.unlink(missing_ok=True)

    def test_retry_stubs_blocked_doctor_makes_no_attempts(self):
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4,
                        "default": {"stdout": "should never be produced", "exit": 0}})
        self._freeze_stubs(bed)
        before = {m["uid"]: (bed.raw / f"{m['uid']}.md").read_text() for m in MEMOS}
        proc = bed.run("--execute", "--retry-stubs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(bed.transcribe_calls(), [])
        self.assertIn("candidates:** 2", bed.last_run.read_text())
        self.assertIn("upgraded:** 0", bed.last_run.read_text())
        for m in MEMOS:  # stub provenance untouched
            self.assertEqual(before[m["uid"]], (bed.raw / f"{m['uid']}.md").read_text())

    def test_retry_stubs_unknown_doctor_makes_no_helper_attempts(self):
        # A pre-doctor helper fails OPEN for normal runs, but a retry pass
        # exists to confirm a FIXED environment — `unknown` doesn't count, so
        # no helper calls are burned.
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4})
        self._freeze_stubs(bed)
        (self.root / "fake_helper_config.json").write_text(json.dumps({
            "doctor_stdout": "", "doctor_exit": 2,
            "default": {"stdout": "should never be produced", "exit": 0},
        }))
        proc = bed.run("--execute", "--retry-stubs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(bed.transcribe_calls(), [])
        for m in MEMOS:
            self.assertIn("transcript_source: none", (bed.raw / f"{m['uid']}.md").read_text())

    def test_retry_stubs_blocked_doctor_still_applies_native_sidecar(self):
        # Native upgrades (Apple-written sidecar next to the .m4a) need no
        # helper, so they apply even while the doctor is blocked — with zero
        # helper transcription calls.
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4,
                        "default": {"stdout": "should never be produced", "exit": 0}})
        self._freeze_stubs(bed)
        (bed.audio_root / "a.txt").write_text("apple wrote this transcript later")
        proc = bed.run("--execute", "--retry-stubs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(bed.transcribe_calls(), [])
        body_a = (bed.raw / "AAAA-1111.md").read_text()
        self.assertIn("transcript_source: native", body_a)
        self.assertIn("apple wrote this transcript later", body_a)
        self.assertIn("transcript_source: none", (bed.raw / "BBBB-2222.md").read_text())
        self.assertIn("upgraded:** 1", bed.last_run.read_text())

    def test_retry_stubs_ready_doctor_upgrades(self):
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4})
        self._freeze_stubs(bed)
        (self.root / "fake_helper_config.json").write_text(json.dumps({
            "doctor_stdout": DOCTOR_READY, "doctor_exit": 0,
            "default": {"stdout": "recovered transcript", "exit": 0},
        }))
        proc = bed.run("--execute", "--retry-stubs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("candidates:** 2", bed.last_run.read_text())
        self.assertIn("upgraded:** 2", bed.last_run.read_text())
        for m in MEMOS:
            body = (bed.raw / f"{m['uid']}.md").read_text()
            self.assertIn("transcript_source: on-device", body)
            self.assertIn("recovered transcript", body)

    def test_ready_doctor_but_single_file_still_stalls(self):
        # Doctor is green, yet one file stalls (exit 7): that stub must stay a
        # stub (no garbage over provenance) while the other upgrades; the
        # per-file failure is surfaced as an error, and the run exits 1.
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4})
        self._freeze_stubs(bed)
        (self.root / "fake_helper_config.json").write_text(json.dumps({
            "doctor_stdout": DOCTOR_READY, "doctor_exit": 0,
            "files": {
                "a.m4a": {"stderr": "atlas-transcribe: no transcription progress within 45s", "exit": 7},
                "b.m4a": {"stdout": "b transcript", "exit": 0},
            },
        }))
        proc = bed.run("--execute", "--retry-stubs")
        self.assertIn("transcript_source: none", (bed.raw / "AAAA-1111.md").read_text())
        self.assertIn("transcript_source: on-device", (bed.raw / "BBBB-2222.md").read_text())
        self.assertIn("upgraded:** 1", bed.last_run.read_text())
        self.assertIn("exit 7", bed.last_run.read_text())
        self.assertEqual(proc.returncode, 1)  # real per-file errors still fail the run

    def test_empty_transcript_is_honest_stub_not_error(self):
        # A no-speech recording: helper exits 0 with empty stdout (engine
        # parity contract). The memo stubs as `none` with NO error and the run
        # exits 0 — honest silence must not fail the nightly.
        bed = self.bed({
            "doctor_stdout": DOCTOR_READY, "doctor_exit": 0,
            "default": {"stdout": "", "exit": 0},
        })
        proc = bed.run("--execute")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("errors=0", proc.stdout)
        for m in MEMOS:
            self.assertIn("transcript_source: none", (bed.raw / f"{m['uid']}.md").read_text())

    def test_locale_reaches_doctor_and_transcription(self):
        # ATLAS_VM_LOCALE must be plumbed into BOTH helper invocation shapes.
        bed = self.bed({
            "doctor_stdout": DOCTOR_READY, "doctor_exit": 0,
            "default": {"stdout": "bonjour", "exit": 0},
        })
        proc = bed.run("--execute", locale="fr-FR")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for call in bed.doctor_calls() + bed.transcribe_calls():
            self.assertIn("--locale", call)
            self.assertEqual(call[call.index("--locale") + 1], "fr-FR", call)
        self.assertEqual(len(bed.transcribe_calls()), 2)

    # --- ingest.py --doctor ---------------------------------------------------

    def test_ingest_doctor_mode_ready(self):
        bed = self.bed({"doctor_stdout": DOCTOR_READY, "doctor_exit": 0})
        proc = bed.run("--doctor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("verdict=ready", proc.stdout)
        self.assertIn("speech_auth=authorized", proc.stdout)

    def test_ingest_doctor_mode_blocked(self):
        bed = self.bed({"doctor_stdout": DOCTOR_MODEL_MISSING, "doctor_exit": 5})
        proc = bed.run("--doctor")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("verdict=blocked", proc.stdout)
        self.assertIn("blocked_by=model", proc.stdout)

    # --- dry-run --------------------------------------------------------------

    def test_dry_run_report_carries_doctor_verdict(self):
        bed = self.bed({"doctor_stdout": DOCTOR_SPEECH_DENIED, "doctor_exit": 4})
        report = self.root / "dryrun.md"
        proc = bed.run("--dry-run-report", str(report))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(bed.transcribe_calls(), [])  # dry-run never transcribes
        self.assertIn("Transcription doctor: **blocked**", report.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
