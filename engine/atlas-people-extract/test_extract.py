#!/usr/bin/env python3
"""Round-trip tests for extract.py's stdlib update_existing_note.
Run under the dep-free system python3 (no yaml)."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import datetime as dt
import extract as e

failures = []
def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)

def rec(mentions, last_seen="2026-06-18", email="jordan.vale@harborlane.example", company="harborlane", domain="harborlane.example"):
    return {"email": email, "name": "Jordan Vale", "slug": "jordan-vale",
            "domain": domain, "company": company, "mention_count": mentions,
            "last_seen": dt.date.fromisoformat(last_seen) if last_seen else None}

# --- 1. basic update: mention_count + last_seen refreshed, body preserved ---
note = ("---\n"
        "type: person\n"
        "name: Jordan Vale\n"
        "email: jordan.vale@harborlane.example\n"
        "company: harborlane\n"
        "domain: harborlane.example\n"
        "tier: 3\n"
        "mention_count: 1\n"
        "last_seen: 2026-01-01\n"
        "tags: [person]\n"
        "---\n"
        "\n"
        "# Jordan Vale\n"
        "\n## Bio\nHand-written bio paragraph.\n\n## Notes\nmy notes\n")
new, old_t, new_t = e.update_existing_note(note, rec(5, "2026-06-18"))
fm = e.parse_frontmatter(new)
check("mention_count refreshed", fm["mention_count"] == "5")
check("last_seen refreshed", fm["last_seen"] == "2026-06-18")
check("tier recomputed 3->2", fm["tier"] == "2" and new_t == 2)
check("tags preserved inline", "tags: [person]" in new)
check("body bio preserved", "Hand-written bio paragraph." in new)
check("body notes preserved", "my notes" in new)
check("structure intact", new.startswith("---\n") and "\n---\n" in new)

# --- 2. custom field + owner body edit preserved (the SKILL.md AC) ---
note2 = ("---\n"
         "type: person\n"
         "name: Jordan Vale\n"
         "email: jordan.vale@harborlane.example\n"
         "company: harborlane\n"
         "domain: harborlane.example\n"
         "tier: 1\n"            # owner-promoted to T1
         "mention_count: 2\n"
         "last_seen: 2026-01-01\n"
         "tags: [person, vip]\n"
         "linkedin: https://linkedin.com/in/jordanvale\n"   # custom field
         "---\n"
         "\n# Jordan Vale\n\n## Bio\nOwner edited this bio.\n")
new2, old_t2, new_t2 = e.update_existing_note(note2, rec(2, "2026-07-01"))
fm2 = e.parse_frontmatter(new2)
check("custom field preserved", fm2.get("linkedin") == "https://linkedin.com/in/jordanvale")
check("custom tags preserved", "vip" in new2)
check("owner-promoted T1 NOT demoted", fm2["tier"] == "1" and new_t2 == 1)
check("mention_count refreshed (custom-field note)", fm2["mention_count"] == "2")
check("last_seen refreshed (custom-field note)", fm2["last_seen"] == "2026-07-01")
check("owner body edit preserved", "Owner edited this bio." in new2)

# --- 3. idempotency: same rec twice -> byte-identical second pass ---
once, _, _ = e.update_existing_note(note, rec(5, "2026-06-18"))
twice, _, _ = e.update_existing_note(once, rec(5, "2026-06-18"))
check("idempotent (byte-stable on repeat)", once == twice)

# --- 4. dossier inserted on promotion to T1 (>=8 mentions), once ---
new4, old4, newt4 = e.update_existing_note(note, rec(9, "2026-06-18"))
check("promoted to T1", newt4 == 1 and old4 == 3)
check("dossier inserted", e.DOSSIER_START in new4 and e.DOSSIER_END in new4)
# re-run: dossier not duplicated
new4b, _, _ = e.update_existing_note(new4, rec(9, "2026-06-18"))
check("dossier not duplicated on re-run", new4b.count(e.DOSSIER_START) == 1)

# --- 5. fills empty email/company/domain but doesn't clobber existing ---
note5 = ("---\ntype: person\nname: B\nemail: \ncompany: \ndomain: \n"
         "tier: 3\nmention_count: 1\nlast_seen: \ntags: [person]\n---\n\n# B\n")
new5, _, _ = e.update_existing_note(note5, rec(1, "2026-06-18", email="priya@pinecrestlodge.example", company="pinecrestlodge", domain="pinecrestlodge.example"))
fm5 = e.parse_frontmatter(new5)
check("empty email filled", fm5["email"] == "priya@pinecrestlodge.example")
check("empty company filled", fm5["company"] == "pinecrestlodge")
# existing non-empty not clobbered
new5b, _, _ = e.update_existing_note(new5, rec(1, "2026-06-18", email="DIFFERENT@example.com"))
fm5b = e.parse_frontmatter(new5b)
check("existing email NOT clobbered", fm5b["email"] == "priya@pinecrestlodge.example")

print()
print("FAILURES:", failures if failures else "none")
sys.exit(1 if failures else 0)
