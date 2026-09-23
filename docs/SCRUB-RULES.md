# Scrub rules

These rules govern everything under `exemplars/` in this repo and everything the
kickoff skill generates for a recipient. They exist so that the design intent of
the maintainer's private Atlas suite can ship with zero personal data. Apply them
before writing, not after; run the gate (section 7) before every commit.

No banned string appears anywhere in this repository, including this file. The
literal patterns the gate greps for live in a git-ignored local file the
maintainer keeps (`docs/scrub-patterns.local.txt`); this file describes the
banned categories and the gate script that reads that local file.

## 1. Banned strings

Never write any of the following anywhere under `exemplars/` or in generated output:

| Category | Rule |
|---|---|
| Maintainer identity | First name, last name, GitHub handle, any email address, any name-derived slug (`First-Last`). |
| Employer | The employer's name, its email domain, its product names, and its industry vocabulary. The exemplars use a fictional employer in a different industry (section 3). |
| Coworkers, clients, vendors, prospects | Every real person name (first names included), every real client or partner organization, and every email domain belonging to one. |
| Monday.com | Real workspace IDs, board IDs, item IDs, workspace names, board names. |
| Slack | Real channel names, channel IDs, user IDs. |
| GitHub | Real owner handle, real repository names. |
| Meeting titles | Any real meeting title, including the fragments used as routing tokens. |
| Source-system IDs | Real Fireflies transcript IDs, Gmail label IDs, Apple Notes ids, commit hashes. |
| Paths | Anything under `/Users/`. Also any home-relative path to the maintainer's vault, skills checkout, or old planning repo (`~/...`), and `file:///` links. |
| Task and phase IDs | `M4P5T2`-style task IDs, `M6P1`-style phase IDs, "AC3"-style acceptance-criterion references, dry-run report paths keyed by task ID. Inline the constraint in plain words. |
| Run history | Dates on which something was "verified", "observed", or "fixed"; counts from the maintainer's corpus (`647 notes`, `738 files`, `42 meetings`). Keep the lesson, drop the incident. |

Decision IDs (`DEC-NNN`) are allowed only when the decision is reproduced in
`exemplars/decisions/DECISIONS.md`. Every DEC number cited by an exemplar SKILL.md
must resolve there; anything else is inlined in plain words and the number dropped.

## 2. Placeholder vocabulary

Placeholders are double-brace tokens. The kickoff substitutes them from the
recipient's interview answers and `atlas.config.json`.

| Placeholder | Meaning |
|---|---|
| `{{owner_name}}` | The vault owner's display name, e.g. `Jordan Vale`. |
| `{{owner_slug}}` | Derived, never asked: `{{owner_name}}` in kebab-case with its capitalization kept (`Jordan Vale` → `Jordan-Vale`), the rule `atlas-people-extract` uses for person-note filenames. Used for the owner's own person note and wikilink (`[[{{owner_slug}}]]`). |
| `{{owner_email}}` | The owner's primary address, from the optional interview follow-up (2.2a). Used only where a real address must be matched (the meeting-routing owner fallback). Blank in the interview means `{{fill-me}}` at each use. |
| `{{employer}}` | The owner's organization name, from the optional follow-up (2.2b). Blank means `{{fill-me}}` at each use. |
| `{{employer_domain}}` | The organization's email domain, no `@`, from the same follow-up. Blank means `{{fill-me}}` at each use. |
| `{{vault_root}}` | Absolute path of the Obsidian vault. |
| `{{skills_root}}` | The target repo root, taken from the kickoff invocation (`--repo <path>`, else the directory `/atlas-kickoff` runs in); never asked in the interview. Under it, `skills/<name>/SKILL.md` holds each generated skill and `engine/<name>/` holds that skill's scripts together with their state files (`state.json`, `last-run.md`, `logs/`) and routing configs. Exemplars always spell out which half they mean: `{{skills_root}}/engine/<name>/...` or `{{skills_root}}/skills/<name>/SKILL.md`. |
| `{{timezone}}` | IANA timezone, e.g. `America/Chicago`. |
| `{{folders.inbox}}` | Folder name for unrouted captures (default `00 - Inbox`). |
| `{{folders.daily}}` | Daily notes (default `10 - Daily Notes`). |
| `{{folders.projects}}` | PARA projects (default `20 - Projects`). |
| `{{folders.areas}}` | PARA areas (default `30 - Areas`). |
| `{{folders.resources}}` | PARA resources (default `40 - Resources`). |
| `{{folders.archive}}` | PARA archive (default `50 - Archive`). |
| `{{folders.meta}}` | Vault-about-the-vault (default `60 - Meta`). |
| `{{folders.attachments}}` | Binary assets (default `99 - Attachments`). |
| `{{folders.crm}}` | Parent of the People CRM (default `CRM`). Person notes live in the fixed `People/` subfolder beneath it (`{{folders.crm}}/People/<First-Last>.md`), so the value names the parent, never `People` itself. |
| `{{folders.clippings}}` | Web-clipper inbox (default `Clippings`). |
| `{{folders.raw}}` | Append-only source layer (default `raw`). |
| `{{folders.wiki}}` | Materialized wiki layer (default `wiki`). |

