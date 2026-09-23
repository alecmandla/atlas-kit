# The kickoff model

Adapted from a colleague's AI kickoff workflow, trimmed to what a vault kickoff needs. The original
model bootstraps software projects with task graphs and slash-command loops; a vault has
no code to build, so the parts that survive are the ones about deciding once, writing the
decision down, and generating from a single brief.

## The big idea

A system is decomposed **once, up front**, into a document precise enough that a fresh
session can generate every downstream artifact with no ambiguity. For a vault that
document is `docs/ATLAS-KICKOFF.md`. It is written from an interview, reviewed by the
human, then executed. Sessions that come later read the generated skills, not the chat
that produced them.

Three properties make it work:

1. **Context is pre-packaged.** Every generated skill names its config, its engine
   script, and its guardrails. A scheduled session never has to rediscover the vault.
2. **State lives in files, not in chat.** The config at `~/.config/atlas/config.json`,
   the routing files, `DECISIONS.md`, and each skill's `last-run.md` are the source of
   truth. Any session can resume from a cold start.
3. **Decisions are gated, not hoped for.** A constraint the user accepted in the
   interview is written into the kickoff, into `DECISIONS.md`, and into the guardrails
   of every skill it touches. Nothing relitigates it later without a new decision.

## Hierarchy

```
Kickoff  (one document, one interview)
  └── Capability  (one generated skill, derived from one exemplar)
        └── Job  (one scheduled or manual run of that skill)
```

- A capability is named after its exemplar (`atlas-<name>`). The generated skill's
  frontmatter carries `derived-from: <exemplar>`. This is the traceability rule: every
  generated file points at the design it came from.
- A job is a capability plus a cadence. On-demand capabilities have no job. The
  orchestrator (`atlas-nightly`) is a capability whose job runs other capabilities.

## The kickoff prompt structure

The kickoff is one self-contained prompt with fixed sections. The order is the order a
generating session needs the information:

1. **Mission.** One paragraph. What the vault is for, in the user's words. Everything
   below serves it.
2. **Vault layout and config.** The scheme, the folder table, and the literal JSON the
   engine will read. No prose where a table will do.
3. **Non-negotiable constraints.** Numbered, enforceable, with rationale. Each one marked
   accepted or waived. This is the highest-value section; see below.
4. **Selected capabilities.** One row per capability: exemplar, prerequisites detected,
   files to generate. A second list for blocked capabilities with the missing piece.
5. **Sources and routing.** The real values that go into the routing configs.
6. **Scheduler.** The chosen option and the cadence table.
7. **Generation plan.** A literal, repo-relative file list.
8. **Verification.** The commands that prove the output works.
9. **Manual steps.** What the human must do afterward.

Sections are never omitted. A section that does not apply gets its header and one line
saying why it is empty, so a reader knows it was considered rather than forgotten.

## The constraints section discipline

A constraint is a rule a future session could violate by accident. Vague constraints
produce a pipeline that relitigates the same decision every night; that is the failure
mode the kickoff exists to prevent.

Each constraint has four parts:

```
N. **<Rule in one sentence, imperative.>** <Rationale in one or two sentences.>
   Status: accepted | waived (<reason>)
```

Rules for writing them:

- Phrase the rule so it can be checked. "Raw files are never edited after they are
  written" can be checked. "Be careful with raw" cannot.
- Say what enforces it. A constraint with no enforcement point is a wish; name the
  guardrail line, the script flag, or the folder permission that makes it hold.
- Record waivers, never delete them. A waived constraint stays in the list with its
  reason. The next person to read the kickoff learns that the question was asked.
- Each constraint becomes a `DEC-` entry and a line in the `## Guardrails` section of
  every generated skill that could break it.

The vault defaults the interview proposes:

| Constraint | Enforcement point |
|---|---|
| Raw is append-only. Corrections are new files with `corrects:` / `corrected_by:` links. | Ingest skills never open an existing raw file for writing; health checks raw mtimes. |
| Wiki is regenerable from raw alone. | Materialize regenerates outside editable regions; hand edits live only inside `## Related` markers. |
| Every claim on a generated wiki page carries a resolvable wikilink. | Synthesize and research run a citation-resolution gate before writing. |
| Nothing generated deletes a note. | No generated skill has a delete step; archive is a move the human does. |
| Meeting transcripts stay summary-only on disk. | Meeting ingest writes summary records and a link; transcripts are fetched on demand. |
| No-nudge practices never appear on nudge surfaces. | Briefing and dashboard skills carry the exclusion list in their guardrails; practice skills are never scheduled. |

## The DECISIONS log

`docs/DECISIONS.md` in the target repo. Append-only. Seeded at kickoff with one entry per
constraint, one for the layout scheme, one for the scheduler choice, and one for the
no-nudge list. Later sessions append; nobody edits or removes an entry.

```
## DEC-NNN — <one-line decision>
- **Date:** YYYY-MM-DD
- **Source:** Kickoff | <skill name> | human
- **Decision:** <what was decided>
- **Rationale:** <why>
- **Affects:** <skills, configs, vault folders touched>
```

Why a log and not just the kickoff document: the kickoff is a snapshot of the day the
vault was set up. Decisions keep arriving (a new source, a renamed folder, a waived
constraint reinstated). The log is where they go, and a skill that is about to do
something unusual reads the log before doing it. A contradiction between the kickoff and
a later decision is resolved by the later decision, and the log entry says so.

## What was trimmed and why

The source model has task graphs with IDs, milestone wrap-ups, issue and gap passes,
amendments with impact sweeps, and a command set that drives all of it. A vault kickoff
generates its whole output in one session and then runs on a schedule; there is no
multi-week build to steer. What carries over is the shape of the brief, the constraint
discipline, the human review step between writing and executing, and the append-only
decision log. If a vault pipeline ever grows a backlog of planned changes, the source
model's amendment idea (cite a decision, sweep for impact, banner the affected artifact)
is the right next thing to borrow.
