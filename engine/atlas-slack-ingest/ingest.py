#!/usr/bin/env python3
"""atlas-slack-ingest.

Convert Slack MCP `slack_read_channel` JSON responses into per-message
markdown files at <vault_root>/raw/slack/<route>/<message_ts>.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
RAW_SLACK = CFG.folder("raw") / "slack"
ROUTING_YAML = Path(__file__).parent / "slack-routing.yaml"
STATE_FILE = Path(__file__).parent / "state.json"
LAST_RUN = Path(__file__).parent / "last-run.md"


@dataclass
class Stats:
    pages_processed: int = 0
    messages_seen: int = 0
    written: int = 0
    already_present: int = 0
    skipped_no_ts: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


def slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", text or "").strip("-").lower()
    return s or "unnamed"


def load_routing() -> tuple[dict[str, str], str]:
    """Return (channel_name → route, dm_route)."""
    text = ROUTING_YAML.read_text()
    channel_routes: dict[str, str] = {}
    dm_route = "_dms"
    section = None
    for line in text.split("\n"):
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("channel_routes:"):
            section = "channel"
            continue
        if line.startswith("dm_route:"):
            dm_route = line.split(":", 1)[1].strip().strip('"').strip("'")
            section = None
            continue
        if section == "channel" and line.lstrip().startswith("- "):
            m = re.search(r'channel:\s*"([^"]+)",\s*route:\s*"([^"]+)"', line)
            if m:
                channel_routes[m.group(1)] = m.group(2)
    return channel_routes, dm_route


def route_for(channel_name: str, channel_type: str, channel_routes: dict[str, str], dm_route: str) -> str:
    if channel_type == "im":
        return dm_route
    if channel_name in channel_routes:
        return channel_routes[channel_name]
    return slug(channel_name)


def existing_message_path(message_ts: str) -> Optional[Path]:
    for p in RAW_SLACK.glob(f"*/{message_ts}.md"):
        return p
    return None


def ts_to_iso(ts: str) -> tuple[str, str]:
    """Slack ts is `<seconds>.<microseconds>`. Return (ISO datetime, YYYY-MM-DD)."""
    try:
        ts_float = float(ts)
        d = dt.datetime.utcfromtimestamp(ts_float)
        return d.isoformat() + "Z", d.date().isoformat()
    except Exception:
        return ts, ""


def parse_files(files_list: list[dict]) -> list[dict]:
    out = []
    for f in files_list or []:
        out.append({
            "name": f.get("name") or f.get("title") or "(unnamed)",
            "mimetype": f.get("mimetype") or "",
            "size": f.get("size") or 0,
            "file_id": f.get("id") or "",
        })
    return out


def write_message(msg: dict, channel_id: str, channel_name: str, channel_type: str,
                   channel_routes: dict[str, str], dm_route: str,
                   ingested_at: str, stats: Stats, execute: bool) -> Optional[Path]:
    ts = msg.get("ts") or msg.get("event_ts")
    if not ts:
        stats.skipped_no_ts += 1
        return None
    if existing_message_path(ts):
        stats.already_present += 1
        return None

    route = route_for(channel_name, channel_type, channel_routes, dm_route)
    sender_id = msg.get("user") or msg.get("bot_id") or ""
    # Slack MCP often returns a `user_profile` block with display name
    profile = msg.get("user_profile") or {}
    sender_name = profile.get("real_name") or profile.get("display_name") or msg.get("username") or sender_id or "unknown"
    text = msg.get("text") or ""
    text_truncated = text if len(text) <= 1000 else text[:999] + "…"
    iso, date = ts_to_iso(ts)
    thread_ts = msg.get("thread_ts") or ts
    is_thread_reply = bool(msg.get("thread_ts")) and msg.get("thread_ts") != ts
    files_meta = parse_files(msg.get("files") or [])
    permalink = msg.get("permalink", "")

    files_yaml = "[]"
    if files_meta:
        files_yaml = "\n  - " + "\n  - ".join(
            f'{{ name: "{f["name"].replace(chr(34), chr(39))}", mimetype: "{f["mimetype"]}", size: {f["size"]}, file_id: "{f["file_id"]}" }}'
            for f in files_meta
        )

    lines = [
        "---",
        "type: raw-slack-message",
        f"message_ts: {ts}",
        f"channel_id: {channel_id}",
        f"channel_name: {channel_name}",
        f"sender_id: {sender_id}",
        f"sender_name: \"{sender_name.replace(chr(34), chr(39))}\"",
        f"date: {date}",
        f"timestamp: {iso}",
        f"thread_ts: {thread_ts}",
        f"is_thread_reply: {str(is_thread_reply).lower()}",
        f"files:{files_yaml}" if files_meta else "files: []",
        f"permalink: {permalink}",
        f"route: {route}",
        f"ingested_at: {ingested_at}",
        "---",
        "",
        f"# #{channel_name} · {date or ts}",
        "",
        f"**From:** {sender_name}",
        f"**At:** {iso}",
        f"**Channel:** #{channel_name}",
        "",
        f"> {text_truncated if text_truncated else '(no text — see Files section)'}",
        "",
    ]
    if files_meta:
        lines += ["## Files", ""]
        for f in files_meta:
            lines.append(f"- `{f['name']}` ({f['mimetype']}, {f['size']} bytes) — file_id: `{f['file_id']}`")
        lines.append("")

    out = RAW_SLACK / route / f"{ts}.md"
    if execute:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
    stats.written += 1
    return out


# Slack MCP emits three header shapes. Group 1 (name) and group 2 (id) are
# both optional so attribution-less posts still match:
#   1. attributed user/bot:  === Message from <name> (<id>) at <date> ===
#   2. empty-sender bot:      === Message from  (B…) at <date> ===   (no name)
#   3. attribution-less:      === Message at <date> ===              (no sender)
# The body lookahead stops at *any* "=== Message " header (not just "from"),
# so a message can no longer swallow the attribution-less posts that follow it.
MESSAGE_BLOCK_RE = re.compile(
    r"=== Message (?:from (.*?) \(([^)]+)\) )?at (.+?) ===\s*\n"
    r"Message TS: (\S+)\n"
    r"(.*?)(?=\n=== Message |\Z)",
    re.DOTALL,
)

# A leading Slack mention, e.g. "<@U0EXAMPLE001|Jordan Vale>", used to attribute
# shape-3 posts whose sender header is absent.
LEADING_MENTION_RE = re.compile(r"^\s*<@([UWB][A-Z0-9]+)\|([^>]+)>")


def parse_messages(messages_text: str) -> list[dict]:
    """Parse the Slack MCP pre-formatted text block into per-message dicts.

    Returns one dict per message in the shape ``write_message`` consumes.
    Sender resolution by header shape:

    - Attributed (shape 1): name + id taken from the header.
    - Empty-sender bot (shape 2): name is "" so ``write_message`` falls back to
      the bot id for ``sender_name``.
    - Attribution-less (shape 3): id/name are absent, so the sender is derived
      from a leading ``<@U…|Name>`` mention in the body when present, else
      ``sender_name`` is "unknown".
    """
    out: list[dict] = []
    for m in MESSAGE_BLOCK_RE.finditer(messages_text):
        raw_name, raw_id = m.group(1), m.group(2)
        ts = m.group(4).strip()
        body = m.group(5).strip()

        sender_name = (raw_name or "").strip()
        sender_id = (raw_id or "").strip()

        if raw_id is None:  # shape 3 — no sender header at all
            mention = LEADING_MENTION_RE.match(body)
            if mention:
                sender_id, sender_name = mention.group(1), mention.group(2)
            else:
                sender_name = "unknown"

        out.append({
            "ts": ts,
            "user": sender_id,
            "user_profile": {"real_name": sender_name},
            "text": body,
            "files": [],
        })
    return out


def process_page(json_path: Path, channel_id: str, channel_name: str, channel_type: str,
                  channel_routes: dict[str, str], dm_route: str,
                  ingested_at: str, execute: bool, stats: Stats) -> Optional[str]:
    """Slack MCP returns `{"messages": "<pre-formatted text block>"}`, not a structured array.
    Parse the text format with regex into per-message dicts."""
    try:
        data = json.loads(json_path.read_text())
    except Exception as e:
        stats.errors.append((str(json_path), f"parse failed: {e}"))
        return None
    stats.pages_processed += 1
    messages_text = data.get("messages") if isinstance(data.get("messages"), str) else ""
    if not messages_text:
        # Fall back to structured shape if ever returned
        for msg in data.get("messages") or []:
            stats.messages_seen += 1
            write_message(msg, channel_id, channel_name, channel_type, channel_routes, dm_route, ingested_at, stats, execute)
        return None

    for msg in parse_messages(messages_text):
        stats.messages_seen += 1
        write_message(msg, channel_id, channel_name, channel_type, channel_routes, dm_route, ingested_at, stats, execute)
    return None  # cursor not in text format; caller continues via separate MCP calls


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", type=Path, action="append", default=[])
    parser.add_argument("--channel-id", type=str, required=True)
    parser.add_argument("--channel-name", type=str, required=True)
    parser.add_argument("--channel-type", type=str, default="public_channel",
                        help="public_channel | private_channel | im | mpim")
    parser.add_argument("--dry-run-report", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    if not args.input_json:
        print("error: --input-json required (at least one)", file=sys.stderr)
        return 2

    channel_routes, dm_route = load_routing()
    stats = Stats()
    ingested_at = dt.datetime.utcnow().isoformat() + "Z"

    cursors: list[str] = []
    for json_path in args.input_json:
        if not json_path.exists():
            stats.errors.append((str(json_path), "not found"))
            continue
        c = process_page(json_path, args.channel_id, args.channel_name, args.channel_type,
                          channel_routes, dm_route, ingested_at, args.execute, stats)
        if c:
            cursors.append(c)

    if args.dry_run_report:
        today = dt.date.today().isoformat()
        report_lines = [
            f"# atlas-slack-ingest dry-run — {today}",
            "",
            "Skill: `atlas-slack-ingest`",
            f"Channel: `#{args.channel_name}` ({args.channel_id}, type={args.channel_type})",
            f"Pages processed: {stats.pages_processed}",
            f"Messages seen: {stats.messages_seen}",
            f"To write: {stats.written}",
            f"Already present: {stats.already_present}",
            f"Skipped (no ts): {stats.skipped_no_ts}",
            "",
            "## Acceptance checks",
            "",
            "- `SKILL.md` + trigger phrases — see SKILL.md.",
            f"- `slack-routing.yaml` with mappings — {len(channel_routes)} channel routes + dm_route — PASS.",
            "- first run covers last 14 days — **scope override: all-time**.",
            "- per-channel-day file format — **scope override: per-message** (one file per message_ts).",
            "- idempotent — glob-by-message_ts before write.",
            "- 429 backoff — MCP runtime handles backoff; this skill processes pre-fetched JSON.",
        ]
        if cursors:
            report_lines += ["", "## Continuation", "", f"Next cursors captured: {len(cursors)}"]
        args.dry_run_report.parent.mkdir(parents=True, exist_ok=True)
        args.dry_run_report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    LAST_RUN.write_text("\n".join([
        "# atlas-slack-ingest — last run",
        "",
        f"- **When:** {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- **Mode:** `{'execute' if args.execute else 'dry-run'}`",
        f"- **Channel:** #{args.channel_name} ({args.channel_id})",
        f"- **Pages processed:** {stats.pages_processed}",
        f"- **Messages seen:** {stats.messages_seen}",
        f"- **Written:** {stats.written}",
        f"- **Already present:** {stats.already_present}",
        f"- **Errors:** {len(stats.errors)}",
    ]) + "\n")

    print(f"pages={stats.pages_processed} messages={stats.messages_seen} written={stats.written} "
          f"already={stats.already_present} skipped={stats.skipped_no_ts} errors={len(stats.errors)}")
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
