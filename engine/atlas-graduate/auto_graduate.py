#!/usr/bin/env python3
"""atlas-auto-graduate — policy engine for unattended thread graduation (DEC-019).

Downstream of `atlas-emerge`, delegating to `graduate.py`. Runs as step 10 of
`atlas-nightly`. Reads `60 - Meta/Dashboards/Emerging-Patterns.md`, scores each
pattern against the Moderate confidence bar, AUTO-graduates the high-confidence
ones (applying the `#thread/<slug>` tag + creating the page via graduate.py),
and routes everything else to a owner review queue at
`60 - Meta/Dashboards/Thread-Review-Queue.md`.

Policy (Moderate — DEC-019):
  A pattern AUTO-graduates iff it passes ALL guards (person / known-entity /
  noise) AND meets one of:
    - work-source rule : >= 2 distinct WORK sources, AND seen on >= 2 runs
    - durable-self rule: >= 5 items,                  AND seen on >= 2 runs
  Patterns that meet a rule but haven't persisted yet are PENDING (auto next run).
  Everything else above the emerge floor -> REVIEW queue.
  Guard hits -> FILTERED (shown in the queue, never auto-tagged).

  WORK sources = fireflies, wispr-meetings, gemini,      (idea crossing systems)
                 teams, zoom, gong, gmail, slack,
                 monday, github
  SELF sources = claude-history, wispr                    (the owner narrating)

  `wispr` is the owner's dictations; emerge reports Wispr Notetaker records
  (raw/wispr/meetings/) as `wispr-meetings`, a real call and so a work source.

  One call captured by several meeting ingests is one work source, not several:
  emerge groups the cross-linked records (their frontmatter ids) into one item and
  writes a single source name for the group into Emerging-Patterns.md, which is
  all this script reads. A Zoom + Gong + Fireflies copy of one call therefore
  arrives as `fireflies` alone and cannot meet the work-source rule by itself.
  A call the Wispr Notetaker also captured arrives as `wispr-meetings`.

Modes:
  (default)   plan  : classify, print, (re)write the review-queue dashboard
                      projection. No graduations. Ledger NOT persisted.
  --execute         : classify, write dashboard, PERSIST ledger, and run
                      graduate.py --auto for each AUTO pattern.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_config  # noqa: E402
import graduate  # noqa: E402  (same-dir sibling: parse_emerge_report, infer_target, ...)

CFG = atlas_config.load()
VAULT_DEFAULT = CFG.vault_root
SKILL_DIR = Path(__file__).resolve().parent
LEDGER_PATH = SKILL_DIR / "seen-ledger.json"
QUEUE_REL = CFG.rel("meta", "Dashboards", "Thread-Review-Queue.md")

# The meeting folders here must match emerge's MEETING_ID_KEYS: emerge collapses
# cross-linked records of one call into one of them before this set is consulted.
WORK_SOURCES = {"fireflies", "wispr-meetings", "gemini", "teams", "zoom", "gong",
                "gmail", "slack", "monday", "github"}
SELF_SOURCES = {"claude-history", "wispr"}

# Cap on auto-graduations per run (DEC-019 addendum). A data backfill
# or burst can push many patterns over the bar at once; rate-limit so a big batch
# rolls out over several nights and the owner can sanity-check each wave.
# Excess auto-eligible patterns are deferred to the next run (they stay eligible).
MAX_AUTO_PER_RUN = 8

# Exact generic slugs that are never threads (procedural noise).
NOISE_SLUGS = {
    "wrap-up", "wrapup", "follow-up", "followup", "next-steps", "action-items",
    "catch-up", "check-in", "checkin", "touch-base", "kick-off", "stand-up",
    "standup", "one-on-one", "sync-up", "recap", "to-do", "todo", "misc",
    "general", "update", "updates", "meeting", "notes", "agenda", "debrief",
}

# Compact common-first-name set for the person guard. A slug like `marcus-hale`
# (a person who isn't even in the CRM) gets routed away from auto-tagging.
# This is a starter list: extend it with first names common in your own
# network (coworkers and clients first) so their slugs never auto-graduate.
COMMON_FIRST_NAMES = {
    "aaron", "adam", "alan", "alex", "alice", "amanda", "amy", "andrew",
    "angela", "anna", "anthony", "benjamin", "bill", "bob",
    "brandon", "brian", "carl", "carlos", "carol", "charles", "chris",
    "christopher", "daniel", "dave", "david", "dennis", "diana", "don",
    "donald", "ed", "edward", "eric", "frank", "gary", "george", "greg",
    "harry", "heather", "henry", "ian", "jack", "jacob", "james", "jamie",
    "jane", "jason", "jeff", "jeffrey", "jennifer", "jeremy", "jerry", "jessica",
    "jim", "joe", "john", "jon", "jonathan", "jordan", "joseph", "josh",
    "joshua", "julie", "justin", "karen", "keith", "kelly", "ken",
    "kim", "kyle", "larry", "laura", "linda", "lisa", "luke", "marie",
    "mark", "martin", "mary", "matt", "matthew", "michael", "nancy",
    "nathan", "nick", "nicholas", "pam", "pat", "patrick", "paul",
    "phil", "rachel", "ralph", "randy", "ray", "rich", "richard", "rob",
    "robert", "roger", "ron", "sam", "samuel", "sandra", "sara", "sarah",
    "scott", "sean", "seth", "shawn", "stephen", "steve", "steven", "susan",
    "ted", "thomas", "tim", "timothy", "todd", "tom", "tony", "travis", "victor",
    "vincent", "walter", "wayne", "will", "william", "zach",
}

# Widened (DEC-019 addendum) after two-token person slugs with modern first
# names slipped the guard. Modern + missed names.
COMMON_FIRST_NAMES |= {
    "emma", "olivia", "sophia", "isabella", "mia", "charlotte",
    "amelia", "harper", "evelyn", "abigail", "emily", "ella", "elizabeth", "sofia",
    "avery", "scarlett", "grace", "chloe", "victoria", "riley", "aria", "lily",
    "hannah", "layla", "zoe", "nora", "leah", "audrey", "savannah", "claire",
    "skylar", "lucy", "anna", "caroline", "nova", "emilia", "kennedy", "maya",
    "willow", "naomi", "elena", "ariana", "allison", "gabriella", "madelyn",
    "cora", "ruby", "eva", "autumn", "adeline", "hailey", "gianna", "valentina",
    "isla", "eliana", "ivy", "sadie", "piper", "lydia", "alexa",
    "josephine", "julia", "vivian", "sophie", "madeline", "liam", "noah", "oliver",
    "elijah", "lucas", "mason", "logan", "ethan", "levi", "sebastian",
    "mateo", "owen", "theodore", "aiden", "wyatt", "asher", "carter", "julian",
    "grayson", "leo", "jayden", "gabriel", "isaac", "lincoln", "hudson", "dylan",
    "ezra", "jaxon", "maverick", "elias", "caleb", "adrian", "miles", "eli",
    "nolan", "christian", "cameron", "colton", "luca", "landon", "hunter",
    "santiago", "easton", "cooper", "roman", "connor", "leonardo",
    "dominic", "everett", "brooks", "xavier", "kai", "parker", "wesley", "silas",
    "bennett", "declan", "weston", "evan", "emmett", "micah", "beau", "damian",
    "rowan", "harrison", "bryson", "sawyer", "blake", "cole", "drew", "grant",
    "reid", "graham", "spencer", "trevor", "wade", "colin", "felix",
}


def norm(s: str) -> str:
    return s.lower().replace("-", "").replace("_", "").replace(" ", "")


def load_ledger() -> dict[str, list[str]]:
    if LEDGER_PATH.exists():
        try:
            data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: list(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_ledger(ledger: dict[str, list[str]]) -> None:
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, LEDGER_PATH)


def report_observation_date(report_path: Path) -> str:
    """The date this emerge report was generated (the observation timestamp)."""
    text = report_path.read_text(encoding="utf-8")
    fm, _, _ = graduate.parse_frontmatter(text)
    if fm:
        d = fm.get("generated_at") or fm.get("window_end")
        if isinstance(d, (dt.date, dt.datetime)):
            return d.isoformat()[:10]
        if isinstance(d, str) and d.strip():
            return d.strip()[:10]
    return dt.date.today().isoformat()


def load_suppress() -> set[str]:
    sup = SKILL_DIR.parent / "atlas-emerge" / "suppress.txt"
    out: set[str] = set()
    if sup.exists():
        for line in sup.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(norm(line))
    return out


def known_entity_set(vault: Path) -> set[str]:
    """Normalized ids of things that already exist as entities/clients/areas —
    a slug matching one of these is an existing entity to LINK, not a new thread."""
    out: set[str] = set()
    ent = vault / CFG.folder_name("wiki") / "entities"
    if ent.exists():
        for p in ent.glob("*.md"):
            out.add(norm(p.stem))
            try:
                fm, _, _ = graduate.parse_frontmatter(p.read_text(encoding="utf-8"))
                if fm and fm.get("canonical_id"):
                    out.add(norm(str(fm["canonical_id"])))
            except OSError:
                pass
    for sub in (CFG.rel("projects", "Clients"), CFG.folder_name("areas")):
        d = vault / sub
        if d.exists():
            for c in d.iterdir():
                if c.is_dir():
                    out.add(norm(c.name))
    return out


def crm_person_set(vault: Path) -> set[str]:
    out: set[str] = set()
    people = vault / CFG.folder_name("crm") / "People"
    if people.exists():
        for p in people.glob("*.md"):
            out.add(norm(p.stem))
    return out


def crm_name_parts(vault: Path) -> set[str]:
    """Individual first/last name tokens across CRM person filenames — lets the
    guard catch a `First-Last` slug that matches the people graph even when the
    first token isn't in COMMON_FIRST_NAMES (e.g. a surname-led or uncommon name)."""
    parts: set[str] = set()
    people = vault / CFG.folder_name("crm") / "People"
    if people.exists():
        for p in people.glob("*.md"):
            for tok in p.stem.split("-"):
                if tok.isalpha() and len(tok) >= 2:
                    parts.add(tok.lower())
    return parts


