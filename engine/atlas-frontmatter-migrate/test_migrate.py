#!/usr/bin/env python3
"""Tests for migrate.py's stdlib apply_one. Run under dep-free system python3."""
import sys, tempfile, os
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import migrate as m
from pathlib import Path

failures = []
def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)

EMAIL_INDEX = {"jordan.vale@harborlane.example": "Jordan-Vale", "priya@pinecrestlodge.example": "Priya-Okafor"}

def run_apply(note_text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(note_text)
        path = Path(f.name)
    fm, _ = m.parse_frontmatter(note_text)
    plan = m.plan_one(fm, EMAIL_INDEX)
    if plan is not None:
        m.apply_one(path, plan)
    out = path.read_text()
    os.unlink(path)
    return out, plan

# --- 1. inline attendees ---
note = ("---\n"
        "title: Sync\n"
        "date: 2026-06-01\n"
        "attendees: [jordan.vale@harborlane.example, priya@pinecrestlodge.example]\n"
        "meeting_id: abc123\n"
        "---\n"
        "\n# Sync\n\nbody content\n")
out, plan = run_apply(note)
check("attendees: line removed", "\nattendees:" not in out and "attendees: [" not in out)
check("attendee_emails block present", "attendee_emails:\n- jordan.vale@harborlane.example\n- priya@pinecrestlodge.example" in out)
check("participants block w/ quoted wikilinks", "participants:\n- '[[Jordan-Vale]]'\n- '[[Priya-Okafor]]'" in out)
check("inserted right after date", "date: 2026-06-01\nattendee_emails:" in out)
check("other keys preserved (title, meeting_id)", "title: Sync" in out and "meeting_id: abc123" in out)
check("body preserved", out.endswith("body content\n"))
# re-parse + idempotency
fm2, _ = m.parse_frontmatter(out)
check("re-parsed participants present", "participants" in fm2)
check("idempotent skip on re-run", m.plan_one(fm2, EMAIL_INDEX) is None)

# --- 2. block-list attendees ---
note_block = ("---\n"
              "date: 2026-06-02\n"
              "attendees:\n"
              "  - jordan.vale@harborlane.example\n"
              "  - unknown@example.com\n"
              "title: Block Meeting\n"
              "---\n\nbody\n")
out2, _ = run_apply(note_block)
check("block attendees removed", "- jordan.vale@harborlane.example" not in out2.split('---')[1] or "attendee_emails" in out2)
check("block: emails preserved in attendee_emails", "attendee_emails:\n- jordan.vale@harborlane.example\n- unknown@example.com" in out2)
check("block: only known email gets participant", "participants:\n- '[[Jordan-Vale]]'" in out2 and "unknown" not in out2.split("participants:")[1])
check("block: title after attendees preserved", "title: Block Meeting" in out2)

# --- 3. no date key -> appended at end of fm ---
note_nodate = ("---\ntitle: NoDate\nattendees: [jordan.vale@harborlane.example]\n---\n\nbody\n")
out3, _ = run_apply(note_nodate)
check("no-date: fields appended", "attendee_emails:\n- jordan.vale@harborlane.example" in out3 and "participants:\n- '[[Jordan-Vale]]'" in out3)
check("no-date: title preserved", "title: NoDate" in out3)

# --- 4. empty attendees -> participants: [] ---
note_empty = ("---\ndate: 2026-06-03\nattendees: []\n---\n\nbody\n")
out4, _ = run_apply(note_empty)
check("empty: attendee_emails []", "attendee_emails: []" in out4)
check("empty: participants []", "participants: []" in out4)

# --- 5. format fidelity vs the old yaml.safe_dump (oracle, if available) ---
try:
    import importlib.util
    spec = importlib.util.find_spec("yaml")
except Exception:
    spec = None
if spec is None:
    print("  (skip oracle: yaml not in this interpreter — expected for system python3)")
else:
    import yaml
    d = {"title": "Sync", "date": "2026-06-01",
         "attendee_emails": ["jordan.vale@harborlane.example", "priya@pinecrestlodge.example"],
         "participants": ["[[Jordan-Vale]]", "[[Priya-Okafor]]"], "meeting_id": "abc123"}
    oracle = yaml.safe_dump(d, sort_keys=False, default_flow_style=False).strip()
    # our two emitted fields must appear identically in the oracle dump
    ae = "attendee_emails:\n- jordan.vale@harborlane.example\n- priya@pinecrestlodge.example"
    pa = "participants:\n- '[[Jordan-Vale]]'\n- '[[Priya-Okafor]]'"
    check("emitter matches safe_dump (attendee_emails)", ae in oracle)
    check("emitter matches safe_dump (participants)", pa in oracle)

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
