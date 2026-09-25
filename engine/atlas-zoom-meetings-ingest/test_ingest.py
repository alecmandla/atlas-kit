#!/usr/bin/env python3
"""Tests for atlas-zoom-meetings-ingest — stdlib-only, no framework (matches
atlas-teams-meetings-ingest/test_ingest.py). Fixtures use the fictional world
from docs/SCRUB-RULES.md. Everything runs in temp dirs; ATLAS_CONFIG points at a
temp config so the timezone is fixed (America/Los_Angeles, PDT in September)."""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
_CFG = Path(_TMP.name) / "atlas-config.json"
_CFG.write_text(json.dumps({"vault_root": str(Path(_TMP.name) / "default-vault"), "timezone": "America/Los_Angeles"}))
os.environ["ATLAS_CONFIG"] = str(_CFG)

import ingest  # noqa: E402

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


dt = ingest.dt

ZOOM_VTT = """WEBVTT

1
00:00:02.140 --> 00:00:05.600
Jordan Vale: Morning, everyone.

2
00:00:05.600 --> 00:00:09.010
Jordan Vale: Let's start with the portal.

3
00:00:09.500 --> 00:00:13.200
Priya Okafor: The booking export is still failing.

4
00:00:13.400 --> 00:00:16.000
Can everyone hear me?

5
00:45:10.000 --> 00:45:12.000
<v Marcus Hale>Retail pricing is ready.</v>
"""

SHORT_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.000
Jordan Vale: Short caption file.

2
00:00:04.000 --> 00:00:07.000
Priya Okafor: Only two cues.
"""

SUMMARY_CONTENT = """## Quick recap

Jordan and Priya reviewed the Pinecrest Lodge booking export and the portal rewrite.

## Next steps

- Priya Okafor: send the failing export sample to Jordan.
- Jordan Vale: patch the booking ETL before Friday.

## Summary

### Booking export

The nightly export still fails on multi-room stays.

