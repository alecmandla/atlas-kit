// atlas-transcribe — on-device transcription of a single audio file.
//
// Part of M8 (atlas-voice-memos-ingest). This is the ONE sanctioned native,
// no-pip, on-device exception to the stdlib-only rule (DEC-023). It wraps
// Apple's speech stack so no audio or transcript ever leaves the machine
// (DEC-002).
//
// Engines (auto-selected):
//   - analyzer  macOS 26+: SpeechAnalyzer/SpeechTranscriber — the supported
//               on-device path on Tahoe. On this OS the legacy engine
//               silently stalls (its recognitionTask never fires a callback)
//               even when authorization, availability, and
//               supportsOnDeviceRecognition all read healthy — the 2026-07
//               "24 identical exit-7 stalls" root cause.
//   - legacy    macOS < 26: SFSpeechRecognizer with
//               requiresOnDeviceRecognition = true.
//   Override with ATLAS_TRANSCRIBE_ENGINE=legacy (diagnosis only).
//
// Usage:
//   atlas-transcribe <audio-path> [--locale en-US] [--no-cache]
//                    [--timeout N] [--first-result-grace N] [--help]
//   atlas-transcribe --doctor [<audio-path>] [--locale en-US]
//                    [--request-auth] [--install-model]
//
// Behaviour:
//   - Prints the transcript to stdout on success, exits 0.
//   - Exits non-zero with a clear, single-line stderr message on any failure.
//   - Caches the transcript keyed by (absolute audio path + mtime + locale):
//     re-invoking on an unchanged file returns the cached transcript and never
//     re-transcribes (DEC-023). A nightly re-run is therefore a no-op.
//   - On-device only. If the locale has no on-device model the tool FAILS
//     rather than silently falling back to cloud recognition (DEC-002).
//
// --doctor preflight (DEC-033): reports each precondition on-device
// transcription needs as stable machine-readable key=value lines on stdout
// (see doctor() below for the contract), including a FUNCTIONAL PROBE — a
// short `say`-synthesized clip is actually transcribed — because the status
// flags alone provably lie (see the engine note above). Exits 0 when ready /
// the matching failure code when blocked. Never touches real audio.
//   --request-auth   trigger the one-time Speech permission prompt when
//                    authorization is notDetermined (interactive use only;
//                    without it the doctor never blocks on a prompt).
//   --install-model  download/reserve the locale's on-device model via
//                    AssetInventory (analyzer engine; model data comes from
//                    Apple, audio still never leaves the machine).
//
// Invoke by ABSOLUTE PATH from ingest.py (never via python3) — DEC-021/DEC-023.
//
// Exit codes:
//   0  success (transcript on stdout; doctor: verdict=ready)
//   2  usage error (bad/missing args)
//   3  audio file not found / unreadable (doctor: blocked_by=audio)
//   4  Speech recognition not authorized (doctor: blocked_by=speech-auth)
//   5  recognizer unavailable, or no on-device model for the locale
//      (doctor: blocked_by=recognizer / blocked_by=model)
//   6  recognition failed (corrupt audio, decode error, etc.)
//      (doctor: blocked_by=probe — the functional probe failed or could not run)
//   7  timed out waiting for recognition (doctor: blocked_by=probe — stall)
//
// An empty transcript is NOT a failure: a recording with no speech prints
// nothing and exits 0 (both engines; the result is cached like any other).

import AVFoundation
import Foundation
import Speech

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

let toolName = "atlas-transcribe"

func die(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write(Data("\(toolName): \(message)\n".utf8))
    exit(code)
}