def guard_reason(slug: str, suppress: set[str], entities: set[str],
                 people: set[str], name_parts: set[str]) -> str | None:
    """Return a guard label if the slug should NOT auto-graduate, else None."""
    n = norm(slug)
    if n in suppress or slug in NOISE_SLUGS or n in {norm(x) for x in NOISE_SLUGS}:
        return "noise"
    if "-" not in slug and len(slug) < 6:
        return "noise"
    if n in entities:
        return "entity"
    if n in people:
        return "person"
    tokens = slug.split("-")
    if len(tokens) == 2 and all(t.isalpha() and len(t) >= 2 for t in tokens):
        # (a) a known first name in either position, or
        # (b) both tokens appear as name components in the CRM people graph.
        if (tokens[0] in COMMON_FIRST_NAMES or tokens[1] in COMMON_FIRST_NAMES
                or (tokens[0] in name_parts and tokens[1] in name_parts)):
            return "person"
    return None


def classify(pattern: dict, slug: str, runs: int) -> tuple[str, str]:
    """Return (bucket, rationale). bucket in {auto, pending, review}."""
    sources = set(pattern["sources"])
    work = sources & WORK_SOURCES
    items = pattern["items"]
    meets_work = len(work) >= 2
    meets_self = items >= 5
    persisted = runs >= 2

    if (meets_work or meets_self) and persisted:
        why = (f">=2 work sources ({', '.join(sorted(work))})" if meets_work
               else f">=5 items ({items})")
        return "auto", f"{why}, seen {runs} runs"
    if meets_work or meets_self:
        why = (f">=2 work sources ({', '.join(sorted(work))})" if meets_work
               else f">=5 items ({items})")
        return "pending", f"{why}, but seen only {runs}/2 runs"
    return "review", (f"{len(work)} work source(s), {items} items — "
                      f"needs your call")


