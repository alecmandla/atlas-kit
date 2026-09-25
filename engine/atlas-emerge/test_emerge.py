#!/usr/bin/env python3
"""Tests for atlas-emerge: stdlib-only, no framework (matches
atlas-teams-meetings-ingest/test_ingest.py). Builds a throwaway vault and points
the config layer at it through ATLAS_CONFIG before importing emerge. Fixtures use
the fictional world from docs/SCRUB-RULES.md."""

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def write(vault: Path, rel: str, fm: list[str], title: str, body: str) -> None:
    p = vault / "raw" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(fm) + "\n---\n\n# " + title + "\n\n" + body + "\n",
                 encoding="utf-8")


TODAY = dt.date(2026, 9, 25)
ZOOM_UUID = "/Xk9pQ2+vT0aZb1cD3eF4g=="
BODY = "Priya Okafor says the booking-export job still fails after the Ledgerline sync."

tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)
vault = root / "Vault"
vault.mkdir()
cfg_path = root / "atlas.config.json"
cfg_path.write_text(json.dumps({"vault_root": str(vault)}), encoding="utf-8")
os.environ["ATLAS_CONFIG"] = str(cfg_path)
skill_dir = root / "skill"  # no suppress.txt: keep the real one out of the test
skill_dir.mkdir()

import emerge  # noqa: E402  (after ATLAS_CONFIG is set)

check("config: emerge reads the temp vault", emerge.VAULT_DEFAULT == vault)

# --- 1. one call, recorded by three ingests ---
# Fireflies first; Zoom links to Fireflies; Gong links only to Zoom (one-directional,
# so the Gong -> Fireflies connection is transitive through Zoom).
write(vault, "fireflies/01ABCDEF.md",
      ["type: meeting", "meeting_id: 01ABCDEF", "date: 2026-09-10"],
      "2026-09-10 — Pinecrest Lodge weekly sync", BODY)
write(vault, "zoom/meetings/2026-09-10-pinecrest-lodge-weekly-sync-5f3a9c1e.md",
      ["type: raw-zoom-meeting", f"zoom_id: {ZOOM_UUID}", "date: 2026-09-10",
       "fireflies_id: 01ABCDEF", 'fireflies_note: "[[2026-09-10 Pinecrest Lodge weekly sync]]"'],
      "Pinecrest Lodge weekly sync", BODY)
write(vault, "gong/meetings/2026-09-10-pinecrest-lodge-weekly-sync-77823422.md",
      ["type: raw-gong-call", "gong_call_id: 7782342274025937895", "date: 2026-09-10",
       f"zoom_id: {ZOOM_UUID}",
       'zoom_note: "[[2026-09-10-pinecrest-lodge-weekly-sync-5f3a9c1e]]"'],
      "Pinecrest Lodge weekly sync", BODY)


def run(min_items=1, min_sources=2):
    items = emerge.walk_raw(vault, 30, TODAY)
    deny = emerge.build_deny_list(vault, skill_dir)
    clusters = emerge.cluster_candidates(items, deny)
    ranked = emerge.rank_clusters(clusters, min_items, min_sources)
    return items, deny, clusters, ranked


items, deny, clusters, ranked = run()
units = {i["unit"] for i in items}
check("group: three records of one call share one unit", len(items) == 3 and len(units) == 1)
check("group: unit reports the fireflies folder",
      {i["unit_source"] for i in items} == {"fireflies"})
c = clusters.get("booking-export")
check("cluster: one call is one item", c is not None and len(c["items"]) == 1)
check("cluster: one call is one source type", c is not None and c["sources"] == {"fireflies"})
check("rank: one call alone does not meet --min-sources 2",
      "booking-export" not in {r["slug"] for r in ranked})
check("rank: nor the defaults (--min-items 3 --min-sources 2)",
      "booking-export" not in {r["slug"] for r in emerge.rank_clusters(clusters, 3, 2)})

# Guard the test itself: without grouping the same files read as three sources.
for i in items:
    del i["unit"], i["unit_source"]
ungrouped = emerge.cluster_candidates(items, deny)["booking-export"]
check("control: ungrouped, the call would count as 3 items from 3 sources",
      len(ungrouped["items"]) == 3 and len(ungrouped["sources"]) == 3)

# --- 2. a real meeting plus a Slack mention ---
write(vault, "slack/client-updates/1757520000.000100.md",
      ["type: raw-slack-message", "message_ts: 1757520000.000100", "channel_id: C0EXAMPLE001",
       "channel_name: client-updates", "date: 2026-09-11"],
      "#client-updates · 2026-09-11", "Jordan Vale: the booking-export retry is queued.")
items, deny, clusters, ranked = run()
row = {r["slug"]: r for r in ranked}.get("booking-export")
check("rank: meeting + Slack meets --min-sources 2", row is not None)
check("rank: meeting + Slack is 2 items from fireflies and slack",
      row is not None and row["items"] == 2 and row["sources"] == ["fireflies", "slack"])

