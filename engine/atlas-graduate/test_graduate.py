#!/usr/bin/env python3
"""Behavioral tests for graduate.py's stdlib frontmatter read paths, and for
auto_graduate's work-source rule fed by a real atlas-emerge report.
Run under the dep-free system python3 (no yaml)."""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

# Point the config layer at a throwaway vault before any engine module loads it.
_tmp = tempfile.TemporaryDirectory()
TMP = Path(_tmp.name)
VAULT = TMP / "Vault"
VAULT.mkdir()
(TMP / "atlas.config.json").write_text(json.dumps({"vault_root": str(VAULT)}), encoding="utf-8")
os.environ["ATLAS_CONFIG"] = str(TMP / "atlas.config.json")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "atlas-emerge"))
import graduate as g
import auto_graduate as ag
import emerge

failures = []
def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)

print("parse_frontmatter_block:")
fm = g.parse_frontmatter_block(
    "type: meeting\n"
    "date: 2026-06-01\n"
    "participants: [[[Jordan-Vale]], [[Priya-Okafor]]]\n"
    "tags: [meeting, client/foo]\n"
)
check("scalar parsed", fm["type"] == "meeting")
check("inline wikilink list -> intended strings",
      fm["participants"] == ["[[Jordan-Vale]]", "[[Priya-Okafor]]"])
check("inline plain list", fm["tags"] == ["meeting", "client/foo"])

# block list
fm2 = g.parse_frontmatter_block(
    "sources:\n- claude-history\n- slack\nmention_count: 12\n"
)
check("block list parsed", fm2["sources"] == ["claude-history", "slack"])
check("scalar after block list", fm2["mention_count"] == "12")

print("aggregate_contributors (inline wikilink participants now counted):")
matches = [
    {"fm": {"participants": ["[[Jordan-Vale]]", "[[Priya-Okafor]]"]}},
    {"fm": {"participants": ["[[Jordan-Vale]]"]}},
    {"fm": {"participants": "[[Marcus-Hale]]"}},  # scalar string form
]
contrib = dict(g.aggregate_contributors(matches))
check("Jordan counted twice", contrib.get("Jordan-Vale") == 2)
check("Priya counted once", contrib.get("Priya-Okafor") == 1)
check("scalar-string participant counted", contrib.get("Marcus-Hale") == 1)

print("add_tag_to_frontmatter:")
# add to inline tags list
t = "---\ntype: meeting\ntags: [meeting]\n---\nbody here\n"
new, changed = g.add_tag_to_frontmatter(t, "thread/foo")
check("tag added", changed and "thread/foo" in new)
check("existing tag preserved", "meeting" in new)
# idempotent: re-adding same tag is a no-op
new2, changed2 = g.add_tag_to_frontmatter(new, "thread/foo")
check("idempotent re-add is no-op", changed2 is False and new2 == new)
# body untouched
check("body preserved", new.endswith("body here\n"))
# no frontmatter at all
new3, changed3 = g.add_tag_to_frontmatter("just body, no fm\n", "thread/bar")
check("adds fm when absent", changed3 and new3.startswith("---\ntags: [thread/bar]\n---\n"))
# tags as a string (not list)
ts = "---\ntags: meeting\n---\nx"
new4, changed4 = g.add_tag_to_frontmatter(ts, "thread/baz")
check("string tags -> list with new tag", changed4 and "thread/baz" in new4)

print("auto_graduate work sources (DEC-019):")
check("meeting ingests are work sources",
      {"fireflies", "wispr-meetings", "gemini", "teams", "zoom", "gong"} <= ag.WORK_SOURCES)
check("wispr dictations stay a self source",
      "wispr" in ag.SELF_SOURCES and "wispr" not in ag.WORK_SOURCES)


def raw(rel, fm, title, body):
    p = VAULT / "raw" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(fm) + "\n---\n\n# " + title + "\n\n" + body + "\n",
                 encoding="utf-8")


TODAY = dt.date(2026, 9, 25)
ZOOM_A = "/Xk9pQ2+vT0aZb1cD3eF4g=="
ZOOM_B = "Qm9ub1N1bW1hcnk+Zm9yWm9vbQ=="
# Call A: one Pinecrest call captured by Fireflies, Zoom, and Gong (Gong links to Zoom,
# Zoom links to Fireflies). Only this call mentions booking-export.
A_BODY = "Priya Okafor flagged the booking-export failure again."
raw("fireflies/01ABCDEF.md", ["type: meeting", "meeting_id: 01ABCDEF", "date: 2026-09-10"],
    "2026-09-10 — Pinecrest Lodge weekly sync", A_BODY)
raw("zoom/meetings/2026-09-10-pinecrest-lodge-weekly-sync-5f3a9c1e.md",
    ["type: raw-zoom-meeting", f"zoom_id: {ZOOM_A}", "date: 2026-09-10", "fireflies_id: 01ABCDEF"],
    "Pinecrest Lodge weekly sync", A_BODY)
raw("gong/meetings/2026-09-10-pinecrest-lodge-weekly-sync-77823422.md",
    ["type: raw-gong-call", "gong_call_id: 7782342274025937895", "date: 2026-09-10",
     f"zoom_id: {ZOOM_A}"],
    "Pinecrest Lodge weekly sync", A_BODY)
# Call B: a Ledgerline call captured by Zoom and Gong, plus a two-message Slack thread.
# Only these mention rate-parity.
B_BODY = "Marcus Hale asked whether rate-parity checks cover the retail feed."
raw("zoom/meetings/2026-09-15-ledgerline-retail-kickoff-9a8b7c6d.md",
    ["type: raw-zoom-meeting", f"zoom_id: {ZOOM_B}", "date: 2026-09-15"],
    "Ledgerline retail kickoff", B_BODY)
