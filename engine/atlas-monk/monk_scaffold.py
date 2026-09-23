#!/usr/bin/env python3
"""atlas-monk scaffolder — the deterministic half of the Monk Manual flows (DEC-027 §1).

Resolves the target path, instantiates the matching `Monk-*` template from
`40 - Resources/Templates/` by evaluating its Templater date tokens, and writes
the file idempotently (never clobbers a filled-in note without --force). The
interactive ranking/judgment stays with the orchestrating Claude session — this
helper only emits a correct, dated skeleton with hand-rolled frontmatter.

Stdlib only (DEC-021, no PyYAML): frontmatter is carried verbatim from the
template with date tokens substituted, never constructed from a dict.

Flows:
  month   Priorities/<label>-Month.md      <- Monk-Monthly.md
  week    Priorities/<YYYY>-W<WW>-Week.md   <- Monk-Weekly.md
  today   Priorities/daily/<date>-monk.md   <- Monk-Prepare.md
  page    pages/<date>-page.md              <- Monk-Page-Capture.md
  reflect Reflections/<YYYY>-W<WW>-Sunday.md <- Monk-Sunday-Reflection.md

Output contract (stdout, machine-parseable):
  STATUS=CREATED|EXISTS
  PATH=<absolute path>
  TEMPLATE=<template filename>
Exit 0 on CREATED or EXISTS; exit 1 on error (missing template, bad args).
"""

import argparse
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
import atlas_config  # noqa: E402

CFG = atlas_config.load()

# Fallback only — the vault root is normally resolved from the invocation cwd so
# the scaffolder writes into whichever vault checkout/worktree it's called from.
DEFAULT_VAULT = CFG.vault_root


