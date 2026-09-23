#!/usr/bin/env python3
"""atlas-distill — distill a Claude chat exchange into an entity-linked resource note.

See SKILL.md for the contract. Human-in-the-loop: the calling agent prepares a
"capture spec" (distilled body + raw exchange + proposed metadata, with entity
wikilinks already resolved against the vault) and passes it as JSON via --input
(or stdin). This script handles the side effects deterministically:

  1. Redact secrets in BOTH outputs (verbatim patterns from atlas-claude-history-ingest).
  2. Write an append-only redacted source record to raw/distill/<capture-id>.md.
  3. Write/update the distilled note at "40 - Resources/<topic>/<title>.md" with a
     source_capture_id backpointer and a preserved editable region.
  4. Resolve [[Entity]] wikilinks against CRM/People/ and wiki/entities/; flag
     unresolved entities (never invent them).
  5. Write state.json (capture-id ledger) + last-run.md.

v1 is mechanical: no LLM body content here — the agent supplies the distilled body.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()
VAULT_DEFAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = CFG.folder_name("resources")
RAW_DISTILL_DIR = CFG.rel("raw", "distill")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

EDITABLE_START = "<!-- atlas-distill:editable-start -->"
EDITABLE_END = "<!-- atlas-distill:editable-end -->"

# Verbatim from atlas-claude-history-ingest/ingest.py — keep in sync.
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


def capture_id_for(topic: str, title: str) -> str:
    """Deterministic capture id from topic+title — the durable dedup key.

    Same topic/title -> same id -> same raw path -> idempotent re-runs.
    """
    digest = hashlib.sha1(f"{topic}/{title}".encode("utf-8")).hexdigest()[:10]
    return f"distill-{digest}"


def extract_editable(existing_text: str) -> str | None:
    """Pull content between editable markers from an existing destination note."""
    pat = re.compile(
        rf"{re.escape(EDITABLE_START)}\s*\n(.*?)\n\s*{re.escape(EDITABLE_END)}",
        re.DOTALL,
    )
    m = pat.search(existing_text)
    if not m:
        return None
    return m.group(1).strip()


def resolve_entities(vault: Path, body: str) -> tuple[list[str], list[str]]:
    """Return (resolved, unresolved) wikilink targets found in the note body.

    A link resolves if CRM/People/<name>.md or wiki/entities/<name>.md exists
    (case-insensitive). Unresolved links are reported, never invented or stripped.
    """
    people_dir = vault / CFG.folder_name("crm") / "People"
    entities_dir = vault / CFG.folder_name("wiki") / "entities"
    people = {p.stem.lower() for p in people_dir.glob("*.md")} if people_dir.exists() else set()
    entities = {p.stem.lower() for p in entities_dir.glob("*.md")} if entities_dir.exists() else set()
    resolved: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for name in WIKILINK_RE.findall(body):
        key = name.strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        if key.lower() in people or key.lower() in entities:
            resolved.append(key)
        else:
            unresolved.append(key)
    return resolved, unresolved


def yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def render_raw_record(
    capture_id: str,
    today: dt.date,
    captured_at: str,
    tags: list[str],
    source_session: str,
    topic: str,
    title: str,
    exchange_redacted: str,
) -> str:
    """The append-only source record. Frontmatter order: date, id, type, tags, then alpha."""
    return (
        "---\n"
        f"date: {today.isoformat()}\n"
        f"id: {capture_id}\n"
        "type: raw-distill\n"
        f"tags: {yaml_list(tags)}\n"
        f"captured_at: {captured_at}\n"
        f"resource_note: \"[[{title}]]\"\n"
        f"source_session: {source_session or 'null'}\n"
        f"topic: {topic}\n"
        "---\n"
        "\n"
        f"# Distill capture · {title}\n"
        "\n"
        "> Append-only source record (DEC-009). The distilled note lives at "
        f"`40 - Resources/{topic}/{title}.md`. Do not edit or delete this file; "
        "corrections use the `corrected_by:` / `corrects:` chain.\n"
        "\n"
        "## Exchange (redacted)\n"
        "\n"
        f"{exchange_redacted.rstrip()}\n"
    )


def render_resource_note(
    capture_id: str,
    today: dt.date,
    tags: list[str],
    source_session: str,
    topic: str,
    title: str,
    body_redacted: str,
    editable_body: str,
) -> str:
    """The distilled note. Frontmatter order: date, id, type, tags, then alphabetical."""
    return (
        "---\n"
        f"date: {today.isoformat()}\n"
        f"id: {capture_id}\n"
        "type: distilled-resource\n"
        f"tags: {yaml_list(tags)}\n"
        f"source_capture_id: \"[[{capture_id}]]\"\n"
        f"source_session: {source_session or 'null'}\n"
        f"topic: {topic}\n"
        "---\n"
        "\n"
        f"# {title.replace('-', ' ').title()}\n"
        "\n"
        f"{body_redacted.rstrip()}\n"
        "\n"
        "## Notes\n"
        "\n"
        f"{EDITABLE_START}\n"
        f"{editable_body}\n"
        f"{EDITABLE_END}\n"
    )


def load_spec(args) -> dict:
    if args.input:
        raw = Path(args.input).expanduser().read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("capture spec must be a JSON object")
    return spec


def validate_spec(spec: dict) -> list[str]:
    errors: list[str] = []
    for field in ("topic", "title", "body", "exchange"):
        if not str(spec.get(field, "")).strip():
            errors.append(f"missing required field: {field}")
    for field in ("topic", "title"):
        val = str(spec.get(field, "")).strip()
        if val and not SLUG_RE.match(val):
            errors.append(f"{field} must be kebab-case (got: {val!r})")
    tags = spec.get("tags") or []
    if not isinstance(tags, list):
        errors.append("tags must be a list")
    return errors


def write_last_run(payload: dict) -> None:
    lines = ["# atlas-distill — last run", ""]
    for k, v in payload.items():
        lines.append(f"- {k}: {v}")
    (SKILL_DIR / "last-run.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_state(capture_id: str, topic: str, title: str, when: str) -> None:
    state_path = SKILL_DIR / "state.json"
    state = {"captures": {}}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {"captures": {}}
    state.setdefault("captures", {})[capture_id] = {
        "topic": topic,
        "title": title,
        "last_written": when,
    }
    state["last_run_iso"] = when
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, state_path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Distill a chat exchange into a resource note.")
    ap.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--input", type=str, default=None,
                    help="path to capture-spec JSON (reads stdin if omitted)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the proposal + preview, write nothing")
    ap.add_argument("--execute", action="store_true", help="write both files")
    ap.add_argument("--force", action="store_true",
                    help="regenerate the resource note outside its editable region")
    ap.add_argument("--today", type=str, default=None)
    args = ap.parse_args()

    if not args.dry_run and not args.execute:
        print("pick a mode: --dry-run or --execute", file=sys.stderr)
        return 2

    start = time.time()
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2

    try:
        spec = load_spec(args)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"could not read capture spec: {e}", file=sys.stderr)
        return 2

    errors = validate_spec(spec)
    if errors:
        for e in errors:
            print(f"spec error: {e}", file=sys.stderr)
        return 2

    topic = str(spec["topic"]).strip()
    title = str(spec["title"]).strip()
    tags = [str(t).strip().lstrip("#") for t in (spec.get("tags") or [])]
    if "distilled" not in tags:
        tags.insert(0, "distilled")  # make every distilled note queryable
    source_session = str(spec.get("source_session", "") or "").strip()
    body = str(spec["body"])
    exchange = str(spec["exchange"])

    capture_id = str(spec.get("capture_id", "") or "").strip() or capture_id_for(topic, title)

    body_redacted, body_red = redact(body)
    exchange_redacted, ex_red = redact(exchange)

    resolved, unresolved = resolve_entities(vault, body_redacted)

    raw_path = vault / RAW_DISTILL_DIR / f"{capture_id}.md"
    dest_dir = vault / RESOURCES_DIR / topic
    dest_path = dest_dir / f"{title}.md"

    existing_editable = None
    dest_exists = dest_path.exists()
    if dest_exists:
        existing_editable = extract_editable(dest_path.read_text(encoding="utf-8"))

    editable_body = existing_editable or (
        "_Hand-add wikilinks, corrections, and follow-ups here. "
        "This region is preserved across re-runs._"
    )
    captured_at = dt.datetime.now().replace(microsecond=0).isoformat()

    resource_text = render_resource_note(
        capture_id, today, tags, source_session, topic, title,
        body_redacted, editable_body,
    )
    raw_text = render_raw_record(
        capture_id, today, captured_at, tags, source_session, topic, title,
        exchange_redacted,
    )

    raw_exists = raw_path.exists()

    if args.dry_run:
        print(f"=== dry-run: distill topic={topic} title={title} ===")
        print(f"capture_id: {capture_id}")
        print(f"raw record: {raw_path}  (exists={raw_exists}, append-only)")
        print(f"resource note: {dest_path}  (exists={dest_exists})")
        print(f"tags: {tags}")
        print(f"entities resolved: {resolved}")
        print(f"entities UNRESOLVED (flag, do not invent): {unresolved}")
        print(f"redactions — body: {body_red}, exchange: {ex_red}")
        print("--- resource note preview (first 30 lines) ---")
        for line in resource_text.splitlines()[:30]:
            print(line)
        return 0

    # Execute
    # 1. Raw record is append-only: write only if absent.
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if not raw_exists:
        raw_path.write_text(raw_text, encoding="utf-8")
        raw_action = "created"
    else:
        raw_action = "untouched (append-only)"

    # 2. Resource note: create, or update only with --force (preserving editable region).
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest_exists:
        dest_path.write_text(resource_text, encoding="utf-8")
        dest_action = "created"
    elif args.force:
        dest_path.write_text(resource_text, encoding="utf-8")
        dest_action = "regenerated (editable region preserved)"
    else:
        dest_action = "exists (use --force to regenerate; not overwritten)"

    when = captured_at
    update_state(capture_id, topic, title, when)
    duration = time.time() - start
    write_last_run({
        "timestamp": when,
        "capture_id": capture_id,
        "topic": topic,
        "title": title,
        "raw_record": f"{raw_path} [{raw_action}]",
        "resource_note": f"{dest_path} [{dest_action}]",
        "tags": tags,
        "entities_resolved": resolved,
        "entities_unresolved": unresolved,
        "redactions_body": body_red,
        "redactions_exchange": ex_red,
        "duration_seconds": f"{duration:.2f}",
    })

    print(f"raw_record: {raw_path} [{raw_action}]")
    print(f"resource_note: {dest_path} [{dest_action}]")
    print(f"capture_id={capture_id} resolved={len(resolved)} unresolved={len(unresolved)} "
          f"redactions_body={body_red} redactions_exchange={ex_red} duration={duration:.2f}s")
    if unresolved:
        print(f"UNRESOLVED entities (flagged, not invented): {unresolved}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