Rules of use:

- Every vault path is written relative to `{{vault_root}}` and uses a folder
  placeholder for its first segment: `{{vault_root}}/{{folders.projects}}/Clients/<client>/meetings/`.
- Never write a literal `00 - Inbox`, `20 - Projects`, etc. in an exemplar. The
  defaults belong to the config layer, not the prose.
- Subfolders below the placeholder (`Clients/`, `Dashboards/`, `MOCs/`, `Templates/`,
  `People/`, `needs-decision/`) are part of the design and stay literal.
- Script invocations are written as `python3 {{skills_root}}/engine/<skill-name>/<script>.py`,
  and skill-local files (`state.json`, `last-run.md`, `suppress.txt`, routing configs)
  as `{{skills_root}}/engine/<skill-name>/<file>`, because that is where the scripts
  read and write them. Session-only skills (morning, weekly, health, nightly) write
  their `last-run.md` under `{{skills_root}}/engine/<skill-name>/` too, so every skill's
  state lives in one place. A reference to another skill's instructions is
  `{{skills_root}}/skills/<skill-name>/SKILL.md`.
- A placeholder inside a fenced code block is still a placeholder; the kickoff
  substitutes inside code blocks too.
- The kickoff substitutes **only** the tokens in the table above. Any other
  `{{...}}` token is a per-run template slot that belongs to the skill itself
  (the meeting-note template's `{{date_iso_full}}`, `{{fireflies_id}}`,
  `{{route_tags}}`; the skill-mirror template's `{{file_mtime_iso}}`) and must be
  left byte-for-byte untouched.

## 3. The fictional world

All example data comes from one consistent fictional world so the examples read
as a coherent whole. Reuse it; do not invent additional names.

**Employer:** Harbor Lane Analytics, a small analytics consultancy serving
boutique hotels and inns. Domain `harborlane.example`.

**Clients (two):**

| Client | Folder slug | Domain | MOC |
|---|---|---|---|
| Pinecrest Lodge | `Pinecrest-Lodge` | `pinecrestlodge.example` | `_Pinecrest-Lodge` |
| Saltmarsh Inn | `Saltmarsh-Inn` | `saltmarshinn.example` | `_Saltmarsh-Inn` |

**Partner / vendor (one):** Ledgerline, a property-management and point-of-sale
vendor whose data the employer integrates. Domain `ledgerline.example`. Ledgerline
is both an integration partner (a client-style route) and, through its separate
retail team, an open sales prospect (`Ledgerline-Retail`). The two teams share one
email domain, which is what the routing examples use to teach tier precedence.

**People (three):**

| Person | Slug | Role | Email |
|---|---|---|---|
| Jordan Vale | `Jordan-Vale` | Coworker at Harbor Lane Analytics | `jordan.vale@harborlane.example` |
| Priya Okafor | `Priya-Okafor` | General manager at Pinecrest Lodge | `priya@pinecrestlodge.example` |
| Marcus Hale | `Marcus-Hale` | Account manager on Ledgerline's retail team | `marcus.hale@ledgerline.example` |

**Fictional products** (for entity seeds): `Ledgerline` (POS / property
management), `Ledgerline-Cloud` (its hosted reporting tier), `Northstar-BI`
(a BI platform). Real tools the kit itself depends on (Obsidian, Dataview,
Templater, Fireflies, Monday.com, Slack, Gmail, GitHub, BigQuery) may be named.

**Fictional threads** (for `#thread/<slug>` examples): `portal-rewrite`,
`ledgerline-integration`, `northstar-migration`.

**Fictional projects and areas:** `Portal` (work project), `Client-Success`,
`Marketing`, `HQ` (areas). `HQ` stands in for the employer's internal-meetings area.

**Fictional Slack channels:** `#ops-feed`, `#eng-incidents`, `#eng-deploys`,
`#mktg-automations`, `#client-updates`.

**Fictional GitHub:** owner `harborlane`, repos `guest-portal`, `booking-etl`,
`reporting-dashboards`.

**Fictional Monday.com:** workspace `1000001` "Client Onboarding", workspace
`1000002` "Company OKRs"; board `2000001` "Dashboard Onboarding", board `2000002`
"Request Triage"; item `3000001` "Pinecrest Lodge" on the onboarding board.

**Fictional Slack channel id:** `C0EXAMPLE001`. **Fictional Gmail label id:**
`Label_1234567890123456789`. **Fictional source ids:** `01ABCDEF…` (Fireflies),
`p2417` (Apple Notes).

**Fictional meeting titles:** "Pinecrest Lodge weekly sync", "Ledgerline retail
kickoff", "Portal roadmap review", "Jordan and {{owner_name}} 1:1".

The owner is never given a fictional name. The owner is `{{owner_name}}` (or
"the owner" in prose), and their email is `{{owner_email}}`.

## 4. Voice

- SKILL.md prose is written in the second person. "You" is whoever is executing
  the skill: the agent session, or the person reading along. Keep the source's
  convention of opening with "You are the ... agent" or "You mirror ...".
- The vault owner is "the owner" or `{{owner_name}}`. Never a real first name.
  Possessives become "the owner's vault", "the owner's notes".
- "The maintainer" (of the private suite) becomes "the owner" everywhere, because
  in the recipient's copy the owner is the maintainer.
- Never use the owner's real name, or "he", "him", "his", about the owner. Use
  "the owner" and "their" where a pronoun is unavoidable.
- American English spelling throughout.

## 5. What to keep

The value of an exemplar is its design, so keep all of it:

- Frontmatter `name` and `description` (scrubbed), plus the added fields
  `exemplar-of`, `status`, and `requires`.
- Section structure and headings.
- The mental model, guardrails, invariants, idempotency contracts, output
  formats, frontmatter schemas, citation rules, edge cases, anti-goals, and
  relationships to other skills.
- Hard-won operational lessons, rewritten as timeless rules ("a fallback rule
  keyed on the owner's own address matches every meeting and starves every
  lower tier") rather than as incidents ("on 2026-07-31 all 42 meetings...").
- MCP tool names and CLI commands.

## 6. Frontmatter contract for exemplar SKILL.md files

```yaml
---
name: atlas-<name>                 # unchanged from the source
description: ...                   # scrubbed
exemplar-of: atlas-<name>          # the private skill this was scrubbed from
status: active | retired | experimental
requires: [mcp/<server>, cli/<tool>]   # prerequisites the kickoff gates on
---
```

`requires` values use `mcp/<server>` for MCP servers (`mcp/fireflies`,
`mcp/gmail`, `mcp/slack`, `mcp/monday`, `mcp/apple-notes`, `mcp/scheduled-tasks`)
and `cli/<tool>` for command-line tools (`cli/python3`, `cli/gh`, `cli/ripgrep`).
List only what the skill itself calls. Skills that are pure Python over the vault
list `cli/python3`.

`status` is `active` unless the source is retired (`atlas-transcript-extract`) or
depends on a platform capability that is broken upstream (`atlas-voice-memos-ingest`
is `experimental`).

## 7. Verification gate

The gate has two parts: a banned-pattern check that reads its patterns from a
git-ignored local file, and a tracked check that needs no real names.

### The local pattern file

`docs/scrub-patterns.local.txt` holds the literal patterns for every banned
string in section 1 (the maintainer's name, employer, clients, coworkers,
products, real identifiers, real paths), one PCRE pattern per line, `#` comments
and blank lines ignored. It is listed in `.gitignore` and **never committed**;
the maintainer keeps it locally and each collaborator who runs the gate keeps
their own copy. On a fresh clone the file is absent and the gate fails closed
with a clear message rather than passing vacuously.

### The gate script

Run from the repository root before every commit. It prints `gate clean` and
exits 0, or prints the offending lines and exits 1.

```bash
#!/bin/sh
# scrub gate: run from the repository root
PAT=docs/scrub-patterns.local.txt
if [ ! -f "$PAT" ]; then
  echo "gate: $PAT is missing; it is git-ignored and must be created locally (see SCRUB-RULES.md section 7)" >&2
  exit 1
fi
fail=0
# 1. Banned patterns (real names, employer, clients, products, ids, paths), case-insensitive PCRE.
while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue;; esac
  if git grep --untracked -niP "$p" -- exemplars/; then fail=1; fi
done < "$PAT"
# 2. Email addresses outside the fictional domains.
if git grep --untracked -noiE '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}' -- exemplars/ \
   | grep -viE '@(harborlane|pinecrestlodge|saltmarshinn|ledgerline)\.example|@example\.com'; then fail=1; fi
# 3. Category-neutral path check (no local file needed).
if git grep --untracked -nE '/Users/|~/Obsidian|~/Skills|file:///' -- exemplars/; then fail=1; fi
[ "$fail" -eq 0 ] && echo "gate clean"
exit "$fail"
```

Save it as `scripts/scrub-gate.sh` in your own checkout if you like; the
repository does not ship it as a file so the section above stays the single
authoritative copy.

The kickoff skill runs the same gate over its generated output before handing
it to the recipient, with the recipient's own name, employer, and domain added
to their local pattern file.