raw("gong/meetings/2026-09-15-ledgerline-retail-kickoff-82000000.md",
    ["type: raw-gong-call", "gong_call_id: 8200000000100000001", "date: 2026-09-15",
     f"zoom_id: {ZOOM_B}"],
    "Ledgerline retail kickoff", B_BODY)
raw("slack/client-updates/1757948400.000100.md",
    ["type: raw-slack-message", "message_ts: 1757948400.000100", "channel_id: C0EXAMPLE001",
     "date: 2026-09-15", "thread_ts: 1757948400.000100", "is_thread_reply: false"],
    "#client-updates · 2026-09-15", "Jordan Vale: rate-parity numbers for Ledgerline are off.")
raw("slack/client-updates/1757948700.000200.md",
    ["type: raw-slack-message", "message_ts: 1757948700.000200", "channel_id: C0EXAMPLE001",
     "date: 2026-09-15", "thread_ts: 1757948400.000100", "is_thread_reply: true"],
    "#client-updates · 2026-09-15", "Jordan Vale: rate-parity fix goes out Friday.")

# Call C: a Wispr Notetaker meeting plus a Slack thread, on room-block-audit.
# D: a Wispr dictation plus a Slack thread, on guest-portal-latency.
raw("wispr/meetings/2026-09-16-portal-roadmap-review-b2c3d4e5.md",
    ["type: raw-wispr-meeting", "wispr_meeting_id: b2c3d4e5-0000-4000-8000-000000000002",
     "date: 2026-09-16", "has_notes: true"],
    "2026-09-16 — Portal roadmap review", "## My Notes\n\nStart the room-block-audit for Pinecrest.")
raw("wispr/2026-09-16-dictation.md",
    ["type: raw-wispr", "wispr_id: 9f8e7d6c", "timestamp: 2026-09-16 11:00:00"],
    "Dictation", "Note to self: guest-portal-latency is getting worse.")
for n, (ts, reply) in enumerate((("1758034800.000100", False), ("1758035100.000200", True))):
    raw(f"slack/client-updates/{ts}.md",
        ["type: raw-slack-message", f"message_ts: {ts}", "channel_id: C0EXAMPLE001",
         "date: 2026-09-16", "thread_ts: 1758034800.000100",
         f"is_thread_reply: {str(reply).lower()}"],
        "#client-updates · 2026-09-16",
        "Jordan Vale: room-block-audit and guest-portal-latency both on my list.")

skill_dir = TMP / "emerge-skill"  # no suppress.txt
skill_dir.mkdir()
items = emerge.walk_raw(VAULT, 30, TODAY)
deny = emerge.build_deny_list(VAULT, skill_dir)
clusters = emerge.cluster_candidates(items, deny)
folded = len(items) - len({i["unit"] for i in items})


def report_patterns(min_items, min_sources, name):
    ranked = emerge.rank_clusters(clusters, min_items, min_sources)
    text = emerge.render_report(ranked, 30, TODAY - dt.timedelta(days=30), TODAY, len(items),
                                emerge.SOURCE_DIRS, len(deny), TODAY, folded)
    path = TMP / name
    path.write_text(text, encoding="utf-8")
    return g.parse_emerge_report(path)


# Emerge at its defaults (>= 3 items, >= 2 source types), then auto_graduate's rule.
pats = report_patterns(3, 2, "Emerging-Patterns.md")
check("one call on three ingests does not even surface", "booking-export" not in pats)
b = pats.get("rate-parity")
check("meeting + Slack thread surfaces with the call counted once",
      b is not None and b["items"] == 3 and b["sources"] == ["slack", "zoom"])
check("meeting + Slack thread seen on 2 runs auto-graduates",
      b is not None and ag.classify(b, "rate-parity", 2)[0] == "auto")
check("meeting + Slack thread seen on 1 run is pending",
      b is not None and ag.classify(b, "rate-parity", 1)[0] == "pending")
check("no guard stops rate-parity",
      ag.guard_reason("rate-parity", set(), set(), set(), set()) is None)

c = pats.get("room-block-audit")
check("Wispr Notetaker meeting reports as wispr-meetings",
      c is not None and c["sources"] == ["slack", "wispr-meetings"])
check("Wispr Notetaker meeting + Slack thread seen on 2 runs auto-graduates",
      c is not None and ag.classify(c, "room-block-audit", 2)[0] == "auto")
d = pats.get("guest-portal-latency")
check("Wispr dictation still reports as wispr",
      d is not None and d["sources"] == ["slack", "wispr"])
check("Wispr dictation + Slack thread does not auto-graduate",
      d is not None and ag.classify(d, "guest-portal-latency", 2)[0] == "review")

# Even with emerge's floors dropped to 1, the lone call reaches auto_graduate as one source.
loose = report_patterns(1, 1, "Emerging-Patterns-loose.md")
a = loose.get("booking-export")
check("lone call reaches the report as fireflies alone",
      a is not None and a["items"] == 1 and a["sources"] == ["fireflies"])
check("lone call seen on 2 runs does not auto-graduate",
      a is not None and ag.classify(a, "booking-export", 2)[0] == "review")
check("control: had the call counted as three sources it would have",
      ag.classify({"sources": ["fireflies", "gong", "zoom"], "items": 1},
                  "booking-export", 2)[0] == "auto")

_tmp.cleanup()
print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
