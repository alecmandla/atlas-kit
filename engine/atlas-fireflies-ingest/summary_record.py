#!/usr/bin/env python3
"""atlas-fireflies-ingest — the summary-record writer (single format seam).

Renders and writes the AI-optimized summary record at
raw/fireflies/<meeting_id>.md from the meeting JSON the agent fetched via the
Fireflies MCP. The ingest SKILL step, the enrichment pilot, and the
retroactive backfill all go through this module, so the record format exists
in exactly one place (DEC-032 / enrichment PRD).

    python3 summary_record.py apply --payload <file|-> [--vault PATH] [--dry-run]
        Payload: one meeting object, a list of them, or {"transcripts": [...]}
        — the shape fireflies_get_transcripts returns, optionally with a
        "meeting_note" key (note basename, no .md) injected by the agent.
        Per meeting: fresh write, stub -> enriched upgrade, or re-render of a
        prior summary record. Atomic, guarded, idempotent.

    python3 summary_record.py mark-unavailable <id> [<id> ...] [--vault PATH] [--dry-run]
        Annotate link-only stubs whose meeting has aged out of Fireflies so
        the backfill completes instead of erroring. Stubs only; never touches
        an enriched record.

    python3 summary_record.py pending [--vault PATH]
        List "<meeting_id>\t<date>" for every stub still awaiting enrichment
        (not yet annotated unavailable) — the backfill's batching input.

Guard rails (DEC-032): never deletes or renames a raw file; a body that is
neither a known stub nor a prior summary record is a conflict, left
untouched; never reads or writes state.json. Stdlib-only (DEC-021).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
DEFAULT_VAULT = CFG.vault_root

STUB_MARKER = "Transcript not retained locally."
UNAVAILABLE_KEY = "unavailable_at_source"

POINTER_TEXT = (
    "---\n"
    "Full transcript lives in Fireflies (fetch via API on demand): [view]({url})\n"
    "\n"
    "AI tools: fetch the transcript by passing this record's `meeting_id` to the\n"
    "Fireflies API — `fireflies_get_transcript` with `transcriptId: \"{id}\"`. Never\n"
    "open the `fireflies_url` link above; it is a human-only web page.\n"
)


# ---------------------------------------------------------------- frontmatter

def split_frontmatter(text):
    """Return (frontmatter_lines, body) or (None, text) when no fence."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end].split("\n"), text[end + 5:]


def fm_scalar(fm_lines, key):
    for line in fm_lines or []:
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip()
    return None


# A conservative plain-scalar shape: starts and ends alphanumeric, inner chars
# limited to a safe set. Anything else (backslashes, leading indicators like
# * & - ? > | % @, bare null/true/yes, punctuation) is double-quoted. Quoting
# the doubtful cases keeps one odd Fireflies keyword from invalidating the whole
# frontmatter for Obsidian/YAML consumers (F4).
SAFE_PLAIN_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/&()+'-]*[A-Za-z0-9.)']")
# Tokens that pass the shape check but YAML would parse as a non-string type.
YAML_RESERVED = {"null", "none", "true", "false", "yes", "no", "on", "off", "~"}
YAML_NUMBER = re.compile(r"[0-9][0-9._]*")  # bare number → int/float on parse


def yaml_plain_safe(it):
    return (it == it.strip()
            and SAFE_PLAIN_SCALAR.fullmatch(it) is not None
            and it.lower() not in YAML_RESERVED
            and YAML_NUMBER.fullmatch(it) is None)


