#!/usr/bin/env python3
"""Tests for atlas-gong-meetings-ingest — stdlib-only, no framework (matches
atlas-teams-meetings-ingest/test_ingest.py). Fixtures use the fictional world
from docs/SCRUB-RULES.md. Everything runs in temp dirs: the Atlas config comes
from ATLAS_CONFIG, and the script's state, lock, last-run, and sources files are
redirected before any run."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = tempfile.TemporaryDirectory()
ROOT = Path(TMP.name)
(ROOT / "atlas.json").write_text(json.dumps({"vault_root": str(ROOT / "default-vault"), "timezone": "America/Chicago"}))
os.environ["ATLAS_CONFIG"] = str(ROOT / "atlas.json")

import ingest  # noqa: E402
import fetch  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


ingest.STATE_FILE = ROOT / "state.json"
ingest.LAST_RUN = ROOT / "last-run.md"
ingest.RUN_LOCK = ROOT / ".run.lock"
ingest.SOURCES_FILE = ROOT / "gong-sources.json"
fetch.SOURCES_FILE = ingest.SOURCES_FILE

OWNER = "owner@harborlane.example"
SECRET_TITLE = "Saltmarsh Inn confidential escalation"


def sources(**extra):
    data = {"_comment": "test", "owner_emails": [OWNER]}
    data.update(extra)
    ingest.SOURCES_FILE.write_text(json.dumps(data))


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ingest.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def party(email, name, aff, sid):
    return {"id": "p" + sid, "emailAddress": email, "name": name, "title": "", "userId": None,
            "speakerId": sid, "affiliation": aff, "methods": ["Invitee"]}


CALL_A = {  # the owner's external call with full Gong AI content
    "metaData": {
        "id": "7782342274025937895", "url": "https://app.gong.io/call?id=7782342274025937895",
        "title": "Ledgerline retail kickoff", "scheduled": "2026-09-10T15:00:00-05:00",
        "started": "2026-09-10T14:57:12.345-05:00", "duration": 4520, "direction": "Conference",
        "system": "Zoom", "scope": "External", "media": "Video", "language": "eng",
        "workspaceId": "1", "isPrivate": False, "meetingUrl": "https://zoom.us/j/85012345678",
        "brandNewField": "ignored",
    },
    "parties": [
        party(OWNER, "Owner", "Internal", "s1"),
        party("jordan.vale@harborlane.example", "Jordan Vale", "Internal", "s2"),
        party("Marcus.Hale@ledgerline.example", "Marcus Hale", "External", "s3"),
    ],
    "content": {
        "brief": "Ledgerline's retail team wants Northstar-BI dashboards for twelve stores.",
        "keyPoints": [{"text": "Twelve stores in scope."}, {"text": "Pricing sent by Friday."}],
        "highlights": [
            {"title": "Next steps", "items": [{"text": "Send pricing to Marcus", "startTimes": [3900]}]},
            {"title": "Action items", "items": [{"text": "Jordan drafts the data map", "startTimes": [4000]}]},
            {"title": "Pricing discussion", "items": [{"text": "Per-store pricing preferred", "startTimes": [1200]}]},
        ],
        "outline": [
            {"section": "Introductions", "startTime": 0, "duration": 300, "items": [{"text": "Team intros", "startTime": 10}]},
            {"section": "Scope", "startTime": 754, "duration": 900, "items": [{"text": "Store count", "startTime": 760}]},
        ],
        "callOutcome": {"id": "o1", "category": "Sales", "name": "Discovery complete"},
        "topics": [{"name": "Pricing", "duration": 600}, {"name": "Small talk", "duration": 0}],
        "trackers": [{"id": "t1", "name": "Competitors", "count": 2, "type": "Smart"},
                     {"id": "t2", "name": "Budget", "count": 0, "type": "Keyword"}],
    },
    "context": [{"system": "Salesforce", "objects": [
        {"objectType": "Account", "objectId": "001", "fields": [{"name": "Name", "value": "Ledgerline"}]}]}],
    "transcript": [
        {"speakerId": "s3", "topic": "Pricing", "sentences": [{"start": 5000, "end": 7000, "text": "ZZ-SPOKEN-IN-A: hello."}]},
    ],
}

CALL_B = {  # the owner's call with no AI content; transcript comes from a separate file
    "metaData": {"id": "5550001111222233334", "url": "https://app.gong.io/call?id=5550001111222233334",
                 "title": "Pinecrest Lodge weekly sync", "started": "2026-09-11T15:00:00Z", "duration": 1800,
                 "direction": "Conference", "system": "Teams", "scope": "External", "isPrivate": False},
    "parties": [party(OWNER, "Owner", "Internal", "s1"),
                party("priya@pinecrestlodge.example", "Priya Okafor", "External", "s9")],
}
TRANSCRIPTS = {"requestId": "r2", "records": {"totalRecords": 2}, "callTranscripts": [
    {"callId": "5550001111222233334", "transcript": [
        {"speakerId": "s9", "topic": "", "sentences": [{"start": 5000, "end": 6000, "text": "The booking export is still failing."}]},
        {"speakerId": "s9", "topic": "", "sentences": [{"start": 6500, "end": 8000, "text": "Since Tuesday."}]},
        {"speakerId": "s1", "topic": "", "sentences": [{"start": 9000, "end": 9500, "text": "Thanks, looking now."}]},
        {"speakerId": "s77", "topic": "", "sentences": [{"start": 3723000, "end": 3724000, "text": "Who am I?"}]},
    ]},
    {"callId": "4440000000000000001", "transcript": [
        {"speakerId": "s1", "sentences": [{"start": 0, "end": 1, "text": "Orphan."}]}]},
]}

CALL_C = {  # someone else's call: must never reach the vault
    "metaData": {"id": "9990001112223334445", "title": SECRET_TITLE, "started": "2026-09-10T16:00:00Z",
                 "duration": 900, "isPrivate": False, "url": "https://app.gong.io/call?id=9990001112223334445"},
    "parties": [party("jordan.vale@harborlane.example", "Jordan Vale", "Internal", "s1"),
                party("marcus.hale@ledgerline.example", "Marcus Hale", "External", "s2")],
    "content": {"brief": "Confidential brief text."},
}

CALL_D = {  # the owner's private call
    "metaData": {"id": "3330001112223334446", "title": "Portal roadmap review", "started": "2026-09-12T15:00:00Z",
                 "duration": 900, "isPrivate": True},
    "parties": [party(OWNER, "Owner", "Internal", "s1")],
    "content": {"brief": "Private roadmap brief."},
}

EXTENSIVE = {"requestId": "r1", "records": {"totalRecords": 4, "currentPageSize": 4, "currentPageNumber": 0},
             "calls": [CALL_A, CALL_B, CALL_C, CALL_D]}


def all_text(folder: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in folder.rglob("*.md")) if folder.exists() else ""


d = ROOT / "work"
d.mkdir()
(d / "extensive.json").write_text(json.dumps(EXTENSIVE))
(d / "transcripts.json").write_text(json.dumps(TRANSCRIPTS))
vault = d / "vault"
raw = vault / "raw"
out_dir = raw / "gong" / "meetings"

# Cross-link fixtures: a Zoom record 3 min after call A (UTC 19:57:12), a far one,
# and a Fireflies record 2 min before call B (UTC 15:00:00).
(raw / "zoom" / "meetings").mkdir(parents=True)
(raw / "fireflies").mkdir(parents=True)
(raw / "zoom" / "meetings" / "2026-09-10-ledgerline-retail-kickoff-85012345.md").write_text(
    "---\nzoom_id: 85012345678\ntimestamp: 2026-09-10 20:00:05\n---\n")
(raw / "zoom" / "meetings" / "2026-09-10-other-meeting-11112222.md").write_text(
    "---\nzoom_id: 11112222333\ntimestamp: 2026-09-10 20:30:00\n---\n")
(raw / "fireflies" / "01ABCDEF.md").write_text(
    '---\nmeeting_id: 01ABCDEF\nmeeting_note: "[[2026-09-11 — Pinecrest Lodge weekly sync]]"\ndate: 2026-09-11 14:58:00.000000+00:00\n---\n')
(raw / "fireflies" / "undated.md").write_text("---\nmeeting_id: 01UNDATED\n---\n")

# --- 1. guards ---
ingest.SOURCES_FILE.write_text(json.dumps({"owner_emails": []}))
code, _, err = run("--input-json", d / "extensive.json", "--vault", vault)
check("guard: missing owner_emails -> exit 1", code == 1 and "owner_emails" in err)
code, _, err = run("--fetch", "--vault", vault)
check("guard: --fetch without owner_emails -> exit 1", code == 1 and "owner_emails" in err)
sources()
code, _, err = run("--fetch", "--no-owner-filter", "--input-json", d / "extensive.json", "--vault", vault)
check("guard: --no-owner-filter rejected with --fetch", code == 1 and "never" in err)
code, _, err = run("--no-owner-filter", "--input-dir", d, "--vault", vault)
check("guard: --no-owner-filter needs --input-json", code == 1)
code, _, err = run("--vault", vault)
check("guard: no input at all -> exit 1", code == 1 and "no input" in err)

# --- 2. dry run writes nothing ---
report = d / "report.md"
code, out, _ = run("--input-json", d / "extensive.json", "--input-json", d / "transcripts.json",
                   "--vault", vault, "--dry-run-report", report)
check("dry run: exit 0 and nothing written", code == 0 and not out_dir.exists())
check("dry run: count line", out.startswith("[dry-run] seen=4 created=3 updated=0 unchanged=0 not_owner=1 "))
rep = report.read_text()
check("dry run: report lists the owner's calls", "Ledgerline retail kickoff" in rep and "Pinecrest Lodge weekly sync" in rep)
check("privacy: non-owner title absent from the report", SECRET_TITLE not in rep and "9990001112223334445" not in rep)
check("privacy: non-owner title absent from last-run.md", SECRET_TITLE not in ingest.LAST_RUN.read_text())
check("dry run: state not advanced", not ingest.STATE_FILE.exists())

# --- 3. execute ---
code, out, _ = run("--input-json", d / "extensive.json", "--input-json", d / "transcripts.json",
                   "--vault", vault, "--execute")
check("execute: count line", out.strip() == "[execute] seen=4 created=3 updated=0 unchanged=0 not_owner=1 "
      "private_skipped=0 not_modified=0 empty=0 orphan_transcript=1 undated=0 linked=2 errors=0")
files = sorted(out_dir.glob("*.md"))
check("execute: three files", len(files) == 3)
check("privacy: non-owner call never written", SECRET_TITLE not in all_text(vault) and "Confidential brief" not in all_text(vault))
check("privacy: non-owner title absent from last-run.md after execute", SECRET_TITLE not in ingest.LAST_RUN.read_text())
a_path = out_dir / "2026-09-10-ledgerline-retail-kickoff-25937895.md"
check("path: <date>-<slug>-<last 8 of id>.md", a_path.exists())
a = a_path.read_text(encoding="utf-8")
fm_keys = [ln.split(":", 1)[0] for ln in a.split("---")[1].strip().splitlines()]
check("fm: key order", fm_keys == [
    "type", "gong_call_id", "title", "date", "timestamp", "timestamp_local", "date_source", "input",
    "source_hash", "call_url", "duration", "direction", "scope", "system", "is_private", "attendee_emails",
    "participants", "external_domains", "topics", "trackers", "call_outcome", "crm_accounts", "has_brief",
    "next_steps", "transcript_available", "transcript_included", "transcript_policy", "transcript_segments",
    "zoom_id", "zoom_note", "ingested_at"])
check("fm: type + id", "type: raw-gong-call\ngong_call_id: 7782342274025937895\n" in a)
check("fm: UTC timestamp from started (offset honored, fraction dropped)", "timestamp: 2026-09-10 19:57:12\n" in a)
check("fm: local timestamp", 'timestamp_local: "2026-09-10 14:57 CDT"' in a and "date: 2026-09-10\n" in a)
check("fm: api input", "date_source: api\ninput: api\n" in a and "source_file" not in a)
check("fm: duration", 'duration: "1h 15m"' in a)
check("fm: call metadata", "direction: Conference\nscope: External\nsystem: Zoom\nis_private: false\n" in a)
check("fm: attendee emails sorted lowercase",
      'attendee_emails: ["jordan.vale@harborlane.example", "marcus.hale@ledgerline.example", "owner@harborlane.example"]' in a)
check("fm: participants", 'participants: ["Owner", "Jordan Vale", "Marcus Hale"]' in a)
check("fm: external domains", 'external_domains: ["ledgerline.example"]' in a)
check("fm: topics with time only", 'topics: ["Pricing"]' in a)
check("fm: trackers with hits only", 'trackers: ["Competitors (2)"]' in a)
check("fm: outcome", 'call_outcome: "Discovery complete"' in a)
check("fm: crm_accounts empty when crm_context is off", "crm_accounts: []" in a)
check("fm: brief + next steps", "has_brief: true\nnext_steps: 2\n" in a)
check("fm: pointer policy", "transcript_available: true\ntranscript_included: false\ntranscript_policy: pointer\ntranscript_segments: 1\n" in a)
check("link: nearest zoom record", "zoom_id: 85012345678\n" in a and 'zoom_note: "[[2026-09-10-ledgerline-retail-kickoff-85012345]]"' in a
      and "11112222333" not in a)
check("link: fireflies out of window not linked", "fireflies_id" not in a)
check("body: callout", "> Also captured by Zoom: [[2026-09-10-ledgerline-retail-kickoff-85012345]]" in a)
check("body: heading", "# 2026-09-10 — Ledgerline retail kickoff" in a)
check("body: internal / external lines", "**Internal:** Owner, Jordan Vale" in a and "**External:** Marcus Hale" in a)
check("body: source link", "**Source:** [Gong call](https://app.gong.io/call?id=7782342274025937895)" in a)
check("body: brief", "## Brief\n\nLedgerline's retail team wants Northstar-BI dashboards for twelve stores." in a)
check("body: key points", "## Key points\n\n- Twelve stores in scope.\n- Pricing sent by Friday." in a)
check("body: next steps from both next-step highlight sections",
      "## Next steps\n\n- [ ] Send pricing to Marcus\n- [ ] Jordan drafts the data map" in a)
check("body: other highlights", "## Highlights\n\n### Pricing discussion\n\n- Per-store pricing preferred" in a)
check("body: outline timestamps", "### Introductions (00:00)" in a and "### Scope (12:34)\n\n- Store count" in a)
check("body: section order", a.index("## Brief") < a.index("## Key points") < a.index("## Next steps")
      < a.index("## Highlights") < a.index("## Outline") < a.index("## Transcript"))
check("body: brief, key points, attendees in the first 2,500 chars",
      a.index("## Key points") < 2500 and a.index("**Attendees:**") < 2500 and a.index("## Brief") < 2500)
check("DEC-032: transcript not written when AI content exists", "ZZ-SPOKEN-IN-A" not in a)
check("DEC-032: pointer to Gong and fetch.py",
      "[open the call in Gong](https://app.gong.io/call?id=7782342274025937895)" in a
      and "`python3 fetch.py transcript 7782342274025937895`" in a)

b = next(p for p in files if "pinecrest" in p.name).read_text(encoding="utf-8")
check("only-content: transcript written", "transcript_included: true\ntranscript_policy: only-content\ntranscript_segments: 4\n" in b)
check("transcript merged from a separate JSON by callId", "The booking export is still failing." in b)
check("speakerId -> name, consecutive monologues merged",
      "**Priya Okafor** (00:00:05) The booking export is still failing. Since Tuesday." in b and b.count("**Priya Okafor**") == 1)
check("speaker: owner resolved", "**Owner** (00:00:09) Thanks, looking now." in b)
check("speaker: unknown speakerId", "**Unknown speaker** (01:02:03) Who am I?" in b)
check("only-content: no brief section", "## Brief" not in b and "has_brief: false" in b)
check("link: fireflies with its meeting_note", "fireflies_id: 01ABCDEF\n" in b
      and 'fireflies_note: "[[2026-09-11 — Pinecrest Lodge weekly sync]]"' in b
      and "> Also captured by Fireflies: [[2026-09-11 — Pinecrest Lodge weekly sync]]" in b)
d_text = next(p for p in files if "portal" in p.name).read_text(encoding="utf-8")
check("private call kept by default, flagged", "is_private: true" in d_text)
state = json.loads(ingest.STATE_FILE.read_text())
check("state: cursor_started = max started (UTC)", state["cursor_started"] == "2026-09-12 15:00:00")

# --- 4. idempotency ---
code, out, _ = run("--input-json", d / "extensive.json", "--input-json", d / "transcripts.json", "--vault", vault, "--execute")
check("idempotent: second run unchanged", "created=0 updated=0 unchanged=3" in out and len(list(out_dir.glob("*.md"))) == 3)
changed = json.loads(json.dumps(EXTENSIVE))
changed["calls"][0]["content"]["brief"] = "Gong refreshed the brief overnight."
(d / "extensive2.json").write_text(json.dumps(changed))
code, out, _ = run("--input-json", d / "extensive2.json", "--input-json", d / "transcripts.json", "--vault", vault, "--execute")
check("idempotent: changed brief rewrites in place", "created=0 updated=1 unchanged=2" in out
      and "Gong refreshed the brief overnight." in a_path.read_text() and len(list(out_dir.glob("*.md"))) == 3)
retitled = json.loads(json.dumps(changed))
retitled["calls"][0]["metaData"]["title"] = "Ledgerline retail kickoff (renamed)"
(d / "extensive3.json").write_text(json.dumps(retitled))
code, out, _ = run("--input-json", d / "extensive3.json", "--vault", vault, "--execute")
check("idempotent: retitled call rewrites its existing file, no duplicate",
      "updated=1" in out and not list(out_dir.glob("*renamed*")) and "(renamed)" in a_path.read_text())

# --- 5. --include-transcript ---
code, out, _ = run("--input-json", d / "extensive3.json", "--vault", vault, "--execute", "--include-transcript")
a = a_path.read_text()
check("--include-transcript: written, policy full", "updated=1" in out and "ZZ-SPOKEN-IN-A: hello." in a
      and "transcript_included: true\ntranscript_policy: full\n" in a)

# --- 6. skip_private, crm_context, MCP envelope, --no-owner-filter ---
vault2 = d / "vault2"
sources(skip_private=True, crm_context=True)
(d / "envelope.json").write_text(json.dumps({"result": json.dumps(EXTENSIVE)}))
code, out, _ = run("--input-json", d / "envelope.json", "--vault", vault2, "--execute")
out2 = vault2 / "raw" / "gong" / "meetings"
check("MCP envelope unwrapped", code == 0 and "seen=4" in out and "created=1" in out)
check("skip_private: private call dropped", "private_skipped=1" in out and "Portal roadmap review" not in all_text(vault2))
check("no content: AI-less call without a transcript is skipped as empty", "empty=1" in out)
a2 = next(out2.glob("*ledgerline*")).read_text()
check("crm_context: account names from context", 'crm_accounts: ["Ledgerline"]' in a2)
sources()
vault3 = d / "vault3"
code, out, _ = run("--input-json", d / "extensive.json", "--no-owner-filter", "--vault", vault3, "--execute")
check("--no-owner-filter with JSON: every call kept", code == 0 and "not_owner=0" in out and SECRET_TITLE in all_text(vault3))
(d / "percall.json").write_text(json.dumps([{"call": CALL_B, "transcript": TRANSCRIPTS}]))
vault4 = d / "vault4"
code, out, _ = run("--input-json", d / "percall.json", "--vault", vault4, "--execute")
check("{call, transcript} per-call shape", "created=1" in out and "Since Tuesday." in all_text(vault4))

# --- 7. hand-downloaded transcript files ---
exports = d / "exports"
exports.mkdir()
(exports / "2026-09-10 1457 Ledgerline retail kickoff.txt").write_text(
    "Ledgerline retail kickoff\nSeptember 10, 2026\nDuration: 75 min\n\n"
    "Marcus Hale  0:05\nThanks for having us.\nWe have twelve stores.\n"
    "Jordan Vale\t1:02\nGreat, let's meet at 3:00 tomorrow.\n"
    "Marcus Hale  1:02:10\nPricing works.\n", encoding="utf-8")
(exports / "2026-09-10 1457 Ledgerline retail kickoff.md").write_text(
    "Follow-up email: pricing by Friday.", encoding="utf-8")
(exports / "pinecrest-sync.txt").write_text(
    "Pinecrest Lodge weekly sync\n2026-09-11 10:00\n\n"
    "Priya Okafor (0:05): The booking export is still failing.\n"
    "[0:40] Jordan Vale: Looking into it.\n", encoding="utf-8")
(exports / "notes-only.txt").write_text(
    "Portal roadmap review\nSeptember 12, 2026\nJordan Vale: First line.\nsecond line\nPriya Okafor: Reply.\n", encoding="utf-8")
(exports / "blob.txt").write_text("Untimed call\nsome words without any speakers at all\n", encoding="utf-8")
(exports / "empty.txt").write_text("", encoding="utf-8")

title, when, segs = ingest.parse_transcript_text((exports / "2026-09-10 1457 Ledgerline retail kickoff.txt").read_text())
check("file: spaces/tab shapes parsed", [s.speaker for s in segs] == ["Marcus Hale", "Jordan Vale", "Marcus Hale"]
      and segs[2].offset_s == 3600 + 130 and "twelve stores" in segs[0].text and "at 3:00 tomorrow" in segs[1].text)
check("file: header title + month date", title == "Ledgerline retail kickoff" and when and when[0].strftime("%Y-%m-%d") == "2026-09-10")
title, when, segs = ingest.parse_transcript_text((exports / "pinecrest-sync.txt").read_text())
check("file: 'Name (m:ss): text' and '[m:ss] Name:' shapes", [(s.speaker, s.offset_s) for s in segs]
      == [("Priya Okafor", 5), ("Jordan Vale", 40)] and segs[1].text == "Looking into it.")
check("file: ISO header date with time", when == (ingest.dt.datetime(2026, 9, 11, 10, 0), True))
title, when, segs = ingest.parse_transcript_text("0:05 | Jordan Vale\nHello there.\n0:09 Priya Okafor: Hi.\n")
check("file: '0:05 | Name' and '0:05 Name:' shapes", [(s.speaker, s.text) for s in segs]
      == [("Jordan Vale", "Hello there."), ("Priya Okafor", "Hi.")])
title, when, segs = ingest.parse_transcript_text((exports / "notes-only.txt").read_text())
check("file: 'Name: text' fallback with continuation", [(s.speaker, s.text) for s in segs]
      == [("Jordan Vale", "First line. second line"), ("Priya Okafor", "Reply.")] and title == "Portal roadmap review")
title, when, segs = ingest.parse_transcript_text((exports / "blob.txt").read_text())
check("file: blob fallback", title == "Untimed call" and len(segs) == 1 and segs[0].speaker == "Unknown speaker")

vault5 = d / "vault5"
(vault5 / "raw" / "zoom" / "meetings").mkdir(parents=True)
(vault5 / "raw" / "zoom" / "meetings" / "zoom-kickoff.md").write_text("---\nzoom_id: 85012345678\ntimestamp: 2026-09-10 19:58:00\n---\n")
sources(export_dirs=[str(exports)])
code, out, _ = run("--vault", vault5, "--execute")
out5 = vault5 / "raw" / "gong" / "meetings"
check("files: export_dirs used when no --input-dir", code == 0 and "seen=5" in out and "created=4" in out and "empty=1" in out)
check("files: undated blob counted", "undated=1" in out)
k = next(out5.glob("*ledgerline*")).read_text()
check("file fm: id, input, source, policy", "gong_call_id: gong-file-" in k and "input: file\nsource_file: " in k
      and "transcript_policy: file" in k and "transcript_included: true" in k)
check("file fm: date from filename (with time), UTC", "date_source: filename" in k and "timestamp: 2026-09-10 19:57:00" in k)
check("file: notes sidecar -> ## Notes", "## Notes\n\nFollow-up email: pricing by Friday." in k and k.index("## Notes") < k.index("## Transcript"))
check("file: transcript in full", "**Marcus Hale** (00:00:05) Thanks for having us. We have twelve stores." in k)
check("file: speakers as participants", 'participants: ["Marcus Hale", "Jordan Vale"]' in k)
check("file: cross-linked to zoom", "zoom_id: 85012345678" in k)
p = next(out5.glob("*pinecrest*")).read_text()
check("file: date from header", "date_source: header" in p and "date: 2026-09-11" in p)
blob = next(out5.glob("*untimed*")).read_text()
check("file: undated -> file-mtime, no timestamp, no links", "date_source: file-mtime" in blob and "timestamp: \n" in blob and "zoom_id" not in blob)
code, out, _ = run("--vault", vault5, "--execute", "--incremental")
check("files: --incremental skips files not modified since cursor_mtime", "not_modified=5" in out and "created=0" in out)
code, out, _ = run("--input-dir", exports, "--vault", vault5)
check("files: rerun unchanged", "unchanged=4" in out)

# --- 8. --fetch end to end with a fake Gong ---
os.environ[fetch.ENV_KEY], os.environ[fetch.ENV_SECRET] = "test-key", "test-secret"
fetch._sleep = lambda s: None
seen_requests = []


def fake_http(method, url, headers, body):
    req = json.loads(body) if body else {}
    seen_requests.append((url, req))
    if url.endswith("/v2/calls/extensive"):
        return 200, {}, json.dumps({"records": {"totalRecords": 3}, "calls": [CALL_A, CALL_B, CALL_C]}).encode()
    if url.endswith("/v2/calls/transcript"):
        return 200, {}, json.dumps(TRANSCRIPTS).encode()
    return 500, {}, b""


fetch._http = fake_http
sources(lookback_days=3)
ingest.STATE_FILE.write_text(json.dumps({"cursor_started": "2026-09-20 12:00:00"}))
vault6 = d / "vault6"
code, out, _ = run("--fetch", "--incremental", "--vault", vault6, "--execute", "--dry-run-report", d / "fetch-report.md")
ext_req = [r for u, r in seen_requests if u.endswith("/extensive")][0]
tr_req = [r for u, r in seen_requests if u.endswith("/transcript")][0]
check("fetch: window from cursor minus lookback", ext_req["filter"]["fromDateTime"] == "2026-09-17T12:00:00Z")
check("fetch: transcript only for the AI-less call", tr_req["filter"]["callIds"] == ["5550001111222233334"])
check("fetch: count line", code == 0 and "seen=3 created=2" in out and "not_owner=1" in out and "orphan_transcript=0" in out)
check("privacy: fetched non-owner call never written or logged",
      SECRET_TITLE not in all_text(vault6) and SECRET_TITLE not in ingest.LAST_RUN.read_text()
      and SECRET_TITLE not in (d / "fetch-report.md").read_text())
check("fetch: records written", "Since Tuesday." in all_text(vault6) and "transcript_policy: pointer" in all_text(vault6))
args = ingest.argparse.Namespace(since=None, incremental=True)
w_from, _ = ingest.fetch_window(args, {}, 3, now=ingest.dt.datetime(2026, 9, 25, 12, 0))
check("fetch window: no cursor -> 30 days back", w_from == ingest.dt.datetime(2026, 8, 26, 12, 0))
w_from, _ = ingest.fetch_window(ingest.argparse.Namespace(since="2026-09-01", incremental=True), {"cursor_started": "2026-09-20 00:00:00"}, 3)
check("fetch window: --since overrides (local midnight -> UTC)", w_from == ingest.dt.datetime(2026, 9, 1, 5, 0))


def failing_http(method, url, headers, body):
    return 401, {}, b""


fetch._http = failing_http
code, out, err = run("--fetch", "--vault", vault6)
check("fetch failure: reported, exit 1, secret-free", code == 1 and "errors=1" in out and "401" in err and "test-secret" not in err)

print()
TMP.cleanup()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
