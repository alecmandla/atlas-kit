# Exemplars

This tree is the design source the `atlas-kickoff` skill reads when it generates
a vault-tailored Atlas suite for a new user. Nothing in it runs. Everything in it
is a scrubbed copy of something that runs in the maintainer's private suite.

## What an exemplar is

An exemplar is one artifact from the private Atlas suite with its personal data
removed and its design intent kept whole:

| Directory | Contents | Count |
|---|---|---|
| `skills/<name>/SKILL.md` | One scrubbed SKILL.md per private skill. Frontmatter carries `exemplar-of`, `status` (`active`, `retired`, `experimental`), and `requires` (the MCP servers and CLI tools the skill needs). Structure, mental model, guardrails, invariants, output formats, and citation rules are preserved; prose is second person; every path is a placeholder. | 25 |
| `vault/*.template` | The vault constitution (`AGENTS.md`), the human guides, the raw-layer README, and the wiki index, with every folder name and identity parameterized. | 6 |
| `configs/*.example.*` | One example per routing or seed config, preserving the exact file format and every comment that explains how routing works, with two or three fictional entries per section. | 6 |
| `decisions/DECISIONS.md` | The design invariants a new vault inherits, with the original `DEC-NNN` numbers so the SKILL.md citations resolve. | 1 |

The scrub rules that produced this tree, and that the kickoff's generated output
must obey too, are in `../docs/SCRUB-RULES.md`.

## How the kickoff uses them

1. The interview establishes the recipient's answers: `owner_name`, `employer`,
   `vault_root`, `skills_root`, `timezone`, the folder names, which sources they
   connect, and which scheduler they use.
2. For each source the recipient connected, the kickoff picks the matching
   `skills/<name>/SKILL.md`, checks its `requires` list against the recipient's
   available MCP servers and CLI tools, substitutes the placeholders from
   `SCRUB-RULES.md` section 2, and writes `skills/<name>/SKILL.md` into the
   recipient's repo with `derived-from: <name>` in the frontmatter. Skills whose
   prerequisites are missing are skipped and listed.
3. `vault/*.template` become the recipient's `AGENTS.md`, `Guide.md`,
   `Getting-Started.md`, `Onboarding-Playbook.md`, `raw/README.md`, and
   `wiki/index.md`.
4. `configs/*.example.*` seed the recipient's real routing files; the fictional
   entries are the pattern to copy, then delete.
5. `decisions/DECISIONS.md` becomes the recipient's `docs/DECISIONS.md`. Any
   decision the recipient rejected during the interview is marked `superseded`,
   never deleted, so citations still resolve.
6. The kickoff runs the verification gate (below) over its output, with the
   recipient's own name, employer, and domain appended to gate 1, before handing
   the result over.

Only the placeholders in `SCRUB-RULES.md` section 2 are substituted. Any other
`{{...}}` token (the meeting-note template's `{{fireflies_id}}`, for example) is
a per-run slot that belongs to the skill and is left untouched.

## The fictional world

Every example in this tree is drawn from one consistent cast so the examples
read as a coherent whole. None of it is real.

- **Employer:** Harbor Lane Analytics (`harborlane.example`), a small analytics
  consultancy serving boutique hotels and inns. The owner works here.
- **Clients:** Pinecrest Lodge (`pinecrestlodge.example`) and Saltmarsh Inn
  (`saltmarshinn.example`).
- **Partner / vendor:** Ledgerline (`ledgerline.example`), a property-management
  and point-of-sale vendor, whose separate retail team is also an open sales
  prospect (`Ledgerline-Retail`). The shared domain is what teaches routing-tier
  precedence.
- **People:** Jordan Vale (coworker, `jordan.vale@harborlane.example`); Priya
  Okafor (general manager at Pinecrest Lodge, `priya@pinecrestlodge.example`);
  Marcus Hale (account manager on Ledgerline's retail team,
  `marcus.hale@ledgerline.example`).
- **Products:** Ledgerline, Ledgerline-Cloud, Northstar-BI.
- **Threads:** `portal-rewrite`, `ledgerline-integration`, `northstar-migration`.
- **Projects and areas:** `Portal`; `HQ`, `Client-Success`, `Marketing`.
- **Slack:** `#ops-feed`, `#eng-incidents`, `#eng-deploys`, `#mktg-automations`,
  `#client-updates`.
- **GitHub:** owner `harborlane`; repos `guest-portal`, `booking-etl`,
  `reporting-dashboards`.
- **Monday.com:** workspaces `1000001` "Client Onboarding" and `1000002`
  "Company OKRs"; boards `2000001` "Dashboard Onboarding" and `2000002`
  "Request Triage"; item `3000001` "Pinecrest Lodge".

The owner is never given a fictional name. The owner is `{{owner_name}}`.

## Verification gate

The gate is a small shell script in `docs/SCRUB-RULES.md` section 7. It greps
`exemplars/` for every banned pattern (real names, employer, clients, products,
identifiers, paths), for email addresses outside the fictional domains, and for
home-directory and machine-local paths. The banned patterns themselves are **not** in this
repository: they live in `docs/scrub-patterns.local.txt`, which is git-ignored
and which the maintainer keeps locally. On a fresh clone the file is absent and
the gate fails closed with a message saying so; create your own copy from the
categories in section 1 before running it.

Run the script from the repository root before every commit that touches this
tree. It must print `gate clean` and exit 0; anything else blocks the commit.
The kickoff runs the same gate over its generated output, with the recipient's
own name, employer, and domain added to the local pattern file.
