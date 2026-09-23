#!/bin/sh
# Track F scratch-vault test. Re-runs the non-interactive kickoff against a throwaway HOME.
#
#   sh docs/scratch-test/run.sh <scratch-dir>
#
# Everything is written under <scratch-dir>; the real HOME, ~/.config, and any real vault
# are never read or written. Needs python3 3.10+ and the claude CLI on PATH. Exits 1 on
# the first failing step. See docs/SCRATCH-TEST.md for what each step checks.
set -eu
SCRATCH=$(cd "$(dirname "${1:?usage: run.sh <scratch-dir>}")" 2>/dev/null && pwd)/$(basename "$1") || SCRATCH=$1
KIT=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$SCRATCH"
SCRATCH=$(cd "$SCRATCH" && pwd)
H="$SCRATCH/home"; R="$H/sam-atlas"; V="$H/Vault"; L="$SCRATCH/logs"
rm -rf "$H" "$L"; mkdir -p "$V" "$H/.config" "$H/.claude/projects/-home-sam-work" "$R" "$L"

# Snapshot the real config dir's mtime (stat only) and set the write-scope marker.
stat -f '%m %N' "$HOME/.config" > "$SCRATCH/real-config-mtime-before.txt" 2>/dev/null || true
ls -d "$HOME/.config/atlas" > "$SCRATCH/real-config-atlas-before.txt" 2>&1 || true
touch "$SCRATCH/marker-before"

# Two tiny synthetic Claude Code sessions for the claude-history ingest.
cat > "$H/.claude/projects/-home-sam-work/11111111-aaaa-4bbb-8ccc-000000000001.jsonl" <<'EOF'
{"type":"user","uuid":"u1","timestamp":"2026-09-20T14:02:11.000Z","sessionId":"11111111-aaaa-4bbb-8ccc-000000000001","cwd":"/home/sam/work","message":{"role":"user","content":"Add a retry to the booking ETL loader when the warehouse times out."}}
{"type":"assistant","uuid":"a1","timestamp":"2026-09-20T14:02:40.000Z","sessionId":"11111111-aaaa-4bbb-8ccc-000000000001","cwd":"/home/sam/work","message":{"role":"assistant","content":[{"type":"text","text":"I added exponential backoff with three attempts in loader.py and a test that simulates a timeout."}]}}
{"type":"user","uuid":"u2","timestamp":"2026-09-20T14:05:02.000Z","sessionId":"11111111-aaaa-4bbb-8ccc-000000000001","cwd":"/home/sam/work","message":{"role":"user","content":"Good. Commit it."}}
EOF
cat > "$H/.claude/projects/-home-sam-work/22222222-aaaa-4bbb-8ccc-000000000002.jsonl" <<'EOF'
{"type":"user","uuid":"u1","timestamp":"2026-09-21T09:15:00.000Z","sessionId":"22222222-aaaa-4bbb-8ccc-000000000002","cwd":"/home/sam/work","message":{"role":"user","content":"Why does the guest portal dashboard show yesterday's totals after midnight?"}}
{"type":"assistant","uuid":"a1","timestamp":"2026-09-21T09:16:30.000Z","sessionId":"22222222-aaaa-4bbb-8ccc-000000000002","cwd":"/home/sam/work","message":{"role":"assistant","content":[{"type":"text","text":"The nightly rollup runs in UTC while the dashboard filters by local date. Aligning both to America/Chicago fixes the off-by-one."}]}}
EOF
# Backdate the sessions: the ingest treats a JSONL modified in the last 5 minutes as in progress.
touch -t 202609211000 "$H/.claude/projects/-home-sam-work/"*.jsonl
(cd "$R" && git init -q)

# Phases 3 and 4 (non-interactive stand-in for the kickoff session).
python3 "$KIT/docs/scratch-test/generate.py" --kit "$KIT" --repo "$R" --vault "$V" --home "$H"