func emitUsage(_ code: Int32) -> Never {
    let usage = """
    usage: \(toolName) <audio-path> [--locale en-US] [--no-cache]
                       [--timeout N] [--first-result-grace N] [--help]
           \(toolName) --doctor [<audio-path>] [--locale en-US]
                       [--request-auth] [--install-model]

    On-device transcription of one audio file (DEC-023). Prints transcript to
    stdout. Caches by audio path + mtime; re-runs on an unchanged file are a
    no-op. On-device only — never sends audio to the cloud (DEC-002).

    --doctor reports each transcription precondition as machine-readable
    key=value lines (stable contract, DEC-033) — including a functional probe
    that transcribes a short synthesized clip — and exits 0 ready / non-zero
    blocked, without touching real audio. --request-auth additionally triggers
    the one-time Speech permission prompt when authorization is notDetermined
    (interactive use only). --install-model downloads/reserves the locale's
    on-device model (macOS 26+ analyzer engine).

    Flags override the matching environment variable:
      --timeout N              overall ceiling, seconds (default 120)
      --first-result-grace N   fail fast if no result streams within N s (default 45)

    Environment:
      ATLAS_TRANSCRIBE_CACHE    cache directory (default ~/Library/Caches/\(toolName))
      ATLAS_TRANSCRIBE_TIMEOUT  overall ceiling, seconds (default 120)
      ATLAS_TRANSCRIBE_FIRST_RESULT_GRACE
                                fail fast if no result streams within N s (default 45)
      ATLAS_TRANSCRIBE_ENGINE   force 'legacy' (diagnosis only; the analyzer
                                engine is auto-selected on macOS 26+)
    """
    if code == 0 {
        print(usage)
    } else {
        FileHandle.standardError.write(Data((usage + "\n").utf8))
    }
    exit(code)
}

// Deterministic, stable 64-bit FNV-1a hash of a string (Swift's Hasher is
// per-process randomized, so it can't name cache files). Used only to derive a
// short, collision-resistant cache filename; the full path is stored inside the
// cache record so a (vanishingly unlikely) collision is detected and bypassed.
func fnv1a(_ s: String) -> String {
    var hash: UInt64 = 0xcbf29ce484222325
    let prime: UInt64 = 0x100000001b3
    for byte in s.utf8 {
        hash ^= UInt64(byte)
        hash = hash &* prime
    }
    return String(format: "%016llx", hash)
}

// A transcription failure that maps 1:1 onto the tool's exit-code contract.
struct TranscribeFailure: Error {
    let code: Int32
    let message: String
}

// Lock-guarded mutable cell. Its methods are synchronous, so it is safe to
// call from async contexts (direct NSLock lock()/unlock() there is an error
// under the Swift 6 language mode).
final class SyncBox<T> {
    private var value: T
    private let lock = NSLock()
    init(_ initial: T) { value = initial }
    func get() -> T { lock.lock(); defer { lock.unlock() }; return value }
    func set(_ newValue: T) { lock.lock(); defer { lock.unlock() }; value = newValue }
}

// Block the main thread on an async operation (top-level CLI — no run loop).
func runBlocking<T>(_ op: @escaping () async throws -> T) -> Result<T, Error> {
    let sem = DispatchSemaphore(value: 0)
    var result: Result<T, Error> = .failure(TranscribeFailure(code: 6, message: "async op never completed"))
    Task {
        do { result = .success(try await op()) } catch { result = .failure(error) }
        sem.signal()
    }
    sem.wait()
    return result
}

func runBlockingNoThrow<T>(_ op: @escaping () async -> T) -> T {
    let sem = DispatchSemaphore(value: 0)
    var result: T!
    Task {
        result = await op()
        sem.signal()
    }
    sem.wait()
    return result
}

// ---------------------------------------------------------------------------
// Argument parsing
// ---------------------------------------------------------------------------

var audioPath: String?
var locale = "en-US"
var useCache = true
var doctorMode = false
var requestAuthFlag = false
var installModelFlag = false
var timeoutFlag: Double?
var firstResultGraceFlag: Double?

func parseSeconds(_ flag: String, _ raw: String) -> Double {
    guard let v = Double(raw), v > 0 else { die(2, "\(flag) requires a positive number of seconds") }
    return v
}

