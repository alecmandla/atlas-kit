#!/usr/bin/env python3
"""atlas-fireflies-ingest — mechanical dedup + state helper (stdlib-only, DEC-021).

The fireflies ingest is agent-driven (MCP fetching + routing judgment), but its
dedup and cursor rules used to be natural-language instructions with
corruption-shaped ambiguities: an 8-directory grep nothing verified for
completeness, an undefined cursor rule on zero-fetch runs, and an ambiguous
backfill cursor. This helper makes those steps deterministic:

    python3 fireflies_state.py guard [--minutes 30]
        Duplicate-fire guard. Exit 1 (prints why) when the last run finished
        within N minutes — the scheduler's thundering-herd bug fires a task
        10-16x on wake. Exit 0 = proceed.

    python3 fireflies_state.py since
        Print the fetch-window start: state.json's last_run_iso, or 30 days
        back on first run. One canonical implementation of "the window".

    python3 fireflies_state.py check-dup <meeting_id>
        Search ALL eight vault roots for an existing note with this
        meeting_id. Prints "duplicate: <path>" or "new". Never write a
        meeting whose id prints duplicate.

    python3 fireflies_state.py advance --newest <iso> [--count N]
        After a successful run: cursor = newest-meeting-timestamp + 1s.
        Called WITHOUT --newest (a zero-fetch run) it changes nothing and
        says so — never rewind or reset the cursor on an empty run.

    python3 fireflies_state.py backfill-done --through <iso>
        After a backfill: set the cursor to the END of the window you
        actually fetched (a full ISO timestamp, not a date — a date-only
        value is interpreted as midnight and re-fetches that whole day).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
STATE_FILE = Path(__file__).resolve().parent / "state.json"
VAULT = CFG.vault_root
# All roots the filing/archival pipeline can land a meeting note in (SKILL.md §4).
VAULT_ROOTS = [CFG.folder_name(k) for k in (
    "inbox", "daily", "projects", "areas",
    "resources", "archive", "attachments", "raw",
)]
FRONTMATTER_PROBE_BYTES = 4096  # meeting_id lives in frontmatter; no need to read bodies


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def parse_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def cmd_guard(minutes: int) -> int:
    last = load_state().get("last_run_iso")
    if not last:
        print("proceed: no previous run recorded")
        return 0
    try:
        age = dt.datetime.now(dt.timezone.utc) - parse_iso(last)
    except ValueError:
        print(f"proceed: unparseable last_run_iso ({last!r})")
        return 0
    if age < dt.timedelta(minutes=minutes):
        print(f"skipped: last run was {int(age.total_seconds() // 60)} minutes ago "
              "(duplicate-fire guard)", file=sys.stderr)
        return 1
    print(f"proceed: last run {age} ago")
    return 0


def cmd_since() -> int:
    last = load_state().get("last_run_iso")
    if last:
        print(last)
    else:
        print((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
              .isoformat(timespec="seconds"))
    return 0


def cmd_check_dup(meeting_id: str) -> int:
    needle = f"meeting_id: {meeting_id}"
    needle_q = f'meeting_id: "{meeting_id}"'
    searched = 0
    for root_name in VAULT_ROOTS:
        root = VAULT / root_name
        if not root.exists():
            continue  # search-tolerate (raw/ may not exist yet)
        for md in root.rglob("*.md"):
            searched += 1
            try:
                with open(md, encoding="utf-8", errors="replace") as f:
                    head = f.read(FRONTMATTER_PROBE_BYTES)
            except OSError:
                continue
            if needle in head or needle_q in head:
                print(f"duplicate: {md}")
                return 0
    print(f"new (searched {searched} notes across {len(VAULT_ROOTS)} roots)")
    return 0


def cmd_advance(newest: str | None, count: int) -> int:
    state = load_state()
    if not newest:
        print("zero-fetch: cursor unchanged "
              f"(last_run_iso stays {state.get('last_run_iso')!r})")
        return 0
    try:
        cursor = parse_iso(newest) + dt.timedelta(seconds=1)
    except ValueError:
        print(f"error: --newest {newest!r} is not an ISO timestamp", file=sys.stderr)
        return 2
    state["last_run_iso"] = cursor.isoformat(timespec="seconds")
    state["last_run_count"] = count if count >= 0 else state.get("last_run_count", 0)
    save_state(state)
    print(f"cursor -> {state['last_run_iso']} (newest meeting + 1s)")
    return 0


def cmd_backfill_done(through: str) -> int:
    if "T" not in through:
        print("error: --through must be a full ISO timestamp (a date-only value is "
              "midnight and would re-fetch that whole day)", file=sys.stderr)
        return 2
    state = load_state()
    try:
        cursor = parse_iso(through)
    except ValueError:
        print(f"error: --through {through!r} is not an ISO timestamp", file=sys.stderr)
        return 2
    state["last_run_iso"] = cursor.isoformat(timespec="seconds")
    save_state(state)
    print(f"cursor -> {state['last_run_iso']} (backfill window end)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="fireflies ingest dedup + state helper")
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("guard")
    g.add_argument("--minutes", type=int, default=30)
    sub.add_parser("since")
    c = sub.add_parser("check-dup")
    c.add_argument("meeting_id")
    a = sub.add_parser("advance")
    a.add_argument("--newest", default=None)
    a.add_argument("--count", type=int, default=-1)
    b = sub.add_parser("backfill-done")
    b.add_argument("--through", required=True)
    args = ap.parse_args(argv)

    if args.command == "guard":
        return cmd_guard(args.minutes)
    if args.command == "since":
        return cmd_since()
    if args.command == "check-dup":
        return cmd_check_dup(args.meeting_id)
    if args.command == "advance":
        return cmd_advance(args.newest, args.count)
    return cmd_backfill_done(args.through)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