def resolve_vault(explicit: str | None = None) -> Path:
    """Resolve the vault root: --vault > MONK_VAULT env > walk up from cwd for a
    .obsidian/ marker > DEFAULT_VAULT. Uses cwd (never __file__) so the target
    follows the worktree the command is invoked from, not where this script lives."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("MONK_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    cur = Path.cwd().resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".obsidian").is_dir():
            return cand
    return DEFAULT_VAULT

# Templater token: <% tp.date.now("FORMAT") %> or <% tp.date.now("FORMAT", offsetDays) %>
_TP_RE = re.compile(r'<%\s*tp\.date\.now\(\s*"([^"]*)"\s*(?:,\s*(-?\d+)\s*)?\)\s*%>')
_LEFTOVER_RE = re.compile(r"<%.*?%>")

# moment.js format tokens, longest-first so e.g. MM never bites into MMMM.
_MOMENT_TOKENS = ["dddd", "ddd", "MMMM", "MMM", "YYYY", "WW", "DD", "MM", "D"]


def render_moment(fmt: str, d: date) -> str:
    """Render a moment.js format string for date `d` (only the tokens we use)."""
    values = {
        "dddd": d.strftime("%A"),
        "ddd": d.strftime("%a"),
        "MMMM": d.strftime("%B"),
        "MMM": d.strftime("%b"),
        "YYYY": f"{d.year:04d}",
        "WW": f"{d.isocalendar()[1]:02d}",
        "DD": f"{d.day:02d}",
        "MM": f"{d.month:02d}",
        "D": str(d.day),
    }
    out = fmt
    pending = []
    for i, tok in enumerate(_MOMENT_TOKENS):
        if tok in out:
            sentinel = f"\x00{i}\x00"
            out = out.replace(tok, sentinel)
            pending.append((sentinel, values[tok]))
    for sentinel, val in pending:
        out = out.replace(sentinel, val)
    return out


def eval_templater(text: str, base: date) -> str:
    """Replace every tp.date.now(...) token, dated relative to `base`."""
    def _sub(m):
        fmt, off = m.group(1), m.group(2)
        d = base + timedelta(days=int(off)) if off else base
        return render_moment(fmt, d)
    return _TP_RE.sub(_sub, text)


def load_template(name: str, template_dir: Path) -> str:
    path = template_dir / name
    if not path.exists():
        sys.exit(f"error: template not found: {path}")
    return path.read_text()


def iso_monday(d: date) -> date:
    return d - timedelta(days=d.isocalendar()[2] - 1)


def first_of_month(label: str) -> date:
    y, m = (int(x) for x in label.split("-"))
    return date(y, m, 1)


def build(flow: str, args, template_dir: Path) -> tuple[str, str, str]:
    """Return (relative_path, template_name, rendered_text)."""
    d = args.date

    if flow == "today":
        rel = f"Priorities/daily/{d.isoformat()}-monk.md"
        tpl = "Monk-Prepare.md"
        text = eval_templater(load_template(tpl, template_dir), d)

    elif flow == "page":
        rel = f"pages/{d.isoformat()}-page.md"
        tpl = "Monk-Page-Capture.md"
        text = eval_templater(load_template(tpl, template_dir), d)

    elif flow == "reflect":
        iso = d.isocalendar()
        rel = f"Reflections/{iso[0]}-W{iso[1]:02d}-Sunday.md"
        tpl = "Monk-Sunday-Reflection.md"
        text = eval_templater(load_template(tpl, template_dir), d)

    elif flow == "week":
        iso = d.isocalendar()
        monday = iso_monday(d)  # base = Monday so period_start=Mon, period_end=+4=Fri
        rel = f"Priorities/{iso[0]}-W{iso[1]:02d}-Week.md"
        tpl = "Monk-Weekly.md"
        text = eval_templater(load_template(tpl, template_dir), monday)

    elif flow == "month":
        label = args.label or f"{d.year:04d}-{d.month:02d}"
        if not re.fullmatch(r"\d{4}-\d{2}", label):
            sys.exit(f"error: --label must be YYYY-MM, got {label!r}")
        start = args.start or first_of_month(label)
        rel = f"Priorities/{label}-Month.md"
        tpl = "Monk-Monthly.md"
        text = eval_templater(load_template(tpl, template_dir), start)  # period_start token -> start
        text = _fill_month_frontmatter(text, end=args.end, theme=args.theme, title=args.title)

    else:
        sys.exit(f"error: unknown flow {flow!r}")

    leftover = _LEFTOVER_RE.search(text)
    if leftover:
        print(f"warning: unresolved template token {leftover.group(0)!r}", file=sys.stderr)
    return rel, tpl, text


def _fill_month_frontmatter(text, end=None, theme=None, title=None) -> str:
    """Fill the month template's judgment-supplied frontmatter/title in place."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if end and line.strip() == "period_end:":
            lines[i] = f"period_end: {end}"
        elif theme and line.strip() == "theme:":
            lines[i] = f"theme: {theme}"
        elif title and line.startswith("# "):
            lines[i] = f"# {title}"
            title = None  # only the H1
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Scaffold a Monk Manual note from its template.")
    p.add_argument("flow", choices=["month", "week", "today", "page", "reflect"])
    p.add_argument("--date", type=date.fromisoformat, default=date.today(),
                   help="anchor date YYYY-MM-DD (default: today)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing note with a fresh skeleton (DESTROYS filled content)")
    p.add_argument("--print", dest="show", action="store_true",
                   help="also print the rendered note to stdout")
    p.add_argument("--vault", help="vault root (default: MONK_VAULT env, else the "
                   ".obsidian/ marker found by walking up from cwd, else vault_root from Atlas config)")
    # month-only judgment inputs
    p.add_argument("--label", help="month flow: book-cycle label YYYY-MM (default: --date's month)")
    p.add_argument("--start", type=date.fromisoformat, help="month flow: period_start (default: 1st of label)")
    p.add_argument("--end", type=date.fromisoformat, help="month flow: period_end")
    p.add_argument("--theme", help="month flow: theme frontmatter value")
    p.add_argument("--title", help="month flow: H1 title (e.g. 'Bridge month (Jun 29 - Aug 5, 2026) - Monthly focus')")
    args = p.parse_args()

    vault = resolve_vault(args.vault)
    template_dir = vault / CFG.folder_name("resources") / "Templates"
    monk_dir = vault / CFG.folder_name("areas") / "Monk-Manual"

    rel, tpl, text = build(args.flow, args, template_dir)
    target = monk_dir / rel

    if target.exists() and not args.force:
        print("STATUS=EXISTS")
        print(f"PATH={target}")
        print(f"TEMPLATE={tpl}")
        if args.show:
            print("---")
            print(target.read_text())
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print("STATUS=CREATED")
    print(f"PATH={target}")
    print(f"TEMPLATE={tpl}")
    if args.show:
        print("---")
        print(text)


if __name__ == "__main__":
    main()