var argv = Array(CommandLine.arguments.dropFirst())
var i = 0
while i < argv.count {
    let arg = argv[i]
    switch arg {
    case "--help", "-h":
        emitUsage(0)
    case "--no-cache":
        useCache = false
    case "--doctor":
        doctorMode = true
    case "--request-auth":
        requestAuthFlag = true
    case "--install-model":
        installModelFlag = true
    case "--locale":
        i += 1
        guard i < argv.count else { die(2, "--locale requires a value (e.g. en-US)") }
        locale = argv[i]
    case "--timeout":
        i += 1
        guard i < argv.count else { die(2, "--timeout requires a value (seconds)") }
        timeoutFlag = parseSeconds("--timeout", argv[i])
    case "--first-result-grace":
        i += 1
        guard i < argv.count else { die(2, "--first-result-grace requires a value (seconds)") }
        firstResultGraceFlag = parseSeconds("--first-result-grace", argv[i])
    default:
        if arg.hasPrefix("--locale=") {
            locale = String(arg.dropFirst("--locale=".count))
        } else if arg.hasPrefix("--timeout=") {
            timeoutFlag = parseSeconds("--timeout", String(arg.dropFirst("--timeout=".count)))
        } else if arg.hasPrefix("--first-result-grace=") {
            firstResultGraceFlag = parseSeconds("--first-result-grace",
                                                String(arg.dropFirst("--first-result-grace=".count)))
        } else if arg.hasPrefix("--") {
            die(2, "unknown option: \(arg)")
        } else if audioPath == nil {
            audioPath = arg
        } else {
            die(2, "unexpected extra argument: \(arg)")
        }
    }
    i += 1
}
if (requestAuthFlag || installModelFlag) && !doctorMode {
    die(2, "--request-auth / --install-model only apply to --doctor")
}

// ---------------------------------------------------------------------------
// Engine selection
// ---------------------------------------------------------------------------

enum Engine: String { case analyzer, legacy }

let engine: Engine = {
    if ProcessInfo.processInfo.environment["ATLAS_TRANSCRIBE_ENGINE"] == "legacy" { return .legacy }
    if #available(macOS 26.0, *) { return .analyzer }
    return .legacy
}()

// ---------------------------------------------------------------------------
// Watchdog timings
// ---------------------------------------------------------------------------

// Overall ceiling. On-device STT runs faster than realtime, so even a long
// memo finishes in well under a minute — a bigger value only delays giving up
// on a stall. Override via --timeout, else ATLAS_TRANSCRIBE_TIMEOUT.
let timeout: Double = {
    if let v = timeoutFlag { return v }
    if let s = ProcessInfo.processInfo.environment["ATLAS_TRANSCRIBE_TIMEOUT"],
       let v = Double(s), v > 0 { return v }
    return 120
}()

// Liveness watchdog: if NOTHING (no result, no error) arrives within this
// grace window, treat it as a stall and bail. Once any result streams we stop
// watching first-result and rely on `timeout`. Raise it (via flag or env) only
// after `--doctor` proves preconditions pass — e.g. for very old recordings —
// so genuine stalls stay detectable. Override via --first-result-grace, else
// ATLAS_TRANSCRIBE_FIRST_RESULT_GRACE.
let firstResultGrace: Double = {
    if let v = firstResultGraceFlag { return v }
    if let s = ProcessInfo.processInfo.environment["ATLAS_TRANSCRIBE_FIRST_RESULT_GRACE"],
       let v = Double(s), v > 0 { return v }
    return 45
}()

// ---------------------------------------------------------------------------
// Authorization (one-time grant — see README / FDA-SETUP.md)
// ---------------------------------------------------------------------------

func requestAuth() -> SFSpeechRecognizerAuthorizationStatus {
    let sem = DispatchSemaphore(value: 0)
    var status: SFSpeechRecognizerAuthorizationStatus = .notDetermined
    SFSpeechRecognizer.requestAuthorization { s in
        status = s
        sem.signal()
    }
    _ = sem.wait(timeout: .now() + 15)
    return status
}

func authString(_ s: SFSpeechRecognizerAuthorizationStatus) -> String {
    switch s {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "notDetermined"
    @unknown default: return "unknown"
    }
}

// ---------------------------------------------------------------------------
// Transcription engines. Both are synchronous wrappers that share the same
// watchdog shape: poll in 1s slices; no first result within `grace` → stall
// (exit 7); `ceiling` caps the whole run.
// ---------------------------------------------------------------------------

