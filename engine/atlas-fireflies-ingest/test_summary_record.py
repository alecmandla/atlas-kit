#!/usr/bin/env python3
"""Tests for summary_record.py — external behavior only: given a meeting JSON
payload and a prior file state, assert the resulting file content and exit
status (enrichment PRD, testing decisions). Run under dep-free system python3."""

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summary_record as sr

failures = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


def vault():
    v = Path(tempfile.mkdtemp(prefix="sr-test-vault-"))
    (v / "raw" / "fireflies").mkdir(parents=True)
    return v


def raw(v, mid):
    return v / "raw" / "fireflies" / f"{mid}.md"


def run_apply(v, payload, dry_run=False):
    """Drive the real CLI entrypoint; returns (exit_code, {id: file_text})."""
    f = v / "payload.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    argv = ["apply", "--payload", str(f), "--vault", str(v)]
    if dry_run:
        argv.append("--dry-run")
    code = sr.main(argv)
    return code


KNOWN_STATUSES = {"fresh", "enriched", "already-current", "stub-written",
                  "no-content", "conflict", "error", "unavailable-marked"}


def run_capture(argv):
    """Run a CLI command, capture stdout, return (exit_code, stdout_text)."""
    buf = io.StringIO()
    _stdout = sys.stdout
    sys.stdout = buf
    try:
        code = sr.main(argv)
    finally:
        sys.stdout = _stdout
    return code, buf.getvalue()


