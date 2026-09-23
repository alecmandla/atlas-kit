#!/usr/bin/env python3
"""Tests for atlas-gemini-meetings-ingest — stdlib-only, no framework (matches
atlas-gmail-ingest/test_ingest.py). Fixture uses the fictional world from docs/SCRUB-RULES.md."""

import json
import os
import sys
import tempfile
from pathlib import Path

import ingest

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


# A multi-tab doc exactly as get_doc_as_markdown renders it: Quick notes tab,
# Full notes tab, Transcript tab, Gemini's footers on each, vertical tabs in
# the Summary, and "Aligned"/"Shelved" nested as H2 under "### Decisions".
MULTI_TAB = """# Quick notes

*Please ****rate the new Quick notes tab**** by taking a *[*short survey*](https://example.com/survey)*.*

## Pinecrest Lodge weekly sync

Sep 10, 2026

[Jordan Vale](mailto:jordan.vale@harborlane.example) [Priya Okafor](mailto:priya@pinecrestlodge.example)

Rebranding exploration and database evaluation

## Rebranding strategy

- Priya proposed "Livery" as a brand name.
- liverydata.com is available.
## Next steps

- [ ] [Priya Okafor] Test Name: Share the proposed branding with contacts.
- [ ] [Jordan Vale] Update Budget: Add an R&D line item.
**Want to see more? **View the full notes\x0bTip: You can always access your full notes from the left sidebar.

*You should review Gemini's notes to make sure they're accurate. *[*Get tips and learn how Gemini takes notes*](https://support.google.com/meet/answer/14754931)

# Full notes

Sep 10, 2026

## Pinecrest Lodge weekly sync

Invited [Priya Okafor](mailto:priya@pinecrestlodge.example) [Jordan Vale](mailto:jordan.vale@harborlane.example)

Attachments [Pinecrest Lodge weekly sync](https://calendar.google.com/calendar/event?eid=abc) [Agenda](https://drive.google.com/open?id=agenda123)

Meeting records [Transcript](https://docs.google.com/document/d/DOC123/edit?usp=drive_web&tab=t.xyz)

### Summary

Rebranding exploration and database evaluation.\x0b\x0b**Rebranding And Legal Status**\x0bDiscussion centered on adopting livery as a brand.\x0b\x0b**Technical Pipeline**\x0bThe team evaluated Postgres.

### Decisions

## Aligned

- **Independent R&D budget line item** Jordan agreed to establish a line item for R&D.
## Shelved

- **Postpone SAM integration** The team decided to postpone it.
We've **updated the Decisions section** using your feedback.

Let us know what you think: [Helpful](https://example.com/yes) or [Not Helpful](https://example.com/no)

### Next steps

- [ ] [Priya Okafor] Test Name: Share the proposed branding with contacts.
- [ ] [Jordan Vale] Update Budget: Add an R&D line item.
### Details

- **Rebranding Exploration**: Jordan Vale and Priya Okafor discussed rebrand names.
- **Database Architecture**: Priya Okafor proposed Postgres over Firestore.
*You should review Gemini's notes to make sure they're accurate. *[*Get tips and learn how Gemini takes notes*](https://support.google.com/meet/answer/14754931)

*How is the quality of ****these specific notes?**** *[*Take a short survey*](https://example.com/q)* to let us know.*

# Transcript

Sep 10, 2026

## Pinecrest Lodge weekly sync - Transcript

### 00:05:22

**Priya Okafor: **Hey.

**Jordan Vale: **Hey,

**Jordan Vale: **how are you?

### 00:10:32

**Priya Okafor: **Good. Let's talk branding.

### Transcription ended after 01:15:14

*This editable transcript was computer generated and might contain errors. People can also change the text after it was created.*
"""

# The single-tab shape (older docs, no H1s, no transcript).
SINGLE_TAB = """Jul 10, 2026

## Portal roadmap review

Invited [Jordan Vale](mailto:jordan.vale@harborlane.example) [Priya Okafor](mailto:priya@pinecrestlodge.example)

### Summary

Operational improvements.

### Next steps

- [ ] [The group] Map Onboarding Process: Define the workflow.
### Details

- **System and Billing**: Priya identified challenges.
*You should review Gemini's notes to make sure they're accurate. *[*Get tips*](https://support.google.com/meet/answer/14754931)
"""