func watchdogLoop(sem: DispatchSemaphore, sawAnyResult: () -> Bool,
                  grace: Double, ceiling: Double, path: String,
                  onAbort: () -> Void) throws {
    let started = Date()
    while true {
        if sem.wait(timeout: .now() + 1) == .success { return }
        let elapsed = Date().timeIntervalSince(started)
        if !sawAnyResult() && elapsed > grace {
            onAbort()
            throw TranscribeFailure(code: 7,
                message: "no transcription progress within \(Int(grace))s — recognizer stalled: \(path) "
                    + "(run '\(toolName) --doctor' to identify the blocked precondition)")
        }
        if elapsed > ceiling {
            onAbort()
            throw TranscribeFailure(code: 7,
                message: "timed out after \(Int(ceiling))s waiting for transcription of \(path)")
        }
    }
}

// --- analyzer engine (macOS 26+) ---------------------------------------------

@available(macOS 26.0, *)
func makeTranscriber(_ localeId: String) -> SpeechTranscriber {
    SpeechTranscriber(locale: Locale(identifier: localeId),
                      transcriptionOptions: [],
                      reportingOptions: [],
                      attributeOptions: [])
}

@available(macOS 26.0, *)
func analyzerLocaleStatus(_ localeId: String) async -> (supported: Bool, installed: Bool) {
    let want = Locale(identifier: localeId).identifier(.bcp47).lowercased()
    let supported = await SpeechTranscriber.supportedLocales
        .contains { $0.identifier(.bcp47).lowercased() == want }
    let installed = await SpeechTranscriber.installedLocales
        .contains { $0.identifier(.bcp47).lowercased() == want }
    return (supported, installed)
}

@available(macOS 26.0, *)
func ensureAnalyzerAssets(_ localeId: String) async throws {
    // Reserves (and if needed downloads) the locale's model for THIS binary.
    // When the model is already installed system-wide this is a fast local
    // reservation, not a network download.
    if let req = try await AssetInventory.assetInstallationRequest(supporting: [makeTranscriber(localeId)]) {
        try await req.downloadAndInstall()
    }
}

@available(macOS 26.0, *)
func analyzerTranscribe(path: String, localeId: String, onProgress: @escaping () -> Void) async throws -> String {
    let transcriber = makeTranscriber(localeId)
    try await ensureAnalyzerAssets(localeId)
    let audioFile = try AVAudioFile(forReading: URL(fileURLWithPath: path))
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    async let done: () = {
        if let lastSample = try await analyzer.analyzeSequence(from: audioFile) {
            try await analyzer.finalizeAndFinish(through: lastSample)
        } else {
            await analyzer.cancelAndFinishNow()
        }
    }()
    var pieces: [String] = []
    for try await result in transcriber.results {
        onProgress()
        pieces.append(String(result.text.characters))
    }
    try await done
    return pieces.joined().trimmingCharacters(in: .whitespacesAndNewlines)
}

@available(macOS 26.0, *)
func analyzerTranscribeSync(path: String, localeId: String, grace: Double, ceiling: Double) throws -> String {
    let (supported, installed) = runBlockingNoThrow { await analyzerLocaleStatus(localeId) }
    guard supported else {
        throw TranscribeFailure(code: 5,
            message: "no on-device speech model exists for locale '\(localeId)' — refusing cloud recognition (DEC-002); pass a supported --locale")
    }
    guard installed else {
        throw TranscribeFailure(code: 5,
            message: "on-device speech model for '\(localeId)' is not installed — run '\(toolName) --doctor --install-model --locale \(localeId)'")
    }

    let sem = DispatchSemaphore(value: 0)
    let transcript = SyncBox<String?>(nil)
    let failure = SyncBox<Error?>(nil)
    let sawAnyResult = SyncBox(false)
    let task = Task {
        do {
            let text = try await analyzerTranscribe(path: path, localeId: localeId) {
                sawAnyResult.set(true)
            }
            transcript.set(text)
        } catch {
            failure.set(error)
        }
        sem.signal()
    }
    try watchdogLoop(sem: sem, sawAnyResult: { sawAnyResult.get() },
                     grace: grace, ceiling: ceiling, path: path,
                     onAbort: { task.cancel() })
    if let failure = failure.get() {
        throw TranscribeFailure(code: 6, message: "transcription failed: \(failure.localizedDescription)")
    }
    guard let text = transcript.get() else {
        throw TranscribeFailure(code: 6, message: "transcription produced no result for \(path)")
    }
    // May be empty: an honest no-speech recording exits 0 (engine parity with
    // legacy, and cacheable so retries quiesce).
    return text
}

