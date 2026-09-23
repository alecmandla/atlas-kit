---
name: atlas-voice-memos-ingest
description: Ingest — mirror iOS/macOS Voice Memos into the vault at raw/voice-memos/<memo-id>.md, one markdown file per recording, idempotent via the recording's stable Core Data id. A stdlib-only ingest.py reads the TCC-protected CloudRecordings.db Core Data store read-only, introspects its schema at runtime (no hardcoded columns), converts the 2001-epoch ZDATE, resolves a transcript (Apple's stored transcript = native, else the on-device Swift helper = on-device, else a none stub), redacts secrets, and writes the raw record. Audio is referenced, never copied (DEC-002). A --doctor preflight (DEC-033) runs before any transcription so a blocked environment yields one precise diagnostic (permission / model / process-context) instead of per-memo watchdog stalls. PREREQUISITE: Full Disk Access on the runner (DEC-024) — without it the run soft-skips (reports TCC-blocked, writes nothing, exits 0), never a hard failure. Runs nightly under atlas-nightly. Triggers on "sync voice memos", "ingest voice memos", "/atlas-voice-memos-ingest", "voice memo doctor".
exemplar-of: atlas-voice-memos-ingest
status: experimental
requires: [cli/python3]
---

# atlas-voice-memos-ingest

> **Status: experimental.** On-device transcription through the native helper is broken upstream on current macOS speech stacks; the ingest half (native transcripts and `none` stubs) works, the helper half may not. Treat the helper as optional until the doctor reports `ready` on your machine. The helper itself is a compiled Swift binary, the one native exception to the stdlib-only rule (DEC-023); it must be built on the target machine.

Mirrors Voice Memos into the vault's raw spine. One recording becomes one
`{{folders.raw}}/voice-memos/<memo-id>.md` file. This is the **heavy half** of
personal capture: Voice Memos has no scriptable/MCP interface (unlike Apple Notes,
DEC-025), so the only path to the data is a direct read of the TCC-protected Core
Data store, which makes **Full Disk Access a hard, user-granted prerequisite**
(DEC-024).

## Prerequisites (DEC-024, DEC-023)

1. **Full Disk Access** for the process that runs this skill, and, under
   `atlas-nightly`, for the process hosting the **scheduled** Claude Code session.
   TCC is per-executable: granting FDA to your interactive terminal does **not**
   cover the scheduled runner. See the FDA setup notes shipped with the helper.
2. **Speech-recognition permission** for the transcription helper
   (`helper/atlas-transcribe`), only needed for memos that lack an Apple-stored
   transcript. See `helper/README.md`.

**Without FDA the run soft-skips:** it reports `TCC-blocked`, writes nothing, and
exits 0. The nightly chain must never abort on it (the defining safety property of
this ingest). The same soft-skip covers a Mac that has simply never used Voice
Memos (`container-missing`).

**Transcription doctor (preflight, DEC-033).** Before any transcription the ingest
runs the helper's `--doctor` once: a blocked environment (Speech permission, missing
on-device model, broken OS speech stack) produces **one precise diagnostic** in
`last-run.md` instead of a 45s watchdog stall per memo. Blocked runs still capture
memos (native transcript or `none` stub) but never invoke the helper. Verdict
policy: normal runs fail **open** on an `unknown` verdict (helper predates
`--doctor`; rebuild noted in the report), while `--retry-stubs` **helper** attempts
require a strict `ready`; helper-free native upgrades apply on any verdict.
Diagnose any time with:

```bash
python3 ingest.py --doctor   # exit 0 = a run would attempt transcription (ready/unknown);
                             # exit 1 = blocked. Fix mapping in helper/README.md
```

## Mental model

Unlike the MCP-driven ingests (Gmail / Slack / Apple Notes), there is no
"agent fetches via MCP" half: Voice Memos has no MCP. This is the **wispr shape**:
a single stdlib-only `ingest.py` reads a local SQLite store directly. The one extra
moving part is the **native transcription helper** (`helper/atlas-transcribe`,
DEC-023), which `ingest.py` shells out to **by absolute path** only when a
recording has no Apple-stored transcript.

- **`ingest.py` (stdlib-only, DEC-021)** discovers `CloudRecordings.db` at runtime,
  opens it read-only with retry/backoff, **introspects the schema** (`sqlite_master`
  + `PRAGMA table_info`; column names are NOT hardcoded because the real schema is
  unknown until FDA is granted and varies by macOS version), converts `ZDATE`
  (Core Data 2001-epoch), resolves a transcript, redacts secrets, and writes the
  raw record + `state.json` + `last-run.md`. The resolved table/column mapping is
  logged to `last-run.md`.