# --- 1. doc-name parsing + timezone conversion ---
dn = ingest.parse_doc_name("Pinecrest Lodge weekly sync - 2026/09/10 14:57 EDT - Notes by Gemini")
check("name: title", dn.title == "Pinecrest Lodge weekly sync")
check("name: local date", dn.date == "2026-09-10")
check("name: EDT -> UTC", dn.utc_ts == "2026-09-10 18:57:00")
check("name: is gemini", dn.is_gemini)
dn2 = ingest.parse_doc_name("A - B - 2026/01/05 09:00 PST - Notes by Gemini")
check("name: title may itself contain ' - '", dn2.title == "A - B" and dn2.utc_ts == "2026-01-05 17:00:00")
dn3 = ingest.parse_doc_name("Random Agenda Doc")
check("name: non-gemini doc flagged", not dn3.is_gemini)
dn4 = ingest.parse_doc_name("Odd - Notes by Gemini")
check("name: gemini without timestamp still gemini, no utc", dn4.is_gemini and dn4.utc_ts == "" and dn4.title == "Odd")
dn5 = ingest.parse_doc_name("Late - 2026/03/08 23:30 EST - Notes by Gemini")
check("name: UTC rolls to next day", dn5.utc_ts == "2026-03-09 04:30:00" and dn5.date == "2026-03-08")

# --- 2. body parsing: multi-tab ---
doc = ingest.parse_doc(MULTI_TAB)
check("multi: title from full notes, no ' - Transcript' suffix", doc.title == "Pinecrest Lodge weekly sync")
check("multi: body date", doc.body_date == "2026-09-10")
check("multi: participants in Invited order (full tab parsed first)", doc.participants == ["Priya Okafor", "Jordan Vale"])
check("multi: attendee emails", sorted(doc.attendee_emails) == ["jordan.vale@harborlane.example", "priya@pinecrestlodge.example"])
check("multi: transcript url captured", doc.transcript_url.startswith("https://docs.google.com/document/d/DOC123"))
check("multi: attachments captured (calendar + agenda)", len(doc.attachments) == 2)
check("multi: summary vertical tabs -> newlines", "\x0b" not in doc.sections["Summary"] and "**Technical Pipeline**\nThe team" in doc.sections["Summary"])
check("multi: decisions demote H2 to H3", "### Aligned" in doc.sections["Decisions"] and "### Shelved" in doc.sections["Decisions"])
check("multi: decisions footer stripped", "updated the Decisions" not in doc.sections["Decisions"] and "Helpful" not in doc.sections["Decisions"])
check("multi: next steps count", doc.next_steps_count == 2)
check("multi: details kept", "Database Architecture" in doc.sections["Details"])
check("multi: no footer survives in any section", all("review Gemini's notes" not in t for t in doc.sections.values()))
check("multi: quick topics parsed", [t for t, _ in doc.quick_topics] == ["Rebranding strategy", "Next steps"])
check("multi: quick blurb", doc.quick_blurb == "Rebranding exploration and database evaluation")
check("multi: transcript segments", len(doc.transcript_segments) == 4)
check("multi: transcript ts + speaker", doc.transcript_segments[0] == ("00:05:22", "Priya Okafor", "Hey."))
check("multi: transcript duration", doc.transcript_duration_s == 1 * 3600 + 15 * 60 + 14)

rendered = ingest.render_transcript(doc.transcript_segments)
check("transcript: consecutive same-speaker lines collapse", rendered.count("**Jordan Vale**") == 1 and "Hey, how are you?" in rendered)

# --- 3. body parsing: single-tab ---
doc1 = ingest.parse_doc(SINGLE_TAB)
check("single: title", doc1.title == "Portal roadmap review")
check("single: sections", set(doc1.sections) == {"Summary", "Next steps", "Details"})
check("single: no transcript", doc1.transcript_segments == [] and doc1.transcript_url == "")
check("single: next steps count", doc1.next_steps_count == 1)

