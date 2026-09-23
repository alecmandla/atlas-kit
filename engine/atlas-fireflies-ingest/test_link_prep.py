#!/usr/bin/env python3
"""Tests for link_prep.py — stdlib-only, no framework (matches test_summary_record.py)."""

import sys
import tempfile
from pathlib import Path

import link_prep as lp

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def write(p: Path, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


PREP_FM = (
    "---\n"
    "date: 2026-07-16\n"
    "type: prep\n"
    "prev_meeting: '[[2026-07-09 — Pinecrest Lodge weekly sync]]'\n"
    "call_note: '[[2026-07-16 — Pinecrest Lodge weekly sync]]'\n"
    "tags:\n- meeting-prep\n"
    "---\n\n"
    "# 2026-07-16 — Pinecrest Lodge weekly sync (Prep)\n\n"
    "## Notes (fill during call)\n- live note the user typed\n"
)

CALL_FOLDER = "20 - Projects/Clients/Pinecrest-Lodge/meetings"


def setup(tmp: Path, prep_names, prep_text=PREP_FM):
    folder = tmp / CALL_FOLDER
    for n in prep_names:
        write(folder / n, prep_text)
    return folder


# --- 1. exactly one prep → links, prints prep_note, preserves body ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    folder = setup(tmp, ["2026-07-16 — Pinecrest Lodge weekly sync (prep).md"])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("single: exit 0", code == 0)
    check("single: prep_note line returned",
          line == 'prep_note: "[[2026-07-16 — Pinecrest Lodge weekly sync (prep)]]"')
    body = (folder / "2026-07-16 — Pinecrest Lodge weekly sync (prep).md").read_text(encoding="utf-8")
    check("single: call_note rewritten to real basename",
          'call_note: "[[2026-07-16 — Pinecrest Lodge weekly sync]]"' in body)
    check("single: live notes in body preserved", "live note the user typed" in body)
    check("single: only one call_note line", body.count("call_note:") == 1)

# --- 2. zero preps → skip, no link ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    setup(tmp, [])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("zero: exit 0", code == 0)
    check("zero: no prep_note line", line is None)

# --- 3. two same-date preps → ambiguous, link none ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    folder = setup(tmp, ["2026-07-16 — Pinecrest Lodge weekly sync (prep).md",
                         "2026-07-16 — Other Call (prep).md"])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("ambiguous: exit 0", code == 0)
    check("ambiguous: no prep_note line", line is None)
    untouched = (folder / "2026-07-16 — Pinecrest Lodge weekly sync (prep).md").read_text(encoding="utf-8")
    check("ambiguous: prep left untouched",
          "call_note: '[[2026-07-16 — Pinecrest Lodge weekly sync]]'" in untouched)

# --- 4. different-date prep → not matched (date join key) ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    setup(tmp, ["2026-07-09 — Pinecrest Lodge weekly sync (prep).md"])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("other-date: not matched", line is None)

# --- 5. title drift: prep title differs, same date → still links ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    folder = setup(tmp, ["2026-07-16 — Weekly Sync (prep).md"])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("drift: links despite title mismatch",
          line == 'prep_note: "[[2026-07-16 — Weekly Sync (prep)]]"')
    body = (folder / "2026-07-16 — Weekly Sync (prep).md").read_text(encoding="utf-8")
    check("drift: prep call_note points at real call",
          'call_note: "[[2026-07-16 — Pinecrest Lodge weekly sync]]"' in body)

# --- 6. prep with no call_note key → inserts one ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    no_key = "---\ndate: 2026-07-16\ntype: prep\ntags:\n- meeting-prep\n---\n\n# Prep\nnotes\n"
    folder = setup(tmp, ["2026-07-16 — Fresh (prep).md"], prep_text=no_key)
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    body = (folder / "2026-07-16 — Fresh (prep).md").read_text(encoding="utf-8")
    check("insert: call_note added when absent",
          'call_note: "[[2026-07-16 — Pinecrest Lodge weekly sync]]"' in body)
    check("insert: added right after type:",
          "type: prep\ncall_note:" in body)

# --- 7. idempotent: second run reports already-current, still prints prep_note ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    setup(tmp, ["2026-07-16 — Pinecrest Lodge weekly sync (prep).md"])
    lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=False)
    check("idempotent: still returns prep_note on rerun",
          line == 'prep_note: "[[2026-07-16 — Pinecrest Lodge weekly sync (prep)]]"')

# --- 8. dry-run: prints line but does not mutate the prep ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    folder = setup(tmp, ["2026-07-16 — Pinecrest Lodge weekly sync (prep).md"])
    code, line = lp.run(CALL_FOLDER, "2026-07-16 — Pinecrest Lodge weekly sync", tmp, dry_run=True)
    check("dry-run: prep_note line returned", line is not None)
    body = (folder / "2026-07-16 — Pinecrest Lodge weekly sync (prep).md").read_text(encoding="utf-8")
    check("dry-run: prep NOT mutated",
          "call_note: '[[2026-07-16 — Pinecrest Lodge weekly sync]]'" in body)

# --- 9. bad basename (no date) → error exit 2 ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    setup(tmp, [])
    code, line = lp.run(CALL_FOLDER, "No Date Here", tmp, dry_run=False)
    check("bad-basename: exit 2", code == 2)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
    sys.exit(1)
print("all passed")