// --- legacy engine (SFSpeechRecognizer; pre-26 macOS) -------------------------

func legacyTranscribe(path: String, localeId: String, grace: Double, ceiling: Double) throws -> String {
    let auth = requestAuth()
    switch auth {
    case .authorized:
        break
    case .denied:
        throw TranscribeFailure(code: 4, message: "Speech recognition denied. Grant it in System Settings > Privacy & Security > Speech Recognition for the runner, then retry.")
    case .restricted:
        throw TranscribeFailure(code: 4, message: "Speech recognition restricted on this device (MDM/parental controls).")
    case .notDetermined:
        throw TranscribeFailure(code: 4, message: "Speech recognition not yet authorized — the one-time permission prompt was not answered. Run '\(toolName) --doctor --request-auth' interactively to grant (see README).")
    @unknown default:
        throw TranscribeFailure(code: 4, message: "Speech recognition unavailable (unknown authorization status).")
    }

    guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeId)) else {
        throw TranscribeFailure(code: 5, message: "no speech recognizer for locale '\(localeId)'.")
    }
    guard recognizer.isAvailable else {
        throw TranscribeFailure(code: 5, message: "speech recognizer for '\(localeId)' is not available right now.")
    }
    guard recognizer.supportsOnDeviceRecognition else {
        throw TranscribeFailure(code: 5, message: "no on-device speech model for locale '\(localeId)' — refusing to use cloud recognition (DEC-002). Install the locale's dictation model or pass a supported --locale.")
    }

    let request = SFSpeechURLRecognitionRequest(url: URL(fileURLWithPath: path))
    request.requiresOnDeviceRecognition = true       // DEC-002: never the cloud.
    // Partial results are our liveness signal: their ABSENCE is how the
    // watchdog detects the silent-stall failure mode (a recognitionTask that
    // never fires its completion handler).
    request.shouldReportPartialResults = true
    if #available(macOS 13.0, *) {
        request.addsPunctuation = true
    }

    let sem = DispatchSemaphore(value: 0)
    let transcript = SyncBox<String?>(nil)
    let failure = SyncBox<String?>(nil)
    let sawAnyResult = SyncBox(false)

    let task = recognizer.recognitionTask(with: request) { result, error in
        if let error = error {
            failure.set(error.localizedDescription)
            sem.signal()
            return
        }
        guard let result = result else { return }
        sawAnyResult.set(true)
        if result.isFinal {
            transcript.set(result.bestTranscription.formattedString)
            sem.signal()
        }
    }
    try watchdogLoop(sem: sem, sawAnyResult: { sawAnyResult.get() },
                     grace: grace, ceiling: ceiling, path: path,
                     onAbort: { task.cancel() })
    if let failure = failure.get() {
        throw TranscribeFailure(code: 6, message: "transcription failed: \(failure)")
    }
    guard let text = transcript.get() else {
        throw TranscribeFailure(code: 6, message: "transcription produced no result for \(path)")
    }
    return text
}

func transcribe(path: String, localeId: String, grace: Double, ceiling: Double) throws -> String {
    if engine == .analyzer, #available(macOS 26.0, *) {
        return try analyzerTranscribeSync(path: path, localeId: localeId, grace: grace, ceiling: ceiling)
    }
    return try legacyTranscribe(path: path, localeId: localeId, grace: grace, ceiling: ceiling)
}

