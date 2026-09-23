# Interview question bank

Three rounds, at most four questions each, asked with `AskUserQuestion`. Each entry
gives the question as the user sees it, what it feeds, the follow-up to use when the
answer is too vague to write down, and the default to propose. Skip any question the
Phase 1 diagnostic already answered; say what was found instead.

Options in `AskUserQuestion` should carry the default first and be phrased so the user
can pick one in a word. Free-text is always allowed.

## Round 1 — Identity and shape

### 1.1 Owner name

- **Question:** "What should generated notes and briefings call you? (One name or a
  short handle. Used only inside your vault.)"
- **Feeds:** `owner_name` in the config; `{{owner_name}}` in vault templates. Also
  `{{owner_slug}}`, which is derived, never asked: the name in kebab-case with its
  capitalization kept (`Jordan Vale` → `Jordan-Vale`), the same rule
  `atlas-people-extract` uses for person-note filenames, so the owner's own person note
  and `[[{{owner_slug}}]]` wikilinks resolve.
- **Follow-up if vague:** none needed; any string works. If empty, use the system
  username and say so.
- **Default:** the login name from `whoami`, offered for confirmation.

Not asked in any round: `{{skills_root}}` is the target repo root from the invocation
(`--repo <path>`, else the directory the skill runs in), recorded in the kickoff's §2.
The interview never asks for a path the diagnostic already has.

### 1.2 Mission

- **Question:** "In one paragraph: a year from now, what should this vault be able to
  do for you that nothing else does? Name a question you would want it to answer."
- **Feeds:** kickoff §Mission.
- **Follow-up if vague:** an answer that describes a container ("a place for my notes")
  gets: "Give me one question you would ask it. For example: 'what did we agree with
  this client across email, meetings, and chat last quarter?'" Write the mission from
  the answer to that.
- **Default:** none. This is the user's sentence; propose nothing.

### 1.3 Vault layout scheme

- **Question:** "How is the vault organized? Options: PARA with numbered prefixes
  (`00 - Inbox`, `20 - Projects`), PARA without numbers (`Inbox`, `Projects`),
  Zettelkasten (flat notes plus `raw/` and `wiki/`), or custom (you name every folder)."
- **Feeds:** the folder table shown in Round 2; `folders.*` in the config.
- **Follow-up if vague:** "I will propose the numbered PARA table and you edit the names.
  Fine?"
- **Default:** PARA with numbered prefixes (matches the scaffold and every exemplar).
  For an existing vault, propose whichever scheme its top-level folders already match.

### 1.4 Timezone

- **Question:** "Daily notes and schedules will use `<detected>` as the timezone.
  Correct?"
- **Feeds:** `timezone` in the config.
- **Follow-up if vague:** ask for an IANA name such as `Europe/Berlin` or `Asia/Tokyo`.
- **Default:** the detected zone.

## Round 2 — Folders, sources, and silence

### 2.1 Folder names

- **Question:** "Here is the folder table for your scheme. Change any name; leave the
  rest. Every generated skill will use these exact names." Then the table:

  | Key | Proposed name | Purpose |
  |---|---|---|
  | inbox | `00 - Inbox` | unrouted captures and things needing a decision |
  | daily | `10 - Daily Notes` | one note per day; briefings write here |
  | projects | `20 - Projects` | active work with an end date |
  | areas | `30 - Areas` | ongoing responsibilities |
  | resources | `40 - Resources` | reference material, distilled notes, books |
  | archive | `50 - Archive` | finished projects and retired areas |
  | meta | `60 - Meta` | templates, dashboards, the vault's own docs |
  | attachments | `99 - Attachments` | binary files |
  | crm | `CRM` | parent of the fixed `People/` subfolder that holds person notes |
  | clippings | `Clippings` | web clips |
  | raw | `raw` | append-only ingested source records |
  | wiki | `wiki` | regenerated entity, concept, and synthesis pages |

