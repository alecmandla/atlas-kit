# Atlas design decisions (exemplar)

These are the design invariants a new Atlas vault inherits. Each entry is a
decision the maintainer's private suite locked and that the exemplar SKILL.md
files cite by number. The numbering is preserved from the source suite so the
citations resolve; gaps are numbers that were either maintainer-specific history
or were never recovered, and nothing here cites them.

When the kickoff generates a recipient's suite, this file is copied into their
`docs/DECISIONS.md` as the starting register. New decisions continue from
DEC-036. A decision that the recipient rejects during the interview is marked
`superseded` here, never deleted, so the SKILL.md citations still resolve.

Format for new entries: `## DEC-NNN — <title>`, then **Decided**, **Status**,
**Decision**, **Why**, **Consequences**.

---

## Index

| DEC | Invariant |
|---|---|
| DEC-001 | Single vault; the PARA folder layout is the folder contract. |
| DEC-002 | Local-first audio: on-device transcription only; no audio or transcript ever leaves the machine. |
| DEC-003 | People attribution is wikilink-encoded; `participants:` holds `[[Person-Slug]]` links. |
| DEC-004 | LLM Wiki spine: `{{folders.raw}}/` is the immutable source layer; `{{folders.wiki}}/` is a rewriteable projection. |
| DEC-007 | Auditors surface, never decide. |
| DEC-008 | Each scheduled agent is registered exactly once through the owner's chosen scheduler. |
| DEC-009 | Ingest writes to `{{folders.raw}}/` first; raw is append-only; consumers read mirrors, never source APIs. |
| DEC-011 | The vault and its generated reports are not git-tracked. |
| DEC-012 | Person-slug collisions disambiguate with a company-shortname suffix. |
| DEC-013 | One nightly orchestrator is the sole trigger for every ingest. |
| DEC-014 | Claude Code session history is fair game for thought-thread mining. |
| DEC-016 | Unroutable meetings go to `{{folders.inbox}}/needs-decision/`, triaged at weekly review. |
| DEC-018 | Authoring skills may write directly into a PARA folder; ingest skills may not. |
| DEC-019 | Auto-graduation policy is Moderate, unattended, with a per-run cap. |
| DEC-020 | Cross-source thread synthesis runs as its own agent after nightly, with token bounding. |
| DEC-021 | Headless scripts are stdlib-only. |
| DEC-022 | Auto-promoted patterns land in a review queue; `MAX_AUTO_PER_RUN` caps rollout. |
| DEC-023 | A compiled on-device transcription helper is the single native exception to stdlib-only. |
| DEC-024 | Full Disk Access is a user-granted prerequisite; blocked runs soft-skip, never fail. |
| DEC-025 | Apple Notes ingest uses the scriptable/MCP interface, not the database. |
| DEC-026 | Book summaries have no raw mirror; the resource note is the durable artifact. |
| DEC-027 | Whatever the owner lists in `no_nudge` is never scheduled and never surfaced as a reminder. |
| DEC-031 | Stdlib connector fetchers are the prerequisite for headless scheduling. |
| DEC-032 | Meeting transcripts: summary on disk, full transcript fetched from the API on demand. |
| DEC-033 | A machine-readable preflight runs before any voice-memo transcription batch. |
| DEC-034 | The raw meeting summary record has exactly one writer. |
| DEC-035 | API-backed meeting ingests: stdlib fetchers, credentials outside the repo, owner-scoped before disk. |

---

## DEC-001 — Single vault; PARA is the folder contract

**Status:** active

**Decision.** There is one vault at `{{vault_root}}`. Its root folders are the
PARA set (`{{folders.inbox}}`, `{{folders.daily}}`, `{{folders.projects}}`,
`{{folders.areas}}`, `{{folders.resources}}`, `{{folders.archive}}`,
`{{folders.meta}}`, `{{folders.attachments}}`) plus `{{folders.crm}}`,
`{{folders.clippings}}`, `{{folders.raw}}`, and `{{folders.wiki}}`. The root is a
locked contract: no skill introduces a new top-level folder. New material goes
inside an existing bucket or gets a decision entry first.

**Why.** Every skill resolves paths against this contract. A second vault or a
drifting root breaks every path assumption at once.

**Consequences.** Skills take the folder names from the config layer, never from
hardcoded strings. Cross-vault operations are out of scope.

