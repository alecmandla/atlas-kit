#!/usr/bin/env python3
"""One-off DEC-032 conversion: rewrite `transcript_status: retained` files under
raw/fireflies/ into summary records.

Full transcripts live in Fireflies (fetched via API on demand); the raw file
keeps the meeting's summary so fireflies still feeds emerge/synthesize keyword
matching. The summary content is rebuilt from the linked meeting note
(`meeting_note:` frontmatter → its ## Summary / ## Action Items sections and
`keywords:` frontmatter).

Safety rules (DEC-032):
- never deletes a raw file (dedup anchors + wikilink targets);
- never converts when the meeting note can't be found or has no summary text —
  the file is left untouched and reported;
- atomic writes (tmp + os.replace);
- dry-run by default, `--execute` to write.

Stdlib-only (DEC-021).
"""

import argparse
import datetime
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT = CFG.vault_root
RAW_FIREFLIES = CFG.folder("raw") / "fireflies"

SKIP_DIRS = {".git", ".obsidian", ".trash", CFG.folder_name("raw")}


def split_frontmatter(text):
    """Return (frontmatter_lines, body) or (None, text) when no fence."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end].split("\n"), text[end + 5:]


def fm_scalar(fm_lines, key):
    for line in fm_lines:
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip()
    return None


def fm_list(fm_lines, key):
    """Extract a block-style list under `key:` (e.g. keywords)."""
    items, in_block = [], False
    for line in fm_lines:
        if line.startswith(key + ":"):
            rest = line[len(key) + 1:].strip()
            if rest.startswith("[") and rest.endswith("]"):
                return [i.strip().strip("'\"") for i in rest[1:-1].split(",") if i.strip()]
            in_block = True
            continue
        if in_block:
            if line.startswith("- "):
                items.append(line[2:].strip().strip("'\""))
            elif line.startswith("  - "):
                items.append(line[4:].strip().strip("'\""))
            elif line.strip() == "":
                continue
            else:
                break
    return items


def section(body, heading):
    """Text of `## <heading>` up to the next ## heading (or EOF), stripped."""
    m = re.search(rf"^## {re.escape(heading)}\s*$", body, re.MULTILINE)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest).strip()


def find_meeting_note(basename):
    """Locate `<basename>.md` anywhere in the vault outside raw/ and dot-dirs."""
    target = basename + ".md"
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if target in files:
            return Path(root) / target
    return None


def yaml_inline_list(items):
    quoted = []
    for it in items:
        if re.search(r'[:,\[\]{}#"\']', it):
            quoted.append('"' + it.replace('"', '\\"') + '"')
        else:
            quoted.append(it)
    return "[" + ", ".join(quoted) + "]"


def atomic_write(path, content):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".convert-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def convert(raw_path, execute, today):
    text = raw_path.read_text(encoding="utf-8")
    fm, _body = split_frontmatter(text)
    if fm is None:
        return ("skipped", "no frontmatter fence")
    if fm_scalar(fm, "transcript_status") != "retained":
        return ("skipped", "not retained")

    note_ref = fm_scalar(fm, "meeting_note") or ""
    m = re.search(r"\[\[(.+?)\]\]", note_ref)
    if not m:
        return ("skipped", "no meeting_note wikilink")
    basename = m.group(1)

    note_path = find_meeting_note(basename)
    if note_path is None:
        return ("skipped", f"meeting note not found: {basename}")

    note_fm, note_body = split_frontmatter(note_path.read_text(encoding="utf-8"))
    overview = section(note_body, "Summary")
    actions = section(note_body, "Action Items")
    keywords = fm_list(note_fm or [], "keywords")
    if not overview and not actions and not keywords:
        return ("skipped", f"no summary content in note: {basename}")

    fireflies_url = fm_scalar(fm, "fireflies_url") or ""

    out_fm = []
    for line in fm:
        if line.startswith("transcript_status:"):
            out_fm.append("transcript_status: summary")
        elif line.startswith("keywords:"):
            continue  # replaced below
        else:
            out_fm.append(line)
    if keywords:
        out_fm.append(f"keywords: {yaml_inline_list(keywords)}")
    out_fm.append(f"converted_at: {today}")

    body_parts = [f"# {basename}", ""]
    if keywords:
        body_parts += ["**Keywords:** " + ", ".join(keywords), ""]
    if overview:
        body_parts += ["## Overview", overview, ""]
    if actions:
        body_parts += ["## Action Items", actions, ""]
    body_parts += [
        "---",
        f"Full transcript lives in Fireflies (fetch via API on demand): [view]({fireflies_url})",
        "",
    ]

    new_text = "---\n" + "\n".join(out_fm) + "\n---\n\n" + "\n".join(body_parts)

    if execute:
        atomic_write(raw_path, new_text)
    return ("converted", f"{len(text)} -> {len(new_text)} bytes")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    today = datetime.date.today().isoformat()

    counts = {"converted": 0, "skipped": 0}
    for raw_path in sorted(RAW_FIREFLIES.glob("*.md")):
        head = raw_path.read_text(encoding="utf-8")[:600]
        if "transcript_status: retained" not in head:
            continue
        status, detail = convert(raw_path, args.execute, today)
        counts[status] += 1
        print(f"{status:9}  {raw_path.name}  ({detail})")

    mode = "execute" if args.execute else "dry-run"
    print(f"\n{mode}: converted={counts['converted']} skipped={counts['skipped']}")
    return 1 if counts["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