- **Feeds:** every `folders.<key>` in the config.
- **Follow-up if vague:** for any key the user drops ("I don't use CRM"), keep the key
  with the default name and tell them the folder is created empty only if a selected
  capability writes to it. Keys are not optional; names are. If the user names `crm`
  `People`, say that person notes land at `<crm>/People/` (the subfolder is an engine
  convention, not configurable), so that name would produce `People/People/`; propose
  `CRM` or `Contacts` for the parent instead.
- **Default:** the table as shown for the numbered scheme; the same names without
  prefixes for the plain scheme; for Zettelkasten, `notes/` for projects, areas, and
  resources with a `type:` frontmatter field, and the rest unchanged.
- **Existing vault:** for each key, propose the existing folder whose name matches
  best; mark keys with no match as "will be created".

### 2.2 Sources to ingest

- **Question:** "Which sources should the pipeline ingest into `raw/`? I can offer
  these now: `<detected list>`. These are blocked until you connect or install
  something: `<blocked list with the missing piece each>`. Pick any number."
- **Feeds:** kickoff §Selected capabilities (ingest group) and §Sources and routing;
  which routing configs get generated.
- **Follow-up if vague:** for each picked source that needs routing, one concrete
  question: email: "Which sender domains or labels should route to which folder?
  Everything else lands in `{{folders.inbox}}/needs-decision`." Slack: "Which channels?"
  Monday: "Which workspaces or boards?" GitHub: "Which owner and repos?" Meetings:
  "Which meeting-title keywords or attendee domains map to which project or client
  folder?" A source picked with no routing answer gets the example config with the
  fictional entries replaced by `{{fill-me}}` markers and a line in manual steps.
- **Default:** every detected source that has a local-only prerequisite (Claude history,
  dictation) on; MCP-backed sources off until named, because they write a lot on the
  first run.

### 2.2a Owner email (optional; follow-up when meetings or email are picked)

- **Question:** "Which email address is yours in meeting invites and mail? Leave blank
  to skip." Asked in the same follow-up call as the routing questions, only when a
  meeting or email source was picked; otherwise skipped and left blank.
- **Feeds:** `owner_email` in the config; `{{owner_email}}` in the meeting-routing
  owner fallback and wherever an exemplar must match the owner's own address.
- **Follow-up if vague:** none. A blank answer writes `{{fill-me}}` at each use and a
  line in manual steps; nothing else depends on it.
- **Default:** blank.

### 2.2b Employer and email domain (optional; same follow-up)

- **Question:** "Your organization's name and its email domain, for telling internal
  meetings and mail from client ones (for example `Harbor Lane Analytics`,
  `harborlane.example`). Leave blank to skip." Asked with 2.2a, under the same
  condition.
- **Feeds:** `employer` and `employer_domain` in the config; `{{employer}}` and
  `{{employer_domain}}` in the meeting and mailbox routing configs (the internal-domain
  rules) and in the exemplars' prose about internal meetings.
- **Follow-up if vague:** a name without a domain gets "What comes after the @ in your
  work address?"; a domain without a name is fine (use the domain's label as the name
  and say so). Blank writes `{{fill-me}}` at each use and a line in manual steps.
- **Default:** blank.

### 2.3 Capabilities beyond ingest

- **Question:** "The knowledge spine (people-extract, materialize, emerge, graduate,
  synthesize, lint, health, research, distill) is on by default. Which of these do you also want? Morning
  briefing into the daily note; weekly review note; nightly orchestrator (runs all
  ingests in order); book summaries."
- **Feeds:** kickoff §Selected capabilities (spine, briefing, orchestrator, capture
  groups).
- **Follow-up if vague:** "on-demand only" answers turn briefings off and keep the
  spine; say that. If the user picks the nightly orchestrator with zero ingests, point
  out it has nothing to run and drop it.
- **Default:** spine on; morning and weekly on if any ingest was picked; nightly on if
  any ingest was picked and Round 3 chooses a scheduler; book summaries off.

### 2.4 The no-nudge question

