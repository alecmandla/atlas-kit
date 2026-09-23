# atlas-transcribe — on-device transcription helper

The single native component of Atlas (DEC-023): a compiled Swift binary that
transcribes **one** audio file entirely **on-device** and prints the transcript
to stdout. It is the sole sanctioned exception to the stdlib-only Python rule
(DEC-021) — speech-to-text is not in the Python standard library, and cloud STT
is barred (DEC-002, local-first). `atlas-voice-memos-ingest/ingest.py`
invokes it **by absolute path** via `subprocess`, never through `python3`.

```
helper/
├── atlas-transcribe.swift   # source (rebuildable — keep in repo)
├── Info.plist               # embeds NSSpeechRecognitionUsageDescription
├── build.sh                 # compile step
├── atlas-transcribe         # compiled binary (produced by build.sh)
└── README.md                # this file
```

## Interface

```
atlas-transcribe <audio-path> [--locale en-US] [--no-cache]
                 [--timeout N] [--first-result-grace N] [--help]
atlas-transcribe --doctor [<audio-path>] [--locale en-US]
                 [--request-auth] [--install-model]
```

- Prints the transcript to **stdout** on success (exit 0).
- Exits **non-zero** with a single clear line on **stderr** on any failure.
- An **empty transcript is not a failure**: a recording containing no speech
  prints nothing and exits 0 on both engines, and the empty result is cached —
  so honest no-speech memos quiesce instead of erroring on every retry.
- On-device only: if a locale has no on-device model the tool **fails** (exit 5)
  rather than silently using the cloud (DEC-002).

### Engines

| engine | when | API |
|---|---|---|
| `analyzer` | macOS 26+ (auto) | `SpeechAnalyzer`/`SpeechTranscriber` |
| `legacy` | macOS < 26, or `ATLAS_TRANSCRIBE_ENGINE=legacy` | `SFSpeechRecognizer` with `requiresOnDeviceRecognition` |

`ATLAS_TRANSCRIBE_ENGINE` accepts only `legacy` (a diagnosis override); the
analyzer engine cannot be forced onto a pre-26 macOS.

On macOS 26 the legacy path **silently stalls** — the recognitionTask never
fires a callback even though authorization, `isAvailable`, and
`supportsOnDeviceRecognition` all read healthy. The symptom is identical
exit-7 stalls on every memo; the analyzer engine is the fix (a 2014
memo that always stalled transcribes in ~1s).

### Exit codes

| code | meaning |
|---|---|
| 0 | success — transcript on stdout (doctor: `verdict=ready`) |
| 2 | usage error (bad/missing args) |
| 3 | audio file not found / unreadable — check Full Disk Access, DEC-024 (doctor: `blocked_by=audio`) |
| 4 | Speech recognition not authorized (doctor: `blocked_by=speech-auth`) |
| 5 | recognizer unavailable, or no on-device model for the locale (doctor: `blocked_by=recognizer` / `model`) |
| 6 | recognition failed (corrupt audio, decode error, …) (doctor: `blocked_by=probe` — probe failed or could not run) |
| 7 | timed out waiting for recognition (doctor: `blocked_by=probe` — stall) |

## `--doctor` preflight (DEC-033)

Reports every precondition on-device transcription needs — without touching
real audio — as **stable machine-readable `key=value` lines** on stdout, then
exits 0 (ready) or the matching failure code (blocked). `ingest.py` runs it
once per batch, so a blocked environment costs one precise diagnostic instead
of one 45-second stall per memo. It includes a **functional probe** (a short
`say`-synthesized clip is actually transcribed) because the status flags alone
provably lie — see the engine note above. If the probe **cannot run** (audio
synthesis fails), the verdict is `blocked`, not `ready`: an unverifiable speech
stack must never wave a batch through into per-memo stalls.

**Ingest verdict policy:** a normal run fails *open* on an `unknown` verdict
(a helper too old to know `--doctor` keeps working, with a rebuild note in the
report); `--retry-stubs` helper attempts require a strict `ready` — a retry
pass exists to confirm a *fixed* environment. Helper-free native upgrades (DB
transcript / Apple sidecar) apply on any verdict.

### Output contract (v1 — stable; ingest.py and tests parse it mechanically)

```
doctor=v1
engine=analyzer|legacy
locale=<locale identifier>
speech_auth=authorized|denied|restricted|notDetermined|unknown
model_supported=true|false        # analyzer engine only
model_installed=true|false        # analyzer engine only
model_install_error=<single line> # only when --install-model was tried and failed
recognizer_present=true|false     # legacy engine only
recognizer_available=true|false   # legacy engine only
on_device_model=true|false        # legacy engine only
audio_path=<path>                 # only when an audio path was passed
audio_readable=true|false         # only when an audio path was passed
probe=pass|fail|skipped           # `skipped` while otherwise unblocked means
                                  # the probe could not run → verdict=blocked
probe_error=<single line>         # only when probe=fail
verdict=ready|blocked
blocked_by=speech-auth|recognizer|model|audio|probe   # only when blocked
reason=<single human-readable line>                   # only when blocked
```

Blocked exit codes mirror the transcription failure classes: 3 audio /
4 speech-auth / 5 recognizer-or-model / 7 probe stall / 6 probe failed or
could not run.

Run it **exactly the way the ingest runs it** (same process context — TCC
answers differ between, say, your interactive terminal and the scheduled
runner):

```bash
cd <skills_root>/atlas-voice-memos-ingest && python3 ingest.py --doctor
```

## Runbook — doctor finding → fix

