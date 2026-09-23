#!/usr/bin/env python3
"""atlas-book-summary writer.

Deterministic, idempotent renderer for a book note in the owner's Obsidian vault.
The agent does the judgment (research, verification, entity resolution, prose);
this script does the mechanics: slug, frontmatter key-order, dedup, idempotent
write, state ledger. Stdlib-only by DEC-021 (no PyYAML).

Usage:
    python3 book_summary.py --input spec.json --dry-run
    python3 book_summary.py --input spec.json --execute
    python3 book_summary.py --input spec.json --execute --force
    # override the vault root (e.g. when testing against a mount):
    python3 book_summary.py --input spec.json --execute --vault /path/to/vault

Spec JSON shape (keys optional unless noted):
    title (required), author, published, isbn, depth,
    status, rating, tags[], thread_tags[], topic_tags[],
    people[] (CRM First-Last stems), projects[] (note titles),
    key_ideas[], concepts[[concept, description, why]],
    summary_md, quotes[[quote, attribution]], takeaways[],
    personal_connections[], related_topics[[topic, connection]],
    sources[[title, url]], caveats[], verified (YYYY-MM-DD)
"""
import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()

GEN_START = "<!-- atlas-book:generated-start -->"
GEN_END = "<!-- atlas-book:generated-end -->"
NOTES_HEADER = "## Notes"


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    # quote anything that could confuse a YAML parser
    if re.search(r"[:#\[\]{}&*!|>'\"%@`,]", s) or s[0] in "-?" or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_frontmatter(spec, today):
    """Fixed key order per AGENTS.md: date, type, title, author, published,
    status, rating, depth, tags, then alphabetical (isbn, summarized, verified)."""
    tags = ["book"]
    for t in spec.get("topic_tags", []):
        tags.append(t)
    for t in spec.get("thread_tags", []):
        tags.append(t if t.startswith("thread/") else "thread/" + t)
    for t in spec.get("tags", []):
        if t not in tags:
            tags.append(t)
    # de-dup, preserve order
    seen, ordered = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            ordered.append(t)

    lines = ["---"]
    lines.append(f"date: {spec.get('date', today)}")
    lines.append("type: book")
    lines.append(f"title: {_yaml_scalar(spec['title'])}")
    if spec.get("author"):
        lines.append(f"author: {_yaml_scalar(spec['author'])}")
    if spec.get("published"):
        lines.append(f"published: {_yaml_scalar(spec['published'])}")
    lines.append(f"status: {spec.get('status', 'summarized')}")
    if spec.get("rating") not in (None, ""):
        lines.append(f"rating: {_yaml_scalar(spec['rating'])}")
    lines.append(f"depth: {spec.get('depth', 'deep')}")
    lines.append("tags: [" + ", ".join(ordered) + "]")
    if spec.get("isbn"):
        lines.append(f"isbn: {_yaml_scalar(spec['isbn'])}")
    lines.append(f"summarized: {spec.get('summarized', today)}")
    lines.append(f"verified: {spec.get('verified', today)}")
    lines.append("---")
    return "\n".join(lines)


def _section(title, body):
    return f"## {title}\n\n{body}\n" if body else ""


def build_body(spec):
    out = [GEN_START, ""]
    title = spec["title"]
    author = spec.get("author", "")
    head = f"# {title}"
    out.append(head)
    out.append("")
    byline = []
    if author:
        byline.append(f"**{author}**")
    if spec.get("published"):
        byline.append(str(spec["published"]))
    if byline:
        out.append(" · ".join(byline))
        out.append("")

    # 1. Key Ideas
    if spec.get("key_ideas"):
        out.append("## Key Ideas")
        out.append("")
        out += [f"- {x}" for x in spec["key_ideas"]]
        out.append("")

    # 2. Core Concepts table
    if spec.get("concepts"):
        out.append("## Core Concepts")
        out.append("")
        out.append("| Concept | Description | Why It Matters |")
        out.append("|---|---|---|")
        for row in spec["concepts"]:
            c, d, w = (row + ["", "", ""])[:3]
            out.append(f"| {c} | {d} | {w} |")
        out.append("")

    # 3. Summary
    if spec.get("summary_md"):
        out.append("## Summary")
        out.append("")
        out.append(spec["summary_md"].strip())
        out.append("")

    # 4. Notable Quotes
    if spec.get("quotes"):
        out.append("## Notable Quotes")
        out.append("")
        for q in spec["quotes"]:
            quote, attrib = (q + ["", ""])[:2]
            line = f"> {quote}"
            if attrib:
                line += f"\n> — {attrib}"
            out.append(line)
            out.append("")

    # 5. Implementable Takeaways
    if spec.get("takeaways"):
        out.append("## Implementable Takeaways")
        out.append("")
        out += [f"- {x}" for x in spec["takeaways"]]
        out.append("")

    # 6. Personal Connections (inference)
    if spec.get("personal_connections"):
        out.append("## Personal Connections")
        out.append("")
        out.append("*Inference — connections to your work, not claims from the book.*")
        out.append("")
        out += [f"- {x}" for x in spec["personal_connections"]]
        out.append("")

    # 7. Related Topics
    if spec.get("related_topics"):
        out.append("## Related Topics for Further Exploration")
        out.append("")
        for row in spec["related_topics"]:
            topic, conn = (row + ["", ""])[:2]
            out.append(f"- **{topic}** — {conn}" if conn else f"- **{topic}**")
        out.append("")

    # 8. Sources
    if spec.get("sources"):
        out.append("## Sources")
        out.append("")
        for s in spec["sources"]:
            t, u = (s + ["", ""])[:2]
            out.append(f"- [{t}]({u})" if u else f"- {t}")
        out.append("")

    # 9. Caveats
    if spec.get("caveats"):
        out.append("## Caveats")
        out.append("")
        out.append("*Claims that did not clear the verification gate.*")
        out.append("")
        out += [f"- {x}" for x in spec["caveats"]]
        out.append("")

    out.append(GEN_END)
    return "\n".join(out).rstrip() + "\n"


