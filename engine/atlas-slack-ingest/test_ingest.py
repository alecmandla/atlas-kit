#!/usr/bin/env python3
"""Regression tests for atlas-slack-ingest message parsing.

Run standalone:  python3 test_ingest.py
Or with pytest:  pytest test_ingest.py

Covers the three Slack-MCP header shapes. A full sweep silently skips shapes 2
and 3 when MESSAGE_BLOCK_RE only matches shape 1, and a too-narrow body
lookahead lets shape-1 bodies swallow the shape-3 posts that follow them.
"""

from ingest import parse_messages

# Trailing space after the closing "===" — the MCP emits one; kept out of the
# source as a literal so editors/linters don't strip it.
SP = " "

# A realistic page ordered the way the real data is: an attributed message
# immediately followed by attribution-less posts (the pollution trigger), then
# an empty-sender bot post.
SAMPLE_PAGE = (
    f"=== Message from Jordan Vale <jordan.vale@harborlane.example> (U0EXAMPLE001) at 2022-01-11 12:32:59 EST ==={SP}\n"
    "Message TS: 1641922379.007200\n"
    "it's the 1st one, yes. Thanks to you both!\n"
    "Reactions: partyparrot (1)\n"
    "\n"
    f"=== Message at 2022-01-11 12:26:52 EST ==={SP}\n"
    "Message TS: 1641922012.005600\n"
    "<@U0EXAMPLE002|Priya Okafor> can you tell me the sales body\n"
    "\n"
    f"=== Message at 2022-01-11 12:31:23 EST ==={SP}\n"
    "Message TS: 1641922283.006600\n"
    "`{`\n"
    '    `"BookingId":  38504`\n'
    " `}`\n"
    "\n"
    f"=== Message from  (B0EXAMPLE001) at 2026-05-01 09:00:00 EST ==={SP}\n"
    "Message TS: 1746104400.000100\n"
    'Campaign "Spring Open" is awaiting approval.\n'
)


def _by_ts(text=SAMPLE_PAGE):
    return {m["ts"]: m for m in parse_messages(text)}


def test_all_three_shapes_parse():
    # Shape 1 + 2 shape-3 + shape 2 = 4 messages. Pre-fix only shape 1 matched.
    assert len(parse_messages(SAMPLE_PAGE)) == 4


def test_attributed_user():
    m = _by_ts()["1641922379.007200"]
    assert m["user"] == "U0EXAMPLE001"
    assert m["user_profile"]["real_name"] == "Jordan Vale <jordan.vale@harborlane.example>"


def test_empty_sender_bot_preserves_bot_id():
    # Shape 2: two spaces after "from", empty name. The bot id must survive so
    # write_message can fall back to it for sender_name.
    m = _by_ts()["1746104400.000100"]
    assert m["user"] == "B0EXAMPLE001"
    assert m["user_profile"]["real_name"] == ""


def test_attribution_less_with_leading_mention():
    m = _by_ts()["1641922012.005600"]
    assert m["user"] == "U0EXAMPLE002"
    assert m["user_profile"]["real_name"] == "Priya Okafor"


def test_attribution_less_without_mention_is_unknown():
    m = _by_ts()["1641922283.006600"]
    assert m["user"] == ""
    assert m["user_profile"]["real_name"] == "unknown"


def test_no_body_pollution_across_shape_boundary():
    # The attributed message body must NOT absorb the following attribution-less
    # blocks — the exact corruption found in the existing vault files.
    m = _by_ts()["1641922379.007200"]
    assert m["text"].startswith("it's the 1st one")
    for leaked in ("Message TS", "Priya", "BookingId", "=== Message"):
        assert leaked not in m["text"], f"body leaked: {leaked!r}"


def test_pre_fix_regex_skipped_shapes_2_and_3():
    # Documents the bug: the old pattern matched only the attributed message.
    import re

    old = re.compile(
        r"=== Message from (.+?) \(([^)]+)\) at (.+?) ===\s*\n"
        r"Message TS: (\S+)\n(.*?)(?=\n=== Message from |\Z)",
        re.DOTALL,
    )
    old_ids = {m.group(4) for m in old.finditer(SAMPLE_PAGE)}
    assert old_ids == {"1641922379.007200"}
    assert len(_by_ts()) == 4


if __name__ == "__main__":
    import sys

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