| doctor says | fix |
|---|---|
| `speech_auth=notDetermined` | Run `./atlas-transcribe --doctor --request-auth` **interactively** and approve the macOS prompt (or pre-approve in System Settings ▸ Privacy & Security ▸ Speech Recognition). |
| `speech_auth=denied` | Enable the runner in System Settings ▸ Privacy & Security ▸ Speech Recognition. **If it already shows enabled:** the running process predates the grant — TCC grants do not propagate to already-running processes. Quit and relaunch the terminal / runner app, then re-run the doctor. |
| `speech_auth=restricted` | MDM / parental controls block it on this device; no local fix. |
| `blocked_by=model` (`model_installed=false`) | Run `./atlas-transcribe --doctor --install-model --locale <loc>` (downloads/reserves the on-device model via AssetInventory — model data comes *from* Apple; audio still never leaves the machine). Equivalent manual path: System Settings ▸ Keyboard ▸ Dictation, add the language. |
| `blocked_by=model` (`model_supported=false`) | No on-device model exists for that locale at all — pass a supported `--locale`. Cloud fallback is barred (DEC-002). |
| `blocked_by=recognizer`, reason "no speech recognizer exists for locale" | The locale identifier is wrong or unsupported — this never resolves by retrying; pass a supported `--locale`. |
| `blocked_by=recognizer`, reason "not available right now" | Transient (model still downloading / speech services restarting) — retry shortly. |
| `blocked_by=audio` / exit 3 | The runner lacks Full Disk Access for the Voice Memos container (DEC-024) — grant it in System Settings ▸ Privacy & Security ▸ Full Disk Access, then **relaunch** the runner. |
| `blocked_by=probe`, `engine=legacy` | If `ATLAS_TRANSCRIBE_ENGINE=legacy` is set on macOS 26+, unset it — the OS no longer serves `SFSpeechRecognizer` there. On macOS < 26 read `probe_error`: the OS speech stack is failing despite healthy flags (restart, re-check Dictation). |
| `blocked_by=probe`, `engine=analyzer` | Unexpected — read `probe_error`. Verify with `say -o /tmp/t.aiff test && ./atlas-transcribe --no-cache /tmp/t.aiff`. |
| `probe=skipped` with `verdict=blocked` | `/usr/bin/say` synthesis failed, so the speech stack can't be verified. Test a manual transcription directly; if that works, investigate why `say` is broken. |
| ingest reports doctor `unknown` | The helper predates `--doctor` — rebuild it (`./build.sh`). Normal runs proceed anyway (fail-open); `--retry-stubs` won't attempt helper upgrades until the doctor reports `ready`. |
| doctor `ready` but one long/old memo exits 7 | Not a precondition problem — raise the watchdog for that run: `--first-result-grace 120` (and `--timeout` if needed). Only tune these **after** the doctor is green, so genuine stalls stay detectable. |

Known constraint worth restating: **TCC is keyed to the process context that
launches the binary.** Granting Speech access to your interactive terminal does
**not** cover the scheduled `atlas-nightly` runner, and a grant made while a
process is running does not reach that process — relaunch first. Grant both Full
Disk Access and Speech Recognition to the process that launches the helper (your
terminal, or the scheduler runner), then relaunch it.

## Build

Requires Apple's Swift toolchain (`/usr/bin/swiftc`, ships with the Command Line
Tools / Xcode) **with a macOS 26+ SDK**: the source references the
`SpeechAnalyzer`/`SpeechTranscriber`/`AssetInventory` symbols, so older SDKs
fail at compile time even though runtime `#available` checks select the legacy
engine on pre-26 systems. (Single-machine constraint accepted; building a
legacy-only binary for an old-SDK machine would mean stripping the analyzer
sections.) From this directory:

```bash
./build.sh
```

That runs:

```bash
/usr/bin/swiftc -O \
  -framework Foundation -framework Speech -framework AVFoundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist \
  -o atlas-transcribe atlas-transcribe.swift
```

The `-sectcreate … __info_plist` flags embed `Info.plist` into the binary's
`__TEXT,__info_plist` section. macOS reads `NSSpeechRecognitionUsageDescription`
from there to show the permission prompt — without it a bare CLI binary cannot
request Speech access. Re-run `./build.sh` whenever the `.swift` source changes.

## Caching

Transcripts are cached keyed by **absolute audio path + file mtime + locale**, so
re-invoking on an unchanged file returns the cached transcript and **never
re-transcribes** — a nightly re-run is a no-op (DEC-023). Editing the audio (new
mtime) or changing `--locale` invalidates the entry and re-transcribes.

- **Location:** `~/Library/Caches/atlas-transcribe/` by default; override with
  `ATLAS_TRANSCRIBE_CACHE`. One small JSON record per audio file
  (`<hash>.json` = `{path, mtime, locale, transcript}`).
- `--no-cache` bypasses both read and write.
- `ATLAS_TRANSCRIBE_TIMEOUT` (seconds, default 120) caps how long a single
  transcription may run; `ATLAS_TRANSCRIBE_FIRST_RESULT_GRACE` (default 45) is
  the no-progress stall watchdog. The `--timeout` / `--first-result-grace`
  flags override the env vars.

## Guarantees

- **On-device only.** The analyzer engine transcribes locally via
  `SpeechAnalyzer`; the legacy engine sets `requiresOnDeviceRecognition = true`
  on every request; both refuse (exit 5) if no on-device model exists rather
  than reaching the cloud (DEC-002). No audio or transcript leaves the machine.
- **Stable cache key.** Uses a deterministic FNV-1a hash of the path (Swift's
  built-in `Hasher` is per-process randomized and unusable for filenames); the
  full path is stored in the record so a hash collision on a different path is
  detected and bypassed.
- **Stable doctor contract.** The `--doctor` key=value output above is v1 and
  parsed mechanically by `ingest.py` and its tests; extend it additively.