## DEC-002 — Local-first audio

**Status:** active

**Decision.** Audio is transcribed on-device or not at all. No recording,
transcript, or screenshot blob leaves the machine, and no cloud speech-to-text
service is called. Audio files are referenced by path from raw records, never
copied into the vault.

**Why.** Voice captures are the most personal material in the corpus. Keeping
them local is the precondition for capturing them at all.

**Consequences.** Voice-memo and dictation ingests read local databases
read-only. The vault is not synced to a cloud service.

## DEC-003 — Wikilink-encoded people attribution

**Status:** active

**Decision.** A note attributes people through `participants:` holding
`[[First-Last]]` wikilinks that resolve to `{{folders.crm}}/People/<First-Last>.md`.
`attendee_emails:` is kept alongside as the mechanical join key for ingest and
people resolution, but attendance queries ("meetings the owner attended") test the
wikilink, not the email.

**Why.** Wikilinks make people first-class graph nodes with backlinks; emails are
strings.

**Consequences.** Every person needs a CRM note, even an empty stub, so the links
resolve (`atlas-people-extract`). Renaming a person note breaks links until an
alias is written.

## DEC-004 — LLM Wiki spine

**Status:** active

**Decision.** `{{folders.raw}}/` is the immutable source layer, one subfolder per
external system. `{{folders.wiki}}/` (entities, concepts, synthesis) is a
projection regenerated from `raw/` and PARA by skills. The vault is rebuildable
from `raw/` alone: delete `wiki/` and a full re-materialization reconstructs it.

**Why.** Separating immutable source from rewriteable projection is what makes
automated synthesis safe. Anything generated can be thrown away and rebuilt.

**Consequences.** Wiki pages are never hand-edited outside their marked editable
region. Raw files are never edited or deleted (DEC-009).

## DEC-007 — Auditors surface, never decide

**Status:** active

**Decision.** Reporting skills (`atlas-health`, `atlas-lint`, `atlas-emerge`,
`atlas-weekly`) flag issues and candidates. They never auto-fix, auto-archive,
auto-merge, or auto-delete. Every mutation of curated content is an explicit
decision by the owner or by a skill the owner deliberately invoked.

**Why.** A wrong automated fix in a personal knowledge base is worse than a
surfaced finding left alone.

**Consequences.** Dashboards are regenerated snapshots with findings, not
commands. The one sanctioned exception is `atlas-auto-graduate` (DEC-019), which
applies a tag under an explicit policy and a review queue.

## DEC-008 — One registration per scheduled agent

**Status:** active

**Decision.** Each scheduled agent (`atlas-morning`, `atlas-nightly`,
`atlas-synthesize`, `atlas-weekly`, `atlas-health`) is registered exactly once
with the scheduler the owner chose during kickoff (desktop scheduled tasks,
launchd, or a manual trigger). The registration fires a Claude Code session with a
self-contained prompt that points back to the skill's SKILL.md.

**Why.** Scheduling logic belongs in one place per agent; the SKILL.md stays the
single source of truth for what the agent does.

**Consequences.** A scheduler that can fire duplicates (wake-from-sleep bursts)
must be paired with the suite run lock in `atlas-nightly` and the duplicate-fire
guard in `atlas-fireflies-ingest`.

## DEC-009 — Ingest writes raw first; raw is append-only

**Status:** active

**Decision.** Every ingest skill writes to `{{folders.raw}}/<source>/` first. Files
there are never edited or deleted once written. Corrections use a forward-pointer
chain (`corrected_by:` on the original, `corrects:` on the replacement). Downstream
consumers (routing, synthesis, research) read the raw mirror and never call the
source API directly.

**Why.** The mirror is the rebuildability guarantee (DEC-004) and the dedup anchor
for idempotent re-runs.

**Consequences.** A mutable-source mirror (Monday items, Wispr meetings) may
rewrite a file in place when the source's `updated_at` advances, because the file
is a projection of the source's current state; this is the documented deviation,
and it still never deletes or renames. Deleting PII or a secret that should never
have been ingested is the one allowed deletion and must be logged here.

## DEC-011 — The vault is not git-tracked

**Status:** active

**Decision.** `{{vault_root}}` is not under git and is never committed from any
checkout. Generated reports inside it (dashboards, weekly reviews, nightly
sections) are not tracked either. Skill folders are tracked; their runtime state
(`state.json`, `last-run.md`, logs) is git-ignored.