# --- 4. render + idempotency + cross-linking, end to end in a temp vault ---
with tempfile.TemporaryDirectory() as d:
    vault = Path(d)
    raw = vault / "raw"
    (raw / "fireflies").mkdir(parents=True)
    (raw / "wispr" / "meetings").mkdir(parents=True)
    # Fireflies started 3 min before Gemini's 18:57 UTC -> inside the window.
    (raw / "fireflies" / "FF1.md").write_text(
        "---\ntype: raw-fireflies\nmeeting_id: FF1\nmeeting_note: \"[[2026-09-10 — Pinecrest Lodge weekly sync]]\"\n"
        "date: 2026-09-10 18:54:00.000000+00:00\n---\n", encoding="utf-8")
    # A second Fireflies meeting 30 min away must NOT match.
    (raw / "fireflies" / "FF2.md").write_text(
        "---\ntype: raw-fireflies\nmeeting_id: FF2\nmeeting_note: \"[[other]]\"\n"
        "date: 2026-09-10 19:27:00.000000+00:00\n---\n", encoding="utf-8")
    (raw / "wispr" / "meetings" / "2026-09-10-pinecrest-lodge-portal-roadmap-review-abcd1234.md").write_text(
        "---\ntype: raw-wispr-meeting\nwispr_meeting_id: abcd1234-0000\ntitle: \"Pinecrest Lodge weekly sync\"\n"
        "timestamp: 2026-09-10 18:48:00\n---\n", encoding="utf-8")

    payload = [{
        "id": "DOC123456789", "name": "Pinecrest Lodge weekly sync - 2026/09/10 14:57 EDT - Notes by Gemini",
        "modified_time": "2026-09-10T20:17:56.874Z", "created_time": "2026-09-10T20:17:01.978Z",
        "link": "https://docs.google.com/document/d/DOC123456789/edit", "markdown": MULTI_TAB,
    }, {
        "id": "NOTGEMINI", "name": "Random Agenda Doc", "modified_time": "2026-09-10T20:17:56.874Z",
        "markdown": "# hi",
    }]
    jpath = vault / "in.json"
    jpath.write_text(json.dumps(payload), encoding="utf-8")

    stats = ingest.Stats()
    preview = []
    out_dir = raw / "gemini" / "meetings"
    docs = ingest.docs_from_json(jpath, stats)
    check("json: both entries loaded", len(docs) == 2 and stats.errors == [])
    ingest.run_ingest(docs, out_dir, raw, None, False, False, stats, "2026-09-23T10:00:00", preview)
    check("dry run: writes nothing", not out_dir.exists())
    check("dry run: preview create", preview and preview[0]["action"] == "create")
    check("dry run: non-gemini skipped", stats.skipped_not_gemini == 1)

    stats = ingest.Stats(); preview = []
    ingest.run_ingest(docs, out_dir, raw, None, True, False, stats, "2026-09-23T10:00:00", preview)
    files = sorted(out_dir.glob("*.md"))
    check("execute: one file", len(files) == 1 and stats.written == 1)
    check("execute: filename shape", files[0].name == "2026-09-10-pinecrest-lodge-weekly-sync-DOC12345.md")
    text = files[0].read_text(encoding="utf-8")
    check("fm: type + id", "type: raw-gemini-meeting" in text and "gemini_doc_id: DOC123456789" in text)
    check("fm: utc timestamp + local", "timestamp: 2026-09-10 18:57:00" in text and 'timestamp_local: "2026-09-10 14:57 EDT"' in text)
    check("fm: source_modified_at normalized", "source_modified_at: 2026-09-10 20:17:56" in text)
    check("fm: duration", 'duration: "1h 15m"' in text)
    check("fm: fireflies nearest match (FF1 not FF2)", "fireflies_id: FF1" in text and "FF2" not in text)
    check("fm: wispr match", "wispr_meeting_id: abcd1234-0000" in text and "wispr_note: \"[[2026-09-10-pinecrest-lodge-portal-roadmap-review-abcd1234]]\"" in text)
    check("fm: transcript flags (available, not included)", "transcript_available: true" in text and "transcript_included: false" in text and "transcript_segments: 4" in text)
    check("body: order Summary -> Decisions -> Next steps -> Details -> Quick notes",
          text.index("## Summary") < text.index("## Decisions") < text.index("## Next steps") < text.index("## Details") < text.index("## Quick notes"))
    check("body: transcript pointer, no transcript text", "get_doc_as_markdown" in text and "Let's talk branding" not in text)
    check("body: quick-notes next steps deduplicated", text.count("Test Name: Share the proposed branding") == 1)
    check("body: cross-link callouts", "> Also captured by Fireflies: [[2026-09-10 — Pinecrest Lodge weekly sync]]" in text and "> Also captured by Wispr:" in text)
    check("body: no gemini boilerplate", "review Gemini's notes" not in text and "short survey" not in text)

    # unchanged source -> skip
    stats = ingest.Stats(); preview = []
    ingest.run_ingest(docs, out_dir, raw, None, True, False, stats, "2026-09-23T10:01:00", preview)
    check("idempotent: unchanged source skipped", stats.already_present == 1 and stats.written == 0 and stats.updated == 0)

    # newer modifiedTime -> rewrite in place, no duplicate file
    docs[0].modified_time = "2026-09-11T08:00:00Z"
    stats = ingest.Stats(); preview = []
    ingest.run_ingest(docs, out_dir, raw, None, True, False, stats, "2026-09-23T10:02:00", preview)
    check("idempotent: newer source rewrites in place", stats.updated == 1 and len(list(out_dir.glob("*.md"))) == 1)
    check("idempotent: file carries new source_modified_at", "source_modified_at: 2026-09-11 08:00:00" in files[0].read_text(encoding="utf-8"))

    # incremental cursor: doc modified before the cursor is skipped
    stats = ingest.Stats(); preview = []
    ingest.run_ingest(docs, out_dir, raw, "2026-09-12 00:00:00", True, False, stats, "x", preview)
    check("incremental: older-than-cursor skipped", stats.already_present == 1 and stats.seen == 2)

    # --include-transcript writes the transcript
    docs[0].modified_time = "2026-09-12T08:00:00Z"
    stats = ingest.Stats(); preview = []
    ingest.run_ingest(docs, out_dir, raw, None, True, True, stats, "x", preview)
    text = files[0].read_text(encoding="utf-8")
    check("include-transcript: transcript rendered", "transcript_included: true" in text and "**Priya Okafor** (00:10:32) Good. Let's talk branding." in text)

    # --input-dir mode: markdown export named after the doc
    exp = vault / "exports"
    exp.mkdir()
    (exp / "Portal roadmap review - 2026/07/10 10:01 EDT - Notes by Gemini.md".replace("/", "-")).write_text(SINGLE_TAB, encoding="utf-8")
    stats = ingest.Stats()
    ddocs = ingest.docs_from_dir(exp, stats)
    check("input-dir: loads export", len(ddocs) == 1 and ddocs[0].doc_id.startswith("md-"))
    # slashes are illegal in filenames, so the export name carries dashes; the
    # date must then come from the body instead of the name.
    stats = ingest.Stats(); preview = []
    ingest.run_ingest(ddocs, out_dir, raw, None, True, False, stats, "x", preview)
    check("input-dir: written, date from body", stats.written == 1 and any(p.name.startswith("2026-07-10-portal-roadmap-review-") for p in out_dir.glob("*.md")))

# --- 5. MCP envelope tolerance + drive query ---
check("envelope: {result: ...} accepted", ingest._coerce_markdown('{"result": "# hi"}') == "# hi")
check("envelope: dict accepted", ingest._coerce_markdown({"result": "x"}) == "x")
check("query: no cursor", "modifiedTime" not in ingest.drive_query(None))
check("query: cursor rendered as RFC3339", "modifiedTime > '2026-09-10T20:17:56Z'" in ingest.drive_query("2026-09-10 20:17:56"))
check("rfc3339: offset form normalized to UTC", ingest.normalize_rfc3339("2026-07-10T11:34:33-04:00") == "2026-07-10 15:34:33")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
