#!/usr/bin/env python3
"""atlas-fireflies-ingest — prep↔call note linker (join on date + folder).

When the Fireflies sync files a call note into a `meetings/` folder, a
meeting-prep note may already sit beside it — hand-authored before the call and
used to take live notes during it. Prep and call stay two separate artifacts
(DEC-004 keeps the human-curated note slim); this helper links them.

    python3 link_prep.py --folder "<folder rel to vault>" \
        --call-basename "YYYY-MM-DD — <sanitized-title>" [--vault PATH] [--dry-run]

Join key is **date + folder**, never the title: the prep filename's leading
`YYYY-MM-DD` must equal the call note's, and it must live in the same folder.
Title drift or a router disambiguator suffix therefore can't break the link.

Match policy (by design): link ONLY when exactly one same-date prep
exists in the folder. Zero or two-plus → skip silently, log the reason to
stderr, change nothing. Never guess.

On the single match this does two things:
  1. Surgically sets the prep's frontmatter `call_note:` to the real call
     basename — frontmatter only; the prep body (live notes) is byte-preserved.
  2. Prints the `prep_note: "[[…]]"` line to stdout for the agent to include in
     the call note's frontmatter (the call note is authored by the SKILL step,
     not here — same division of labour as summary_record.py).

Guard rails mirror summary_record.py: stdlib-only (DEC-021), atomic write,
never deletes/renames, a prep without a frontmatter fence is left untouched.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
DEFAULT_VAULT = CFG.vault_root

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
PREP_SUFFIX = "(prep).md"


def split_frontmatter(text):
    """Return (frontmatter_lines, body) or (None, text) when no fence.
    Same contract as summary_record.split_frontmatter (kept local — DEC-021,
    no cross-module import needed for one small function)."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end].split("\n"), text[end + 5:]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".lp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def wikilink_value(basename):
    """A double-quoted `[[basename]]` scalar, inner quotes escaped — matches
    summary_record.meeting_note_line so quoting never diverges across the skill."""
    return '"[[' + basename.replace('"', '\\"') + ']]"'


def find_prep(folder: Path, date: str, call_basename: str):
    """Every `.md` in `folder` whose name starts `<date> ` and ends `(prep).md`,
    excluding the call note itself. Returns a sorted list of Paths."""
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.glob("*.md")):
        name = p.name
        if name == call_basename + ".md":
            continue
        if name.startswith(date + " ") and name.endswith(PREP_SUFFIX):
            out.append(p)
    return out


def set_call_note(prep: Path, call_basename: str, dry_run: bool):
    """Set/replace the prep's frontmatter `call_note:` to point at the call
    note. Frontmatter-only surgery; the body is preserved verbatim. Returns
    (status, detail): linked / already-current / no-frontmatter / error."""
    text = prep.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    if fm is None:
        return "no-frontmatter", f"{prep.name} has no frontmatter fence — left untouched"

    new_line = "call_note: " + wikilink_value(call_basename)
    replaced = False
    out_lines = []
    for line in fm:
        if line.startswith("call_note:"):
            if line.strip() == new_line:
                return "already-current", "call_note already points at the call note"
            out_lines.append(new_line)
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        # Insert after prev_meeting: if present (keeps the pointer block together),
        # else after type:, else at the end of the frontmatter block.
        idx = next((i for i, l in enumerate(out_lines) if l.startswith("prev_meeting:")), None)
        if idx is None:
            idx = next((i for i, l in enumerate(out_lines) if l.startswith("type:")), None)
        if idx is None:
            out_lines.append(new_line)
        else:
            out_lines.insert(idx + 1, new_line)

    new_text = "---\n" + "\n".join(out_lines) + "\n---\n" + body
    if not dry_run:
        atomic_write(prep, new_text)
    return "linked", f"set call_note -> [[{call_basename}]]"


def run(folder_rel: str, call_basename: str, vault: Path, dry_run: bool):
    """Returns (exit_code, prep_note_line_or_None). prep_note_line is the
    frontmatter line the caller should add to the CALL note, or None when no
    single prep matched."""
    m = DATE_RE.match(call_basename)
    if not m:
        print(f"error: call-basename {call_basename!r} does not start with YYYY-MM-DD",
              file=sys.stderr)
        return 2, None
    date = m.group(1)
    folder = (vault / folder_rel).resolve()

    preps = find_prep(folder, date, call_basename)
    if len(preps) == 0:
        print(f"skip: no prep note for {date} in {folder_rel}", file=sys.stderr)
        return 0, None
    if len(preps) > 1:
        names = ", ".join(p.name for p in preps)
        print(f"skip: {len(preps)} preps for {date} in {folder_rel} — ambiguous, "
              f"linking none ({names})", file=sys.stderr)
        return 0, None

    prep = preps[0]
    status, detail = set_call_note(prep, call_basename, dry_run)
    if status in ("linked", "already-current"):
        prep_basename = prep.name[:-3]  # strip .md
        prep_line = "prep_note: " + wikilink_value(prep_basename)
        print(f"{status}: {prep.name} ({detail})", file=sys.stderr)
        print(prep_line)  # stdout: the line the agent adds to the call note
        return 0, prep_line
    # no-frontmatter / error — never fabricate; skip the link, non-fatal.
    print(f"skip: {status}: {detail}", file=sys.stderr)
    return 0, None


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="link a call note to its same-date prep sibling")
    ap.add_argument("--folder", required=True,
                    help="routed meetings folder, relative to the vault root")
    ap.add_argument("--call-basename", required=True,
                    help="the call note's basename without .md (starts with YYYY-MM-DD)")
    ap.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    code, _ = run(args.folder, args.call_basename, args.vault or DEFAULT_VAULT, args.dry_run)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