**Why.** The vault is personal data; the skill repo is design. Mixing them leaks
one into the other.

**Consequences.** History of a dashboard is not available through git; the
dashboard is a snapshot. Verification of a skill run happens through file counts
and spot checks, not through a vault diff.

## DEC-012 — Person-slug collisions

**Status:** active

**Decision.** When two distinct email addresses derive to the same `First-Last`
slug, both person notes get a company-shortname suffix:
`First-Last-CompanyShort.md` (for example `Sam-Pinecrestlodge.md` and
`Sam-Saltmarshinn.md`). The company shortname is the primary label of the email
domain, lowercased and alphanumeric-only.

**Why.** Wikilinks resolve by filename; two people cannot share one.

**Consequences.** Skills that resolve people (`atlas-distill`,
`atlas-book-summary`) flag an ambiguous match rather than guessing.

## DEC-013 — One nightly trigger

**Status:** active

**Decision.** `atlas-nightly` is the sole scheduled trigger for every ingest
skill. No ingest has its own schedule. The chain runs serially in a fixed order.

**Why.** One trigger means one lock, one report, one place to look when something
did not run.

**Consequences.** Adding an ingest means adding a row to the nightly run-order
table, not a new cron entry.

## DEC-014 — Session history is fair game

**Status:** active

**Decision.** Claude Code session transcripts are mined for thought threads like
any other raw source, after secret redaction. The same license extends to
dictation history and Slack.

**Why.** The owner's thinking-out-loud is where ideas first appear; excluding it
blinds the emergence pipeline to the earliest signal.

**Consequences.** `atlas-claude-history-ingest` redacts known secret patterns
before writing. Summaries, not full transcripts, land in the vault.

## DEC-016 — Unroutable meetings go to needs-decision

**Status:** active

**Decision.** A meeting that no routing rule claims is filed under
`{{folders.inbox}}/needs-decision/` with a `needs-decision` tag and triaged at the
weekly review. The fix for a misroute is a routing-file edit so it never recurs.

**Why.** The router must never guess a destination; a wrong folder is worse than
an inbox item.

**Consequences.** The inbox-overflow warning in `atlas-fireflies-ingest` and
`atlas-health` counts only the inbox root, not `needs-decision/`.

## DEC-018 — Authoring skills may write to PARA directly

**Status:** active

**Decision.** Skills whose source is the owner's judgment or the open web (a
distilled chat capture, a book summary) are authoring, not ingest, so DEC-009's
raw-first rule does not bind them. They may write directly into
`{{folders.resources}}/`. To preserve rebuildability, an authoring skill that
captures an external exchange also writes a redacted append-only source record
under `{{folders.raw}}/`.

**Why.** Forcing every human-authored note through `raw/` adds a hop with no
provenance value.

**Consequences.** `atlas-distill` writes both a raw capture and a resource note;
`atlas-book-summary` writes only the resource note (DEC-026).

## DEC-019 — Auto-graduation: Moderate, unattended, capped

**Status:** active

**Decision.** `atlas-auto-graduate` runs at the end of the nightly chain and
graduates an emerging pattern without a prompt when it passes every guard
(person, known-entity, noise) AND has either two or more work sources or five or
more items AND has persisted across two or more nightly runs. Everything else goes
to a review queue dashboard. A per-run cap bounds how many graduate per night.

**Why.** Prompting the owner for every slug does not scale; the Moderate bar
catches real threads while leaving ambiguous ones for a one-glance human call.

**Consequences.** This reverses the original "no automatic tag application"
anti-goal of `atlas-emerge`, which itself still only surfaces. `graduate.py`
gains `--auto`; a persistence ledger records observation dates.

## DEC-020 — Separate synthesis agent with token bounding

**Status:** active

**Decision.** Cross-source thread synthesis runs as its own scheduled agent
(`atlas-synthesize`) after `atlas-nightly` finishes. The evidence bundle per
thread is capped (a bounded number of raw items, source-diversity balanced, short
excerpts) and a content fingerprint gates re-synthesis so unchanged threads cost
nothing.

**Why.** Synthesis is the only LLM-heavy nightly step; bounding it keeps the
nightly cost predictable.

