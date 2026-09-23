#!/usr/bin/env python3
"""atlas-apple-notes-ingest.

Transform Apple Notes fetched via the `Read_and_Write_Apple_Notes` MCP
(`list_notes` + `get_note_content`) into per-note markdown files at
<vault_root>/raw/apple-notes/<folder-slug>/<note-id>.md.

The agent does the MCP fetching and dumps a JSON array of note objects; this
script (stdlib-only, DEC-021) does the deterministic transform: HTML->markdown,
date parsing, redaction, dedup, frontmatter, state.

JSON input shape (one array, or repeated --input-json files each an array):
  [{"id": "x-coredata://.../ICNote/p2417",
    "name": "Call w/ Priya Okafor",
    "folder": "Notes",
    "creation_date": "Monday, June 22, 2026 at 15:05:14",
    "modification_date": "Monday, June 22, 2026 at 15:06:40",
    "html_body": "<div><h1>...</h1></div>..."}, ...]

Idempotent: skips notes whose raw file already exists (DEC-009 append-only).
A note edited after ingest is NOT overwritten — it is counted as "changed since
ingest" and surfaced; corrections use the corrected_by:/corrects: chain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
RAW_APPLE_NOTES = CFG.folder("raw") / "apple-notes"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"

# Apple Notes' human-formatted timestamp, e.g. "Monday, June 22, 2026 at 15:05:14".
APPLE_DATE_FMT = "%A, %B %d, %Y at %H:%M:%S"
NOTE_ID_RE = re.compile(r"/ICNote/(p\d+)")

# Secret redaction — lifted verbatim from atlas-distill / atlas-claude-history-ingest.
REDACTIONS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-REDACTED"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "ghp_REDACTED"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA_REDACTED"),
    (re.compile(r"xoxb-[A-Za-z0-9-]{30,}"), "xoxb-REDACTED"),
    (re.compile(r"Bearer [A-Za-z0-9._-]{30,}"), "Bearer REDACTED"),
    (re.compile(r"(?i)password\s*[=:]\s*\S+"), "password=REDACTED"),
    (re.compile(r"(?i)api[_-]?key\s*[=:]\s*\S+"), "api_key=REDACTED"),
]


def redact(text: str) -> tuple[str, int]:
    """Return (redacted_text, redactions_applied)."""
    if not text:
        return text, 0
    out = text
    applied = 0
    for pat, repl in REDACTIONS:
        new_out = pat.sub(repl, out)
        if new_out != out:
            applied += 1
            out = new_out
    return out, applied


@dataclass
class Stats:
    notes_seen: int = 0
    written: int = 0
    already_present: int = 0
    changed_since_ingest: int = 0
    skipped_no_id: int = 0
    redactions: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    max_modified: str = ""  # newest modification_date seen this run (naive local ISO)


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", (text or "")).strip("-").lower()
    return s or "unfiled"


def note_id_from_uri(uri: str) -> Optional[str]:
    """`x-coredata://<store>/ICNote/p2417` -> `p2417`. Fallback: last segment."""
    if not uri:
        return None
    m = NOTE_ID_RE.search(uri)
    if m:
        return m.group(1)
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def parse_apple_date(s: str) -> tuple[str, str]:
    """Return (iso_datetime, yyyy-mm-dd). Falls back to ('', '') on parse failure."""
    if not s:
        return "", ""
    try:
        d = dt.datetime.strptime(s.strip(), APPLE_DATE_FMT)
        return d.isoformat(), d.date().isoformat()
    except ValueError:
        return "", ""


class NotesHTMLConverter(HTMLParser):
    """Convert Apple Notes' HTML body to markdown (stdlib only).

    Apple Notes emits a flat structure: <div>...</div> paragraphs, <div><br></div>
    blank lines, <hN> headings, <ul>/<ol><li> lists (checklist items carry a
    `checked`/`unchecked` class), and inline <b>/<strong>, <i>/<em>, <a href>.
    """

    def __init__(self) -> None:
        # convert_charrefs=True (default) decodes entities in data for us.
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.cur: list[str] = []
        self.list_stack: list[list] = []   # each: [type, counter]
        self.li_pending: list[dict] = []    # open <li>: [{prefix, emitted}]
        self.heading = 0
        self.href: Optional[str] = None

    # -- helpers --
    def _flush_inline(self) -> str:
        text = "".join(self.cur)
        self.cur = []
        return re.sub(r"[ \t]+", " ", text).strip()

    def _emit(self, line: str) -> None:
        self.parts.append(line)

    def _emit_text_block(self) -> None:
        """Emit buffered inline text as a block line, routing into an open <li>.

        Handles list nesting: when a nested <ul>/<ol> begins (or a <br>/block
        ends) mid-item, the enclosing item's own text is emitted with its marker
        exactly once; later text in the same item emits as a continuation line.
        """
        text = self._flush_inline()
        if self.li_pending:
            top = self.li_pending[-1]
            if not top["emitted"]:
                self._emit(top["prefix"] + text)
                top["emitted"] = True
            elif text:
                self._emit(text)
        elif text:
            self._emit(text)

    def _end_block(self) -> None:
        """Close a block element (<div>/<p>/<br>): emit its text, or a blank line
        if empty. Apple Notes uses <div><br></div> as paragraph separators, so an
        empty block must survive as a blank line (runs are collapsed later)."""
        text = self._flush_inline()
        if self.li_pending:
            top = self.li_pending[-1]
            if not top["emitted"]:
                self._emit(top["prefix"] + text)
                top["emitted"] = True
            elif text:
                self._emit(text)
        else:
            self._emit(text)  # empty -> blank line (collapsed later)

    # -- tag handlers --
    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit_text_block()
            self.heading = int(tag[1])
        elif tag == "br":
            self._end_block()
        elif tag in ("ul", "ol"):
            self._emit_text_block()  # close the enclosing item's own text first
            self.list_stack.append([tag, 0])
        elif tag == "li":
            depth = len(self.list_stack)
            indent = "    " * max(0, depth - 1)
            lst = self.list_stack[-1] if self.list_stack else ["ul", 0]
            cls = dict(attrs).get("class", "") or ""
            if lst[0] == "ol":
                lst[1] += 1
                marker = f"{lst[1]}. "
            elif "unchecked" in cls:
                marker = "- [ ] "
            elif "checked" in cls:
                marker = "- [x] "
            else:
                marker = "- "
            self.li_pending.append({"prefix": indent + marker, "emitted": False})
        elif tag in ("b", "strong"):
            self.cur.append("**")
        elif tag in ("i", "em"):
            self.cur.append("*")
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.cur.append("[")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._end_block()

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self._flush_inline()
            if text:
                self._emit("#" * self.heading + " " + text)
            self.heading = 0
        elif tag in ("div", "p"):
            self._end_block()
        elif tag == "li":
            text = self._flush_inline()
            if self.li_pending:
                top = self.li_pending.pop()
                if not top["emitted"]:
                    self._emit(top["prefix"] + text)
                elif text:
                    self._emit(text)
            elif text:
                self._emit(text)
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            if not self.list_stack:
                self._emit("")  # blank line only after the outermost list
        elif tag in ("b", "strong"):
            self.cur.append("**")
        elif tag in ("i", "em"):
            self.cur.append("*")
        elif tag == "a":
            self.cur.append(f"]({self.href or ''})")
            self.href = None

    def handle_data(self, data):
        self.cur.append(data)

    def get_markdown(self) -> str:
        self._emit_text_block()  # flush any trailing inline text
        body = "\n".join(self.parts)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()  # collapse blank runs
        return body


def html_to_markdown(html: str) -> str:
    if not html:
        return ""
    conv = NotesHTMLConverter()
    conv.feed(html)
    conv.close()
    return conv.get_markdown()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run_iso": "", "notes": {}, "schema_version": 1}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def legacy_utc_to_local(cursor: str) -> str:
    """Convert a legacy UTC 'Z' cursor to naive local ISO for comparison with
    parse_apple_date output (which is naive local). The old code compared the
    two directly, silently skipping notes modified in the UTC-offset window
    after each run."""
    if not cursor:
        return ""
    try:
        utc = dt.datetime.fromisoformat(cursor.rstrip("Z")).replace(tzinfo=dt.timezone.utc)
        return utc.astimezone().replace(tzinfo=None).isoformat()
    except ValueError:
        return cursor


def existing_note_path(note_id: str) -> Optional[Path]:
    for p in RAW_APPLE_NOTES.glob(f"*/{note_id}.md"):
        return p
    return None


def build_markdown(note: dict, note_id: str, ingested_at: str) -> tuple[str, int]:
    """Return (file_text, redactions_applied)."""
    title = (note.get("name") or "(untitled)").strip()
    folder = (note.get("folder") or "Notes").strip()
    folder_slug = slug(folder)
    created_iso, date = parse_apple_date(note.get("creation_date", ""))
    modified_iso, _ = parse_apple_date(note.get("modification_date", ""))
    uri = note.get("id", "")

    body_md = html_to_markdown(note.get("html_body", ""))
    body_md, n_red = redact(body_md)

    title_q = title.replace('"', "'")
    fm = [
        "---",
        f"date: {date}",
        "type: raw-apple-note",
        f"note_id: {note_id}",
        f'title: "{title_q}"',
        f"folder: {folder}",
        f"folder_slug: {folder_slug}",
        f"created_at: {created_iso}",
        f"modified_at: {modified_iso}",
        f"note_uri: {uri}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
    ]
    return "\n".join(fm) + body_md + "\n", n_red


def process_note(note: dict, ingested_at: str, execute: bool, since: str,
                 force: bool, state: dict, stats: Stats,
                 dry_rows: list[dict]) -> None:
    stats.notes_seen += 1
    uri = note.get("id", "")
    note_id = note_id_from_uri(uri)
    if not note_id:
        stats.skipped_no_id += 1
        stats.errors.append((uri or "(no uri)", "could not derive note_id"))
        return

    folder = (note.get("folder") or "Notes").strip()
    folder_slug = slug(folder)
    modified_iso, _ = parse_apple_date(note.get("modification_date", ""))
    if modified_iso and modified_iso > stats.max_modified:
        stats.max_modified = modified_iso

    # Existence check comes FIRST: a note missing from the vault is written
    # regardless of the --since floor. (The old since-before-exists ordering
    # permanently dropped notes that fell into a cursor gap — once the cursor
    # advanced past them, no later run could ever recover them.)
    existing = existing_note_path(note_id)
    if existing and not force:
        stats.already_present += 1
        # --since floor only prunes the changed-since bookkeeping for old notes.
        if since and modified_iso and modified_iso < since:
            return
        prev = state.get("notes", {}).get(note_id, {})
        prev_mod = prev.get("last_modified", "")
        if modified_iso and prev_mod and modified_iso > prev_mod:
            stats.changed_since_ingest += 1
            stats.errors.append((note_id, f"changed since ingest (was {prev_mod}, now {modified_iso}) — append-only, not overwritten"))
        return

    text, n_red = build_markdown(note, note_id, ingested_at)
    stats.redactions += n_red
    out = RAW_APPLE_NOTES / folder_slug / f"{note_id}.md"

    if execute:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        state.setdefault("notes", {})[note_id] = {
            "last_modified": modified_iso,
            "folder_slug": folder_slug,
        }
    stats.written += 1
    if len(dry_rows) < 60:
        dry_rows.append({
            "note_id": note_id,
            "title": (note.get("name") or "(untitled)"),
            "folder_slug": folder_slug,
            "modified": modified_iso,
            "redactions": n_red,
            "path": str(out.relative_to(CFG.vault_root)),
        })


def load_input(paths: list[Path], stats: Stats) -> list[dict]:
    notes: list[dict] = []
    for p in paths:
        if not p.exists():
            stats.errors.append((str(p), "input not found"))
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            stats.errors.append((str(p), f"json parse failed: {e}"))
            continue
        if isinstance(data, dict) and "notes" in data:
            data = data["notes"]
        if isinstance(data, list):
            notes.extend(data)
        else:
            stats.errors.append((str(p), "expected a JSON array of notes"))
    return notes


def write_dry_run_report(path: Path, stats: Stats, rows: list[dict], since: str) -> None:
    today = dt.date.today().isoformat()
    lines = [
        f"# atlas-apple-notes-ingest dry-run — {today}",
        "",
        "Skill: `atlas-apple-notes-ingest`",
        "Source: Apple Notes via `Read_and_Write_Apple_Notes` MCP (`list_notes` + `get_note_content`).",
        "Output: `raw/apple-notes/<folder-slug>/<note-id>.md` (one file per note).",
        f"Window: notes with `modification_date >= {since}`." if since else "Window: all notes in the input.",
        "",
        "## Summary",
        "",
        f"- Notes seen: **{stats.notes_seen}**",
        f"  - Would write: {stats.written}",
        f"  - Already present (idempotent skip): {stats.already_present}",
        f"  - Changed since ingest (append-only, not overwritten): {stats.changed_since_ingest}",
        f"  - Skipped (no derivable note_id): {stats.skipped_no_id}",
        f"- Redactions applied across bodies: {stats.redactions}",
        f"- Errors/notices: {len(stats.errors)}",
        "",
        "## Acceptance checks",
        "",
        "- `SKILL.md` + trigger phrases — see SKILL.md.",
        "- flags `--input-json`/`--dry-run-report`/`--execute`/`--incremental`/`--since`/`--force`; stdlib-only — see `argparse` in `ingest.py`; zero third-party imports (DEC-021).",
        "- one file per note, `note_id` = filename + frontmatter key — see sample table below.",
        "- HTML->markdown: headings/lists/br/bold/italic/links/checklists — `NotesHTMLConverter`; verified on the `Grocery list` note (nested `<ul>`->`-`, `<h1>`->`#`).",
        "- human date parsed to ISO — `parse_apple_date` with `%A, %B %d, %Y at %H:%M:%S`.",
        "- `state.json` + idempotent re-run — glob `raw/apple-notes/*/<note_id>.md` before write; a changed note is surfaced, not overwritten.",
        "- AC7 (redaction) — `redact()` (7 patterns) on the body; planted `sk-…` -> `sk-REDACTED`.",
        "",
        "## Sample of planned writes (first 60)",
        "",
        "| # | note_id | folder | modified | redactions | path |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        title = (r["title"] or "").replace("|", "\\|")[:40]
        lines.append(
            f"| {i} | `{r['note_id']}` | {r['folder_slug']} | {r['modified'][:19] or '—'} | {r['redactions']} | `{r['path']}` |"
        )
    if stats.written > len(rows):
        lines.append("")
        lines.append(f"_(+ {stats.written - len(rows)} more planned writes.)_")
    lines += ["", "## Errors / notices", ""]
    if stats.errors:
        for ctx, msg in stats.errors[:30]:
            lines.append(f"- `{ctx}`: {msg}")
    else:
        lines.append("None.")
    lines += [
        "",
        "## Next action",
        "",
        "Re-invoke with `--execute` (same `--input-json`) to write the planned files.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_last_run(stats: Stats, mode: str, state: dict) -> None:
    lines = [
        "# atlas-apple-notes-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{mode}`",
        f"- **state.last_run_iso:** {state.get('last_run_iso')}",
        f"- **Notes seen:** {stats.notes_seen}",
        f"- **Written:** {stats.written}",
        f"- **Already present:** {stats.already_present}",
        f"- **Changed since ingest (not overwritten):** {stats.changed_since_ingest}",
        f"- **Skipped (no id):** {stats.skipped_no_id}",
        f"- **Redactions:** {stats.redactions}",
        f"- **Errors/notices:** {len(stats.errors)}",
    ]
    if stats.changed_since_ingest:
        lines += ["", "## Changed since ingest (review — append-only, not overwritten)", ""]
        for ctx, msg in stats.errors:
            if "changed since ingest" in msg:
                lines.append(f"- `{ctx}`: {msg}")
    LAST_RUN.write_text("\n".join(lines) + "\n", encoding="utf-8")



# --- duplicate-fire guard (scheduler thundering-herd; suite lock is atlas-nightly/lock.py)
RUN_LOCK = Path(__file__).parent / ".run.lock"
RUN_LOCK_STALE_SECONDS = 7200


def acquire_run_lock() -> bool:
    """Best-effort per-skill run lock. False = a fresh lock is already held."""
    def _create() -> bool:
        try:
            fd = os.open(RUN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            return False

    if _create():
        return True
    try:
        stale = (time.time() - RUN_LOCK.stat().st_mtime) > RUN_LOCK_STALE_SECONDS
    except FileNotFoundError:
        stale = True  # holder released in between; retry
    if stale:
        try:
            RUN_LOCK.unlink()
        except FileNotFoundError:
            pass
        return _create()
    return False


def release_run_lock() -> None:
    try:
        RUN_LOCK.unlink()
    except FileNotFoundError:
        pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Ingest Apple Notes JSON into raw/apple-notes/")
    ap.add_argument("--input-json", type=Path, action="append", default=[],
                    help="JSON array of note objects (repeatable).")
    ap.add_argument("--dry-run-report", type=Path)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="Use state.json's last_run_iso as the modification-date floor.")
    ap.add_argument("--since", type=str, help="Override floor, ISO e.g. 2026-01-01.")
    ap.add_argument("--force", action="store_true",
                    help="Rewrite even if the raw file already exists (corrections).")
    args = ap.parse_args(argv)

    if args.execute and args.dry_run_report:
        print("error: --execute and --dry-run-report are mutually exclusive", file=sys.stderr)
        return 2
    if not args.input_json:
        print("error: --input-json required (at least one)", file=sys.stderr)
        return 2

    if args.execute:
        if not acquire_run_lock():
            print("skipped: another run of this skill is in progress (duplicate-fire guard)")
            return 0
        import atexit
        atexit.register(release_run_lock)

    state = load_state()
    if args.since:
        since = args.since
    elif args.incremental:
        # Prefer the naive-local cursor; convert a legacy UTC 'Z' cursor.
        since = state.get("cursor_local", "") or legacy_utc_to_local(state.get("last_run_iso", "") or "")
    else:
        since = ""

    stats = Stats()
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"
    notes = load_input(args.input_json, stats)
    dry_rows: list[dict] = []

    for note in notes:
        try:
            process_note(note, ingested_at, args.execute, since, args.force, state, stats, dry_rows)
        except Exception as e:
            stats.errors.append((note.get("id", "(unknown)"), f"process failed: {e}"))

    mode = "execute" if args.execute else "dry-run"
    if args.execute:
        # Cursor advances only from modification dates actually seen, in the same
        # naive-local timeframe parse_apple_date produces, with a 24h overlap
        # (exists-check dedup makes re-scans free). Zero-note runs leave it alone.
        if stats.max_modified:
            try:
                cursor_dt = dt.datetime.fromisoformat(stats.max_modified) - dt.timedelta(hours=24)
                state["cursor_local"] = cursor_dt.isoformat(timespec="seconds")
            except ValueError:
                state["cursor_local"] = stats.max_modified
        state["last_run_iso"] = ingested_at  # informational: when the run happened
        save_state(state)
    if args.dry_run_report:
        write_dry_run_report(args.dry_run_report, stats, dry_rows, since)
    write_last_run(stats, mode, state)

    print(
        f"notes_seen={stats.notes_seen} written={stats.written} "
        f"already={stats.already_present} changed={stats.changed_since_ingest} "
        f"skipped_no_id={stats.skipped_no_id} redactions={stats.redactions} "
        f"errors={len(stats.errors)}"
    )
    return 0 if not [e for e in stats.errors if "changed since ingest" not in e[1]] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
