#!/usr/bin/env python3
"""Suite-level run lock for the atlas-nightly chain (stdlib-only, DEC-021).

The Claude desktop scheduler has a documented thundering-herd bug that can fire
a scheduled task 10-16 times in a few seconds on wake-from-sleep. Concurrent
nightly chains race on state files and daily-note section edits. This lock makes
step 0 of atlas-nightly mutual-exclusive:

    python3 <repo>/engine/atlas-nightly/lock.py acquire   # exit 0 = you hold it
    python3 <repo>/engine/atlas-nightly/lock.py release
    python3 <repo>/engine/atlas-nightly/lock.py status

`acquire` exits 1 (and prints the holder's age) when a fresh lock is held —
the caller must STOP and report "another nightly run is in progress", never
proceed. A lock older than --stale-minutes (default 180) is presumed crashed
and is stolen atomically.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / ".atlas-suite.lock"


def try_create(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(f"pid={os.getpid()} acquired={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    return True


def acquire(stale_minutes: int) -> int:
    if try_create(LOCK_PATH):
        print(f"acquired: {LOCK_PATH}")
        return 0
    try:
        age = time.time() - LOCK_PATH.stat().st_mtime
    except FileNotFoundError:
        # Holder released between our O_EXCL failure and the stat — retry once.
        if try_create(LOCK_PATH):
            print(f"acquired: {LOCK_PATH}")
            return 0
        print("held: lost the re-acquire race", file=sys.stderr)
        return 1
    if age > stale_minutes * 60:
        # Presumed-crashed holder. Unlink and retry once; if a rival stealer
        # wins the O_EXCL race, we lose gracefully.
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        if try_create(LOCK_PATH):
            print(f"acquired (stole stale lock, age {age/60:.0f}m): {LOCK_PATH}")
            return 0
        print("held: another run stole the stale lock first", file=sys.stderr)
        return 1
    print(f"held: lock is {age/60:.1f}m old (stale after {stale_minutes}m) — "
          "another nightly run is in progress; STOP.", file=sys.stderr)
    return 1


def release() -> int:
    try:
        LOCK_PATH.unlink()
        print("released")
    except FileNotFoundError:
        print("released (no lock present)")
    return 0


def status() -> int:
    if not LOCK_PATH.exists():
        print("unlocked")
        return 0
    age = time.time() - LOCK_PATH.stat().st_mtime
    print(f"locked ({age/60:.1f}m old): {LOCK_PATH.read_text().strip()}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="atlas suite run lock")
    ap.add_argument("command", choices=["acquire", "release", "status"])
    ap.add_argument("--stale-minutes", type=int, default=180)
    args = ap.parse_args(argv)
    if args.command == "acquire":
        return acquire(args.stale_minutes)
    if args.command == "release":
        return release()
    return status()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