**Consequences.** A synthesis page states when its bundle is thin rather than
implying completeness. Cross-thread synthesis is deferred.

## DEC-021 — Headless scripts are stdlib-only

**Status:** active

**Decision.** Every Python script that can run unattended imports only the
standard library. No PyYAML, no requests, no pip installs. YAML configs are
parsed by a small stdlib reader; frontmatter is emitted by hand-ordered writers,
not dumped from a dict.

**Why.** The interpreter a scheduler launches does not have the interactive
shell's environment; a third-party import is the most common way a nightly run
dies silently.

**Consequences.** The compiled transcription helper (DEC-023) is the single
exception, and it is a separate binary invoked by absolute path.

## DEC-022 — Review queue and rollout cap

**Status:** active

**Decision.** Patterns that are auto-promoted (DEC-019) or deferred are listed in
a review-queue dashboard. `MAX_AUTO_PER_RUN` (and the analogous per-run
transcription cap in the voice-memo ingest) limits how much unattended change
lands per night; the remainder rolls over.

**Why.** Bounded nightly change is reviewable the next morning; unbounded change
is not.

**Consequences.** "Pending" is a normal state. Deferred items are never lost
because file-exists or the ledger is the authoritative dedup.

## DEC-023 — One native helper

**Status:** active

**Decision.** On-device speech transcription lives in a compiled helper binary
that the voice-memo ingest invokes by absolute path. It is the only non-stdlib,
non-Python component in the suite. It caches by audio path and mtime so a re-run
never re-transcribes.

**Why.** Apple's on-device speech stack is only reachable from native code; a
subprocess boundary keeps the ingest itself stdlib-only (DEC-021).

**Consequences.** The ingest tolerates a missing or outdated helper (it writes
`none` stubs and says why). The helper's `--doctor` contract (DEC-033) is
additive-only.

## DEC-024 — Full Disk Access soft-skips

**Status:** active

**Decision.** Reading a TCC-protected store (Voice Memos) requires Full Disk
Access granted by the user to the exact process that runs the skill. When it is
missing, the run reports `TCC-blocked`, writes nothing, and exits 0. It never
fails hard, so the nightly chain is never blocked by it.

**Why.** Permission is the user's decision; a nightly chain should not fail
because one optional source is locked.

**Consequences.** TCC is per-executable: granting access to an interactive
terminal does not cover a scheduled runner. The same soft-skip covers a machine
that has never used the source.

## DEC-025 — Apple Notes through the scriptable interface

**Status:** active

**Decision.** Apple Notes are mirrored through the AppleScript-backed MCP
(`list_notes`, `get_note_content`), which needs no Full Disk Access, using the
note's stable Core Data id as the dedup key. Direct reads of the Notes database
are deferred.

**Why.** The scriptable path is low-friction and permission-free; the database
path would put a second source behind DEC-024.

**Consequences.** Titleless notes may not resolve by name; the nightly
incremental pass keeps retrying them as they are renamed or edited.

## DEC-026 — Book summaries have no raw mirror

**Status:** active

**Decision.** A book note in `{{folders.resources}}/Books/` is the durable
artifact. Provenance lives in the note as a cited `## Sources` section and a
`verified:` date. There is no `{{folders.raw}}/books/`.

**Why.** The source is the open web, not an external system of record; a raw copy
of web research adds nothing rebuildable.

**Consequences.** The note's generated region is re-renderable with `--force`;
the owner's `## Notes` region is preserved.

## DEC-027 — The no-nudge list is never scheduled and never surfaced

**Status:** active

**Decision.** The owner names, at kickoff, the folders, tags, and practices that
must never appear on any nudge surface. That answer is the config's `no_nudge`
list: vault-relative folder paths and `#tags`, empty by default. Every nudge
surface (the morning report's overdue list, the weekly review's open items and
"needs attention" alert, dashboards, any scheduled output) skips files under a
listed folder and lines carrying a listed tag. Nothing on the list is ever wired
into `atlas-nightly` or any scheduler; whatever lives there is pull-only, reached
by the owner on their own initiative.

**Why.** The owner explicitly opted out of accountability mechanics for these.
Carry-over there is quiet; patterns are neutral data. The kit ships no skill for
any personal practice; it only keeps the practice silent.

