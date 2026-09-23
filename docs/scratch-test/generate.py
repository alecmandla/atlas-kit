#!/usr/bin/env python3
"""Track F scratch-test harness: emulate /atlas-kickoff Phases 3 and 4 mechanically.

Stands in for the Claude session that would execute skills/atlas-kickoff/SKILL.md, using
the fictional profile below in place of the AskUserQuestion interview. It writes only under
the target repo and the vault given on the command line.

    python3 generate.py --kit <atlas-kit root> --repo <target repo> --vault <vault> [--as-written]

--as-written follows the skill text literally where it disagrees with the engine
(routing configs under <repo>/configs/, state paths under <repo>/skills/, every
exemplars/vault/*.template dropped at the vault root). Without it, the harness applies the
corrections the Track F fixes put into the kit. Every targeted edit asserts that its anchor
text was found and prints EDIT-MISS otherwise, so exemplar drift becomes a finding.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

TODAY = "2026-09-22"

# ---------------------------------------------------------------- profile (interview answers)
PROFILE = {
    "owner_name": "Sam Rivera",
    "owner_slug": "Sam-Rivera",
    "timezone": "America/Chicago",
    "layout_scheme": "PARA without numbered prefixes",
    "mission": (
        "A place where meeting notes, GitHub activity, and my own dictation accumulate into a "
        "wiki I can ask questions of.\n\n"
        "OPEN: the interview's follow-up (\"name one question you would ask it\") has no answer "
        "in the profile; this is the acceptance test for the pipeline and must be filled before "
        "the first synthesis batch is judged."
    ),
    "folders": {
        "inbox": "Inbox", "daily": "Daily", "projects": "Projects", "areas": "Areas",
        "resources": "Resources", "archive": "Archive", "meta": "Meta",
        "attachments": "Attachments", "crm": "People", "clippings": "Clippings",
        "raw": "raw", "wiki": "wiki",
    },
    "no_nudge": "Areas/Journal",
    "user_rule": "Never rewrite the headings of a daily note.",
}
DEFAULT_FOLDERS = {
    "inbox": "00 - Inbox", "daily": "10 - Daily Notes", "projects": "20 - Projects",
    "areas": "30 - Areas", "resources": "40 - Resources", "archive": "50 - Archive",
    "meta": "60 - Meta", "attachments": "99 - Attachments", "crm": "CRM",
    "clippings": "Clippings", "raw": "raw", "wiki": "wiki",
}
FOLDER_PURPOSE = {
    "inbox": "unrouted captures and things needing a decision",
    "daily": "one note per day; briefings write here",
    "projects": "active work with an end date",
    "areas": "ongoing responsibilities",
    "resources": "reference material, distilled notes, books",
    "archive": "finished projects and retired areas",
    "meta": "templates, dashboards, the vault's own docs",
    "attachments": "binary files",
    "crm": "person notes",
    "clippings": "web clips",
    "raw": "append-only ingested source records",
    "wiki": "regenerated entity, concept, and synthesis pages",
}

# capability -> (group, prerequisites detected, config file or None, session job?)
SELECTED = [
    ("atlas-claude-history-ingest", "ingest", "~/.claude/projects/ non-empty (2 sessions in 1 project)", None, False),
    ("atlas-github-ingest", "ingest", "gh 2.96.0, `gh auth status` exit 0", "github-repos.yaml", False),
    ("atlas-wiki-materialize", "spine", "python3 3.11.7", "entity_seeds.json", False),
    ("atlas-emerge", "spine", "python3 3.11.7", None, False),
    ("atlas-graduate", "spine", "python3 3.11.7", None, False),
    ("atlas-lint", "spine", "python3 3.11.7", None, False),
    ("atlas-health", "spine", "python3 3.11.7 (prose-only skill; no engine script)", None, True),
    ("atlas-synthesize", "spine", "python3 3.11.7", None, True),
    ("atlas-research", "query", "python3 3.11.7; ripgrep present (optional)", None, True),
    ("atlas-distill", "query", "python3 3.11.7", None, True),
    ("atlas-morning", "briefing", "daily-note template from the scaffold", None, True),
    ("atlas-weekly", "briefing", "python3 3.11.7", None, True),
    ("atlas-nightly", "orchestrator", "2 ingests selected; scheduler = manual", None, True),
]
BLOCKED = [
    ("atlas-fireflies-ingest", "Fireflies MCP not connected (declined)"),
    ("atlas-gmail-ingest", "Gmail MCP not connected (declined)"),
    ("atlas-slack-ingest", "Slack MCP not connected (declined)"),
    ("atlas-monday-ingest", "Monday MCP not connected (declined)"),
    ("atlas-apple-notes-ingest", "Apple Notes MCP not connected (declined)"),
    ("atlas-wispr-ingest", "no Wispr Flow store under ~/Library"),
    ("atlas-wispr-meetings-ingest", "no Wispr Flow store under ~/Library"),
    ("atlas-voice-memos-ingest", "experimental; Full Disk Access not confirmed (declined)"),
    ("atlas-book-summary", "declined in the interview"),
    ("atlas-monk", "selected as the reflection practice, but the exemplar depends on five Monk-* templates and /atlas-monk-* commands the kit does not ship; needs a decision"),
    ("atlas-transcript-extract", "status: retired"),
]
SCRIPT_JOBS = {
    # name -> (command from the repo root, writes, cadence)
    "atlas-claude-history-ingest": ("python3 engine/atlas-claude-history-ingest/ingest.py --execute", "raw/claude-history/<project>/<session>.md", "every evening (inside nightly)"),
    "atlas-github-ingest": ("python3 engine/atlas-github-ingest/ingest.py --execute --days 7", "raw/github/<owner>/<repo>/<kind>-<n>.md", "every evening (inside nightly)"),
    "atlas-wiki-materialize": ("python3 engine/atlas-wiki-materialize/materialize.py --execute", "wiki/entities/*.md, wiki/index.md", "every evening (inside nightly)"),
    "atlas-emerge": ("python3 engine/atlas-emerge/emerge.py", "Meta/Dashboards/Emerging-Patterns.md", "every evening, after materialize"),
    "atlas-graduate": ("python3 engine/atlas-graduate/auto_graduate.py --execute", "Meta/Dashboards/Thread-Review-Queue.md; graduated pages", "every evening, after emerge"),
    "atlas-lint": ("python3 engine/atlas-lint/lint.py report", "Meta/Dashboards/Wiki-Lint.md", "Sunday evening"),
}
SESSION_JOBS = {
    "atlas-nightly": ("/atlas-nightly", "raw/ files, wiki pages, dashboards, a report section in today's daily note", "every evening"),
    "atlas-synthesize": ("/atlas-synthesize", "wiki/synthesis/<slug>.md", "every evening, after nightly"),
    "atlas-morning": ("/atlas-morning", "the morning report section in today's daily note", "every morning"),
    "atlas-weekly": ("/atlas-weekly", "Areas/Weekly-Reviews/<ISO-week>.md", "Friday evening"),
    "atlas-health": ("/atlas-health", "Meta/Dashboards/Vault-Health.md", "Sunday evening"),
    "atlas-research": ("/atlas-research <question>", "answer in chat; optionally Meta/Research/<date>-<slug>.md", "on demand"),
    "atlas-distill": ("/atlas-distill", "Resources/<topic>/<title>.md and raw/distill/<capture-id>.md", "on demand"),
}

MISSES: list[str] = []


def edit(text: str, old: str, new: str, label: str, regex: bool = False, count: int = 1) -> str:
    """Targeted replacement that records a miss instead of silently doing nothing."""
    if regex:
        out, n = re.subn(old, new, text, count=count, flags=re.S)
    else:
        n = text.count(old)
        out = text.replace(old, new, count if count else -1)
    if n == 0:
        MISSES.append(label)
        print(f"EDIT-MISS: {label}")
    return out


def fill(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ------------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kit", required=True, type=Path)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--home", required=True, type=Path, help="HOME for this run; the config goes to <home>/.config/atlas/")
    ap.add_argument("--as-written", action="store_true")
    a = ap.parse_args()
    kit, repo, vault, home = a.kit.resolve(), a.repo.resolve(), a.vault.resolve(), a.home.resolve()
    F = PROFILE["folders"]
    skills_root = repo / "skills"
    engine_root = repo / "engine"

    values = {f"folders.{k}": v for k, v in F.items()}
    values.update({
        "owner_name": PROFILE["owner_name"], "owner_slug": PROFILE["owner_slug"],
        "timezone": PROFILE["timezone"], "vault_root": str(vault),
        "skills_root": str(skills_root), "repo_root": str(repo),
    })

    # ---------------------------------------------------------------- Phase 3: kickoff doc
    tpl = (kit / "skills/atlas-kickoff/references/kickoff-template.md").read_text(encoding="utf-8")
    body = tpl.split("# TEMPLATE BEGINS\n", 1)[1].split("\n# TEMPLATE ENDS", 1)[0]
    body = re.sub(r"\n?<!-- AUTHOR:.*?-->\n?", "\n", body, flags=re.S)

    folder_rows = "\n".join(f"| {k} | `{v}` | no | {FOLDER_PURPOSE[k]} |" for k, v in F.items())
    constraints = [
        ("Raw is append-only: corrections are new files with `corrects:` / `corrected_by:` links, never edits.",
         "The wiki can only be rebuilt from raw if raw never changes under it. Enforced by every ingest skill's guardrails and the health check on raw mtimes.", "accepted"),
        ("The wiki is regenerable from raw alone: hand edits live only inside the marked editable regions.",
         "Delete `wiki/`, re-run materialize, graduate, synthesize, and get it back. Enforced by materialize's editable-region markers.", "accepted"),
        ("Every claim on a generated wiki page carries a resolvable wikilink.",
         "A fact with no source is a guess. Enforced by the citation gate in synthesize and research before any page is written.", "accepted"),
        ("Nothing generated ever deletes a note.",
         "Archive is a move the human does. Enforced by the absence of any delete step in every generated skill.", "accepted"),
        ("Meeting transcripts stay summary-only on disk; the full transcript is fetched on demand.",
         "Keeps the vault small and cheap to search. No meeting source is selected in this vault; the rule stands for any added later.", "accepted"),
        (f"Nothing under `{PROFILE['no_nudge']}` appears on any nudge surface.",
         "Nudge surfaces are the morning report, weekly review, dashboards, and any scheduled output. Skills for these practices are on-demand only and never wired to a scheduler.",
         f"accepted (list: `{PROFILE['no_nudge']}`)"),
        (PROFILE["user_rule"],
         "The owner has been burned by a tool that rewrote daily-note headers. Skills that write into a daily note replace only the body of their own `## Atlas ...` section and leave every other heading byte-identical.", "accepted"),
    ]
    constraints_list = "\n".join(
        f"{i}. **{rule}** {why}\n   Status: {status}" for i, (rule, why, status) in enumerate(constraints, 1)
    )
    cap_rows = []
    for name, group, prereq, cfg, _ in SELECTED:
        gen = f"skills/{name}/SKILL.md"
        if cfg:
            gen += f", {'configs/' if a.as_written else 'engine/' + name + '/'}{cfg}"
        cap_rows.append(f"| {name} ({group}) | `{name}` | {prereq} | `{gen}` |")
    blocked_list = "\n".join(f"- `{n}`: {why}. Re-run `/atlas-kickoff --resume` after connecting it." for n, why in BLOCKED)
    cfg_dir = "configs" if a.as_written else "engine/atlas-github-ingest"
    seed_dir = "configs" if a.as_written else "engine/atlas-wiki-materialize"
    routing = (
        f"## GitHub\n\nConfig: `{cfg_dir}/github-repos.yaml`. The interview gave no owner or repo list, so the file carries "
        "`{{fill-me}}` markers (owner and repos) and §9 lists the step.\n\n"
        f"- claude history: no routing; writes `{F['raw']}/claude-history/<project>/`.\n\n"
        f"## Entity seeds\n\nConfig: `{seed_dir}/entity_seeds.json`. No product or technology seeds were given; the file keeps the "
        "example's structure with empty lists and an empty `client_aliases` map."
    )
    job_rows = []
    for name, (cmd, writes, cadence) in {**SESSION_JOBS, **SCRIPT_JOBS}.items():
        session = "yes" if name in SESSION_JOBS else "no"
        job_rows.append(f"| {name} | `{name}` | {cadence} | {session} |")
    gen_tree = "\n".join([
        "atlas.config.json",
        "docs/ATLAS-KICKOFF.md            (this file)",
        "docs/DECISIONS.md",
        "docs/RUNBOOK.md                  (manual scheduler)",
        "README.md",
        "engine/                          (copied from the plugin; stdlib-only)",
        f"{cfg_dir}/github-repos.yaml",
        f"{seed_dir}/entity_seeds.json",
    ] + [f"skills/{n}/SKILL.md" for n, *_ in SELECTED] + [
        f"vault: {', '.join(F.values())}, .obsidian/, {F['meta']}/Templates/*, {F['meta']}/PLUGIN-CHECKLIST.md,",
        f"       {F['meta']}/Dashboards/, {F['raw']}/README.md, {F['wiki']}/index.md, AGENTS.md, Guide.md,",
        "       Getting-Started.md, Onboarding-Playbook.md",
    ])
    manual_steps = "\n".join([
        f"1. Install the community plugins listed in `{F['meta']}/PLUGIN-CHECKLIST.md` (Settings, Community plugins, Browse). This cannot be automated.",
        "2. Scheduler: manual. Nothing to register; `docs/RUNBOOK.md` has one command per job.",
        f"3. Fill every `{{{{fill-me}}}}` in `{cfg_dir}/github-repos.yaml` (owner, repo names).",
        "4. Full Disk Access: not needed (voice memos not selected).",
        f"5. Run the first job by hand (`python3 engine/atlas-claude-history-ingest/ingest.py --execute`) and check `{F['raw']}/claude-history/` and `engine/atlas-claude-history-ingest/last-run.md`, not the timestamp.",
        f"6. The weekly review expects a `Weekly-Review.md` template under `{F['meta']}/Templates/`; the scaffold does not ship one. Create it before the first `/atlas-weekly` run.",
        f"7. Reflection practice: `{PROFILE['no_nudge']}` is excluded from every nudge surface, but no practice skill was generated (see blocked list).",
    ])
    kick = fill(body, {
        **values,
        "date": TODAY, "n_rounds": "3", "mission_paragraph": PROFILE["mission"],
        "vault_state": "existing, empty (0 top-level folders)", "layout_scheme": PROFILE["layout_scheme"],
        "runtime": "Claude Code CLI", "claude_cli_state": "claude 2.1.280 on PATH",
        "folder_table_rows": folder_rows, "constraints_list": constraints_list,
        "capability_rows": "\n".join(cap_rows), "blocked_list": blocked_list,
        "routing_sections": routing, "schedule_mode": "on-demand only", "scheduler_choice": "manual",
        "python3_path": "/opt/anaconda3/bin/python3", "python3_version": "3.11.7",
        "job_rows": "\n".join(job_rows), "generation_tree": gen_tree,
        "folder_keys_list": json.dumps(list(F.keys())),
        "verification_commands": f'python3 engine/atlas-lint/lint.py --vault "{vault}" report',
        "manual_steps": manual_steps, "no_nudge_list": f"`{PROFILE['no_nudge']}`",
    })
    leftover = sorted(set(re.findall(r"\{\{[^}]+\}\}", kick)) - {"{{fill-me}}"})
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs/ATLAS-KICKOFF.md").write_text(kick, encoding="utf-8")
    print("phase3: docs/ATLAS-KICKOFF.md written; unresolved placeholders:", leftover or "none")

    # ---------------------------------------------------------------- Phase 4.1 config
    config = {"vault_root": str(vault), "owner_name": PROFILE["owner_name"], "timezone": PROFILE["timezone"], "folders": F}
    (repo / "atlas.config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    user_cfg = home / ".config/atlas/config.json"
    if user_cfg.exists():
        print("phase4.1: ~/.config/atlas/config.json exists; the skill would show a diff and ask. Not overwriting.")
    else:
        user_cfg.parent.mkdir(parents=True, exist_ok=True)
        user_cfg.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print("phase4.1: config written")

    # ---------------------------------------------------------------- Phase 4.2 engine
    if engine_root.exists():
        print("phase4.2: engine/ exists in the target repo; the skill would ask. Leaving it.")
    else:
        shutil.copytree(kit / "engine", engine_root, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print("phase4.2: engine copied")

    # ---------------------------------------------------------------- Phase 4.3 skills
    dec_base = 35
    guardrails = "\n".join([
        "", "## Guardrails", "",
        "Decided at kickoff (`docs/ATLAS-KICKOFF.md` §3; `docs/DECISIONS.md` DEC-035 to DEC-043). Do not relitigate without a new decision.", "",
    ] + [f"{i}. **{rule}** {why} (DEC-{dec_base + i - 1:03d})" for i, (rule, why, _) in enumerate(constraints, 1)] + [
        f"8. Skip `{PROFILE['no_nudge']}/` when scanning for tasks, tags, threads, or activity; never list it, never wire it to a scheduler. (DEC-042)",
        "",
    ])
    engine_files = r"(state\.json|last-run\.md|auto-graduate-last-run\.md|seen-ledger\.json|suppress\.txt|github-repos\.yaml|entity_seeds\.json|thread-vocab\.yml|logs/|\.run\.lock)"
    for name, *_ in SELECTED:
        src = (kit / "exemplars/skills" / name / "SKILL.md").read_text(encoding="utf-8")
        fm, rest = src.split("\n---\n", 1)
        fm_lines = [l for l in fm.splitlines()[1:]]
        new_fm = ["---"]
        for l in fm_lines:
            if l.startswith("exemplar-of:"):
                new_fm.append("derived-from: " + l.split(":", 1)[1].strip())
            elif l.startswith(("status:", "requires:")):
                continue
            else:
                new_fm.append(l)
        new_fm.append("---")
        text = rest
        # script invocations always point at the copied engine
        text = re.sub(r"\{\{skills_root\}\}/(atlas-[a-z-]+)/([A-Za-z_]+\.py)", rf"{engine_root}/\1/\2", text)
        if not a.as_written:
            # state, routing, and suppression files live where the engine reads and writes them
            text = re.sub(r"\{\{skills_root\}\}/(atlas-[a-z-]+)/" + engine_files, rf"{engine_root}/\1/\2", text)
            text = text.replace("`last-run.md`", f"`{engine_root}/{name}/last-run.md`")
        # scheduler wording: manual runbook, no scheduled-tasks MCP
        text = re.sub(r"\(scheduled via `mcp__scheduled-tasks`\)", "(run by hand from `docs/RUNBOOK.md`; no scheduler is configured)", text)
        text = re.sub(r"scheduled via `mcp__scheduled-tasks`", "run by hand from `docs/RUNBOOK.md`; no scheduler is configured", text)
        text = re.sub(r"Registered via `mcp__scheduled-tasks__create_scheduled_task`[^\n]*", "Not registered with a scheduler (manual runbook); the cron line above is the suggested cadence.", text)
        text = re.sub(r"\(registered via DEC-008 / `mcp__scheduled-tasks__create_scheduled_task`\)", "(manual runbook; DEC-008 records that no scheduler is registered)", text)

        if name == "atlas-nightly":
            text = edit(text, r"\| # \| Sub-skill \| SKILL\.md path \| What it produces \|\n\|---\|---\|---\|---\|\n(?:\|[^\n]*\n)+",
                        "| # | Sub-skill | SKILL.md path | What it produces |\n|---|---|---|---|\n"
                        "| 1 | `atlas-claude-history-ingest` | `{{skills_root}}/atlas-claude-history-ingest/SKILL.md` | `{{folders.raw}}/claude-history/<project>/<session>.md` |\n"
                        "| 2 | `atlas-github-ingest` | `{{skills_root}}/atlas-github-ingest/SKILL.md` | `{{folders.raw}}/github/<owner>/<repo>/<item>.md` |\n"
                        "| 3 | `atlas-wiki-materialize` | `{{skills_root}}/atlas-wiki-materialize/SKILL.md` | Regenerates `{{folders.wiki}}/entities/*.md` from CRM + raw/ |\n"
                        "| 4 | `atlas-emerge` | `{{skills_root}}/atlas-emerge/SKILL.md` | Regenerates `{{folders.meta}}/Dashboards/Emerging-Patterns.md` |\n"
                        f"| 5 | `atlas-auto-graduate` | `{engine_root}/atlas-graduate/auto_graduate.py` | Auto-graduates high-confidence patterns (DEC-019); writes `{{{{folders.meta}}}}/Dashboards/Thread-Review-Queue.md` |\n",
                        "nightly run-order table", regex=True)
            text = edit(text, r"\nIf the owner also runs `atlas-apple-notes-ingest`.*?contract\.\n", "\n", "nightly optional-ingests paragraph", regex=True)
            text = edit(text, "Run the eight ingest skills in deterministic order.", "Run the two selected ingest skills in deterministic order.", "nightly step 1 wording")
            text = edit(text, r"\| Skill \| Nightly invocation \| Notes \|\n\|---\|---\|---\|\n(?:\|[^\n]*\n)+",
                        "| Skill | Nightly invocation | Notes |\n|---|---|---|\n"
                        "| `atlas-claude-history-ingest` | `ingest.py --execute` | also takes `--limit`; **no** `--incremental` |\n"
                        "| `atlas-github-ingest` | `ingest.py --execute --days 7` | also takes `--repo`; **no** `--incremental`. Pin `--days`: an unbounded window can exceed a timeout and leave a stale `.run.lock` |\n"
                        "| `atlas-wiki-materialize` | `materialize.py --execute` | **defaults to dry-run** — without `--execute` it reports `created=0 updated=0` and writes nothing |\n"
                        "| `atlas-emerge` | `emerge.py` | writes by default; has `--dry-run` to suppress |\n",
                        "nightly invocation table", regex=True)
            text = edit(text, r"\| # \| Skill \| Status \| Duration \| Files \| Notes \|\n\|---\|---\|---\|---\|---\|---\|\n(?:\|[^\n]*\n)+",
                        "| # | Skill | Status | Duration | Files | Notes |\n|---|---|---|---|---|---|\n"
                        "| 1 | `atlas-claude-history-ingest` | ok | 5s | 3 new | — |\n"
                        "| 2 | `atlas-github-ingest` | failed | 4s | — | `429 Too Many Requests` (see logs) |\n"
                        "| 3 | `atlas-wiki-materialize` | ok | 18s | 32 regenerated | — |\n"
                        "| 4 | `atlas-emerge` | ok | 7s | 1 report | 12 patterns surfaced |\n"
                        "| 5 | `atlas-auto-graduate` | ok | 3s | 1 queue | 1 auto, 2 pending, 6 review |\n",
                        "nightly example report table", regex=True)
            text = edit(text, "Sub-skills succeeded: <N> / 11", "Sub-skills succeeded: <N> / 5", "nightly succeeded count")
            text = edit(text, "Sub-skills succeeded: 11 / 11", "Sub-skills succeeded: 5 / 5", "nightly idempotent count")
            text = edit(text, "sub_skills_run: 11", "sub_skills_run: 5", "nightly last-run count")
            text = edit(text, "each of the eight ingest skills", "both ingest skills", "nightly upstream wording")
            text = edit(text, "sole nightly trigger for all eight ingests", "sole nightly trigger for both ingests", "nightly DEC-013 wording")
            text = edit(text, "All eight run at 22:00, full stop (DEC-013).", "Both run at 22:00, full stop (DEC-013).", "nightly anti-goal wording")
            new_fm = [l.replace("Runs all eight ingest skills (fireflies, wispr, wispr-meetings, claude-history, github, gmail, slack, monday)", "Runs the two selected ingests (claude-history, github)") for l in new_fm]
        if name == "atlas-morning":
            text = edit(text, r"### 3a — Today's calendar\n.*?(?=### 3b)",
                        "### 3a — Today's calendar\n\nNo calendar source is configured for this vault (no meeting ingest, no dictation ingest, no calendar MCP). Write:\n\n```markdown\n### Today's calendar\n\n*(No calendar source configured.)*\n```\n\nIf a calendar source is connected later, re-run `/atlas-kickoff --resume` so this section is regenerated from the exemplar.\n\n",
                        "morning calendar section", regex=True)
            text = edit(text, r"\*\*Exclude no-nudge practice tasks\.\*\*.*?\n\n",
                        f"**Exclude no-nudge practice tasks.** Skip every file under `{{{{vault_root}}}}/{PROFILE['no_nudge']}/` before matching. The owner's reflection practice is intentionally quiet (DEC-027, DEC-042); its tasks never appear as overdue here.\n\n",
                        "morning no-nudge paragraph", regex=True)
            text = edit(text, r"- `atlas-wispr-ingest` — writes[^\n]*\n", "", "morning wispr relationship", regex=True)
            text = edit(text, r"- `atlas-fireflies-ingest` — writes[^\n]*\n", "", "morning fireflies relationship", regex=True)
            text = edit(text, "- **CalendarEvents table missing**: skip silently; write `*(CalendarEvents not available.)*` in the calendar section.\n", "", "morning calendar edge case 1")
            text = edit(text, r"- \*\*Wispr DB locked\*\*[^\n]*\n", "", "morning calendar edge case 2", regex=True)
        if name == "atlas-research":
            text = edit(text, r"### Step 2a — Transcript escalation.*?(?=## Step 3)", "", "research transcript escalation", regex=True)
            text = edit(text, r" \(The Step 2a Fireflies transcript escalation[^)]*\)", "", "research anti-goal sentence", regex=True)
        if name == "atlas-health":
            text = edit(text, "Recommended action: triage with `/atlas-fireflies-ingest` or move to a PARA folder.", "Recommended action: triage by hand into a PARA folder.", "health inbox action")

        text = fill(text, values)
        new_fm = [fill(l, values) for l in new_fm]
        text = text.replace("{{owner_slug}}", PROFILE["owner_slug"])
        out = "\n".join(new_fm) + "\n" + text.rstrip("\n") + "\n" + guardrails
        d = skills_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(out, encoding="utf-8")
    print(f"phase4.3: {len(SELECTED)} skills written")

    # ---------------------------------------------------------------- Phase 4.4 routing configs
    gh_src = (kit / "exemplars/configs/github-repos.example.yaml").read_text(encoding="utf-8")
    gh_cfg = gh_src.split("owner:", 1)[0] + "owner: {{fill-me}}\nrepos:\n  - name: {{fill-me}}\n    enabled: true\n"
    seeds = json.loads((kit / "exemplars/configs/entity_seeds.example.json").read_text(encoding="utf-8"))
    seed_cfg = {"_comment": seeds["_comment"], "products": [], "technologies": [], "client_aliases": {}}
    if a.as_written:
        (repo / "configs").mkdir(exist_ok=True)
        (repo / "configs/github-repos.yaml").write_text(gh_cfg, encoding="utf-8")
        (repo / "configs/entity_seeds.json").write_text(json.dumps(seed_cfg, indent=2) + "\n", encoding="utf-8")
    else:
        (engine_root / "atlas-github-ingest/github-repos.yaml").write_text(gh_cfg, encoding="utf-8")
        (engine_root / "atlas-wiki-materialize/entity_seeds.json").write_text(json.dumps(seed_cfg, indent=2) + "\n", encoding="utf-8")
    print("phase4.4: routing configs written")

    # ---------------------------------------------------------------- Phase 4.5 vault scaffold
    scaffold = kit / "vault-scaffold"
    rename = {DEFAULT_FOLDERS[k]: F[k] for k in F}
    json_rewrites = {**{DEFAULT_FOLDERS[k]: F[k] for k in F if DEFAULT_FOLDERS[k] != F[k]}, "CRM/People": f"{F['crm']}/People"}
    json_rewrites = dict(sorted(json_rewrites.items(), key=lambda kv: -len(kv[0])))
    created, skipped = [], []
    for p in sorted(scaffold.rglob("*")):
        rel = p.relative_to(scaffold)
        parts = list(rel.parts)
        if parts[0] in rename:
            parts[0] = rename[parts[0]]
        dest = vault.joinpath(*parts)
        if p.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        if p.name == ".gitkeep":
            dest.parent.mkdir(parents=True, exist_ok=True)
            continue
        if p.name == "PLUGIN-CHECKLIST.md":
            continue  # step 6
        if dest.exists():
            skipped.append(str(dest.relative_to(vault)))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rel.parts[0] == ".obsidian":
            body = p.read_text(encoding="utf-8")
            for old, new in json_rewrites.items():
                body = body.replace(old, new)
            dest.write_text(body, encoding="utf-8")
        else:
            shutil.copy2(p, dest)
        created.append(str(dest.relative_to(vault)))
    (vault / F["meta"] / "Dashboards").mkdir(parents=True, exist_ok=True)
    # vault document templates
    for t in sorted((kit / "exemplars/vault").glob("*.template")):
        base = t.name[: -len(".template")]
        if not a.as_written and base in ("raw-README.md", "wiki-index.md"):
            continue  # the scaffold already placed raw/README.md and wiki/index.md
        dest = vault / base
        if dest.exists():
            skipped.append(base)
            continue
        dest.write_text(fill(t.read_text(encoding="utf-8"), values), encoding="utf-8")
        created.append(base)
    print(f"phase4.5: scaffold copied: {len(created)} files created, {len(skipped)} skipped (existing)")

    # ---------------------------------------------------------------- Phase 4.6 plugin checklist
    chk = (scaffold / "PLUGIN-CHECKLIST.md").read_text(encoding="utf-8")
    # Every plugin in the scaffold's list is needed by at least one capability the profile selects
    # or is a listed convenience, so nothing is pruned for this profile.
    (vault / F["meta"] / "PLUGIN-CHECKLIST.md").write_text(chk, encoding="utf-8")
    print("phase4.6: plugin checklist written")

    # ---------------------------------------------------------------- Phase 4.7 decisions
    dec = fill((kit / "exemplars/decisions/DECISIONS.md").read_text(encoding="utf-8"), values)
    new = []
    entries = [(f"DEC-{dec_base + i:03d}", rule, why) for i, (rule, why, _) in enumerate(constraints)]
    entries.insert(5, ("DEC-040", f"Layout scheme: {PROFILE['layout_scheme']}", "Chosen in the interview; folder names are in `atlas.config.json`."))
    entries.insert(6, ("DEC-041", "Scheduler: manual (on-demand only)", "Chosen in the interview. Every job is runnable by hand from `docs/RUNBOOK.md`; nothing is registered."))
    entries[7] = ("DEC-042", entries[7][1], entries[7][2])
    entries[8] = ("DEC-043", entries[8][1], entries[8][2])
    for did, title, why in entries:
        new.append(f"\n## {did} — {title}\n\n- **Date:** {TODAY}\n- **Source:** Kickoff\n- **Decision:** {title}\n- **Rationale:** {why}\n- **Affects:** every generated skill's `## Guardrails`; `docs/ATLAS-KICKOFF.md` §3\n")
    (repo / "docs/DECISIONS.md").write_text(dec.rstrip("\n") + "\n" + "".join(new), encoding="utf-8")
    print("phase4.7: DECISIONS.md written")

    # ---------------------------------------------------------------- Phase 4.8 scheduler (manual runbook)
    rb = [f"# Runbook\n\nRun from `{repo}`. Every job is idempotent: running it twice in a row is safe.\n"
          "Verify by artifacts (the files each job writes), never by the fact that it exited.\n\n"
          "## Suggested cadence\n\n| When | Jobs |\n|---|---|\n"
          "| Every evening | nightly (both ingests, then materialize, emerge, auto-graduate), then synthesize |\n"
          "| Every morning | morning |\n| Friday evening | weekly |\n| Sunday evening | health, lint |\n"
          "| When you have a question | research |\n| When a chat is worth keeping | distill |\n"
          "| When the emerging-patterns dashboard shows something real | graduate <slug> |\n\n## Jobs\n"]
    for name, (cmd, writes, cadence) in SESSION_JOBS.items():
        rb.append(f"\n### {name}\n- **Run:** in a Claude Code session here, `{cmd}`\n- **Writes:** {writes}\n"
                  f"- **Verify:** the artifacts above; `{name}` records its run in `engine/{name}/last-run.md` only if it has a script\n- **Cadence:** {cadence}\n")
    for name, (cmd, writes, cadence) in SCRIPT_JOBS.items():
        rb.append(f"\n### {name}\n- **Run:** `{cmd}`\n- **Writes:** {writes}\n- **Verify:** `cat engine/{name}/last-run.md`\n- **Cadence:** {cadence}\n")
    (repo / "docs/RUNBOOK.md").write_text("".join(rb), encoding="utf-8")
    print("phase4.8: RUNBOOK.md written")

    # ---------------------------------------------------------------- Phase 4.9 index
    rd = ["# sam-atlas\n\nGenerated by `/atlas-kickoff` on " + TODAY + ". Brief: `docs/ATLAS-KICKOFF.md`.\n\n## Skills\n\n| Skill | Derived from |\n|---|---|\n"]
    rd += [f"| `skills/{n}/SKILL.md` | `{n}` |\n" for n, *_ in SELECTED]
    rd.append(f"\n## Configs\n\n- `atlas.config.json` (also at `~/.config/atlas/config.json`)\n- `{cfg_dir}/github-repos.yaml`\n- `{seed_dir}/entity_seeds.json`\n\n"
              "## Scheduler\n\nManual; see `docs/RUNBOOK.md`.\n\n## Verify\n\n```bash\n"
              "ATLAS_CONFIG=./atlas.config.json python3 -c \"import sys; sys.path.insert(0, 'engine/_shared'); import atlas_config as c; cfg = c.load(); print(cfg.vault_root); [print(k, cfg.folder(k)) for k in "
              + json.dumps(list(F.keys())) + "]\"\ngrep -L '^derived-from:' skills/*/SKILL.md   # must print nothing\n```\n")
    (repo / "README.md").write_text("".join(rd), encoding="utf-8")
    print("phase4.9: README.md written")
    print("edit misses:", MISSES or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