// ---------------------------------------------------------------------------
// Doctor preflight (DEC-033)
// ---------------------------------------------------------------------------
//
// Reports every precondition (never fail-fast — the point is one complete
// picture instead of N identical stalls) as key=value lines on stdout. The
// key set and value vocabulary are a STABLE CONTRACT parsed mechanically by
// ingest.py and its tests:
//
//   doctor=v1
//   engine=analyzer|legacy
//   locale=<locale identifier>
//   speech_auth=authorized|denied|restricted|notDetermined|unknown
//   model_supported=true|false      (analyzer engine)
//   model_installed=true|false      (analyzer engine)
//   recognizer_present=true|false   (legacy engine)
//   recognizer_available=true|false (legacy engine)
//   on_device_model=true|false      (legacy engine)
//   model_install_error=<line>      (only when --install-model was tried and failed)
//   audio_path=<path>               (only when an audio path was passed)
//   audio_readable=true|false       (only when an audio path was passed)
//   probe=pass|fail|skipped         (functional end-to-end check; `skipped`
//                                    while unblocked means the probe could not
//                                    run and the verdict is blocked)
//   probe_error=<single line>       (only when probe=fail)
//   verdict=ready|blocked
//   blocked_by=speech-auth|recognizer|model|audio|probe  (only when blocked)
//   reason=<single human-readable line>                  (only when blocked)
//
// Exit codes mirror the transcription failure classes: 0 ready, 4 speech-auth,
// 5 recognizer/model, 3 audio, 7 probe-stall, 6 probe failed / could not run.

// Synthesize a short known-content clip with /usr/bin/say for the functional
// probe. Returns nil when synthesis isn't possible (which the doctor treats as
// blocked — an unverifiable speech stack must not report ready). Cleans up its
// temp dir on every failure path; the success-path dir is removed by doctor().
func synthesizeProbeAudio() -> String? {
    let fm = FileManager.default
    let dir = NSTemporaryDirectory() + "atlas-transcribe-doctor-\(getpid())"
    try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true)
    func fail() -> String? { try? fm.removeItem(atPath: dir); return nil }
    let path = dir + "/probe.aiff"
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/say")
    p.arguments = ["-o", path, "hello world this is the atlas transcription preflight probe"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return fail() }
    let deadline = Date().addingTimeInterval(30)  // say normally takes <1s
    while p.isRunning && Date() < deadline { usleep(100_000) }
    if p.isRunning { p.terminate(); return fail() }
    guard p.terminationStatus == 0, fm.fileExists(atPath: path) else { return fail() }
    return path
}