**Consequences.** Briefing and dashboard skills read `no_nudge` from the config
and repeat the list in their guardrails. Adding an entry is a config edit;
surfacing anything on the list, or scheduling it, requires a new decision.

## DEC-031 — Stdlib connector fetchers

**Status:** active

**Decision.** MCP-mediated sources (Gmail, Slack, Monday, Fireflies) run headless
only once a stdlib-only fetcher exists for them. Until then those ingests run in
an attended session where the agent performs the MCP calls and hands JSON to the
script.

**Why.** MCP is a Claude Code runtime feature; a Python script cannot call it.

**Consequences.** Attended-only passes (backfills, enrichment) are labeled as such
in their SKILL.md.

## DEC-032 — Summary on disk, transcript via API

**Status:** active

**Decision.** Full meeting transcripts do not live on disk. Per meeting the vault
keeps a meeting note (PARA layer) with the summary and a link, and a summary
record at `{{folders.raw}}/fireflies/<meeting_id>.md` (raw layer). A tool that
needs the full transcript fetches it from the Fireflies API on demand
(`fireflies_get_transcript`, `transcriptId` = the `meeting_id` frontmatter).

**Why.** Transcripts cost disk, and more importantly context tokens every time
raw bodies are scanned. The summary carries nearly all of the retrieval signal at
a small fraction of the tokens, and Fireflies already stores the authoritative
transcript.

**Raw-file shape (the summary record).**

```markdown
---
type: raw-fireflies
meeting_id: <fireflies_id>
meeting_note: "[[<note basename without ext>]]"
fireflies_url: https://app.fireflies.ai/view/<fireflies_id>
date: <date_iso_full>
transcript_status: summary
extracted_at: <YYYY-MM-DD>
keywords: [<kw>, <kw>]
---

# <note basename without ext>

**Keywords:** <kw>, <kw>, …

## Overview
<summary overview>

## Action Items
<action items>

---
Full transcript lives in Fireflies (fetch via API on demand): [view](<fireflies_url>)
```

`transcript_status:` is `summary` (the norm) or `not-retained-locally` (a
link-only stub for a meeting with no summary content).

**Canonical meeting-note format.** Frontmatter: `date`, `attendee_emails`,
`participants` (wikilinks, DEC-003), `meeting_id`, `project`, `tags`,
`fireflies_url`, `meeting_link` (when present), `keywords`, `monday_context`
(when matched). Sections: `## Summary`, `## Key Decisions`, `## Action Items`,
`## Full Transcript` (a single link to the raw summary record, never an inline
transcript or `<details>` block).

**Consequences.**

- The ingest writes the summary record from the list call's payload; there is no
  per-meeting transcript fetch at ingest time.
- `atlas-transcript-extract` is retired; its purpose inverts under this policy.
- `atlas-research` gains a capped escalation tier: at most three live transcript
  fetches per question, cited to the meeting note.
- Raw summary records are never deleted: they are dedup anchors and wikilink
  targets. Bodies are converted in place.
- A raw file that still holds a full transcript is never overwritten with a stub
  generated from nothing.

## DEC-033 — Transcription doctor preflight

**Status:** active

**Decision.** The transcription helper has a `--doctor` mode reporting every
precondition on-device transcription needs (speech authorization for the invoking
process, on-device model availability for the locale, and a functional probe that
actually transcribes a short synthesized clip) as stable machine-readable
`key=value` lines. Exit 0 means ready; blocked exits reuse the transcription
failure classes. The voice-memo ingest runs the doctor once per run before any
transcription. On a blocked verdict it soft-skips transcription (DEC-024
semantics): memos still ingest as `native` or `none` stubs, the helper is never
invoked, and the reason lands in `last-run.md`.

Verdict policy: normal runs fail open on an `unknown` verdict (a helper too old
to know `--doctor` keeps working), but `--retry-stubs` helper attempts require a
strict `ready`, because a retry pass exists to confirm a fixed environment. An
empty transcript is not a failure: a no-speech recording prints nothing, exits 0,
and is cached.

**Why.** The status flags of the OS speech stack can all read healthy while the
recognizer stalls; only a functional probe tells permission, model, and
process-context causes apart. One precise diagnostic beats a watchdog stall per
memo.

**Consequences.** The stall watchdog is tunable only after the doctor is green.
Interactive-only remediation flags (`--request-auth`, `--install-model`) exist;
unattended runs never block on a prompt. The doctor contract is extended
additively.