def split_preserved(existing):
    """Return whatever the owner wrote in/after the ## Notes region."""
    if NOTES_HEADER in existing:
        idx = existing.index(NOTES_HEADER)
        return existing[idx:]
    return NOTES_HEADER + "\n\n"


def render_note(spec, today, preserved=None):
    fm = build_frontmatter(spec, today)
    body = build_body(spec)
    notes = preserved if preserved is not None else (NOTES_HEADER + "\n\n")
    return f"{fm}\n\n{body}\n{notes.rstrip()}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="spec JSON path")
    ap.add_argument("--vault", default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--skills-dir",
                    default=str(Path(__file__).resolve().parent))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="rewrite the generated region of an existing note")
    args = ap.parse_args()
    args.vault = str(atlas_config.vault_from_arg(args.vault))

    with open(args.input, encoding="utf-8") as f:
        spec = json.load(f)
    if not spec.get("title"):
        sys.exit("ERROR: spec.title is required")

    today = _dt.date.today().isoformat()
    slug = spec.get("slug") or slugify(spec["title"])
    books_dir = os.path.join(args.vault, CFG.folder_name("resources"), "Books")
    note_path = os.path.join(books_dir, slug + ".md")
    exists = os.path.exists(note_path)

    preserved = None
    if exists:
        with open(note_path, encoding="utf-8") as f:
            preserved = split_preserved(f.read())

    note_text = render_note(spec, today, preserved)

    if args.dry_run or not args.execute:
        status = "UPDATE (force)" if exists and args.force else (
            "SKIP (exists, no --force)" if exists else "CREATE")
        print("=== atlas-book-summary DRY RUN ===")
        print(f"book:    {spec['title']} — {spec.get('author', '?')}")
        print(f"slug:    {slug}")
        print(f"path:    {note_path}")
        print(f"action:  {status}")
        print(f"sources: {len(spec.get('sources', []))}  "
              f"quotes: {len(spec.get('quotes', []))}  "
              f"caveats: {len(spec.get('caveats', []))}")
        print(f"people:  {', '.join(spec.get('people', [])) or '(none)'}")
        print(f"projects:{', '.join(spec.get('projects', [])) or '(none)'}")
        print(f"threads: {', '.join(spec.get('thread_tags', [])) or '(none)'}")
        print(f"bytes:   {len(note_text)}")
        return

    if exists and not args.force:
        print(f"SKIP: {note_path} already exists (use --force to refresh).")
        return

    os.makedirs(books_dir, exist_ok=True)
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(note_text)
    action = "UPDATED" if exists else "CREATED"
    print(f"{action}: {note_path} ({len(note_text)} bytes)")

    # state ledger + last-run
    os.makedirs(args.skills_dir, exist_ok=True)
    ledger_path = os.path.join(args.skills_dir, "state.json")
    ledger = {}
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, encoding="utf-8") as f:
                ledger = json.load(f)
        except (ValueError, OSError):
            ledger = {}
    ledger[slug] = {
        "title": spec["title"],
        "author": spec.get("author", ""),
        "depth": spec.get("depth", "deep"),
        "written_at": today,
        "sources": len(spec.get("sources", [])),
    }
    tmp_ledger = str(ledger_path) + ".tmp"
    with open(tmp_ledger, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    os.replace(tmp_ledger, ledger_path)
    with open(os.path.join(args.skills_dir, "last-run.md"), "w",
              encoding="utf-8") as f:
        f.write(f"timestamp: {_dt.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"book: {spec['title']} — {spec.get('author', '?')}\n")
        f.write(f"slug: {slug}\n")
        f.write(f"depth: {spec.get('depth', 'deep')}\n")
        f.write(f"action: {action}\n")
        f.write(f"sources: {len(spec.get('sources', []))}\n")
        f.write(f"people_linked: {', '.join(spec.get('people', [])) or '(none)'}\n")
        f.write(f"projects_linked: {', '.join(spec.get('projects', [])) or '(none)'}\n")


if __name__ == "__main__":
    main()