- **`helper/atlas-transcribe`** does on-device STT, invoked by absolute path. Caches
  by audio path + mtime so a nightly re-run never re-transcribes.

## Transcript resolution (in order)

1. **DB-stored transcript** (a transcript column on the recordings table) → `native`.
2. **Sidecar** next to the `.m4a` (`.transcription` / `.txt` / `.plist`) → `native`.
3. **On-device helper** on the `.m4a` → `on-device` (only when the doctor preflight
   is `ready`; subject to a per-run cap).
4. **Nothing available / audio unreadable / doctor blocked** → `none` stub (the
   doctor's reason lands in `last-run.md`; fix per the runbook in
   `helper/README.md`, then `--execute --retry-stubs` backfills).

A recording that *would* use the helper but is **over the per-run cap**
(`ATLAS_VM_MAX_TRANSCRIBE`, default 20) is **deferred**: no file is written this
run, and it rolls out over subsequent nights (the DEC-022 `MAX_AUTO_PER_RUN`
pattern). File-exists is the authoritative dedup, so deferred/missed recordings are
never lost; a non-incremental run is the periodic backstop.

## Frontmatter

Order: `date` first, then the rest alphabetically.

```yaml
---
date: 2026-06-20
audio_path: ~/Library/.../Recordings/<uuid>.m4a   # reference, never copied
duration_seconds: 43
ingested_at: 2026-06-23T17:26:55Z
memo_id: <ZUNIQUEID or pk-<Z_PK>>
title: Quarterly planning
transcript_source: native        # native | on-device | none
type: raw-voice-memo
---
```

## Workflow

```bash
cd {{skills_root}}/atlas-voice-memos-ingest

# Diagnose (runs the helper --doctor exactly as the ingest does):
python3 ingest.py --doctor

# Preview (writes a dry-run report; degrades to "TCC-blocked: 0 memos" without FDA):
python3 ingest.py --dry-run-report /tmp/atlas-voice-memos-ingest-dryrun.md

# Ingest (atlas-nightly calls this):
python3 ingest.py --execute --incremental

# Backfill `transcript_source: none` stubs once the doctor is green:
python3 ingest.py --execute --retry-stubs
```

- **Idempotent.** Dedup is file-exists at `{{folders.raw}}/voice-memos/<memo-id>.md`; a
  re-run is a no-op for already-ingested recordings (DEC-009 append-only).
- **`--incremental`** limits the scan via `state.json`'s `last_recording_date`
  (with a 30-day overlap), a perf bound only; file-exists is the real dedup.
- **Read-only.** sqlite opened with `?mode=ro` + retry/backoff (lifted from
  `atlas-wispr-ingest`). Audio is referenced via `audio_path:`, never copied.
- **Local only (DEC-002).** No audio/transcript leaves the machine; transcription is
  on-device; secrets are redacted with the shared 7-pattern set (verbatim from
  `atlas-distill`).

## Env overrides

| var | meaning |
|---|---|
| `ATLAS_VM_DB` | explicit `CloudRecordings.db` path (skips discovery) |
| `ATLAS_VM_CONTAINER` | Voice Memos group-container root |
| `ATLAS_VM_RAW` | output dir (default `{{vault_root}}/{{folders.raw}}/voice-memos`) |
| `ATLAS_VM_AUDIO_ROOT` | base for resolving relative audio paths |
| `ATLAS_TRANSCRIBE_BIN` | helper path (default `helper/atlas-transcribe`) |
| `ATLAS_VM_MAX_TRANSCRIBE` | per-run helper-call cap (default 20) |
| `ATLAS_VM_LOCALE` | transcription locale (default `en-US`) |
| `ATLAS_VM_STATE` / `ATLAS_VM_LASTRUN` | override state/last-run paths (testing) |

## Files

```
atlas-voice-memos-ingest/
├── ingest.py          # stdlib-only ingest
├── test_ingest.py     # doctor-preflight + retry-stubs contract tests (synthetic DB + fake helper)
├── SKILL.md           # this file
├── last-run.md        # status of the most recent run (incl. doctor verdict + resolved schema mapping)
├── state.json         # last_run_iso, last_run_count, last_recording_date (written on a non-soft-skip execute)
└── helper/            # on-device transcription helper (compiled Swift) + --doctor preflight (see helper/README.md)
```

## Relationship to other skills

- `atlas-apple-notes-ingest` — the light half of personal capture (MCP-backed, no FDA needed).
- `atlas-wispr-ingest` — the local-SQLite read-only pattern this skill copies.
- `atlas-nightly` — runs this skill nightly; a soft-skip here must never abort the chain.
- `atlas-emerge` — consumes `{{folders.raw}}/voice-memos/` like any other raw source.