def run_capture_all(argv):
    """Run a CLI command, capture stdout AND stderr; returns (code, out, err)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    _o, _e = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        code = sr.main(argv)
    finally:
        sys.stdout, sys.stderr = _o, _e
    return code, out_buf.getvalue(), err_buf.getvalue()


def apply_statuses(v, payload, dry_run=False):
    """Run apply, parse the per-meeting report; returns (exit_code, {mid: status})."""
    f = v / "payload.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    argv = ["apply", "--payload", str(f), "--vault", str(v)]
    if dry_run:
        argv.append("--dry-run")
    code, text = run_capture(argv)
    statuses = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in KNOWN_STATUSES:
            statuses[parts[1]] = parts[0]
    return code, statuses


MEETING = {
    "id": "01TESTFRESH000000000000000",
    "title": "Pinecrest Lodge kickoff",
    "date": 1751035800000,  # 2025-06-27 14:50:00 UTC
    "meeting_note": "2025-06-27 — Pinecrest Lodge kickoff",
    "participants": ["priya@pinecrestlodge.example", "jordan.vale@harborlane.example"],
    "meeting_attendees": [
        {"displayName": "Priya Okafor", "email": "priya@pinecrestlodge.example"},
        {"displayName": "Jordan Vale", "email": "jordan.vale@harborlane.example"},
    ],
    "transcript_url": "https://app.fireflies.ai/view/01TESTFRESH000000000000000",
    "summary": {
        "overview": "Kickoff covering scope and timeline.",
        "action_items": "**Jordan Vale**\n- [ ] Send SOW (03:14)",
        "keywords": ["kickoff", "scope"],
    },
}

STUB = (
    "---\n"
    "type: raw-fireflies\n"
    "meeting_id: {mid}\n"
    'meeting_note: "[[2025-06-27 — Pinecrest Lodge kickoff]]"\n'
    "fireflies_url: https://app.fireflies.ai/view/{mid}\n"
    "date: 2025-06-27\n"
    "transcript_status: not-retained-locally\n"
    "extracted_at: 2026-05-20\n"
    "---\n\n"
    "# 2025-06-27 — Pinecrest Lodge kickoff\n\n"
    "Transcript not retained locally.\n\n"
    "This meeting's full transcript lives at Fireflies: [view](https://app.fireflies.ai/view/{mid}).\n\n"
    "Original meeting note: [[2025-06-27 — Pinecrest Lodge kickoff]]\n"
)

# A pre-DEC-034 conversion (convert_retained_to_summary.py shape): transcript_status
# summary, speakers:/converted_at: keys, hand-shaped Overview, legacy pointer with no
# machine escalation block, and crucially NO enriched_at:. Mirrors the shape of
# a real pre-conversion record.
LEGACY = (
    "---\n"
    "type: raw-fireflies\n"
    "meeting_id: {mid}\n"
    'meeting_note: "[[2026-05-29 — Ledgerline retail kickoff]]"\n'
    "fireflies_url: https://app.fireflies.ai/view/{mid}\n"
    "date: 2026-05-29 20:00:00.000000+00:00\n"
    "transcript_status: summary\n"
    "extracted_at: 2026-06-01\n"
    "speakers: [Jordan Vale, Priya Okafor, Marcus Hale]\n"
    "keywords: [Email marketing, ActiveCampaign]\n"
    "converted_at: 2026-07-09\n"
    "---\n\n"
    "# 2026-05-29 — Ledgerline retail kickoff\n\n"
    "**Keywords:** Email marketing, ActiveCampaign\n\n"
    "## Overview\n"
    "Hand-curated overview carried over from the meeting note.\n\n"
    "## Action Items\n"
    "**Jordan Vale**\n- [ ] Resize large images (08:27)\n\n"
    "---\n"
    "Full transcript lives in Fireflies (fetch via API on demand): [view](https://app.fireflies.ai/view/{mid})\n"
)

# --- 1. fresh write ---
v = vault()
code = run_apply(v, MEETING)
out = raw(v, MEETING["id"]).read_text(encoding="utf-8")
check("fresh: exit 0", code == 0)
check("fresh: transcript_status summary", "transcript_status: summary" in out)
check("fresh: meeting_note wikilink", 'meeting_note: "[[2025-06-27 — Pinecrest Lodge kickoff]]"' in out)
check("fresh: date from epoch ms", "date: 2025-06-27 14:50:00.000000+00:00" in out)
check("fresh: attendee_emails block sorted",
      "attendee_emails:\n- jordan.vale@harborlane.example\n- priya@pinecrestlodge.example" in out)
check("fresh: participants names", "participants: [Jordan Vale, Priya Okafor]" in out)
check("fresh: keywords fm + body line",
      "keywords: [kickoff, scope]" in out and "**Keywords:** kickoff, scope" in out)
check("fresh: attendees body line", "**Attendees:** Jordan Vale <jordan.vale@harborlane.example>, Priya Okafor <priya@pinecrestlodge.example>" in out)
check("fresh: overview + action items sections",
      "## Overview\nKickoff covering scope and timeline." in out
      and "## Action Items\n**Jordan Vale**\n- [ ] Send SOW (03:14)" in out)
check("fresh: pointer text kept", "Full transcript lives in Fireflies (fetch via API on demand): [view](https://app.fireflies.ai/view/01TESTFRESH000000000000000)" in out)
check("fresh: machine escalation instruction",
      "`fireflies_get_transcript`" in out and '`transcriptId: "01TESTFRESH000000000000000"`' in out
      and "Never\nopen the `fireflies_url` link" in out)
check("fresh: keywords+overview inside first 2500 chars",
      out.find("## Overview") != -1 and out.find("## Overview") < 2500 and out.find("**Keywords:**") < 2500)

# --- 2. stub -> enriched upgrade (preserves join keys) ---
v = vault()
mid = "01TESTSTUB0000000000000000"
payload = dict(MEETING, id=mid, transcript_url="", date=None)
payload.pop("meeting_note")
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
code = run_apply(v, payload)
out = raw(v, mid).read_text(encoding="utf-8")
check("upgrade: exit 0", code == 0)
check("upgrade: became summary record", "transcript_status: summary" in out and "Transcript not retained locally." not in out)
check("upgrade: meeting_note preserved from stub", 'meeting_note: "[[2025-06-27 — Pinecrest Lodge kickoff]]"' in out)
check("upgrade: date preserved from stub when payload lacks it", "date: 2025-06-27\n" in out)
check("upgrade: fireflies_url preserved", f"fireflies_url: https://app.fireflies.ai/view/{mid}" in out)
check("upgrade: extracted_at preserved, enriched_at added",
      "extracted_at: 2026-05-20" in out and "\nenriched_at: " in out)
check("upgrade: H1 is note basename", "# 2025-06-27 — Pinecrest Lodge kickoff" in out)

# --- 3. already-enriched no-op (idempotency) ---
before = out
code = run_apply(v, payload)
after = raw(v, mid).read_text(encoding="utf-8")
check("idempotent: exit 0", code == 0)
check("idempotent: byte-identical on re-run", after == before)

# --- 4. missing/empty summary fields degrade gracefully ---
v = vault()
mid = "01TESTPARTIAL0000000000000"
partial = dict(MEETING, id=mid, transcript_url="",
               summary={"overview": "Only an overview.", "action_items": "", "keywords": []})
run_apply(v, partial)
out = raw(v, mid).read_text(encoding="utf-8")
check("partial: overview kept, empty sections omitted",
      "## Overview\nOnly an overview." in out and "## Action Items" not in out
      and "**Keywords:**" not in out and "keywords:" not in out)

# fully-empty payload -> link-only stub, not an enriched record
v = vault()
mid = "01TESTEMPTY000000000000000"
empty = {"id": mid, "title": "Ghost", "meeting_note": "2025-01-01 — Ghost", "summary": {}}
code = run_apply(v, empty)
out = raw(v, mid).read_text(encoding="utf-8")
check("empty: writes stub with not-retained-locally", code == 0
      and "transcript_status: not-retained-locally" in out
      and "Transcript not retained locally." in out)
# empty payload against an existing enriched record leaves it alone
v = vault()
mid = MEETING["id"]
run_apply(v, MEETING)
before = raw(v, mid).read_text(encoding="utf-8")
code = run_apply(v, {"id": mid, "summary": {}})
check("empty-vs-enriched: existing record untouched, exit 0",
      code == 0 and raw(v, mid).read_text(encoding="utf-8") == before)

# --- 5. attendee names absent -> emails only ---
v = vault()
mid = "01TESTNONAMES0000000000000"
noname = dict(MEETING, id=mid, meeting_attendees=[], participants=["a@example.com", "b@example.com"])
run_apply(v, noname)
out = raw(v, mid).read_text(encoding="utf-8")
check("no-names: attendee_emails block present", "attendee_emails:\n- a@example.com\n- b@example.com" in out)
check("no-names: participants emitted as empty list (always-emit, DEC-034)", "participants: []" in out)
check("no-names: attendees line uses bare emails", "**Attendees:** a@example.com, b@example.com" in out)

# --- 6. unicode / odd titles ---
v = vault()
mid = "01TESTUNICODE0000000000000"
odd = dict(MEETING, id=mid,
           title='Café "Läunch" — 50% ready?',
           meeting_note='2025-06-27 — Café "Läunch" — 50% ready?')
run_apply(v, odd)
out = raw(v, mid).read_text(encoding="utf-8")
check("unicode: H1 carries the odd title", '# 2025-06-27 — Café "Läunch" — 50% ready?' in out)
check("unicode: meeting_note quotes escaped",
      'meeting_note: "[[2025-06-27 — Café \\"Läunch\\" — 50% ready?]]"' in out)
check("unicode: still a summary record", "transcript_status: summary" in out)

# --- 7. never-overwrite guard ---
v = vault()
mid = "01TESTCONFLICT000000000000"
alien = "---\ntype: raw-fireflies\nmeeting_id: %s\ntranscript_status: retained\n---\n\nA full transcript someone kept.\n" % mid
raw(v, mid).write_text(alien, encoding="utf-8")
code = run_apply(v, dict(MEETING, id=mid))
check("guard: retained body untouched", raw(v, mid).read_text(encoding="utf-8") == alien)
check("guard: conflict exits non-zero", code == 1)
noframe = "just some prose, no frontmatter\n"
raw(v, mid).write_text(noframe, encoding="utf-8")
code = run_apply(v, dict(MEETING, id=mid))
check("guard: fenceless body untouched + non-zero",
      raw(v, mid).read_text(encoding="utf-8") == noframe and code == 1)

# --- 8. atomicity: a failed write leaves no partial file, original intact ---
v = vault()
mid = "01TESTATOMIC00000000000000"
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
orig_replace = sr.os.replace
sr.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated failure"))
try:
    code = run_apply(v, dict(MEETING, id=mid))
finally:
    sr.os.replace = orig_replace
check("atomic: error reported, exit non-zero", code == 1)
check("atomic: original stub intact", raw(v, mid).read_text(encoding="utf-8") == STUB.format(mid=mid))
check("atomic: no tmp litter", not list((v / "raw" / "fireflies").glob(".sr-*")))

# --- 9. dry-run writes nothing ---
v = vault()
code = run_apply(v, MEETING, dry_run=True)
check("dry-run: exit 0, no file written", code == 0 and not raw(v, MEETING["id"]).exists())

# --- 10. mark-unavailable ---
v = vault()
mid = "01TESTUNAVAIL0000000000000"
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
code = sr.main(["mark-unavailable", mid, "--vault", str(v)])
out = raw(v, mid).read_text(encoding="utf-8")
check("unavailable: exit 0, stub annotated", code == 0
      and "unavailable_at_source: " in out
      and "no longer available" in out
      and "Transcript not retained locally." in out)
code = sr.main(["mark-unavailable", mid, "--vault", str(v)])
check("unavailable: second run is a no-op",
      code == 0 and raw(v, mid).read_text(encoding="utf-8") == out)
# refuses to annotate an enriched record
run_apply(v, MEETING)
before = raw(v, MEETING["id"]).read_text(encoding="utf-8")
code = sr.main(["mark-unavailable", MEETING["id"], "--vault", str(v)])
check("unavailable: refuses summary record (conflict, non-zero)",
      code == 1 and raw(v, MEETING["id"]).read_text(encoding="utf-8") == before)

# --- 11. pending lists stubs, skips enriched + unavailable ---
v = vault()
raw(v, "01AAA").write_text(STUB.format(mid="01AAA"), encoding="utf-8")
raw(v, "01BBB").write_text(STUB.format(mid="01BBB"), encoding="utf-8")
sr.main(["mark-unavailable", "01BBB", "--vault", str(v)])
run_apply(v, MEETING)
import io
buf = io.StringIO()
_stdout = sys.stdout
sys.stdout = buf
try:
    sr.main(["pending", "--vault", str(v)])
finally:
    sys.stdout = _stdout
pend = buf.getvalue()
check("pending: lists the plain stub only",
      "01AAA" in pend and "01BBB" not in pend and MEETING["id"] not in pend)

# --- 12. connector-shaped payload (dateString / meetingAttendees / short_summary) ---
v = vault()
mid = "01TESTCONNECTOR00000000000"
connector = {
    "id": mid,
    "title": "Connector Shape",
    "dateString": "2026-07-06T15:02:42.308Z",
    "meeting_note": "2026-07-06 — Connector Shape",
    "meetingAttendees": [{"displayName": None, "email": "marcus.hale@ledgerline.example"}],
    "participants": ["jordan.vale@harborlane.example", "marcus.hale@ledgerline.example"],
    "summary": {"short_summary": "Connector overview text.",
                "keywords": ["room rates"], "action_items": "\n**Marcus Hale**\nDo the thing (16:55)\n"},
}
run_apply(v, connector)
out = raw(v, mid).read_text(encoding="utf-8")
check("connector: dateString normalized", "date: 2026-07-06 15:02:42.308000+00:00" in out)
check("connector: camelCase attendees + participants merged",
      "attendee_emails:\n- jordan.vale@harborlane.example\n- marcus.hale@ledgerline.example" in out)
check("connector: short_summary becomes Overview", "## Overview\nConnector overview text." in out)
check("connector: action items stripped of padding", "## Action Items\n**Marcus Hale**\nDo the thing (16:55)\n" in out)

# ============================================================================
# Review-fix regression tests (F1-F6). Each closes a mutation the reviews found
# passing under the pre-fix code.
# ============================================================================

# --- T1 (F1): contentless payload (participants but empty/null summary) never
#     guts a record, and never silently converts a stub into a contentless
#     summary that pending would then drop. ---
# (a) against an enriched record -> no-content, byte-unchanged
v = vault()
mid = MEETING["id"]
run_apply(v, MEETING)
before = raw(v, mid).read_text(encoding="utf-8")
code, st = apply_statuses(v, {"id": mid, "participants": ["ghost@example.com"], "summary": {}})
check("T1a: contentless over enriched -> no-content", st.get(mid) == "no-content")
check("T1a: enriched file byte-unchanged", raw(v, mid).read_text(encoding="utf-8") == before and code == 0)

# (b) against a stub -> no-content, stub unchanged and STILL pending
v = vault()
mid = "01T1BSTUB00000000000000000"
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
stub_before = raw(v, mid).read_text(encoding="utf-8")
code, st = apply_statuses(v, {"id": mid, "participants": ["ghost@example.com"], "summary": {}})
check("T1b: contentless over stub -> no-content", st.get(mid) == "no-content")
check("T1b: stub byte-unchanged", raw(v, mid).read_text(encoding="utf-8") == stub_before)
_, pend = run_capture(["pending", "--vault", str(v)])
check("T1b: stub still listed by pending", mid in pend)

# (c) fresh (no existing file) -> link-only stub written
v = vault()
mid = "01T1CFRESH0000000000000000"
code, st = apply_statuses(v, {"id": mid, "title": "Ghost", "meeting_note": "2025-01-01 — Ghost",
                              "participants": ["ghost@example.com"], "summary": {}})
out = raw(v, mid).read_text(encoding="utf-8")
check("T1c: contentless fresh -> stub-written", st.get(mid) == "stub-written")
check("T1c: wrote a not-retained-locally stub",
      "transcript_status: not-retained-locally" in out and "Transcript not retained locally." in out)

# (d) summary: null must not crash parse_meeting
v = vault()
mid = "01T1DNULL00000000000000000"
code, st = apply_statuses(v, {"id": mid, "title": "NullSummary",
                              "meeting_note": "2025-01-01 — NullSummary", "summary": None})
check("T1d: null summary handled, no traceback -> stub-written",
      code == 0 and st.get(mid) == "stub-written")

# --- T2 (F1 layer 2): a payload that passes has_content via keywords only, over
#     a record with a populated Overview, is refused (never gutted). ---
v = vault()
mid = MEETING["id"]
run_apply(v, MEETING)  # populated ## Overview + ## Action Items
before = raw(v, mid).read_text(encoding="utf-8")
code, st = apply_statuses(v, {"id": mid, "summary": {"keywords": ["latekw"], "overview": "", "action_items": ""}})
check("T2: keywords-only over populated Overview -> no-content", st.get(mid) == "no-content")
check("T2: populated body byte-unchanged", raw(v, mid).read_text(encoding="utf-8") == before and code == 0)

# --- T11 (F2): pending lists a legacy summary (no enriched_at) tagged
#     legacy-summary, and stops listing it once it is enriched. ---
v = vault()
mid = "01T11LEGACY000000000000000"
raw(v, mid).write_text(LEGACY.format(mid=mid), encoding="utf-8")
_, pend = run_capture(["pending", "--vault", str(v)])
check("T11: legacy summary appears in pending, tagged legacy-summary",
      mid in pend and "legacy-summary" in pend)
run_apply(v, dict(MEETING, id=mid))  # re-render to DEC-034 format
out = raw(v, mid).read_text(encoding="utf-8")
check("T11: enrichment adds enriched_at", "enriched_at: " in out)
_, pend2 = run_capture(["pending", "--vault", str(v)])
check("T11: no longer pending after enrichment", mid not in pend2)

# --- T6b (F3): a meeting id with path separators is rejected; nothing is
#     written outside raw/fireflies/. ---
v = vault()
bad_id = "../../escaped-note"
code, st = apply_statuses(v, {"id": bad_id, "meeting_note": "x",
                              "summary": {"overview": "should never be written"}})
check("T6b: path-traversal id -> error", st.get(bad_id) == "error")
check("T6b: batch exit non-zero", code == 1)
check("T6b: no file escaped to the vault root", not (v / "escaped-note.md").exists())
check("T6b: nothing written under raw/fireflies either",
      not list((v / "raw" / "fireflies").glob("*.md")))
# mark-unavailable rejects it the same way
code2, _ = run_capture(["mark-unavailable", bad_id, "--vault", str(v)])
check("T6b: mark-unavailable rejects path-y id, exit non-zero",
      code2 == 1 and not (v / "escaped-note.md").exists())

# --- T9 (F4): pathological keywords are all quoted + backslash-escaped, and the
#     frontmatter stays valid YAML. ---
v = vault()
mid = "01T9YAMLHOLES00000000000000"
kws = ["back\\slash", "*star", "- dash", "null", "?q", "safe"]
run_apply(v, dict(MEETING, id=mid, summary={"overview": "o", "keywords": kws}))
out = raw(v, mid).read_text(encoding="utf-8")
kwline = next(l for l in out.splitlines() if l.startswith("keywords:"))
check("T9: backslash doubled inside quotes", '"back\\\\slash"' in kwline)
check("T9: leading-indicator items quoted",
      '"*star"' in kwline and '"- dash"' in kwline and '"?q"' in kwline)
check("T9: bare null quoted (not type-corrupted)", '"null"' in kwline)
check("T9: safe item stays plain", ", safe]" in kwline)
try:
    import yaml
    fm_text = out.split("---\n", 2)[1]
    parsed = yaml.safe_load(fm_text)
    check("T9: PyYAML oracle round-trips keywords exactly", parsed.get("keywords") == kws)
except ImportError:
    check("T9: PyYAML absent — structural quoting asserted above", True)

# --- T3 (idempotency, real): a record with a rewound enriched_at re-applies as
#     already-current, byte-unchanged (old date preserved). Kills the mutation
#     that drops strip_volatile — same-day re-runs never exercise it. ---
v = vault()
mid = MEETING["id"]
run_apply(v, MEETING)
rendered = raw(v, mid).read_text(encoding="utf-8")
rewound = "\n".join(
    ("enriched_at: 2020-01-01" if l.startswith("enriched_at:") else l)
    for l in rendered.split("\n"))
raw(v, mid).write_text(rewound, encoding="utf-8")
code, st = apply_statuses(v, MEETING)
after = raw(v, mid).read_text(encoding="utf-8")
check("T3: re-apply with a past enriched_at -> already-current", st.get(mid) == "already-current")
check("T3: byte-unchanged, old enriched_at preserved",
      after == rewound and "enriched_at: 2020-01-01" in after and code == 0)

# --- T4 (batch + report): fresh / conflict / id-less in one call; assert the
#     three statuses, the counts line, exit 1, and that the fresh file survived. ---
v = vault()
fresh_id, conflict_id = "01T4FRESH00000000000000000", "01T4CONFLICT00000000000000"
raw(v, conflict_id).write_text(
    "---\ntype: raw-fireflies\nmeeting_id: %s\ntranscript_status: retained\n---\n\nKept.\n" % conflict_id,
    encoding="utf-8")
batch = {"transcripts": [
    dict(MEETING, id=fresh_id),
    dict(MEETING, id=conflict_id),
    {"title": "no id here", "summary": {"overview": "x"}},
]}
pf = v / "batch.json"
pf.write_text(json.dumps(batch), encoding="utf-8")
code, text, _ = run_capture_all(["apply", "--payload", str(pf), "--vault", str(v)])
st = {}
for line in text.splitlines():
    parts = line.split()
    if len(parts) >= 2 and parts[0] in KNOWN_STATUSES:
        st[parts[1]] = parts[0]
check("T4: fresh entry -> fresh", st.get(fresh_id) == "fresh")
check("T4: alien-body entry -> conflict", st.get(conflict_id) == "conflict")
check("T4: id-less entry -> error", st.get("?") == "error")
check("T4: counts line present", "conflict=1" in text and "error=1" in text and "fresh=1" in text)
check("T4: batch exit 1 (conflict+error present)", code == 1)
check("T4: fresh file still written despite failing siblings", raw(v, fresh_id).exists())

# --- T5 (legacy DEC-032 conversion): a full payload re-renders it to DEC-034;
#     legacy keys dropped, join keys preserved, machine block gained. ---
v = vault()
mid = "01T5LEGACYCONV000000000000"
raw(v, mid).write_text(LEGACY.format(mid=mid), encoding="utf-8")
payload = dict(MEETING, id=mid, transcript_url="", date=None)
payload.pop("meeting_note")  # preserved from the legacy record, not the payload
code, st = apply_statuses(v, payload)
out = raw(v, mid).read_text(encoding="utf-8")
check("T5: legacy conversion -> enriched", st.get(mid) == "enriched")
check("T5: legacy speakers key dropped", "speakers:" not in out)
check("T5: legacy converted_at key dropped", "converted_at:" not in out)
check("T5: meeting_note preserved from legacy record",
      'meeting_note: "[[2026-05-29 — Ledgerline retail kickoff]]"' in out)
check("T5: date preserved from legacy record", "date: 2026-05-29 20:00:00.000000+00:00" in out)
check("T5: extracted_at preserved", "extracted_at: 2026-06-01" in out)
check("T5: enriched_at now present", "enriched_at: " in out)
check("T5: machine escalation block present",
      "`fireflies_get_transcript`" in out and "Never\nopen the `fireflies_url` link" in out)

# --- T6a (marker half of classify): a not-retained-locally file whose stub
#     marker line was hand-edited away is a conflict, left untouched. ---
v = vault()
mid = "01T6AMARKER0000000000000000"
broken = STUB.format(mid=mid).replace("Transcript not retained locally.\n\n", "")
raw(v, mid).write_text(broken, encoding="utf-8")
code, st = apply_statuses(v, dict(MEETING, id=mid))
check("T6a: not-retained-locally without marker -> conflict", st.get(mid) == "conflict")
check("T6a: file left untouched, exit 1",
      raw(v, mid).read_text(encoding="utf-8") == broken and code == 1)

# --- T7 (dry-run everywhere): the stub-upgrade path and mark-unavailable each
#     report their status and write nothing under --dry-run. ---
v = vault()
mid = "01T7DRYUPGRADE0000000000000"
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
before = raw(v, mid).read_text(encoding="utf-8")
code, st = apply_statuses(v, dict(MEETING, id=mid, date=None), dry_run=True)
check("T7: dry-run stub upgrade reports enriched", st.get(mid) == "enriched")
check("T7: dry-run stub upgrade wrote nothing, exit 0",
      raw(v, mid).read_text(encoding="utf-8") == before and code == 0)
code2, out2, _ = run_capture_all(["mark-unavailable", mid, "--vault", str(v), "--dry-run"])
check("T7: dry-run mark-unavailable reports unavailable-marked", "unavailable-marked" in out2)
check("T7: dry-run mark-unavailable wrote nothing, exit 0",
      raw(v, mid).read_text(encoding="utf-8") == before and code2 == 0)

# --- T8 (error contracts): missing id errors non-zero; malformed payload JSON
#     is reported cleanly (exit 2, no traceback). ---
v = vault()
code, out, _ = run_capture_all(["mark-unavailable", "01T8MISSING000000000000000", "--vault", str(v)])
check("T8: mark-unavailable on missing id -> error, exit 1", code == 1 and "error" in out)
bad = v / "bad.json"
bad.write_text("{not valid json", encoding="utf-8")
raised = False
try:
    code2, _, err2 = run_capture_all(["apply", "--payload", str(bad), "--vault", str(v)])
except Exception:
    raised = True
    code2, err2 = None, ""
check("T8: malformed JSON handled without traceback", not raised)
check("T8: malformed JSON exits 2 with an error: line", code2 == 2 and err2.startswith("error:"))

# --- T10 (F6): enriching a stub previously annotated unavailable_at_source
#     re-renders it and drops the annotation deliberately — a reappeared meeting
#     is good news, not a conflict (by design: enrich, don't refuse). ---
v = vault()
mid = "01T10REAPPEAR00000000000000"
raw(v, mid).write_text(STUB.format(mid=mid), encoding="utf-8")
run_capture_all(["mark-unavailable", mid, "--vault", str(v)])
annotated = raw(v, mid).read_text(encoding="utf-8")
check("T10: precondition — stub annotated unavailable",
      "unavailable_at_source: " in annotated and "no longer available" in annotated)
code, st = apply_statuses(v, dict(MEETING, id=mid, date=None))
out = raw(v, mid).read_text(encoding="utf-8")
check("T10: real content over unavailable stub -> enriched", st.get(mid) == "enriched")
check("T10: unavailable_at_source annotation dropped", "unavailable_at_source:" not in out)
check("T10: aged-out note gone from body", "no longer available" not in out)
check("T10: now a normal summary record", "transcript_status: summary" in out and code == 0)

# --- T12 (DEC-034 sign-off): junk/logger attendee emails are dropped from the
#     record, real attendees kept; always-emit participants holds the real names.
#     The shipped JUNK_EMAIL_DOMAINS is empty; inject a logger domain for the test. ---
sr.JUNK_EMAIL_DOMAINS = ("example.com",)
v = vault()
mid = "01T12JUNKEMAIL00000000000000"
junky = dict(MEETING, id=mid,
             meeting_attendees=[
                 {"displayName": "Marcus Hale", "email": "marcus.hale@ledgerline.example"},
                 {"displayName": None, "email": "2212975@example.com"},   # CRM BCC logger
             ],
             participants=["marcus.hale@ledgerline.example", "2212975@example.com", "99887766@saltmarshinn.example"])
run_apply(v, junky)
out = raw(v, mid).read_text(encoding="utf-8")
check("T12: crm bcc logger dropped from attendee_emails", "2212975@example.com" not in out)
check("T12: numeric-local logger dropped", "99887766@saltmarshinn.example" not in out)
check("T12: real attendee kept", "attendee_emails:\n- marcus.hale@ledgerline.example" in out
      and "\nparticipants: [Marcus Hale]" in out)
check("T12: attendees body line has no junk", "**Attendees:** Marcus Hale <marcus.hale@ledgerline.example>" in out
      and "example.com" not in out)

# --- T13 (always-emit participants): even with only bot emails, participants is
#     emitted (empty) and the record still degrades cleanly. ---
v = vault()
mid = "01T13ALLBOTS0000000000000000"
allbots = dict(MEETING, id=mid, meeting_attendees=[],
               participants=["2212975@example.com"],
               summary={"overview": "Just a bot in the room.", "action_items": "", "keywords": []})
run_apply(v, allbots)
out = raw(v, mid).read_text(encoding="utf-8")
check("T13: all-bot attendees -> participants []", "participants: []" in out)
check("T13: no attendee_emails block when only junk", "attendee_emails:" not in out)
check("T13: still a summary record (overview carries it)", "## Overview\nJust a bot in the room." in out)
sr.JUNK_EMAIL_DOMAINS = ()

# --- T14 (adversarial review): a NAMED attendee with a numeric-local email is
#     RETAINED — the numeric rule only fires on bare (nameless) addresses, so a
#     real person and their people-resolution join key are never silently erased.
#     A known logger domain is still dropped even with a name; bare numeric-local
#     is still dropped and surfaced in the report as an audit trail. ---
v = vault()
mid = "01T14NUMNAMED00000000000000"
run_apply(v, dict(MEETING, id=mid,
    meeting_attendees=[{"displayName": "Real Person", "email": "123456@example.com"}],
    participants=["123456@example.com"]))
out = raw(v, mid).read_text(encoding="utf-8")
check("T14: named numeric-local retained in attendee_emails", "attendee_emails:\n- 123456@example.com" in out)
check("T14: named numeric-local carried into participants", "participants: [Real Person]" in out)
check("T14: named numeric-local in Attendees body line", "**Attendees:** Real Person <123456@example.com>" in out)

# known logger domain is dropped even WITH a display name
sr.JUNK_EMAIL_DOMAINS = ("example.com",)
v = vault()
mid = "01T14LOGGERNAMED0000000000"
run_apply(v, dict(MEETING, id=mid,
    meeting_attendees=[{"displayName": "CRM Bot", "email": "42@example.com"},
                       {"displayName": "Priya Okafor", "email": "priya@pinecrestlodge.example"}],
    participants=["42@example.com", "priya@pinecrestlodge.example"]))
out = raw(v, mid).read_text(encoding="utf-8")
check("T14: named-but-logger-domain still dropped", "42@example.com" not in out)
check("T14: real named attendee kept alongside it", "attendee_emails:\n- priya@pinecrestlodge.example" in out)
sr.JUNK_EMAIL_DOMAINS = ()

# bare numeric-local (no name) still dropped, and the drop is surfaced in the report
v = vault()
mid = "01T14NUMBARE000000000000000"
pf = v / "p.json"
pf.write_text(json.dumps(dict(MEETING, id=mid, meeting_attendees=[],
    participants=["555001@example.com", "priya@pinecrestlodge.example"])), encoding="utf-8")
code, out_text, _ = run_capture_all(["apply", "--payload", str(pf), "--vault", str(v)])
out = raw(v, mid).read_text(encoding="utf-8")
check("T14: bare numeric-local (no name) still dropped, real kept",
      "555001@example.com" not in out and "attendee_emails:\n- priya@pinecrestlodge.example" in out)
check("T14: filtered-junk audit surfaced in apply report", "filtered 1 junk addr" in out_text)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("all tests passed")