func doctor() -> Never {
    var lines = ["doctor=v1", "engine=\(engine.rawValue)", "locale=\(locale)"]
    var blockedBy: String?
    var reason = ""
    var exitCode: Int32 = 0

    func block(_ by: String, _ code: Int32, _ why: String) {
        if blockedBy == nil { blockedBy = by; exitCode = code; reason = why }
    }

    // Authorization — read without prompting; TCC answers for THIS process
    // context, which is exactly what a helper spawned by the ingest would see.
    var auth = SFSpeechRecognizer.authorizationStatus()
    if auth == .notDetermined && requestAuthFlag {
        auth = requestAuth()  // one-time macOS prompt (interactive runs only)
    }
    lines.append("speech_auth=\(authString(auth))")

    if engine == .analyzer, #available(macOS 26.0, *) {
        // The analyzer engine's model inventory is authoritative (unlike the
        // legacy supportsOnDeviceRecognition flag, which lies on macOS 26).
        var (supported, installed) = runBlockingNoThrow { await analyzerLocaleStatus(locale) }
        if supported && !installed && installModelFlag {
            switch runBlocking({ try await ensureAnalyzerAssets(locale) }) {
            case .success:
                installed = runBlockingNoThrow { await analyzerLocaleStatus(locale) }.installed
            case .failure(let e):
                lines.append("model_install_error=\(String(describing: e).replacingOccurrences(of: "\n", with: " "))")
            }
        }
        lines.append("model_supported=\(supported)")
        lines.append("model_installed=\(installed)")
        if !supported {
            block("model", 5, "no on-device speech model exists for locale '\(locale)' — pass a supported --locale; cloud recognition is barred (DEC-002)")
        } else if !installed {
            block("model", 5, "on-device speech model for '\(locale)' is not installed — run '\(toolName) --doctor --install-model --locale \(locale)' (or System Settings > Keyboard > Dictation, add the language)")
        }
    } else {
        // Legacy engine: report its (weaker) status flags, and gate on auth —
        // SFSpeechRecognizer refuses to run unauthorized.
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: locale))
        let present = recognizer != nil
        let available = recognizer?.isAvailable ?? false
        let onDevice = recognizer?.supportsOnDeviceRecognition ?? false
        lines.append("recognizer_present=\(present)")
        lines.append("recognizer_available=\(available)")
        lines.append("on_device_model=\(onDevice)")
        if auth != .authorized {
            switch auth {
            case .denied:
                block("speech-auth", 4, "Speech Recognition is denied for this process context — enable it in System Settings > Privacy & Security > Speech Recognition (if it already shows enabled, this process predates the grant: relaunch the terminal/runner and re-run)")
            case .restricted:
                block("speech-auth", 4, "Speech Recognition is restricted on this device (MDM/parental controls)")
            default:
                block("speech-auth", 4, "Speech Recognition was never asked for this process context — run '\(toolName) --doctor --request-auth' interactively and approve the prompt")
            }
        } else if !present {
            block("recognizer", 5, "no speech recognizer exists for locale '\(locale)'")
        } else if !onDevice {
            block("model", 5, "no on-device speech model for locale '\(locale)' — download it via System Settings > Keyboard > Dictation (add the language); cloud recognition is barred (DEC-002)")
        } else if !available {
            block("recognizer", 5, "speech recognizer for '\(locale)' is not available right now (model still downloading?) — retry shortly")
        }
    }

    // Audio readability — only when a path was passed (container access is the
    // ingest's own FDA check; this probes one representative file by hand).
    if let rawPath = audioPath {
        let fm = FileManager.default
        let abs = (rawPath as NSString).isAbsolutePath
            ? (rawPath as NSString).standardizingPath
            : ((fm.currentDirectoryPath as NSString).appendingPathComponent(rawPath) as NSString).standardizingPath
        var isDir: ObjCBool = false
        let readable = fm.fileExists(atPath: abs, isDirectory: &isDir)
            && !isDir.boolValue && fm.isReadableFile(atPath: abs)
        lines.append("audio_path=\(abs)")
        lines.append("audio_readable=\(readable)")
        if !readable {
            block("audio", 3, "audio file not readable — check Full Disk Access for the runner (DEC-024)")
        }
    }

    // Functional probe — transcribe a short synthesized clip end-to-end. This
    // is the check that catches the macOS 26 legacy silent stall, where every
    // flag above reads healthy. Skipped when already blocked (its failure
    // would only echo the known blocker). When the probe CANNOT run, the
    // verdict is blocked, not ready — an unverifiable speech stack must never
    // wave a batch through into per-memo watchdog stalls.
    if blockedBy != nil {
        lines.append("probe=skipped")
    } else if let probePath = synthesizeProbeAudio() {
        defer { try? FileManager.default.removeItem(atPath: (probePath as NSString).deletingLastPathComponent) }
        func probeFailed(_ code: Int32, _ message: String, stalled: Bool) {
            lines.append("probe=fail")
            lines.append("probe_error=\(message.replacingOccurrences(of: "\n", with: " "))")
            // Attribute to speech-auth only on the legacy engine, which hard-
            // requires authorization; the analyzer engine does not, so there a
            // non-authorized status would be a misdiagnosis.
            if engine == .legacy && auth != .authorized {
                block("speech-auth", 4, "functional probe failed and Speech Recognition is \(authString(auth)) for this process context — grant it (System Settings > Privacy & Security > Speech Recognition), then relaunch the runner so a fresh process context picks up the grant")
            } else if engine == .legacy {
                block("probe", code, "all preconditions read healthy but a real transcription \(stalled ? "stalled" : "failed") on the legacy engine — if ATLAS_TRANSCRIBE_ENGINE=legacy is set on macOS 26+, unset it (the OS no longer serves SFSpeechRecognizer there); otherwise the OS speech stack is failing despite healthy flags: see probe_error")
            } else {
                block("probe", code, "all preconditions read healthy but a real transcription \(stalled ? "stalled" : "failed") on the analyzer engine — unexpected; see probe_error")
            }
        }
        // Bounded: a broken engine costs one short stall here instead of one
        // 45s stall per memo in the batch.
        let probeGrace = min(firstResultGrace, 30)
        do {
            let text = try transcribe(path: probePath, localeId: locale, grace: probeGrace, ceiling: min(timeout, 60))
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                // The probe clip contains known speech; silence back means the
                // engine is not actually recognizing.
                probeFailed(6, "probe transcription produced no text for a known-speech clip", stalled: false)
            } else {
                lines.append("probe=pass")
            }
        } catch let f as TranscribeFailure {
            probeFailed(f.code, f.message, stalled: f.code == 7)
        } catch {
            probeFailed(6, error.localizedDescription, stalled: false)
        }
    } else {
        lines.append("probe=skipped")
        block("probe", 6, "functional probe could not run (/usr/bin/say synthesis failed) — cannot verify the speech stack, refusing to report ready; test manually: \(toolName) --no-cache <some-audio-file>")
    }

    if let by = blockedBy {
        lines.append("verdict=blocked")
        lines.append("blocked_by=\(by)")
        lines.append("reason=\(reason)")
    } else {
        lines.append("verdict=ready")
    }
    print(lines.joined(separator: "\n"))
    exit(exitCode)
}

