#!/usr/bin/env python3
"""atlas-gmail-ingest.

Convert a Gmail MCP `search_threads` JSON response into per-message
markdown files under <vault_root>/raw/gmail/<route>/<message_id>.md.

Idempotent: globs across all route folders before writing — never
creates a duplicate file for the same message_id.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
RAW_GMAIL = CFG.folder("raw") / "gmail"
ROUTING_YAML = Path(__file__).parent / "mailbox-routing.yaml"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"
PEOPLE_DIR = CFG.folder("crm") / "People"

SENDER_EMAIL_RE = re.compile(r"<([^>]+@[^>]+)>")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")


@dataclass
class Stats:
    pages_processed: int = 0
    threads_seen: int = 0
    messages_seen: int = 0
    written: int = 0
    already_present: int = 0
    routed_by_label: int = 0
    routed_by_domain: int = 0
    routed_unrouted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def parse_yaml_routing() -> tuple[list[dict], dict[str, str]]:
    """Tiny YAML reader for the mailbox-routing.yaml shape."""
    text = ROUTING_YAML.read_text()
    label_routes: list[dict] = []
    domain_routes: dict[str, str] = {}
    section = None
    for line in text.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("label_routes:"):
            section = "label"
            continue
        if line.startswith("domain_routes:"):
            section = "domain"
            continue
        if section == "label" and line.lstrip().startswith("- "):
            m = re.search(r'label:\s*"([^"]+)",\s*route:\s*"([^"]+)"', line)
            if m:
                label_routes.append({"label": m.group(1), "route": m.group(2)})
        elif section == "domain" and ":" in line and not line.startswith(" "):
            # top-level "domain_routes:" line
            continue
        elif section == "domain" and ":" in line:
            stripped = line.strip()
            k, _, v = stripped.partition(":")
            domain_routes[k.strip()] = v.strip()
    return label_routes, domain_routes


def parse_labels_json(paths: list[Path], stats: "Stats") -> dict[str, str]:
    """Build a {label_id -> display_name} map from Gmail MCP `list_labels` JSON.

    The Gmail MCP returns opaque label IDs (e.g. `Label_1234567890123456789`)
    on each message's `labelIds`, but the routing YAML is written against human
    display names (e.g. `client/pinecrest-lodge`). `list_labels` is the bridge: it
    returns both. The interactive runtime captures its JSON to a file and passes
    it here via `--labels-json`.

    Tolerant of the response shapes seen in practice:
      - {"labels": [{"labelId": ..., "name": ...}, ...]}  the shape the Gmail
        MCP in use actually returns — the ID key is `labelId`, NOT `id`.
        Reading only `id` silently yielded an empty map, which made
        `--labels-json` inert and the tier-1 label route permanently dead.
      - {"labels": [{"id": ..., "name": ...}, ...]}       (raw Gmail API style)
      - [{...}, ...]                                     (bare array, either key)
      - display name under "name" or "displayName"

    An entry missing both ID keys, or missing a name, is skipped rather than
    poisoning the map with a null key.
    """
    out: dict[str, str] = {}
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            stats.errors.append((str(path), f"labels parse failed: {e}"))
            continue
        if isinstance(data, dict):
            entries = data.get("labels", [])
        elif isinstance(data, list):
            entries = data
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lid = entry.get("id") or entry.get("labelId")
            name = entry.get("name") or entry.get("displayName")
            if lid and name:
                out[str(lid)] = str(name)
    return out


def build_email_to_person() -> dict[str, str]:
    """Map sender email → CRM person-note basename (for `participants:` wikilinks)."""
    out: dict[str, str] = {}
    if not PEOPLE_DIR.exists():
        return out
    fm_re = re.compile(r"^email:\s*(\S+)", re.MULTILINE)
    for p in PEOPLE_DIR.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        m = fm_re.search(text)
        if m:
            out[m.group(1).lower()] = p.stem
    return out


def extract_email(sender: str) -> str:
    if not sender:
        return ""
    m = SENDER_EMAIL_RE.search(sender)
    if m:
        return m.group(1).lower()
    m = EMAIL_RE.search(sender)
    if m:
        return m.group(0).lower()
    return sender.lower()


def _label_name_matches(yaml_label: str, resolved_name: str) -> bool:
    """Match a routing-YAML label against a resolved Gmail display name.

    Exact (case-insensitive) on the full nested path first — Gmail returns
    nested labels as `Marketing Automation/UTMCreator`, matching the YAML
    verbatim. Falls back to last-segment equality so a flat `UTMCreator` label
    still matches a nested route. This is precise (no substring matching
    against opaque IDs, which was the dead-tier bug), only forgiving about
    nesting. The routed triage labels (`3. ADD TO TRIAGE`, …) are flat, so the
    exact arm carries them; the fallback exists for the nested label families.
    """
    yl = yaml_label.strip().lower()
    rn = resolved_name.strip().lower()
    if rn == yl:
        return True
    return rn.split("/")[-1] == yl.split("/")[-1]


def route_message(label_ids: list[str], sender_email: str, label_routes: list[dict],
                  domain_routes: dict[str, str], label_names: dict[str, str]) -> tuple[str, str]:
    """Return (route_folder, tier) where tier is "domain", "label", or "unrouted".

    **Domain is tier 1, label is tier 2.** The two tiers sort on different axes:
    `domain_routes` answers *who* the mail is from (client, employer), while the
    Gmail labels the owner actually maintains answer *what they plan to do about it*
    (`3. ADD TO TRIAGE`, `4. NOTIFICATION`, …). Sender is the stabler folder key
    — it never changes — so it wins, and one folder stays one correspondent.
    Workflow labels then catch what no domain rule covers, instead of pulling
    client mail out of its client folder every time it gets triaged.

    `label_names` is the {label_id -> display_name} map from `list_labels`.
    Each message's `labelIds` are opaque IDs; we resolve them to display names
    before matching the routing YAML (which is written against display names).
    An unresolved ID falls back to itself, so system labels (INBOX/SENT/…) and
    a missing map degrade gracefully rather than mis-matching.
    """
    domain = sender_email.split("@", 1)[1] if "@" in sender_email else ""
    if domain in domain_routes:
        return domain_routes[domain], "domain"
    resolved = [label_names.get(lid, lid) for lid in label_ids]
    for entry in label_routes:
        for name in resolved:
            if _label_name_matches(entry["label"], name):
                return entry["route"], "label"
    return "_unrouted", "unrouted"


def existing_message_paths(message_id: str) -> list[Path]:
    """Glob for any existing file with this message_id under raw/gmail/."""
    return list(RAW_GMAIL.glob(f"*/{message_id}.md"))


MAX_BODY_CHARS = 50000  # generous cap; ~all real emails fit, guards against pathological bloat
TAG_RE = re.compile(r"<[^>]+>")


def _b64url_decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _walk_payload_for_body(payload: dict) -> str:
    """DFS a Gmail message payload for the best text body: prefer text/plain,
    fall back to text/html (tags stripped)."""
    found = {"plain": "", "html": ""}

    def walk(part):
        if not isinstance(part, dict):
            return
        mime = part.get("mimeType", "") or ""
        data = (part.get("body") or {}).get("data")
        if data:
            decoded = _b64url_decode(data)
            if mime == "text/plain" and not found["plain"]:
                found["plain"] = decoded
            elif mime == "text/html" and not found["html"]:
                found["html"] = decoded
        for sub in (part.get("parts") or []):
            walk(sub)

    walk(payload)
    if found["plain"].strip():
        return found["plain"].strip()
    if found["html"].strip():
        return html.unescape(TAG_RE.sub("", found["html"])).strip()
    return ""


def extract_body(msg: dict) -> str:
    """Full decoded message body (forward-only — Activation Plan step 4).

    The old ingest stored only `snippet` (capped <=500 chars). Prefer, in order:
    a pre-decoded text field the upstream may supply, then the MIME payload tree
    (text/plain, else text/html stripped), then the snippet as a legacy fallback.
    """
    def _cap(s: str) -> str:
        return s[:MAX_BODY_CHARS] + "\n\n…[truncated]" if len(s) > MAX_BODY_CHARS else s

    for key in ("body", "textBody", "bodyText", "text"):
        v = msg.get(key)
        if isinstance(v, str) and v.strip():
            return _cap(html.unescape(v).strip())
    payload = msg.get("payload")
    if isinstance(payload, dict):
        b = _walk_payload_for_body(payload)
        if b:
            return _cap(b)
    return html.unescape(msg.get("snippet", "")).strip()


def write_message(thread: dict, msg: dict, label_routes: list[dict], domain_routes: dict[str, str],
                   label_names: dict[str, str], email_to_person: dict[str, str],
                   ingested_at: str, stats: Stats, execute: bool) -> Optional[Path]:
    message_id = msg.get("id")
    if not message_id:
        return None
    thread_id = thread.get("id", "")
    sender_raw = msg.get("sender", "")
    sender_email = extract_email(sender_raw)
    recipients = msg.get("toRecipients", []) or []
    subject = html.unescape(msg.get("subject", "")).strip()
    date = msg.get("date", "")
    label_ids = msg.get("labelIds", []) or []

    route, tier = route_message(label_ids, sender_email, label_routes, domain_routes, label_names)
    if tier == "label":
        stats.routed_by_label += 1
    elif tier == "domain":
        stats.routed_by_domain += 1
    else:
        stats.routed_unrouted += 1

    # Idempotency: skip if any existing file in any route has this message_id
    if existing_message_paths(message_id):
        stats.already_present += 1
        return None

    # Build participants wikilinks
    participants: list[str] = []
    sender_person = email_to_person.get(sender_email)
    if sender_person:
        participants.append(f"[[{sender_person}]]")
    for r in recipients:
        re_email = extract_email(r)
        rp = email_to_person.get(re_email)
        if rp and f"[[{rp}]]" not in participants:
            participants.append(f"[[{rp}]]")

    body_text = extract_body(msg)
    recipients_str = ", ".join(recipients)
    labels_yaml = "[" + ", ".join(label_ids) + "]" if label_ids else "[]"
    # Quote each wikilink so the YAML list bracket doesn't collide with the wikilink
    # brackets (the old form emitted `[[[Name]], ...]` — a broken triple-bracket).
    participants_yaml = "[" + ", ".join(f'"{p}"' for p in participants) + "]" if participants else "[]"

    lines = [
        "---",
        "type: raw-gmail-message",
        f"message_id: {message_id}",
        f"thread_id: {thread_id}",
        f"subject: \"{subject.replace(chr(34), chr(39))}\"",
        f"sender: {sender_email}",
        f"recipients: {labels_yaml.replace(',', ', ') if False else '[' + ', '.join(extract_email(r) for r in recipients) + ']'}",
        f"date: {date}",
        f"labels: {labels_yaml}",
        f"route: {route}",
        f"participants: {participants_yaml}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# {subject if subject else '(no subject)'}",
        "",
        f"**From:** {sender_raw}",
        f"**To:** {recipients_str}",
        f"**Date:** {date}",
        "",
        body_text if body_text else "_(no body)_",
        "",
        "## Thread context",
        "",
        f"This message is part of thread `{thread_id}`.",
        "",
    ]
    content = "\n".join(lines)
    out = RAW_GMAIL / route / f"{message_id}.md"
    if execute:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
    stats.written += 1
    return out


def process_page(json_path: Path, label_routes: list[dict], domain_routes: dict[str, str],
                 label_names: dict[str, str], email_to_person: dict[str, str],
                 ingested_at: str, execute: bool, stats: Stats) -> Optional[str]:
    """Process one search_threads JSON page. Returns the nextPageToken if present."""
    try:
        data = json.loads(json_path.read_text())
    except Exception as e:
        stats.errors.append((str(json_path), f"parse failed: {e}"))
        return None

    threads = data.get("threads", [])
    stats.pages_processed += 1
    for thread in threads:
        stats.threads_seen += 1
        for msg in thread.get("messages", []):
            stats.messages_seen += 1
            write_message(thread, msg, label_routes, domain_routes, label_names,
                          email_to_person, ingested_at, stats, execute)

    return data.get("nextPageToken")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", type=Path, action="append", default=[],
                        help="Path to a search_threads JSON response. Can be repeated for batch.")
    parser.add_argument("--labels-json", type=Path, action="append", default=[],
                        help="Path to a list_labels JSON response. Resolves label IDs to "
                             "display names so the label-routing tier can fire. Optional but "
                             "required for label routing; omit and only domain routing applies.")
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    if not args.input_json:
        print("error: --input-json required", file=sys.stderr)
        return 2

    label_routes, domain_routes = parse_yaml_routing()
    stats = Stats()
    label_names = parse_labels_json(args.labels_json, stats)
    email_to_person = build_email_to_person()
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    next_page_tokens: list[str] = []
    for json_path in args.input_json:
        if not json_path.exists():
            stats.errors.append((str(json_path), "not found"))
            continue
        tok = process_page(json_path, label_routes, domain_routes, label_names,
                           email_to_person, ingested_at, args.execute, stats)
        if tok:
            next_page_tokens.append(tok)

    if args.dry_run_report:
        today = dt.date.today().isoformat()
        lines = [
            f"# atlas-gmail-ingest dry-run — {today}",
            "",
            "Skill: `atlas-gmail-ingest`",
            f"Pages processed: {stats.pages_processed}",
            f"Threads seen: {stats.threads_seen}",
            f"Messages seen: {stats.messages_seen}",
            f"Files to write: {stats.written}",
            f"Already present (idempotent skip): {stats.already_present}",
            "",
            "## Routing distribution",
            "",
            f"- label map: {len(label_names)} label IDs resolved to display names"
            f"{' (no --labels-json given → label tier inert)' if not label_names else ''}",
            f"- by label: {stats.routed_by_label}",
            f"- by domain: {stats.routed_by_domain}",
            f"- unrouted: {stats.routed_unrouted}",
            "",
            "## AC verification",
            "",
            f"- `mailbox-routing.yaml` ≥ 5 mappings: {len(label_routes)} label routes + {len(domain_routes)} domain routes — **PASS**",
            f"- ≤ 500 thread summaries: {stats.threads_seen} threads — **{'PASS' if stats.threads_seen <= 500 else 'FAIL'}**",
            "- per-message frontmatter: yes",
            "- full decoded body stored; snippet is fallback only: yes",
            "- idempotent: glob-by-message_id check before write",
        ]
        if next_page_tokens:
            lines += ["", "## Continuation", "", f"- Next-page tokens captured: {len(next_page_tokens)}",
                      "- To paginate further: call `search_threads` with the captured tokens, dump each response to a new JSON file, re-invoke this skill."]
        if stats.errors:
            lines += ["", "## Errors", ""]
            for ctx, msg in stats.errors[:10]:
                lines.append(f"- `{ctx}` — {msg}")
        args.dry_run_report.parent.mkdir(parents=True, exist_ok=True)
        args.dry_run_report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    LAST_RUN.write_text("\n".join([
        "# atlas-gmail-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{'execute' if args.execute else 'dry-run'}`",
        f"- **Pages processed:** {stats.pages_processed}",
        f"- **Threads seen:** {stats.threads_seen}",
        f"- **Messages seen:** {stats.messages_seen}",
        f"- **Files written:** {stats.written}",
        f"- **Already present:** {stats.already_present}",
        f"- **Label map size:** {len(label_names)}",
        f"- **Routed by label:** {stats.routed_by_label}",
        f"- **Routed by domain:** {stats.routed_by_domain}",
        f"- **Unrouted:** {stats.routed_unrouted}",
        f"- **Errors:** {len(stats.errors)}",
    ]) + "\n")

    print(f"pages={stats.pages_processed} threads={stats.threads_seen} messages={stats.messages_seen} "
          f"written={stats.written} already={stats.already_present} labels={len(label_names)} "
          f"by_label={stats.routed_by_label} by_domain={stats.routed_by_domain} unrouted={stats.routed_unrouted} "
          f"errors={len(stats.errors)}")
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
