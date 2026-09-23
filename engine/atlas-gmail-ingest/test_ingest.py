#!/usr/bin/env python3
"""Tests for ingest.py's label map + routing — stdlib-only, no framework
(matches atlas-fireflies-ingest/test_link_prep.py)."""

import json
import sys
import tempfile
from pathlib import Path

import ingest

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def labels_map(payload):
    """Write `payload` as a --labels-json file and parse it. Returns (map, stats)."""
    stats = ingest.Stats()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "labels.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return ingest.parse_labels_json([p], stats), stats


# --- 1. REGRESSION: the real Gmail MCP shape keys the ID as `labelId`, not `id` ---
# Reading only `id` produced an empty map, so --labels-json was inert and tier-1
# label routing never fired — everything fell through to domain/_unrouted.
MCP_SHAPE = {
    "labels": [
        {"labelId": "Label_1234567890123456789", "name": "3. ADD TO TRIAGE",
         "messagesTotal": 94, "messagesUnread": 12,
         "threadsTotal": 80, "threadsUnread": 10},
        {"labelId": "Label_20", "name": "Triage-Forwarded", "messagesTotal": 69},
        {"labelId": "INBOX", "name": "INBOX", "messagesTotal": 122},
    ]
}
m, stats = labels_map(MCP_SHAPE)
check("labelId shape: map is non-empty", len(m) == 3)
check("labelId shape: opaque ID resolves to display name",
      m.get("Label_1234567890123456789") == "3. ADD TO TRIAGE")
check("labelId shape: system label resolves too", m.get("INBOX") == "INBOX")
check("labelId shape: no parse errors", stats.errors == [])

# --- 2. raw Gmail API shape (`id`) still works ---
m, _ = labels_map({"labels": [{"id": "Label_1", "name": "client/pinecrest-lodge"}]})
check("id shape: still resolves", m.get("Label_1") == "client/pinecrest-lodge")

# --- 3. bare array, either key ---
m, _ = labels_map([{"labelId": "Label_2", "name": "hq/general"},
                   {"id": "Label_3", "name": "hq/eng"}])
check("bare array: labelId entry resolves", m.get("Label_2") == "hq/general")
check("bare array: id entry resolves", m.get("Label_3") == "hq/eng")

# --- 4. displayName fallback for the name ---
m, _ = labels_map({"labels": [{"labelId": "Label_4", "displayName": "client/saltmarsh-inn"}]})
check("displayName fallback: resolves", m.get("Label_4") == "client/saltmarsh-inn")

# --- 5. malformed entries are skipped, not mapped under a null key ---
m, _ = labels_map({"labels": [
    {"name": "no id at all"},
    {"labelId": "Label_5"},          # no name
    "not-a-dict",
    {"labelId": "Label_6", "name": "keeper"},
]})
check("malformed: only the well-formed entry survives", m == {"Label_6": "keeper"})

# --- 6. unreadable file records an error and yields an empty map ---
stats = ingest.Stats()
with tempfile.TemporaryDirectory() as d:
    bad = Path(d) / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    m = ingest.parse_labels_json([bad], stats)
check("bad json: empty map", m == {})
check("bad json: error recorded", len(stats.errors) == 1)

# --- 7. end-to-end: the labelId shape actually drives label routing ---
# This is the assertion that would have caught the bug at the routing layer:
# with the real MCP shape, a message carrying only an opaque ID routes by label.
LABEL_ROUTES = [{"label": "3. ADD TO TRIAGE", "route": "add-to-triage"}]
DOMAIN_ROUTES = {"harborlane.example": "hq"}
TRIAGE = {"labels": [{"labelId": "Label_1234567890123456789", "name": "3. ADD TO TRIAGE"}]}
names, _ = labels_map(TRIAGE)
route, tier = ingest.route_message(["Label_1234567890123456789"], "outsider@example.com",
                                   LABEL_ROUTES, DOMAIN_ROUTES, names)
check("e2e: label routes mail no domain rule claimed",
      (route, tier) == ("add-to-triage", "label"))

# ...and with an empty map (the pre-fix state) the same message is unrouted.
route, tier = ingest.route_message(["Label_1234567890123456789"], "outsider@example.com",
                                   LABEL_ROUTES, DOMAIN_ROUTES, {})
check("e2e: empty map drops the message to _unrouted",
      (route, tier) == ("_unrouted", "unrouted"))

# --- 8. PRECEDENCE: domain is tier 1, label is tier 2 ---
# A client email that also carries a triage label stays in its client folder;
# triaging must never pull mail out of the folder for its correspondent.
route, tier = ingest.route_message(["Label_1234567890123456789"], "jordan.vale@harborlane.example",
                                   LABEL_ROUTES, DOMAIN_ROUTES, names)
check("precedence: domain beats label", (route, tier) == ("hq", "domain"))

# --- 9. nested-label last-segment fallback still matches ---
names_nested, _ = labels_map({"labels": [{"labelId": "Label_98", "name": "UTMCreator"}]})
route, tier = ingest.route_message(
    ["Label_98"], "x@example.com",
    [{"label": "Marketing Automation/UTMCreator", "route": "utm"}], {}, names_nested)
check("nested fallback: flat label matches nested route", (route, tier) == ("utm", "label"))

# --- 10. unresolved ID falls back to itself, does not match a route ---
route, tier = ingest.route_message(["Label_unknown"], "x@example.com",
                                   LABEL_ROUTES, {}, {})
check("unresolved id: unrouted", (route, tier) == ("_unrouted", "unrouted"))

# --- 11. the shipped mailbox-routing.yaml only names labels that exist ---
# A route that names a label which does not exist in Gmail matches nothing even
# once the map works, so the shipped YAML is checked against a captured label list.
REAL_LABELS = {e["name"] for e in json.loads(
    Path("/tmp/atlas-gmail-labels.json").read_text())["labels"]} \
    if Path("/tmp/atlas-gmail-labels.json").exists() else None
if REAL_LABELS is None:
    print("skip labels-exist check (no /tmp/atlas-gmail-labels.json capture)")
else:
    shipped, _ = ingest.parse_yaml_routing()
    missing = [e["label"] for e in shipped if e["label"] not in REAL_LABELS]
    check(f"yaml labels all exist in Gmail (missing: {missing})", not missing)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
    sys.exit(1)
print("all passed")
