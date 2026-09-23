#!/usr/bin/env python3
"""Behavioral tests for graduate.py's stdlib frontmatter read paths.
Run under the dep-free system python3 (no yaml)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import graduate as g

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

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