def graduate_one(vault: Path, report_path: Path, slug: str, today: str,
                 execute: bool) -> dict:
    target = graduate.infer_target(vault, slug)
    cmd = [
        sys.executable, str(SKILL_DIR / "graduate.py"),
        "--slug", slug, "--target", target, "--auto",
        "--vault", str(vault), "--report-path", str(report_path),
        "--today", today,
    ]
    if not execute:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "slug": slug, "target": target, "rc": proc.returncode,
        "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(),
    }


def render_queue(vault: Path, today: str, observation_date: str, mode: str,
                 auto_results: list[dict], pending: list[tuple], review: list[tuple],
                 filtered: list[tuple], deferred_auto: list[tuple]) -> str:
    def src_str(p):
        return ", ".join(p["sources"])

    auto_lines = "\n".join(
        f"- `#thread/{r['slug']}` → `{r['target']}` "
        f"({'created' if r['rc'] == 0 else 'FAILED rc=' + str(r['rc'])})"
        for r in auto_results
    ) or "- _(none this run)_"

    deferred_lines = "\n".join(
        f"- `{slug}` — {why}" for slug, _p, why in deferred_auto
    ) or "- _(none)_"

    pending_lines = "\n".join(
        f"- `{slug}` — {why}  ·  sources: {src_str(p)}"
        for slug, p, why in pending
    ) or "- _(none)_"

    if review:
        review_rows = "\n".join(
            f"| {i} | {'; '.join(p['keywords'])} | {src_str(p)} | {p['items']} | "
            f"{p['first']} → {p['latest']} | `{graduate.infer_target(vault, slug)}` | "
            f"`/atlas-graduate {slug}` |"
            for i, (slug, p, _why) in enumerate(review, 1)
        )
        review_block = (
            "| # | Keywords | Sources | Items | Span | Suggested | Graduate |\n"
            "|---|---|---|---|---|---|---|\n" + review_rows
        )
    else:
        review_block = "_Queue empty — nothing awaiting your call._"

    filtered_lines = "\n".join(
        f"- `{slug}` — **{reason}** "
        f"({'add to CRM instead' if reason == 'person' else 'link to existing entity' if reason == 'entity' else 'procedural noise'})"
        for slug, _p, reason in filtered
    ) or "- _(none)_"

    mode_note = ("**Plan mode** — nothing was graduated and the persistence ledger "
                 "was not advanced. Run with `--execute` (or let tonight's "
                 "`atlas-nightly` do it) to apply."
                 if mode == "plan" else
                 "**Execute mode** — auto-graduations below were applied to the vault.")

    return (
        "---\n"
        "type: dashboard-thread-review-queue\n"
        f"generated_by: atlas-auto-graduate\n"
        f"generated_at: {today}\n"
        f"observation_date: {observation_date}\n"
        f"mode: {mode}\n"
        f"auto_graduated: {len(auto_results)}\n"
        f"deferred_by_cap: {len(deferred_auto)}\n"
        f"pending: {len(pending)}\n"
        f"review: {len(review)}\n"
        f"filtered: {len(filtered)}\n"
        "tags: [dashboard, thread-review-queue]\n"
        "---\n\n"
        "# Thread Review Queue\n\n"
        f"Generated {today} by `atlas-auto-graduate` from "
        "`[[Emerging-Patterns]]`. {note}\n\n".replace("{note}", mode_note)
        + "Auto-graduation policy: **Moderate** (DEC-019) — a pattern auto-tags "
        "when it spans **≥2 of your work systems** (meeting / email / Slack / "
        "Monday / GitHub; one call recorded by several meeting tools counts once) "
        "*or* has **≥5 items**, and has **persisted ≥2 nightly "
        "runs**. Person / known-entity / noise patterns are filtered out.\n\n"
        f"Per-run cap: at most **{MAX_AUTO_PER_RUN}** auto-graduations per run — a "
        "backfill/burst rolls out over several nights.\n\n"
        "## 🤖 Auto-graduated this run\n\n"
        f"{auto_lines}\n\n"
        "## 🧢 Deferred by per-run cap (auto-graduate next run)\n\n"
        "_Cleared the bar but exceeded this run's cap — they stay eligible and "
        "graduate on an upcoming run._\n\n"
        f"{deferred_lines}\n\n"
        "## ⏳ Pending — auto-graduates once it persists\n\n"
        "_Meets the confidence bar but hasn't cleared the 2-run safety yet._\n\n"
        f"{pending_lines}\n\n"
        "## 👀 Awaiting your call\n\n"
        "_One glance each: is it a thread (then `concept`/`project`/`area`/"
        "`resource`?), a person, an entity, or noise? `graduate.py` writes the "
        "page + backfills tags once you pick._\n\n"
        f"{review_block}\n\n"
        "## 🚫 Filtered by guards\n\n"
        "_Surfaced by emerge but auto-skipped — not threads. Check for mistakes._\n\n"
        f"{filtered_lines}\n\n"
        "## Methodology\n\n"
        f"- Observation date: {observation_date}. Ledger: "
        f"`{SKILL_DIR / 'seen-ledger.json'}`.\n"
        f"- Work sources: {', '.join(sorted(WORK_SOURCES))}. "
        f"Self sources: {', '.join(sorted(SELF_SOURCES))}. One call captured by several "
        "meeting ingests counts as one work source (emerge groups the cross-linked records).\n"
        "- Regenerated every run — hand-edits are overwritten. Suppress a "
        f"false-positive via `{SKILL_DIR.parent / 'atlas-emerge' / 'suppress.txt'}`.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-graduate high-confidence patterns (DEC-019).")
    ap.add_argument("--vault", type=Path, default=None, help="vault root (default: vault_root from Atlas config)")
    ap.add_argument("--report-path", type=Path, default=None)
    ap.add_argument("--execute", action="store_true",
                    help="Apply graduations + persist ledger. Default is plan mode.")
    ap.add_argument("--today", type=str, default=None)
    args = ap.parse_args()

    start = time.time()
    vault = (args.vault or VAULT_DEFAULT).expanduser().resolve()
    if not vault.exists():
        print(f"vault not found: {vault}", file=sys.stderr)
        return 2
    report_path = args.report_path or (vault / CFG.folder_name("meta") / "Dashboards" / "Emerging-Patterns.md")
    if not report_path.exists():
        print(f"emerge report not found: {report_path}", file=sys.stderr)
        return 2

    today = args.today or dt.date.today().isoformat()
    obs_date = report_observation_date(report_path)
    mode = "execute" if args.execute else "plan"

    patterns = graduate.parse_emerge_report(report_path)
    suppress = load_suppress()
    entities = known_entity_set(vault)
    people = crm_person_set(vault)
    name_parts = crm_name_parts(vault)

    ledger = load_ledger()
    # Advance the ledger for every currently-surfaced slug (in-memory; persisted
    # only on --execute) so persistence reflects this observation.
    for slug in patterns:
        seen = set(ledger.get(slug, []))
        seen.add(obs_date)
        ledger[slug] = sorted(seen)

    auto, pending, review, filtered = [], [], [], []
    for slug, pat in patterns.items():
        reason = guard_reason(slug, suppress, entities, people, name_parts)
        if reason:
            filtered.append((slug, pat, reason))
            continue
        runs = len(ledger.get(slug, []))
        bucket, why = classify(pat, slug, runs)
        if bucket == "auto":
            auto.append((slug, pat, why))
        elif bucket == "pending":
            pending.append((slug, pat, why))
        else:
            review.append((slug, pat, why))

    # Per-run cap (DEC-019 addendum): graduate at most MAX_AUTO_PER_RUN this run;
    # defer the rest. Deferred patterns stay surfaced + eligible, so they auto-
    # graduate on subsequent runs — a burst rolls out over several nights.
    deferred_auto = auto[MAX_AUTO_PER_RUN:]
    auto = auto[:MAX_AUTO_PER_RUN]

    # Persist the ledger BEFORE spawning graduations: a crash mid-graduation must
    # not leave vault edits (tags, pages) with no ledger record of the observation.
    if args.execute:
        save_ledger(ledger)

    # Execute auto-graduations (or dry-run them in plan mode).
    auto_results = []
    for slug, _pat, _why in auto:
        auto_results.append(graduate_one(vault, report_path, slug, today, args.execute))

    queue_text = render_queue(vault, today, obs_date, mode, auto_results,
                              pending, review, filtered, deferred_auto)
    queue_path = vault / QUEUE_REL
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(queue_text, encoding="utf-8")

    duration = time.time() - start
    last_run = (
        "# atlas-auto-graduate — last run\n\n"
        f"- timestamp: {dt.datetime.now().replace(microsecond=0).isoformat()}\n"
        f"- mode: {mode}\n"
        f"- observation_date: {obs_date}\n"
        f"- patterns_scanned: {len(patterns)}\n"
        f"- auto_graduated: {len(auto_results)} "
        f"({sum(1 for r in auto_results if r['rc'] == 0)} ok)\n"
        f"- deferred_by_cap: {len(deferred_auto)} (cap {MAX_AUTO_PER_RUN}/run)\n"
        f"- pending: {len(pending)}\n"
        f"- review: {len(review)}\n"
        f"- filtered: {len(filtered)}\n"
        f"- queue_path: {QUEUE_REL}\n"
        f"- ledger_persisted: {str(args.execute).lower()}\n"
        f"- duration_seconds: {duration:.2f}\n"
    )
    (SKILL_DIR / "auto-graduate-last-run.md").write_text(last_run, encoding="utf-8")

    # Console summary
    print(f"=== atlas-auto-graduate ({mode}) — obs {obs_date} ===")
    print(f"scanned={len(patterns)} auto={len(auto)} deferred={len(deferred_auto)} "
          f"pending={len(pending)} review={len(review)} filtered={len(filtered)}")
    for slug, _p, why in auto:
        print(f"  AUTO     {slug:32} {why}")
    for slug, _p, why in deferred_auto:
        print(f"  deferred {slug:32} {why} (cap {MAX_AUTO_PER_RUN})")
    for slug, _p, why in pending:
        print(f"  pending  {slug:32} {why}")
    for slug, _p, why in review:
        print(f"  review   {slug:32} {why}")
    for slug, _p, reason in filtered:
        print(f"  filtered {slug:32} {reason}")
    if not args.execute:
        print("[plan mode] wrote review-queue dashboard only; no tags applied, "
              "ledger not persisted.")
    print(f"queue: {queue_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