## DEC-034 — Single writer for the summary record

**Status:** active

**Decision.** The `{{folders.raw}}/fireflies/<meeting_id>.md` summary record is
rendered by exactly one module (`summary_record.py`). The ingest, any format
pilot, and any retroactive backfill all pass the Fireflies meeting JSON to
`summary_record.py apply`; nothing hand-formats the record. The record extends
the DEC-032 shape with `enriched_at`, a sorted `attendee_emails` block list (the
mechanical join key for people resolution), an always-emitted `participants`
list (display names, best effort, empty when none), an `**Attendees:**` line,
and a machine instruction telling AI tools to fetch the transcript by passing
`meeting_id` to `fireflies_get_transcript` rather than opening the URL. Known
logger addresses (BCC-logger domains; all-numeric local parts with no display
name) are filtered from the attendee fields and reported.

**Writer guarantees.** Atomic (tmp + `os.replace`); idempotent (re-running on an
already-enriched record is a byte-level no-op); guarded (a body that is neither a
known stub nor a prior summary record is a `conflict`, left untouched, non-zero
exit); degrades gracefully (no summary content means a link-only stub on fresh
write and a no-op against an existing file; a contentless render never overwrites
a populated body); stdlib-only (DEC-021); never touches the ingest cursor.
Records are machine-owned: hand edits to a summary record's body are overwritten
on the next render by design. Curate the meeting note, not the raw record.

**Retroactive backfill.** Existing stubs converge on this format in attended,
date-windowed batches (`pending` → `fireflies_get_transcripts` → `apply`).
Meetings aged out of the source get `mark-unavailable` (an annotation, never an
error); if such a meeting later reappears, applying real content drops the
annotation deliberately. No file is deleted or renamed; the pass is resumable
because the writer is idempotent.

**Why.** One writer means the format has one definition and every path through
the system produces byte-identical output.

**Consequences.** Keywords and overview stay within the first ~2,500 characters
of the body, because `atlas-synthesize` keyword-matches only that window and
`atlas-emerge` scans raw bodies; this file is the meeting source's entire
contribution to pattern emergence.

## DEC-035 — API-backed meeting ingests

**Status:** active

**Decision.** The Zoom and Gong meeting ingests ship a stdlib-only REST client
(`fetch.py`) beside `ingest.py`, the first sources to meet DEC-031's bar for
headless scheduling. Three rules govern every such fetcher:

1. **Credentials live outside the repo.** Read from environment variables first,
   then from `~/.config/atlas/<source>-credentials.json`, which the fetcher refuses
   to read unless its mode is 600. A rotating OAuth refresh token is written back
   to `~/.config/atlas/` atomically, mode 600, before the new access token is used.
   Nothing credential-shaped is ever written into a config next to the script,
   into the vault, or into a log.
2. **Owner-scoped before disk.** When a credential can see more than the owner's
   own meetings (a Gong API key sees the whole company), the fetcher keeps only
   meetings the owner was a party to, matched on `owner_emails`, and applies the
   filter before anything is written anywhere, including debug output. Dropped
   meetings are counted, never named.
3. **Every API path has a no-API sibling.** The same ingest also reads files the
   owner exported by hand, and JSON a session fetched through an MCP server, so
   an owner without developer or admin rights still gets the source.

The DEC-032 transcript rule generalizes as: the transcript stays at the source
when a re-fetch path exists (an MCP tool or `fetch.py transcript <id>`) and the
record has summary content; it is written in full when the input is a
hand-exported file, or when the transcript is the record's only content.
`transcript_policy:` in each record says which applied.

**Why.** A personal pipeline that needs an attended session for every meeting
source stops ingesting whenever the owner is away. Zoom and Gong both offer
plain HTTPS APIs that the standard library can call, so the attended-only
limitation is avoidable for them. The owner-scope rule exists because an
organization-wide key would otherwise mirror colleagues' private calls into a
personal vault.

**Consequences.** `atlas-research`'s transcript escalation gains `fetch.py
transcript <id>` as a sanctioned read, under the same cap and never persisted.
Schedulers do not source the interactive shell, so a headless run needs the
credentials file or the scheduler's own environment block; the fetcher's
`check` command says which is missing. A fetcher is an optional input to its
ingest: nightly runs without credentials still ingest exported files.