def yaml_inline_list(items):
    out = []
    for it in items:
        if yaml_plain_safe(it):
            out.append(it)
        else:
            out.append('"' + it.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "[" + ", ".join(out) + "]"


def meeting_note_line(basename):
    """The frontmatter `meeting_note:` wikilink line, inner quotes escaped.
    Used by both render() and the stub write so escaping never diverges (F5)."""
    return 'meeting_note: "[[' + basename.replace('"', '\\"') + ']]"'


def valid_meeting_id(mid):
    """Fireflies ids are Crockford-base32 ULIDs, but don't over-fit: accept any
    non-empty run of [0-9A-Za-z_-] and reject anything with path separators,
    dots, or `..` so a crafted id can't escape raw/fireflies/ (F3)."""
    return bool(mid) and re.fullmatch(r"[0-9A-Za-z_-]+", mid) is not None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sr-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------ payload parsing

def iso_date(value):
    """Fireflies sends epoch ms (`date`) or ISO text (`dateString`); existing
    records use `YYYY-MM-DD HH:MM:SS.ffffff+00:00`. Normalize to that."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        d = dt.datetime.fromtimestamp(value / 1000.0, dt.timezone.utc)
        return d.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"
    s = str(value).strip()
    if len(s) <= 10:  # date-only: don't invent a midnight timestamp
        return s
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"


# Machine/logger addresses that ride along in the attendee list but aren't people.
# The canonical case is a CRM's BCC-to-logger address (many CRMs log an email when
# it is bcc'd to a per-account address; that address then shows up as an attendee).
# Conservative by design — a NAMED attendee is NEVER dropped: an all-numeric local
# part is only a bot signal when no display name accompanies it, and known logger
# domains are dropped outright. Refined per adversarial review so a real person and
# their people-resolution join key are never silently erased.
JUNK_EMAIL_DOMAINS: tuple = ()  # domain suffixes of BCC-to-CRM loggers; add your CRM's here


def is_junk_email(email):
    """A known logger address — dropped regardless of context (name or not)."""
    e = (email or "").strip().lower()
    local, _, domain = e.partition("@")
    if not local or not domain:
        return False
    return domain.endswith(JUNK_EMAIL_DOMAINS)


def numeric_local(email):
    """All-numeric local part — a weak bot signal, acted on ONLY when no display
    name accompanies the address (see attendee_pairs). A numeric local part with a
    real name (e.g. Real Person <123456@example.com>) is a person, not a logger."""
    local = (email or "").strip().partition("@")[0]
    return bool(local) and local.isdigit()


def attendee_pairs(meeting):
    """Returns (pairs, dropped): best-effort (name, email) pairs plus the list of
    addresses filtered as junk. meeting_attendees first (names), then the bare
    participants email list; emails are the dedup key. A named attendee is always
    kept; a known logger domain, or a bare all-numeric local part with no name, is
    dropped (DEC-034 sign-off, refined per adversarial review)."""
    pairs, seen, dropped = [], set(), []
    for att in meeting.get("meeting_attendees") or meeting.get("meetingAttendees") or []:
        if not isinstance(att, dict):
            continue
        email = (att.get("email") or "").strip()
        name = (att.get("displayName") or att.get("name") or "").strip()
        if is_junk_email(email) or (not name and numeric_local(email)):
            if email:
                dropped.append(email)
            continue
        key = email.lower() or name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        pairs.append((name, email))
    for email in meeting.get("participants") or []:
        email = (email or "").strip()
        if not email or email.lower() in seen:
            continue
        if is_junk_email(email) or numeric_local(email):  # bare list: no name to save it
            dropped.append(email)
            continue
        seen.add(email.lower())
        pairs.append(("", email))
    pairs.sort(key=lambda p: (p[1].lower() or "~", p[0].lower()))
    return pairs, dropped


def action_items_text(summary):
    ai = (summary or {}).get("action_items")
    if isinstance(ai, list):
        return "\n".join(str(x).strip() for x in ai if str(x).strip()).strip()
    return (ai or "").strip()


def parse_meeting(meeting):
    summary = meeting.get("summary") or {}
    keywords = [str(k).strip() for k in (summary.get("keywords") or []) if str(k).strip()]
    mid = str(meeting.get("id") or "").strip()
    attendees, dropped_junk = attendee_pairs(meeting)
    return {
        "id": mid,
        "title": str(meeting.get("title") or "").strip(),
        "date": iso_date(meeting.get("date") if meeting.get("date") not in (None, "")
                         else meeting.get("dateString")),
        "url": (meeting.get("transcript_url") or "").strip()
               or (f"https://app.fireflies.ai/view/{mid}" if mid else ""),
        "meeting_note": str(meeting.get("meeting_note") or "").strip(),
        "attendees": attendees,
        "dropped_junk": sorted(set(dropped_junk)),
        "keywords": keywords,
        "overview": str(summary.get("overview") or summary.get("short_summary") or "").strip(),
        "action_items": action_items_text(summary),
    }


# ----------------------------------------------------------------- rendering

def stub_body(url, note_basename):
    lines = [
        STUB_MARKER,
        "",
        f"This meeting's full transcript lives at Fireflies: [view]({url}).",
    ]
    if note_basename:
        lines += ["", f"Original meeting note: [[{note_basename}]]"]
    return "\n".join(lines) + "\n"


def render(m, today, extracted_at=None, enriched_at=None):
    """The one place the summary-record format is defined."""
    fm = [
        "type: raw-fireflies",
        f"meeting_id: {m['id']}",
    ]
    if m["meeting_note"]:
        fm.append(meeting_note_line(m["meeting_note"]))
    fm.append(f"fireflies_url: {m['url']}")
    if m["date"]:
        fm.append(f"date: {m['date']}")
    fm.append("transcript_status: summary")
    if extracted_at:
        fm.append(f"extracted_at: {extracted_at}")
    fm.append(f"enriched_at: {enriched_at or today}")
    emails = [e for _, e in m["attendees"] if e]
    if emails:
        fm.append("attendee_emails:")
        fm += [f"- {e}" for e in emails]
    # Always emit participants (empty list allowed) for frontmatter alignment
    # across the corpus (DEC-034 sign-off).
    names = [n for n, _ in m["attendees"] if n]
    fm.append(f"participants: {yaml_inline_list(names)}")
    if m["keywords"]:
        fm.append(f"keywords: {yaml_inline_list(m['keywords'])}")

    body = [f"# {m['meeting_note'] or m['title'] or m['id']}", ""]
    if m["keywords"]:
        body += ["**Keywords:** " + ", ".join(m["keywords"]), ""]
    if m["attendees"]:
        rendered = [f"{n} <{e}>" if n and e else (n or e) for n, e in m["attendees"]]
        body += ["**Attendees:** " + ", ".join(rendered), ""]
    if m["overview"]:
        body += ["## Overview", m["overview"], ""]
    if m["action_items"]:
        body += ["## Action Items", m["action_items"], ""]
    body.append(POINTER_TEXT.format(url=m["url"], id=m["id"]))

    return "---\n" + "\n".join(fm) + "\n---\n\n" + "\n".join(body)


# ------------------------------------------------------------------ applying

def classify(text):
    """What is currently on disk: 'stub' | 'summary' | 'conflict'."""
    fm, body = split_frontmatter(text)
    if fm is None:
        return "conflict", None
    status = fm_scalar(fm, "transcript_status")
    if status == "summary":
        return "summary", fm
    if status == "not-retained-locally" and STUB_MARKER in body:
        return "stub", fm
    return "conflict", fm


def strip_volatile(text):
    """Record content minus the lines that legitimately differ between
    re-renders of identical data (enrichment timestamp)."""
    return "\n".join(
        line for line in text.split("\n") if not line.startswith("enriched_at:")
    )


def body_has_summary_section(text):
    """True if the record body already carries a populated ## Overview or
    ## Action Items section. render() never emits an empty one, but legacy
    DEC-032 conversions and hand edits can — this is the belt-and-braces check
    that stops a contentless re-render from gutting substantive content (F1)."""
    _, body = split_frontmatter(text)
    capture = False
    for line in (body or "").split("\n"):
        if line.startswith("## "):
            capture = line.strip() in ("## Overview", "## Action Items")
            continue
        if line.strip() == "---":  # start of the transcript-pointer block
            capture = False
            continue
        if capture and line.strip():
            return True
    return False


def apply_one(meeting, vault: Path, today: str, dry_run: bool):
    """Returns (status, detail). Statuses: fresh / enriched / already-current /
    stub-written / no-content / conflict / error."""
    m = parse_meeting(meeting)
    if not m["id"]:
        return "error", "payload entry has no id"
    if not valid_meeting_id(m["id"]):
        return "error", f"invalid meeting id {m['id']!r} — rejected before building a path"
    target = vault / CFG.folder_name("raw") / "fireflies" / f"{m['id']}.md"

    existing, kind, existing_fm = None, None, None
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        kind, existing_fm = classify(existing)
        if kind == "conflict":
            return "conflict", f"{target.name} is neither a stub nor a summary record — left untouched"
        # Preserve join keys the payload doesn't carry.
        if not m["meeting_note"]:
            ref = fm_scalar(existing_fm, "meeting_note") or ""
            wl = re.search(r"\[\[(.+?)\]\]", ref)
            if wl:
                m["meeting_note"] = wl.group(1)
        if not m["date"]:
            m["date"] = fm_scalar(existing_fm, "date")
        existing_url = fm_scalar(existing_fm, "fireflies_url")
        if existing_url:
            m["url"] = existing_url

    # Attendees alone are NOT summary content: Fireflies returns participants for
    # essentially every meeting, so counting them would let a contentless payload
    # (summary aged out / AI notes off) re-render a record without its Overview
    # (F1). Substantive content = overview, action items, or keywords only.
    has_content = bool(m["overview"] or m["action_items"] or m["keywords"])
    if not has_content:
        if existing is not None:
            return "no-content", "payload carries no summary content; existing file left as-is"
        content = (
            "---\n"
            f"type: raw-fireflies\n"
            f"meeting_id: {m['id']}\n"
            + (meeting_note_line(m["meeting_note"]) + "\n" if m["meeting_note"] else "")
            + f"fireflies_url: {m['url']}\n"
            + (f"date: {m['date']}\n" if m["date"] else "")
            + "transcript_status: not-retained-locally\n"
            f"extracted_at: {today}\n"
            "---\n\n"
            f"# {m['meeting_note'] or m['title'] or m['id']}\n\n"
            + stub_body(m["url"], m["meeting_note"])
        )
        if not dry_run:
            atomic_write(target, content)
        return "stub-written", "no summary content in payload; wrote link-only stub"

    # F1 layer 2 (belt-and-braces): even if has_content passed (e.g. keywords
    # only), never rewrite an existing summary record that already has a
    # populated Overview/Action Items with a render that would carry neither.
    if (existing is not None and kind == "summary"
            and not (m["overview"] or m["action_items"])
            and body_has_summary_section(existing)):
        return "no-content", "payload has no overview/action items; populated summary body left as-is"

    extracted_at = fm_scalar(existing_fm, "extracted_at") if existing_fm else None
    new_content = render(m, today, extracted_at=extracted_at)

    if existing is not None and strip_volatile(existing) == strip_volatile(new_content):
        return "already-current", "no change"

    if not dry_run:
        atomic_write(target, new_content)
    # Audit trail: never let attendee filtering be silent (adversarial review).
    junk = f"; filtered {len(m['dropped_junk'])} junk addr" if m["dropped_junk"] else ""
    if existing is None:
        return "fresh", f"{len(new_content)} bytes{junk}"
    return "enriched", f"{kind} -> summary record, {len(existing)} -> {len(new_content)} bytes{junk}"


def mark_unavailable_one(meeting_id: str, vault: Path, today: str, dry_run: bool):
    if not valid_meeting_id(meeting_id):
        return "error", f"invalid meeting id {meeting_id!r} — rejected before building a path"
    target = vault / CFG.folder_name("raw") / "fireflies" / f"{meeting_id}.md"
    if not target.exists():
        return "error", f"{target.name} does not exist"
    text = target.read_text(encoding="utf-8")
    kind, fm = classify(text)
    if kind != "stub":
        return "conflict", f"{target.name} is not a link-only stub ({kind}) — refusing to annotate"
    if fm_scalar(fm, UNAVAILABLE_KEY):
        return "already-current", "already annotated unavailable"
    fm_out = list(fm) + [f"{UNAVAILABLE_KEY}: {today}"]
    _, body = split_frontmatter(text)
    note = (
        f"\nChecked against Fireflies on {today}: this meeting is no longer available\n"
        "at source (aged out), so its summary cannot be recovered.\n"
    )
    new_text = "---\n" + "\n".join(fm_out) + "\n---\n" + body.rstrip("\n") + "\n" + note
    if not dry_run:
        atomic_write(target, new_text)
    return "unavailable-marked", "stub annotated unavailable-at-source"


def cmd_pending(vault: Path) -> int:
    """List every raw file that has not yet converged on the DEC-034 format:
    link-only stubs awaiting enrichment, and legacy DEC-032 summary conversions
    that predate the `enriched_at:` marker and re-render on first touch. Without
    the legacy rows the backfill's "repeat until pending: 0" loop would terminate
    with the 26 conversions untouched (F2). One row per file:
    `<meeting_id>\t<date>\t<stub|legacy-summary>`."""
    raw_dir = vault / CFG.folder_name("raw") / "fireflies"
    rows = []
    for path in sorted(raw_dir.glob("*.md")):
        fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
        if fm is None:
            continue
        status = fm_scalar(fm, "transcript_status")
        if status == "not-retained-locally":
            if fm_scalar(fm, UNAVAILABLE_KEY):
                continue
            kind = "stub"
        elif status == "summary" and not fm_scalar(fm, "enriched_at"):
            # A summary record with no enriched_at is a pre-DEC-034 conversion;
            # render() always emits enriched_at, so its absence marks legacy.
            kind = "legacy-summary"
        else:
            continue
        mid = fm_scalar(fm, "meeting_id") or path.stem
        rows.append((fm_scalar(fm, "date") or "", mid, kind))
    for date, mid, kind in sorted(rows):
        print(f"{mid}\t{date}\t{kind}")
    counts = {}
    for _, _, kind in rows:
        counts[kind] = counts.get(kind, 0) + 1
    breakdown = ", ".join(f"{k}={counts[k]}" for k in sorted(counts))
    tail = f" ({breakdown})" if breakdown else ""
    print(f"pending: {len(rows)}{tail}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------- cli

def load_payload(spec: str):
    raw = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict) and "transcripts" in data:
        data = data["transcripts"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("payload must be a meeting object or a list of them")
    return data


def report(results) -> int:
    counts = {}
    for status, mid, detail in results:
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:17} {mid}  ({detail})")
    print("\n" + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("conflict") or counts.get("error") else 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="fireflies summary-record writer")
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("apply")
    a.add_argument("--payload", required=True, help="meeting JSON file, or - for stdin")
    a.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    a.add_argument("--dry-run", action="store_true")

    u = sub.add_parser("mark-unavailable")
    u.add_argument("ids", nargs="+")
    u.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    u.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("pending")
    p.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")

    args = ap.parse_args(argv)
    args.vault = args.vault or DEFAULT_VAULT
    today = dt.date.today().isoformat()

    if args.command == "pending":
        return cmd_pending(args.vault)

    results = []
    if args.command == "apply":
        try:
            payload = load_payload(args.payload)
        except (OSError, ValueError) as e:  # bad path / malformed JSON / wrong shape
            print(f"error: {e}", file=sys.stderr)
            return 2
        for meeting in payload:
            mid = str((meeting or {}).get("id") or "?")
            try:
                status, detail = apply_one(meeting, args.vault, today, args.dry_run)
            except Exception as e:  # keep the batch going; report the failure
                status, detail = "error", str(e)
            results.append((status, mid, detail))
    else:
        for mid in args.ids:
            try:
                status, detail = mark_unavailable_one(mid, args.vault, today, args.dry_run)
            except Exception as e:
                status, detail = "error", str(e)
            results.append((status, mid, detail))
    return report(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