# Phase 5 under the scratch HOME.
export HOME="$H" XDG_CONFIG_HOME="$H/.config" ATLAS_CONFIG="$R/atlas.config.json"
cd "$R"
step() { name=$1; shift; printf -- '--- %s\n' "$name"; if "$@" > "$L/$name.log" 2>&1; then tail -n 3 "$L/$name.log"; else rc=$?; cat "$L/$name.log"; echo "FAILED: $name (exit $rc)"; exit 1; fi; }
step config-import python3 -c "import sys; sys.path.insert(0, 'engine/_shared'); import atlas_config as c; cfg = c.load(); print(cfg.vault_root); [print(k, cfg.folder(k)) for k in ['inbox','daily','projects','areas','resources','archive','meta','attachments','crm','clippings','raw','wiki']]"
step lint-report python3 engine/atlas-lint/lint.py --vault "$V" report
step ch-dry python3 engine/atlas-claude-history-ingest/ingest.py --dry-run-report "$L/ch-dry.md"
step ch-exec-1 python3 engine/atlas-claude-history-ingest/ingest.py --execute
step ch-exec-2 python3 engine/atlas-claude-history-ingest/ingest.py --execute
grep -q 'written=0 already=2' "$L/ch-exec-2.log" || { echo "FAILED: claude-history ingest is not idempotent"; exit 1; }
step gh-parse python3 -c "import sys; sys.path.insert(0, 'engine/atlas-github-ingest'); import ingest as g; assert g.REPOS_YAML.exists(), g.REPOS_YAML; print(g.parse_repos_yaml())"
step people-extract-dry python3 engine/atlas-people-extract/extract.py --dry-run-report "$L/people-dry.md"
step materialize-dry python3 engine/atlas-wiki-materialize/materialize.py --dry-run-report "$L/mat-dry.md"
step emerge-dry python3 engine/atlas-emerge/emerge.py --dry-run
step emerge-write python3 engine/atlas-emerge/emerge.py
step auto-graduate-plan python3 engine/atlas-graduate/auto_graduate.py

# Write scope: nothing outside the scratch tree, nothing in the kit, real ~/.config untouched.
printf -- '--- write-scope\n'
strays=$(find "$SCRATCH" -newer "$SCRATCH/marker-before" -type f -not -path "$R/*" -not -path "$V/*" -not -path "$L/*" -not -path "$H/.config/*" -not -path "$H/.claude/*")
[ -z "$strays" ] || { echo "FAILED: unexpected writes: $strays"; exit 1; }
[ -z "$(git -C "$KIT" status --short -- engine exemplars skills vault-scaffold)" ] || { echo "FAILED: the kit tree changed during the run"; exit 1; }
echo "ok"

# Generated skills: valid frontmatter, every derived-from resolves, no leaked placeholders.
printf -- '--- validate generated skills\n'
claude plugin validate "$R/skills" --strict
[ -z "$(grep -L '^derived-from:' skills/*/SKILL.md)" ] || { echo "FAILED: a skill lacks derived-from"; exit 1; }
for f in skills/*/SKILL.md; do d=$(grep -m1 '^derived-from:' "$f" | awk '{print $2}'); [ -f "$KIT/exemplars/skills/$d/SKILL.md" ] || { echo "FAILED: $f derives from unknown exemplar $d"; exit 1; }; done
if grep -rnoE '\{\{[^}]+\}\}' "$R" "$V" --include='*.md' --include='*.json' --include='*.yaml' | grep -v '/engine/' | grep -v '{{fill-me}}'; then echo "FAILED: unresolved placeholder"; exit 1; fi
echo "ok"

# Scrub gate over the generated output, with the scratch path normalized so the gate
# sees the content rather than the throwaway directory name. Implemented in python3 (already
# required) rather than grep: BSD grep has no -P for the PCRE patterns in the local file, and
# GNU and BSD grep disagree on option placement, so a shell loop is not portable.
printf -- '--- scrub gate (generated output)\n'
PAT="$KIT/docs/scrub-patterns.local.txt"
[ -f "$PAT" ] || { echo "gate: $PAT is missing (git-ignored; see SCRUB-RULES.md section 7)"; exit 1; }
SCRATCH="$SCRATCH" REPO="$R" VAULT="$V" PAT="$PAT" python3 - <<'PY' || { echo "FAILED: gate"; exit 1; }
import os, re, sys
from pathlib import Path
scratch, pat = os.environ["SCRATCH"], Path(os.environ["PAT"])
roots = [Path(os.environ["REPO"]), Path(os.environ["VAULT"])]
banned = [re.compile(l.strip(), re.I) for l in pat.read_text(encoding="utf-8").splitlines()
          if l.strip() and not l.startswith("#")]
email = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
fictional = re.compile(r"@(harborlane|pinecrestlodge|saltmarshinn|ledgerline)\.example|@example\.com", re.I)
paths = re.compile(r"/Users/|~/Obsidian|~/Skills|file:///")
fail = False
for root in roots:
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(f)[len(scratch) + 1:]
        for n, line in enumerate(text.replace(scratch, "<scratch>").splitlines(), 1):
            hit = any(b.search(line) for b in banned) or paths.search(line) \
                or any(not fictional.search(m.group(0)) for m in email.finditer(line))
            if hit:
                print(f"{rel}:{n}: {line}")
                fail = True
print("gate: hits above" if fail else "gate clean")
sys.exit(1 if fail else 0)
PY
echo "scratch test passed: $SCRATCH"