```
# not a heading inside a code fence
```
"""


def utc(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s)


def fm_value(text, key):
    for line in text.split("\n"):
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None


# --- 1. VTT parsing ---
segs, dur = ingest.parse_vtt(ZOOM_VTT)
check("vtt: numbered cues parsed", len(segs) == 5)
check("vtt: Name: text speaker + offset", segs[0].speaker == "Jordan Vale" and segs[0].offset_s == 2 and segs[0].text == "Morning, everyone.")
check("vtt: cue without a name -> Unknown speaker", segs[3].speaker == "Unknown speaker" and segs[3].text == "Can everyone hear me?")
check("vtt: <v> tag accepted", segs[4].speaker == "Marcus Hale" and segs[4].text == "Retail pricing is ready.")
check("vtt: duration from last cue end", dur == 45 * 60 + 12)
rendered = ingest.render_transcript(segs)
check("render: consecutive same-speaker cues merge", rendered.count("**Jordan Vale**") == 1 and "Morning, everyone. Let's start with the portal." in rendered)
check("render: offset formatted", "**Priya Okafor** (00:00:09)" in rendered)
check("speakers: first-appearance order, unknown excluded", ingest.speakers_of(segs) == ["Jordan Vale", "Priya Okafor", "Marcus Hale"])

# --- 2. summary helpers ---
demoted = ingest.demote_headings(SUMMARY_CONTENT)
check("demote: ## -> ###", "### Quick recap" in demoted and "\n## " not in "\n" + demoted)
check("demote: ### -> #### keeps hierarchy", "#### Booking export" in demoted)
check("demote: fenced code untouched", "# not a heading inside a code fence" in demoted)
check("next steps counted under heading", ingest.count_next_steps(demoted) == 2)
check("id8: uuid hashed, zoom- id sliced", len(ingest.id8_of("aDYlohsHRtCd4ii1uC2+hA==")) == 8 and ingest.id8_of("zoom-0123456789ab") == "01234567")

# --- 3. window math ---
today = dt.date(2026, 9, 25)
state = {"cursor_start": "2026-09-20 18:00:00"}
check("window: incremental = cursor_start - lookback .. tomorrow",
      ingest.compute_window(state, True, None, 7, today) == (dt.date(2026, 9, 13), dt.date(2026, 9, 26)))
check("window: no cursor = 30 days back",
      ingest.compute_window({}, True, None, 7, today) == (dt.date(2026, 8, 26), dt.date(2026, 9, 26)))
check("window: not incremental ignores cursor",
      ingest.compute_window(state, False, None, 7, today)[0] == dt.date(2026, 8, 26))
check("window: --since overrides", ingest.compute_window(state, True, "2026-07-01", 7, today)[0] == dt.date(2026, 7, 1))


def patched(d):
    """Point every module-level path at the temp dir."""
    ingest.STATE_FILE = d / "state.json"
    ingest.LAST_RUN = d / "last-run.md"
    ingest.RUN_LOCK = d / ".run.lock"
    ingest.SOURCES_FILE = d / "zoom-sources.json"


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = ingest.main(argv)
    return code, out.getvalue()


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    patched(d)
    exports = d / "exports"
    exports.mkdir()
    roots = {exports.resolve()}

    # --- 4. file names -> start ---
    gmt = exports / "GMT20260910-185830_Recording.transcript.vtt"
    gmt.write_text(ZOOM_VTT, encoding="utf-8")
    m = ingest.load_file_meeting(gmt, roots)
    check("gmt: stamp is UTC, no timezone shift", m.start_utc == utc(2026, 9, 10, 18, 58, 30) and m.date_source == "filename-gmt")
    check("gmt: local date via timezone", m.local_date == "2026-09-10")
    check("gmt: generic parent -> 'Zoom meeting'", m.title == "Zoom meeting")

    named = exports / "Ledgerline retail kickoff"
    named.mkdir()
    gmt2 = named / "GMT20260915-160000_Recording.transcript.vtt"
    gmt2.write_text(SHORT_VTT, encoding="utf-8")
    m = ingest.load_file_meeting(gmt2, roots)
    check("gmt: title from a non-generic parent folder", m.title == "Ledgerline retail kickoff" and m.start_utc == utc(2026, 9, 15, 16, 0))

    local_dir = exports / "2026-09-11 09.00.00 Portal roadmap review 81234567890"
    local_dir.mkdir()
    lvtt = local_dir / "Portal roadmap review.vtt"
    lvtt.write_text(SHORT_VTT, encoding="utf-8")
    m = ingest.load_file_meeting(lvtt, roots)
    check("folder: local 09:00 PDT -> 16:00 UTC", m.start_utc == utc(2026, 9, 11, 16, 0) and m.date_source == "folder-name")
    check("folder: title + meeting number", m.title == "Portal roadmap review" and m.number == "81234567890")

    iso = exports / "2026-09-12 1457 Portal roadmap review.vtt"
    iso.write_text(SHORT_VTT, encoding="utf-8")
    m = ingest.load_file_meeting(iso, roots)
    check("iso name: local 14:57 -> 21:57 UTC", m.start_utc == utc(2026, 9, 12, 21, 57) and m.date_source == "filename" and m.title == "Portal roadmap review")

    # .transcript.vtt + .cc.vtt for the same meeting, plus a notes sidecar.
    tr = exports / "GMT20260914-170000_Recording.transcript.vtt"
    cc = exports / "GMT20260914-170000_Recording.cc.vtt"
    tr.write_text(ZOOM_VTT, encoding="utf-8")
    cc.write_text(SHORT_VTT, encoding="utf-8")
    (exports / "GMT20260914-170000_Recording.md").write_text("AI Companion email: Priya owns the export fix.", encoding="utf-8")
    (exports / "meeting_saved_chat.txt").write_text("legacy chat, never a sidecar", encoding="utf-8")
    undated = exports / "Saltmarsh Inn check-in.vtt"
    undated.write_text(SHORT_VTT, encoding="utf-8")
    (exports / "empty.vtt").write_text("WEBVTT\n", encoding="utf-8")

    stats = ingest.Stats()
    paths = ingest.collect_files([exports], [], stats)
    check("collect: only .vtt files, recursive", len(paths) == 8 and all(p.suffix == ".vtt" for p in paths))
    files = ingest.load_files(paths, roots, None, stats)
    check("dedupe: .transcript.vtt and .cc.vtt -> one record", stats.skipped_duplicate_input == 1)
    kept = [f for f in files if f.base_stem == "GMT20260914-170000_Recording"]
    check("dedupe: keeps the one with more segments", len(kept) == 1 and len(kept[0].segments) == 5 and kept[0].source_file == tr.name)
    check("sidecar: notes attached (stem without .transcript)", "Priya owns the export fix" in kept[0].notes)

    # --- 5. JSON inputs ---
    vault = d / "vault"
    raw = vault / "raw"
    (raw / "fireflies").mkdir(parents=True)
    (raw / "gemini" / "meetings").mkdir(parents=True)
    (raw / "teams" / "meetings").mkdir(parents=True)
    (raw / "fireflies" / "01ABCDEF1.md").write_text(
        "---\nmeeting_id: 01ABCDEF1\nmeeting_note: \"[[2026-09-10 — Pinecrest Lodge weekly sync]]\"\n"
        "date: 2026-09-10 18:59:00.000000+00:00\n---\n", encoding="utf-8")
    (raw / "fireflies" / "01ABCDEF2.md").write_text(
        "---\nmeeting_id: 01ABCDEF2\nmeeting_note: \"[[other]]\"\ndate: 2026-09-10 19:40:00.000000+00:00\n---\n", encoding="utf-8")
    (raw / "gemini" / "meetings" / "2026-09-10-pinecrest-lodge-weekly-sync-abcd1234.md").write_text(
        "---\ngemini_doc_id: GDOC1\ntimestamp: 2026-09-10 19:05:00\n---\n", encoding="utf-8")
    (raw / "teams" / "meetings" / "2026-09-10-pinecrest-lodge-weekly-sync-1a2b3c4d.md").write_text(
        "---\nteams_meeting_id: teams-1a2b3c4d5e6f\ntimestamp: 2026-09-10 19:01:00\n---\n", encoding="utf-8")

    uuid_main = "aDYlohsHRtCd4ii1uC2+hA=="
    main_json = {
        "uuid": uuid_main,
        "id": 81234567000,
        "topic": "Pinecrest Lodge weekly sync",
        "start_time": "2026-09-10T18:57:00Z",
        "end_time": "2026-09-10T20:12:00Z",
        "host_email": "Jordan.Vale@harborlane.example",
        "participants": [{"name": "Jordan Vale", "email": "jordan.vale@harborlane.example"},
                         {"name": "Priya Okafor", "user_email": "priya@pinecrestlodge.example"}],
        "summary": {"summary_content": SUMMARY_CONTENT, "summary_doc_url": "https://docs.zoom.us/doc/example123",
                    "summary_last_modified_time": "2026-09-10T20:20:00Z"},
        "share_url": "https://zoom.us/rec/share/example-share",
    }
    j1 = d / "zoom-main.json"
    j1.write_text(json.dumps(main_json), encoding="utf-8")

    deprecated = {"meetings": [{
        "meeting_uuid": "/Xk9pQ2+vT0aZb1cD3eF4g==",
        "meeting_id": 82000000001,
        "summary_title": "Meeting summary for Ledgerline retail kickoff (09/12/2026)",
        "meeting_start_time": "2026-09-12T17:00:00Z",
        "duration": 30,
        "meeting_host_email": "marcus.hale@ledgerline.example",
        "participants": ["Marcus Hale", "jordan.vale@harborlane.example"],
        "summary_overview": "Marcus walked through the Ledgerline retail pricing tiers.",
        "summary_details": [{"label": "Pricing tiers", "summary": "Three tiers, billed per property."}],
        "next_steps": ["Old step that the host edited away."],
        "edited_summary": {"next_steps": ["Marcus Hale: send the pricing sheet.", "Jordan Vale: review the Ledgerline-Cloud export."]},
    }]}
    j2 = d / "zoom-deprecated.json"
    j2.write_text(json.dumps({"result": json.dumps(deprecated)}), encoding="utf-8")  # MCP envelope

    no_summary = [{"uuid": "Qm9ub1N1bW1hcnk+Zm9yWm9vbQ==", "topic": "Jordan and {{owner_name}} 1:1",
                   "start_time": "2026-09-13T16:30:00Z", "transcript_vtt": SHORT_VTT}]
    j3 = d / "zoom-nosummary.json"
    j3.write_text(json.dumps(no_summary), encoding="utf-8")
    bad = d / "zoom-bad.json"
    bad.write_text("{not json", encoding="utf-8")

    stats = ingest.Stats()
    api = []
    for p in (j1, j2, j3, bad):
        api += ingest.meetings_from_json(p, stats)
    check("json: three meetings parsed, bad file is an error not a crash", len(api) == 3 and len(stats.errors) == 1)
    dep = next(a for a in api if a.uuid.startswith("/"))
    check("json: MCP envelope unwrapped + aliases", dep.number == "82000000001" and dep.host_email == "marcus.hale@ledgerline.example")
    check("json: summary_title prefix and date stripped", dep.title == "Ledgerline retail kickoff")
    check("json: duration minutes", dep.duration_s == 30 * 60)
    check("json: string participants split into names + emails",
          dep.participants == ["Marcus Hale"] and dep.attendee_emails == ["jordan.vale@harborlane.example", "marcus.hale@ledgerline.example"])

    # --- 6. end to end through run_ingest ---
    out_dir = raw / "zoom" / "meetings"
    file_recs = ingest.load_files(ingest.collect_files([exports], [], stats), roots, None, stats)
    stats = ingest.Stats()
    records = ingest.merge_records(api, file_recs, stats)
    check("merge: GMT file within 2 min of the JSON start folds into it", stats.merged == 1)
    main_rec = next(r for r in records if r.uuid == uuid_main)
    check("merge: file supplied the transcript JSON lacked", len(main_rec.segments) == 5 and main_rec.source_file == gmt.name)

    preview = []
    ingest.run_ingest(records, out_dir, raw, False, False, stats, "x", preview)
    check("dry run: writes nothing", not out_dir.exists())
    check("dry run: previews every non-empty record", len(preview) == 8 and stats.skipped_empty == 1)

    stats, preview = ingest.Stats(), []
    ingest.run_ingest(records, out_dir, raw, True, False, stats, "2026-09-25T10:00:00", preview)
    written = sorted(out_dir.glob("*.md"))
    check("execute: eight files", len(written) == 8 and stats.written == 8)

    main_file = next(p for p in written if p.name.endswith(ingest.id8_of(uuid_main) + ".md"))
    text = main_file.read_text(encoding="utf-8")
    check("file name: date-slug-id8", main_file.name.startswith("2026-09-10-pinecrest-lodge-weekly-sync-"))
    check("fm: type + zoom_id is the uuid", "type: raw-zoom-meeting" in text and f"zoom_id: {uuid_main}" in text)
    check("fm: meeting number", "zoom_meeting_number: 81234567000" in text)
    check("fm: utc + local timestamps", "timestamp: 2026-09-10 18:57:00" in text and 'timestamp_local: "2026-09-10 11:57 PDT"' in text)
    check("fm: date_source api, input api", "date_source: api" in text and "input: api" in text)
    check("fm: duration from end - start", 'duration: "1h 15m"' in text)
    check("fm: attendee emails lowercased, host included",
          'attendee_emails: ["jordan.vale@harborlane.example", "priya@pinecrestlodge.example"]' in text)
    check("fm: participants from JSON", 'participants: ["Jordan Vale", "Priya Okafor"]' in text)
    check("fm: has_summary + next_steps counted from summary_content", "has_summary: true" in text and "next_steps: 2" in text)
    check("fm: transcript pointer policy", "transcript_available: true" in text and "transcript_included: false" in text
          and "transcript_policy: pointer" in text and "transcript_segments: 5" in text)
    check("fm: urls", "summary_doc_url: https://docs.zoom.us/doc/example123" in text and "recording_url: https://zoom.us/rec/share/example-share" in text)
    check("fm: fireflies nearest (FF1 not FF2)", "fireflies_id: 01ABCDEF1" in text and "01ABCDEF2" not in text)
    check("fm: gemini + teams linked with [[stem]] notes",
          "gemini_doc_id: GDOC1" in text and "teams_meeting_id: teams-1a2b3c4d5e6f" in text
          and 'teams_note: "[[2026-09-10-pinecrest-lodge-weekly-sync-1a2b3c4d]]"' in text)
    check("body: headings demoted, one H2 spine", "### Quick recap" in text and "\n## Quick recap" not in text and "#### Booking export" in text)
    check("body: next steps left in Summary, no duplicate section", "\n## Next steps" not in text)
    check("body: transcript NOT written", "The booking export is still failing." not in text)
    check("body: pointer names MCP tool + fetch.py", "get_recording_resource" in text and f"fetch.py transcript '{uuid_main}'" in text)
    check("body: summary + attendees inside the first 2,500 chars",
          0 < text.index("## Summary") < 2500 and text.index("**Attendees:**") < 2500)
    check("body: also-captured callout", "> Also captured by Fireflies: [[2026-09-10 — Pinecrest Lodge weekly sync]]" in text)

    dep_text = next(p for p in written if p.name.endswith(ingest.id8_of(dep.uuid) + ".md")).read_text(encoding="utf-8")
    check("deprecated: overview + ### label", "Marcus walked through" in dep_text and "### Pricing tiers" in dep_text)
    check("deprecated: edited next steps as checkboxes", "## Next steps" in dep_text and "- [ ] Marcus Hale: send the pricing sheet." in dep_text
          and "Old step" not in dep_text and "next_steps: 2" in dep_text)

    ns_text = next(p for p in written if "jordan-and" in p.name).read_text(encoding="utf-8")
    check("no summary: transcript written as only content", "transcript_policy: only-content" in ns_text
          and "transcript_included: true" in ns_text and "Short caption file." in ns_text and "has_summary: false" in ns_text)

    kept_text = next(p for p in written if p.name.startswith("2026-09-14-")).read_text(encoding="utf-8")
    check("file record: transcript in full, notes section", "transcript_policy: file" in kept_text and "## Notes" in kept_text
          and "The booking export is still failing." in kept_text and "input: file" in kept_text)
    check("file record: participants are speakers", 'participants: ["Jordan Vale", "Priya Okafor", "Marcus Hale"]' in kept_text)
    check("file record: zoom- id", fm_value(kept_text, "zoom_id").startswith("zoom-"))

    und = next(p for p in written if "saltmarsh" in p.name).read_text(encoding="utf-8")
    check("undated: file-mtime, no timestamp, never linked", "date_source: file-mtime" in und and "timestamp: \n" in und
          and 'timestamp_local: ""' in und and "fireflies_id" not in und and "Start time unknown" in und)

    # --- 7. idempotency ---
    stats, preview = ingest.Stats(), []
    ingest.run_ingest(records, out_dir, raw, True, False, stats, "x", preview)
    check("idempotent: second run unchanged", stats.already_present == 8 and stats.written == 0 and stats.updated == 0)

    main_json["summary"]["summary_content"] = SUMMARY_CONTENT.replace("multi-room stays", "multi-room stays and group blocks")
    j1.write_text(json.dumps(main_json), encoding="utf-8")
    stats = ingest.Stats()
    api2 = ingest.meetings_from_json(j1, stats)
    stats, preview = ingest.Stats(), []
    ingest.run_ingest(api2, out_dir, raw, True, False, stats, "x", preview)
    check("idempotent: changed summary rewrites in place", stats.updated == 1 and len(list(out_dir.glob("*.md"))) == 8)
    check("idempotent: new content on disk", "group blocks" in main_file.read_text(encoding="utf-8"))

    stats, preview = ingest.Stats(), []
    ingest.run_ingest(ingest.merge_records(api2, [f for f in file_recs if f.source_file == gmt.name], ingest.Stats()),
                      out_dir, raw, True, True, stats, "x", preview)
    inc_text = main_file.read_text(encoding="utf-8")
    check("--include-transcript: writes it for a summary record", stats.updated == 1 and "transcript_included: true" in inc_text
          and "transcript_policy: full" in inc_text
          and "The booking export is still failing." in inc_text)

    stats = ingest.Stats()
    ingest.load_files(ingest.collect_files([exports], [], stats), roots, "9999-01-01 00:00:00", stats)
    check("incremental: files older than cursor skipped", stats.skipped_not_modified == 8)

    # --- 8. main(): config dirs + --input-json, state, last-run, print-window, no input ---
    code, out = run_main(["--vault", str(d / "vault2")])
    check("main: no input at all -> exit 1", code == 1)

    ingest.SOURCES_FILE.write_text(json.dumps({"export_dirs": [str(exports), str(d / "missing-dir")],
                                               "api": {"lookback_days": 3}}), encoding="utf-8")
    code, out = run_main(["--input-json", str(j1), "--vault", str(d / "vault2")])
    check("main: dry run by default", code == 0 and out.startswith("[dry-run]") and not (d / "vault2").exists())
    check("main: config dirs scanned with --input-json; missing dir is an error line",
          "seen=9" in out and "merged=1" in out and "errors=1" in out)
    check("main: last-run.md written", ingest.LAST_RUN.exists())

    code, out = run_main(["--input-json", str(j1), "--execute", "--vault", str(d / "vault2")])
    st = json.loads(ingest.STATE_FILE.read_text())
    check("main: execute advances cursors", code == 0 and st["cursor_start"] == "2026-09-15 16:00:00" and st["cursor_mtime"])
    check("main: count line shape", all(k + "=" in out for k in
          ("seen", "created", "updated", "unchanged", "not_modified", "empty", "dup_format", "merged", "undated", "linked", "errors")))

    code, out = run_main(["--print-window", "--incremental"])
    today = dt.date.today()
    exp_from = dt.date(2026, 9, 15) - dt.timedelta(days=3)
    check("print-window: cursor_start - lookback_days .. tomorrow",
          out.strip() == f"from={exp_from.isoformat()} to={(today + dt.timedelta(days=1)).isoformat()}")
    code, out = run_main(["--print-window", "--since", "2026-09-01"])
    check("print-window: --since", out.startswith("from=2026-09-01 "))

    # --fetch goes through fetch.pull (stubbed; no network).
    import fetch
    calls = []

    def fake_pull(frm, to, warnings=None):
        calls.append((frm, to))
        warnings.append("Portal roadmap review: no summary (HTTP 403 (code 2305))")
        return [{"uuid": "RmV0Y2hlZE1lZXRpbmc=", "topic": "Portal roadmap review", "start_time": "2026-09-18T15:00:00Z",
                 "summary": {"summary_overview": "Roadmap agreed."}}]

    orig_pull = fetch.pull
    fetch.pull = fake_pull
    try:
        code, out = run_main(["--fetch", "--since", "2026-09-17", "--execute", "--vault", str(d / "vault3"), "--input-dir", str(d / "nowhere")])
    finally:
        fetch.pull = orig_pull
    check("fetch: window passed to fetch.pull", calls and calls[0][0] == dt.date(2026, 9, 17))
    check("fetch: records written", code == 0 and len(list((d / "vault3" / "raw" / "zoom" / "meetings").glob("*.md"))) == 1)
    check("fetch: warnings land in last-run.md", "code 2305" in ingest.LAST_RUN.read_text(encoding="utf-8"))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
