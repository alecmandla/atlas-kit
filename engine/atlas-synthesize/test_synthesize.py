#!/usr/bin/env python3
"""Tests for atlas-synthesize's thread vocabulary: stdlib-only, no framework
(matches atlas-emerge/test_emerge.py). Builds a throwaway vault and points the
config layer at it through ATLAS_CONFIG before importing synthesize. Fixtures
use the fictional world from docs/SCRUB-RULES.md."""

import json
import os
import sys
import tempfile
from pathlib import Path

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def raw(vault: Path, rel: str, date: str, title: str, body: str) -> None:
    write(vault / "raw" / rel, f"---\ndate: {date}\n---\n\n# {title}\n\n{body}\n")


tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)
vault = root / "Vault"
vault.mkdir()
cfg_path = root / "atlas.config.json"
cfg_path.write_text(json.dumps({"vault_root": str(vault)}), encoding="utf-8")
os.environ["ATLAS_CONFIG"] = str(cfg_path)

import synthesize  # noqa: E402  (after ATLAS_CONFIG is set)

vocab_path = root / "thread-vocab.yml"
write(vocab_path, """# comment line
ledgerline-integration:
  include: ["Ledgerline sync", "booking-export", "GL"]   # "GL" is under 4 chars
  exclude: ["rate parity"]
guest-portal-latency:
  include: []
""")
synthesize.THREAD_VOCAB_PATH = vocab_path  # keep any real vocab out of the test

# --- parsing ---
vocab = synthesize.load_thread_vocab()
check("vocab: both slugs parsed", set(vocab) == {"ledgerline-integration", "guest-portal-latency"})
check("vocab: include list and trailing comment",
      vocab["ledgerline-integration"]["include"] == ["Ledgerline sync", "booking-export", "GL"])
check("vocab: exclude list", vocab["ledgerline-integration"]["exclude"] == ["rate parity"])
check("vocab: empty list", vocab["guest-portal-latency"] == {"include": [], "exclude": []})
check("vocab: missing file is empty", synthesize.load_thread_vocab(root / "absent.yml") == {})

for bad, why in [("ledgerline-integration\n", "slug without colon"),
                 ("  include: [\"x\"]\n", "entry before any slug"),
                 ("a:\n  keywords: [\"x\"]\n", "unknown key"),
                 ("a:\n  include: x\n", "not an inline list")]:
    p = root / "bad.yml"
    write(p, bad)
    try:
        synthesize.load_thread_vocab(p)
        check(f"vocab: {why} raises", False)
    except ValueError:
        check(f"vocab: {why} raises", True)

# --- matching ---
kw = synthesize.thread_keywords(vault, "ledgerline-integration")
check("keywords: include terms added", "Ledgerline sync" in kw and "booking-export" in kw)
pats = synthesize.keyword_patterns("ledgerline-integration", kw)
check("keywords: terms under 4 characters dropped",
      not any(p.pattern == r"\bgl\b" for p in pats))
check("excludes: whole-word, case-insensitive",
      synthesize.matches("Rate Parity check", synthesize.thread_excludes("ledgerline-integration")))
check("excludes: none for a thread without any",
      synthesize.thread_excludes("guest-portal-latency") == [])

raw(vault, "slack/client-updates/1.md", "2026-09-10", "#client-updates",
    "Jordan Vale: the Ledgerline sync finished overnight.")
raw(vault, "fireflies/01ABCDEF.md", "2026-09-11", "Pinecrest Lodge weekly sync",
    "Priya Okafor: booking-export fails on rate parity rows after the Ledgerline sync.")
raw(vault, "gmail/inbox/2.md", "2026-09-12", "Unrelated", "Marcus Hale on the guest portal.")

para, found = synthesize.evidence_for(vault, "ledgerline-integration")
paths = {r["path"] for r in found}
check("evidence: vocab term finds a raw item the slug alone would miss",
      "raw/slack/client-updates/1.md" in paths)
check("evidence: exclude term drops a matching item",
      "raw/fireflies/01ABCDEF.md" not in paths)
check("evidence: unrelated item not matched", "raw/gmail/inbox/2.md" not in paths)
check("control: slug alone finds nothing",
      synthesize.gather_raw(vault, synthesize.keyword_patterns("ledgerline-integration", [])) == [])

tmp.cleanup()
print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("all passed")
