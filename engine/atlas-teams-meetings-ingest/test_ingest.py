#!/usr/bin/env python3
"""Tests for atlas-teams-meetings-ingest — stdlib-only, no framework (matches
atlas-gmail-ingest/test_ingest.py). Fixtures use the fictional world from
docs/SCRUB-RULES.md; the .docx is built in memory with zipfile."""

import os
import sys
import tempfile
import zipfile
from pathlib import Path

import ingest

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


VTT = """WEBVTT

8f1c2a44-0001/12-0
00:00:03.250 --> 00:00:05.120
<v Jordan Vale>Morning, everyone.</v>

8f1c2a44-0001/13-0
00:00:05.500 --> 00:00:08.000
<v Jordan Vale>Let's start with the portal.</v>

8f1c2a44-0001/14-0
00:00:09.000 --> 00:00:12.400
<v Priya Okafor>The booking export is <b>still</b> failing.</v>

00:45:10.000 --> 00:45:12.000
Marcus Hale: Plain-colon speaker line.
"""


def make_docx(path: Path, paragraphs: list[str]) -> None:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = []
    for p in paragraphs:
        runs = []
        for i, chunk in enumerate(p.split("\t")):
            if i:
                runs.append("<w:r><w:tab/></w:r>")
            if chunk:
                chunk = chunk.replace("&", "&amp;").replace("<", "&lt;")
                runs.append(f'<w:r><w:t xml:space="preserve">{chunk}</w:t></w:r>')
        body.append("<w:p>" + "".join(runs) + "</w:p>")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {w}><w:body>{"".join(body)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


DOCX_PARAS = [
    "Pinecrest Lodge weekly sync",
    "September 10, 2026, 2:57PM",
    "1h 15m 14s",
    "",
    "Jordan Vale started transcription",
    "Jordan Vale\t0:03",
    "Morning, everyone.",
    "Priya Okafor   0:09",
    "The booking export is still failing.",
    "We should meet at 3:00",
    "Marcus Hale 1:02:10",
    "Retail pricing is ready.",
    "Jordan Vale stopped transcription",
]

# --- 1. file names ---
t, s = ingest.parse_filename("Portal roadmap review-20260910_145700-Meeting Recording")
check("name: OneDrive stamp -> start", s is not None and s.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-10 14:57:00")
check("name: OneDrive stamp -> clean title", t == "Portal roadmap review")
t, s = ingest.parse_filename("2026-09-10 1457 Ledgerline retail kickoff")
check("name: ISO date + time prefix", s is not None and s.strftime("%Y-%m-%d %H:%M") == "2026-09-10 14:57" and t == "Ledgerline retail kickoff")
t, s = ingest.parse_filename("Saltmarsh Inn check-in_2026-08-03 Transcript")
check("name: ISO date suffix, noise stripped", s is not None and s.strftime("%Y-%m-%d") == "2026-08-03" and t == "Saltmarsh Inn check-in")
t, s = ingest.parse_filename("Portal roadmap review")
check("name: no date", s is None and t == "Portal roadmap review")

# --- 2. VTT ---
segs, dur = ingest.parse_vtt(VTT)
check("vtt: segments", len(segs) == 4)
check("vtt: voice tag speaker + offset", segs[0].speaker == "Jordan Vale" and segs[0].offset_s == 3)
check("vtt: inline tags stripped", segs[2].text == "The booking export is still failing.")
check("vtt: plain 'Name: text' fallback", segs[3].speaker == "Marcus Hale" and segs[3].text == "Plain-colon speaker line.")
check("vtt: duration from last cue end", dur == 45 * 60 + 12)
rendered = ingest.render_transcript(segs)
check("vtt: same-speaker cues collapse", rendered.count("**Jordan Vale**") == 1 and "Morning, everyone. Let's start" in rendered)
check("vtt: offset formatted", "**Priya Okafor** (00:00:09)" in rendered)

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    # --- 3. DOCX ---
    docx = d / "export.docx"
    make_docx(docx, DOCX_PARAS)
    title, start, dur, segs = ingest.parse_docx(docx)
    check("docx: title", title == "Pinecrest Lodge weekly sync")
    check("docx: header date + time", start is not None and start.strftime("%Y-%m-%d %H:%M") == "2026-09-10 14:57")
    check("docx: duration line", dur == 3600 + 15 * 60 + 14)
    check("docx: tab and multi-space speaker lines", [x.speaker for x in segs] == ["Jordan Vale", "Priya Okafor", "Marcus Hale"])
    check("docx: h:mm:ss offset", segs[2].offset_s == 3600 + 2 * 60 + 10)
    check("docx: 'at 3:00' sentence is text, not a speaker", "We should meet at 3:00" in segs[1].text)
    check("docx: transcription events dropped", all("transcription" not in x.text for x in segs))

    # --- 4. end to end in a temp vault ---
    vault = d / "vault"
    raw = vault / "raw"
    (raw / "fireflies").mkdir(parents=True)
    (raw / "gemini" / "meetings").mkdir(parents=True)
    exports = d / "exports"
    exports.mkdir()
    # Local 14:57 in the configured timezone. Put linked records 3 minutes earlier in UTC.
    utc = ingest.to_utc(ingest.dt.datetime(2026, 9, 10, 14, 57))
    near = (utc - ingest.dt.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    far = (utc + ingest.dt.timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    (raw / "fireflies" / "FF1.md").write_text(f"---\nmeeting_id: FF1\nmeeting_note: \"[[2026-09-10 — Pinecrest Lodge weekly sync]]\"\ndate: {near}.000000+00:00\n---\n")
    (raw / "fireflies" / "FF2.md").write_text(f"---\nmeeting_id: FF2\nmeeting_note: \"[[other]]\"\ndate: {far}.000000+00:00\n---\n")
    (raw / "gemini" / "meetings" / "2026-09-10-pinecrest-lodge-weekly-sync-abcd1234.md").write_text(f"---\ngemini_doc_id: GDOC1\ntimestamp: {near}\n---\n")

    vtt_path = exports / "Pinecrest Lodge weekly sync-20260910_145700-Meeting Recording.vtt"
    vtt_path.write_text(VTT, encoding="utf-8")
    # Same meeting as .docx (richer? no: 3 segs vs 4) -> deduplicated to the vtt.
    make_docx(exports / "Pinecrest Lodge weekly sync-20260910_145700.docx", DOCX_PARAS)
    (exports / "Pinecrest Lodge weekly sync-20260910_145700-Meeting Recording.md").write_text("Copilot recap: export fix owned by Priya.", encoding="utf-8")
    (exports / "Portal roadmap review.vtt").write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Jordan Vale>Undated one.</v>\n", encoding="utf-8")
    (exports / "empty.vtt").write_text("WEBVTT\n", encoding="utf-8")

    out_dir = raw / "teams" / "meetings"
    stats, preview = ingest.Stats(), []
    paths = ingest.collect_files([exports], [], stats)
    check("collect: vtt + docx picked up, md sidecar not", len(paths) == 4)
    ingest.run_ingest(paths, out_dir, raw, None, False, stats, "x", preview)
    check("dry run: writes nothing", not out_dir.exists())
    check("dry run: two meetings, one duplicate format, one empty", len(preview) == 2 and stats.skipped_duplicate_input == 1 and stats.skipped_empty == 1)

    stats, preview = ingest.Stats(), []
    ingest.run_ingest(paths, out_dir, raw, None, True, stats, "2026-09-23T10:00:00", preview)
    files = sorted(out_dir.glob("*.md"))
    check("execute: two files", len(files) == 2 and stats.written == 2)
    main = next(p for p in files if "pinecrest" in p.name)
    text = main.read_text(encoding="utf-8")
    check("fm: type + id", "type: raw-teams-meeting" in text and "teams_meeting_id: teams-" in text)
    check("fm: utc timestamp from local filename stamp", f"timestamp: {utc.strftime('%Y-%m-%d %H:%M:%S')}" in text)
    check("fm: date source", "date_source: filename" in text)
    check("fm: participants are speakers", 'participants: ["Jordan Vale", "Priya Okafor", "Marcus Hale"]' in text)
    check("fm: fireflies nearest (FF1 not FF2)", "fireflies_id: FF1" in text and "FF2" not in text)
    check("fm: gemini linked", "gemini_doc_id: GDOC1" in text)
    check("body: notes sidecar leads", text.index("## Notes") < text.index("## Transcript") and "Copilot recap" in text)
    check("body: transcript stored in full", "The booking export is still failing." in text)
    undated = next(p for p in files if "portal" in p.name).read_text(encoding="utf-8")
    check("undated: flagged, no timestamp, no links", "date_source: file-mtime" in undated and "timestamp: \n" in undated and "fireflies_id" not in undated)

    stats, preview = ingest.Stats(), []
    ingest.run_ingest(paths, out_dir, raw, None, True, stats, "x", preview)
    check("idempotent: unchanged content skipped", stats.already_present == 2 and stats.written == 0 and stats.updated == 0)

    vtt_path.write_text(VTT.replace("<b>still</b> failing", "fixed now"), encoding="utf-8")
    stats, preview = ingest.Stats(), []
    ingest.run_ingest(paths, out_dir, raw, None, True, stats, "x", preview)
    check("idempotent: edited transcript rewrites in place", stats.updated == 1 and len(list(out_dir.glob("*.md"))) == 2)
    check("idempotent: new content on disk", "fixed now" in main.read_text(encoding="utf-8"))

    stats, preview = ingest.Stats(), []
    ingest.run_ingest(paths, out_dir, raw, "9999-01-01 00:00:00", True, stats, "x", preview)
    check("incremental: files older than cursor skipped", stats.skipped_not_modified == 4 and stats.written == 0)

    # --- 5. sources file ---
    orig = ingest.SOURCES_FILE
    try:
        ingest.SOURCES_FILE = d / "teams-sources.json"
        ingest.SOURCES_FILE.write_text('{"export_dirs": ["~/Teams-Transcripts", ""]}', encoding="utf-8")
        dirs = ingest.load_source_dirs()
        check("sources: ~ expanded, blanks dropped", dirs == [Path(os.path.expanduser("~/Teams-Transcripts"))])
    finally:
        ingest.SOURCES_FILE = orig

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