if doctorMode { doctor() }

// ---------------------------------------------------------------------------
// Transcribe one file
// ---------------------------------------------------------------------------

guard let rawPath = audioPath else { emitUsage(2) }

// Resolve to an absolute, standardized path for a stable cache key.
let fm = FileManager.default
let absPath = (rawPath as NSString).isAbsolutePath
    ? (rawPath as NSString).standardizingPath
    : ((fm.currentDirectoryPath as NSString).appendingPathComponent(rawPath) as NSString).standardizingPath

var isDir: ObjCBool = false
guard fm.fileExists(atPath: absPath, isDirectory: &isDir), !isDir.boolValue else {
    die(3, "audio file not found: \(absPath)")
}
guard fm.isReadableFile(atPath: absPath) else {
    die(3, "audio file not readable (check Full Disk Access on the runner): \(absPath)")
}

// mtime for the cache key.
let attrs = (try? fm.attributesOfItem(atPath: absPath)) ?? [:]
let mtime = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
let mtimeKey = String(format: "%.0f", mtime)

// ---------------------------------------------------------------------------
// Cache
// ---------------------------------------------------------------------------

let cacheDir: String = {
    if let override = ProcessInfo.processInfo.environment["ATLAS_TRANSCRIBE_CACHE"], !override.isEmpty {
        return (override as NSString).standardizingPath
    }
    let home = fm.homeDirectoryForCurrentUser.path
    return "\(home)/Library/Caches/\(toolName)"
}()

let cacheFile = "\(cacheDir)/\(fnv1a(absPath)).json"

struct CacheRecord: Codable {
    let path: String
    let mtime: String
    let locale: String
    let transcript: String
}

func readCache() -> String? {
    guard useCache, let data = fm.contents(atPath: cacheFile) else { return nil }
    guard let rec = try? JSONDecoder().decode(CacheRecord.self, from: data) else { return nil }
    // All three must match; mismatch (edited audio, different locale, hash
    // collision on a different path) bypasses the cache and re-transcribes.
    guard rec.path == absPath, rec.mtime == mtimeKey, rec.locale == locale else { return nil }
    return rec.transcript
}

func writeCache(_ transcript: String) {
    guard useCache else { return }
    try? fm.createDirectory(atPath: cacheDir, withIntermediateDirectories: true)
    let rec = CacheRecord(path: absPath, mtime: mtimeKey, locale: locale, transcript: transcript)
    if let data = try? JSONEncoder().encode(rec) {
        try? data.write(to: URL(fileURLWithPath: cacheFile))
    }
}

if let cached = readCache() {
    print(cached)
    exit(0)
}

do {
    let text = try transcribe(path: absPath, localeId: locale, grace: firstResultGrace, ceiling: timeout)
    writeCache(text)
    print(text)
    exit(0)
} catch let f as TranscribeFailure {
    die(f.code, f.message)
} catch {
    die(6, "transcription failed: \(error.localizedDescription)")
}