- **Question:** "Is there anything in this vault that must never show up in a morning
  report, weekly review, dashboard, or any scheduled output, even as a reminder? A
  journaling or reflection practice, a health log, a folder, a tag. Answer 'none' if
  nothing."
- **Feeds:** `no_nudge` in the config, a JSON list of vault-relative folder paths
  (`Areas/Journal`) and tags (`#journal`), empty when the answer is none; kickoff
  §Non-negotiable constraints (the no-nudge entry); the exclusion list in every
  generated briefing and dashboard skill's guardrails; `DEC-` entry.
- **Follow-up if vague:** "whatever you recommend" is not an answer here; offer "none"
  explicitly and ask once more. If they name a practice, ask for the folder or tag
  that identifies it so the exclusion can be written as a path or a tag prefix. The kit
  generates no skill for the practice itself; it only keeps it off every nudge surface.
- **Default:** none. Never propose a practice to exclude; the user names it or the
  list is empty.

## Round 3 — Operation and non-negotiables

### 3.1 On-demand or scheduled

- **Question:** "Should the pipeline run on a schedule (unattended writes into your
  vault, typically nightly) or only when you invoke it? Scheduled keeps the vault
  current; on-demand means nothing happens unless you remember."
- **Feeds:** kickoff §Scheduler; whether jobs are generated.
- **Follow-up if vague:** "I will schedule the nightly orchestrator and leave everything
  else on demand. Fine?"
- **Default:** scheduled if any ingest was picked; on-demand otherwise.

### 3.2 Scheduler choice

- **Question:** "Which scheduler? Options available on this machine: `<list>`."
  Desktop app: "desktop scheduled tasks (managed in the app sidebar; pinned to the
  working directory this session runs in)". macOS CLI: "launchd (plist files; runs
  the Python jobs without a Claude session; session jobs need the `claude` CLI)".
  Always: "manual (a runbook with one command per job and a suggested cadence)".
- **Feeds:** kickoff §Scheduler; which `references/schedulers/` guide Phase 4 follows.
- **Follow-up if vague:** none; the options are exhaustive for the runtime. If the user
  picks desktop tasks, confirm the current working directory is the target repo, because
  the tasks will be pinned to it.
- **Default:** desktop scheduled tasks in the desktop app; launchd on macOS CLI; manual
  elsewhere.

### 3.3 Non-negotiables

- **Question:** "Accept or waive each of these. A waiver needs a one-line reason.
  (1) `raw/` is append-only: corrections are new files, never edits. (2) `wiki/` is
  regenerable from `raw/` alone: hand edits only inside marked regions. (3) Every
  claim on a generated wiki page carries a resolvable wikilink. (4) Nothing generated
  ever deletes a note. (5) Meeting transcripts stay summary-only on disk; the full
  transcript is fetched on demand."
- **Feeds:** kickoff §Non-negotiable constraints; `DEC-` entries; guardrail lines in
  generated skills.
- **Follow-up if vague:** a waiver without a reason gets "What is the reason? It goes
  in the record." A waiver of (1) gets the tradeoff stated once: "With raw editable,
  the wiki can no longer be rebuilt from raw after a mistake. Still waive?" Then honor
  the answer.
- **Default:** all five accepted.

### 3.4 What must never happen

- **Question:** "What has a tool done to your notes before that you never want
  repeated? Anything here becomes a guardrail line in every generated skill."
- **Feeds:** kickoff §Non-negotiable constraints (user-added entries); guardrails.
- **Follow-up if vague:** offer the common ones: rewriting frontmatter on hand-written
  notes, moving files between folders, creating hundreds of stub pages, writing into
  the inbox without a tag. Take any they pick.
- **Default:** none added.

## Closing confirmation

After Round 3 (or earlier if the exit condition in `grilling.md` is met), show one table:
owner, mission (first line), scheme, folder table, sources on, sources blocked,
capabilities on, no-nudge list, schedule and scheduler, constraints with status,
user-added rules. Ask: "Write the kickoff from this?" A correction here loops back to
the single affected question, not to a full round.