report = emerge.render_report(ranked, 30, TODAY - dt.timedelta(days=30), TODAY, len(items),
                              emerge.SOURCE_DIRS, len(deny), TODAY,
                              len(items) - len({i["unit"] for i in items}))
check("report: row lists the grouped source once",
      any("`booking-export`" in ln and "| 2 | fireflies, slack |" in ln for ln in report.splitlines()))
check("report: methodology counts the folded records",
      "This run folded 2 such record(s) into another." in report)

# --- 3. grouping rules ---
# Same title, different call: never merged (titles are not compared).
write(vault, "fireflies/01ABCDEF1.md",
      ["type: meeting", "meeting_id: 01ABCDEF1", "date: 2026-09-17"],
      "2026-09-17 — Pinecrest Lodge weekly sync", "Weekly check-in.")
# Wispr Notetaker + Gemini of one call: the group reports wispr-meetings (ranks first).
write(vault, "wispr/meetings/2026-09-15-ledgerline-retail-kickoff-a1b2c3d4.md",
      ["type: raw-wispr-meeting", "wispr_meeting_id: a1b2c3d4-0000-4000-8000-000000000001",
       "timestamp: 2026-09-15 16:00:00"],
      "Ledgerline retail kickoff", "Marcus Hale walked through pricing.")
write(vault, "gemini/meetings/2026-09-15-ledgerline-retail-kickoff-1f2e3d4c.md",
      ["type: raw-gemini-meeting", "gemini_doc_id: 1f2e3d4c-doc", "date: 2026-09-15",
       "wispr_meeting_id: a1b2c3d4-0000-4000-8000-000000000001"],
      "Ledgerline retail kickoff", "Marcus Hale walked through pricing.")
# A lone Wispr meeting is its own wispr-meetings unit; a dictation stays plain wispr.
write(vault, "wispr/meetings/2026-09-16-portal-roadmap-review-b2c3d4e5.md",
      ["type: raw-wispr-meeting", "wispr_meeting_id: b2c3d4e5-0000-4000-8000-000000000002",
       "timestamp: 2026-09-16 10:00:00"],
      "Portal roadmap review", "Portal notes.")
write(vault, "wispr/2026-09-16-dictation.md",
      ["type: raw-wispr", "wispr_id: 9f8e7d6c", "timestamp: 2026-09-16 11:00:00"],
      "Dictation", "Idea for the portal.")
# A Teams record whose link targets a Fireflies record outside the corpus still
# groups with a Gong record carrying the same Fireflies id.
write(vault, "teams/meetings/2026-09-18-portal-roadmap-review-3c4d5e6f.md",
      ["type: raw-teams-meeting", "teams_meeting_id: teams-1a2b3c4d5e6f", "date: 2026-09-18",
       "fireflies_id: 01ABCDEF2"],
      "Portal roadmap review", "Roadmap.")
write(vault, "gong/meetings/2026-09-18-portal-roadmap-review-88000001.md",
      ["type: raw-gong-call", "gong_call_id: 8800000100000000001", "date: 2026-09-18",
       "fireflies_id: 01ABCDEF2"],
      "Portal roadmap review", "Roadmap.")

items = emerge.walk_raw(vault, 30, TODAY)
by_name = {i["path"].name: i for i in items}
ff_a, ff_b = by_name["01ABCDEF.md"], by_name["01ABCDEF1.md"]
check("rule: same title, different call is a separate unit", ff_a["unit"] != ff_b["unit"])
wm = by_name["2026-09-15-ledgerline-retail-kickoff-a1b2c3d4.md"]
gm = by_name["2026-09-15-ledgerline-retail-kickoff-1f2e3d4c.md"]
check("rule: wispr + gemini of one call share a unit", wm["unit"] == gm["unit"])
check("rule: that group reports wispr-meetings, not gemini",
      wm["unit_source"] == "wispr-meetings" and gm["unit_source"] == "wispr-meetings")
lone = by_name["2026-09-16-portal-roadmap-review-b2c3d4e5.md"]
dict_ = by_name["2026-09-16-dictation.md"]
check("rule: a lone wispr meeting is wispr-meetings", lone["unit_source"] == "wispr-meetings")
check("rule: a dictation is source wispr", dict_["source"] == "wispr")
check("rule: a dictation is its own unit", dict_["unit"] == str(dict_["path"]))
tm = by_name["2026-09-18-portal-roadmap-review-3c4d5e6f.md"]
gg = by_name["2026-09-18-portal-roadmap-review-88000001.md"]
check("rule: a shared link id groups records whose target is absent", tm["unit"] == gg["unit"])
check("rule: teams outranks gong", tm["unit_source"] == "teams" and gg["unit_source"] == "teams")
check("rule: slack is untouched",
      by_name["1757520000.000100.md"]["unit_source"] == "slack")

tmp.cleanup()
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("all passed")
