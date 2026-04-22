import AVFoundation
import Accelerate
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sync")

/// Owner of all audio-I/O for mutual chirp sync. Runs its own
/// `AVAudioEngine` (input tap + `AVAudioPlayerNode`) and is **fully
/// decoupled from `AVCaptureSession`**. Since sync startup no longer has
/// to wait for the capture session to cold-boot, we go from 14–23 s to
/// ~1 s from `beginSync` to the first mic buffer reaching the buffer.
///
/// **Phase A architecture (2026-04-22):** this class does NOT run any
/// matched-filter detection anymore. It faithfully records the full
/// listening window as raw PCM, packages it as a 16-bit mono WAV, and
/// hands the bytes plus timing metadata back to the caller via
/// `onRecordingComplete`. The caller POSTs the WAV to
/// `/sync/audio_upload`, where server-side detection runs and feeds
/// the existing mutual-sync state machine. This trades ~1 MB of LAN
/// upload + ~1 s of round-trip latency for:
///   - Detection-algorithm iteration without iOS rebuild + deploy.
///   - Every failed attempt persists its raw bytes to
///     `data/sync_audio/` for offline replay + offline tuning.
///   - Eliminates ~2000 LOC of Swift matched-filter / CFAR code.
///
/// Lifecycle per sync run:
///   `beginSync` → activate AVAudioSession (.playAndRecord, .measurement,
///     .defaultToSpeaker) → engine.prepare → install input tap
///     (appending to buffer instead of feeding detector) → engine.start
///     → schedule chirp playback (~300 ms later so the tap is live)
///     → after `recordingDurationS`: stop tap, build WAV, fire
///       `onRecordingComplete`, teardown.
///   `endSync` → early teardown (operator cancel / watchdog timeout),
///     no `onRecordingComplete`.
final class MutualSyncAudio {
    /// Payload handed to the caller once the 3 s listen window closes.
    /// `wavData` is a standard 16-bit mono PCM WAV file the server can
    /// decode with the stdlib `wave` module. `audioStartPtsS` is the
    /// host-clock time of the first recorded sample, used on the server
    /// to map detected chirp sample offsets back to iOS session-clock
    /// time (same clock as video frame PTS, since
    /// `AVCaptureSession.masterClock` is `CMClockGetHostTimeClock()`).
    struct RecordingResult {
        let wavData: Data
        let sampleRate: Double
        let audioStartPtsS: Double
        /// Host-clock times at which each `player.play()` was invoked.
        /// One entry per scheduled emission (matches `emitAtS` count).
        /// Server cross-checks these against detected self-chirp centers.
        let emissionPtsS: [Double]
    }

    private let emitAtS: [Double]       // emission offsets relative to engine start
    private let recordingDurationS: TimeInterval
    private let chirpDurationS: Double
    private let bandAF0: Double
    private let bandAF1: Double
    private let bandBF0: Double
    private let bandBF1: Double
    private let amplitude: Float = 0.7

    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var tapInstalled = false
    private var running = false
    private let stateQueue = DispatchQueue(label: "audio.mutualsync.state")

    // Recording state — written from the tap thread, read from the
    // completion path on the same `stateQueue` (hopped there explicitly).
    private var recordedSamples: [Float] = []
    private var recordingSampleRate: Double = 0
    private var recordingFirstPTS: Double?
    private var emissionPTSList: [Double] = []
    private var completionFired = false

    /// - Parameters:
    ///   - emitAtS: Emission offsets (seconds from engine-start) for each burst.
    ///             Defaults to a single emission at 0.3 s (legacy behaviour).
    ///             Server pushes this per-camera in the WS sync_run message.
    ///   - recordingDurationS: Total mic recording window. Must exceed the last
    ///             emission offset + chirp duration + max propagation time.
    init(
        emitAtS: [Double] = [0.3],
        recordingDurationS: TimeInterval = 3.0,
        chirpDurationS: Double = 0.1,
        bandAF0: Double = 2000.0,
        bandAF1: Double = 4000.0,
        bandBF0: Double = 5000.0,
        bandBF1: Double = 7000.0
    ) {
        self.emitAtS = emitAtS.isEmpty ? [0.3] : emitAtS
        self.recordingDurationS = recordingDurationS
        self.chirpDurationS = chirpDurationS
        self.bandAF0 = bandAF0
        self.bandAF1 = bandAF1
        self.bandBF0 = bandBF0
        self.bandBF1 = bandBF1
    }

    /// Start the engine, install the mic tap, and schedule one chirp in
    /// `emittedRole`'s band. `onEmitted` fires after the player has
    /// scheduled the buffer; `onRecordingComplete` fires once
    /// `recordingDurationS` has elapsed from the first recorded sample
    /// with a WAV-encoded payload ready to upload. Callbacks both run
    /// on the main queue.
    ///
    /// Idempotent: calling twice without `endSync` in between is a no-op
    /// on the second call.
    func beginSync(
        emittedRole: String,
        onEmitted: (() -> Void)? = nil,
        onRecordingComplete: @escaping (RecordingResult) -> Void,
        onError: @escaping (String) -> Void
    ) {
        stateQueue.sync {
            guard !running else {
                log.warning("mutual-sync audio beginSync while already running — ignoring")
                return
            }
            running = true
            recordedSamples.removeAll(keepingCapacity: true)
            recordingFirstPTS = nil
            emissionPTSList.removeAll()
            completionFired = false
        }

        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()

        do {
            // Configure the audio session first so the engine picks up the
            // mic + speaker route we actually want. `.measurement` disables
            // AGC / beamforming / voice processing — critical for getting a
            // flat-response chirp recording; iOS defaults crush 2–8 kHz
            // sweeps by up to 30 dB (see CameraViewController's capture
            // session config for the equivalent fix on the other path).
            // `.defaultToSpeaker` routes output through the main speaker so
            // the peer 2–3 m away can hear the chirp — the earpiece is
            // too quiet for cross-device reception.
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.defaultToSpeaker, .allowBluetoothA2DP]
            )
            try session.setActive(true, options: [])

            engine.attach(player)
            let mixerFormat = engine.mainMixerNode.outputFormat(forBus: 0)
            engine.connect(player, to: engine.mainMixerNode, format: mixerFormat)

            // Install the mic tap. Buffer size 1024 at 44.1/48 kHz is ~21-23
            // ms per buffer. Format `nil` = tap bus's native format.
            let inputNode = engine.inputNode
            let inputFormat = inputNode.outputFormat(forBus: 0)
            inputNode.installTap(
                onBus: 0,
                bufferSize: 1024,
                format: inputFormat
            ) { [weak self] buffer, when in
                self?.ingestTapBuffer(buffer, at: when)
            }
            self.tapInstalled = true

            try engine.start()
        } catch {
            log.error("mutual-sync audio beginSync failed: \(error.localizedDescription, privacy: .public)")
            let message = error.localizedDescription
            self.teardown()
            stateQueue.sync { running = false }
            DispatchQueue.main.async { onError(message) }
            return
        }

        self.engine = engine
        self.player = player

        // Synthesise the emission buffer in the player's connected format
        // so `scheduleBuffer` doesn't need an extra converter stage.
        guard let chirpBuffer = makeChirpBuffer(
            role: emittedRole,
            format: engine.mainMixerNode.outputFormat(forBus: 0)
        ) else {
            log.error("mutual-sync audio failed to build chirp buffer role=\(emittedRole, privacy: .public)")
            let err = "failed to build chirp buffer for role \(emittedRole)"
            self.teardown()
            stateQueue.sync { running = false }
            DispatchQueue.main.async { onError(err) }
            return
        }

        // Schedule each burst chirp at the server-specified offsets.
        // A handful of buffers accumulate before the first emission so
        // the tap is live when the self-hear arrives.
        let emitOffsets = self.emitAtS
        for (idx, offset) in emitOffsets.enumerated() {
            DispatchQueue.main.asyncAfter(deadline: .now() + offset) { [weak self] in
                guard let self else { return }
                guard let player = self.player, let engine = self.engine, engine.isRunning else {
                    if idx == emitOffsets.count - 1 { onEmitted?() }
                    return
                }
                guard let buf = self.makeChirpBuffer(role: emittedRole,
                    format: engine.mainMixerNode.outputFormat(forBus: 0)) else { return }
                player.scheduleBuffer(buf, at: nil, options: [], completionHandler: nil)
                if !player.isPlaying { player.play() }
                let hostCMTime = CMClockGetTime(CMClockGetHostTimeClock())
                self.stateQueue.async {
                    self.emissionPTSList.append(CMTimeGetSeconds(hostCMTime))
                }
                log.info("mutual-sync chirp emitted idx=\(idx) offset_s=\(offset, privacy: .public) role=\(emittedRole, privacy: .public)")
                if idx == emitOffsets.count - 1 { onEmitted?() }
            }
        }

        // Arm the recording-complete watchdog. Fires `recordingDurationS`
        // after the first sample arrives (scheduled from inside the tap
        // once we know when t=0 actually happens). If no sample ever
        // arrives, the outer VC's own watchdog handles that via endSync.
        armRecordingWatchdog(onRecordingComplete: onRecordingComplete)
    }

    /// Stop the player, uninstall the tap, stop the engine, deactivate the
    /// audio session. Idempotent — safe to call in both success and
    /// abort paths. Does NOT fire `onRecordingComplete` — those success
    /// callbacks are armed from inside the tap via the watchdog.
    func endSync() {
        let shouldTeardown = stateQueue.sync { () -> Bool in
            guard running else { return false }
            running = false
            return true
        }
        if !shouldTeardown { return }
        teardown()
    }

    // MARK: - Private

    private func ingestTapBuffer(_ buffer: AVAudioPCMBuffer, at audioTime: AVAudioTime) {
        let rate = buffer.format.sampleRate
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0, rate > 0,
              let channelData = buffer.floatChannelData else { return }

        // Flatten to mono. iPhone mic is typically 1-ch but defensive mean
        // over channels handles an older 2-ch format without a crash.
        let channels = Int(buffer.format.channelCount)
        var mono = [Float](repeating: 0, count: frameCount)
        if channels == 1 {
            let src = channelData[0]
            for i in 0..<frameCount { mono[i] = src[i] }
        } else {
            let gain = 1.0 / Float(channels)
            for i in 0..<frameCount {
                var sum: Float = 0
                for c in 0..<channels { sum += channelData[c][i] }
                mono[i] = sum * gain
            }
        }

        // Host ticks → host-clock seconds via the documented bridge.
        let bufferStartS: Double
        if audioTime.isHostTimeValid {
            let hostCMTime = CMClockMakeHostTimeFromSystemUnits(audioTime.hostTime)
            bufferStartS = CMTimeGetSeconds(hostCMTime)
        } else {
            let hostCMTime = CMClockGetTime(CMClockGetHostTimeClock())
            bufferStartS = CMTimeGetSeconds(hostCMTime)
        }

        stateQueue.async { [weak self] in
            guard let self, self.running else { return }
            if self.recordingFirstPTS == nil {
                self.recordingFirstPTS = bufferStartS
                self.recordingSampleRate = rate
            }
            self.recordedSamples.append(contentsOf: mono)
        }
    }

    private func armRecordingWatchdog(
        onRecordingComplete: @escaping (RecordingResult) -> Void
    ) {
        // Poll the firstPTS state at 50 ms intervals; as soon as the first
        // buffer has landed, schedule the completion fire for
        // `recordingDurationS` from that moment. This keeps the watchdog
        // aligned to actual mic-first-sample time rather than
        // `beginSync`-wallclock (which would include engine.start latency).
        func pollForFirstSample() {
            stateQueue.async { [weak self] in
                guard let self, self.running else { return }
                if let first = self.recordingFirstPTS {
                    let elapsed = CMTimeGetSeconds(
                        CMClockGetTime(CMClockGetHostTimeClock())
                    ) - first
                    let remaining = max(0.05, self.recordingDurationS - elapsed)
                    DispatchQueue.main.asyncAfter(deadline: .now() + remaining) { [weak self] in
                        self?.finishRecording(
                            onRecordingComplete: onRecordingComplete
                        )
                    }
                    return
                }
                // No samples yet — retry in 50 ms.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    pollForFirstSample()
                }
            }
        }
        DispatchQueue.main.async { pollForFirstSample() }
    }

    private func finishRecording(
        onRecordingComplete: @escaping (RecordingResult) -> Void
    ) {
        var payload: RecordingResult?
        stateQueue.sync {
            guard running, !completionFired else { return }
            guard let firstPTS = recordingFirstPTS, recordingSampleRate > 0 else {
                return
            }
            completionFired = true
            let wav = Self.encodeWAV(
                samples: recordedSamples, sampleRate: recordingSampleRate
            )
            payload = RecordingResult(
                wavData: wav,
                sampleRate: recordingSampleRate,
                audioStartPtsS: firstPTS,
                emissionPtsS: emissionPTSList
            )
            running = false
        }
        if let payload {
            log.info("mutual-sync recording complete samples=\(self.recordedSamples.count, privacy: .public) duration_s=\(Double(self.recordedSamples.count) / max(1.0, self.recordingSampleRate), privacy: .public) wav_bytes=\(payload.wavData.count, privacy: .public)")
        }
        teardown()
        if let payload {
            DispatchQueue.main.async { onRecordingComplete(payload) }
        }
    }

    private func teardown() {
        if let player = self.player {
            if player.isPlaying { player.stop() }
        }
        if let engine = self.engine, tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
        }
        tapInstalled = false
        if let engine = self.engine, engine.isRunning {
            engine.stop()
        }
        self.player = nil
        self.engine = nil

        do {
            try AVAudioSession.sharedInstance().setActive(
                false,
                options: [.notifyOthersOnDeactivation]
            )
        } catch {
            // Expected when the capture session also holds the audio
            // session active — not an error condition, don't alarm logs.
            log.info("mutual-sync audio deactivate skipped: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Build a Hann-windowed linear chirp buffer in the given output
    /// format. Same phase + window math as the server-side reference so
    /// the emission and the detector's reference are spectrally
    /// identical.
    private func makeChirpBuffer(
        role: String,
        format: AVAudioFormat
    ) -> AVAudioPCMBuffer? {
        let f0: Double
        let f1: Double
        switch role {
        case "A": f0 = bandAF0; f1 = bandAF1
        case "B": f0 = bandBF0; f1 = bandBF1
        default:
            log.error("mutual-sync chirp unknown role=\(role, privacy: .public)")
            return nil
        }

        let sampleRate = format.sampleRate
        let n = Int(sampleRate * chirpDurationS)
        guard n > 1, let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(n)
        ) else { return nil }
        buffer.frameLength = AVAudioFrameCount(n)
        guard let channelData = buffer.floatChannelData else { return nil }
        let channels = Int(format.channelCount)
        let denom = 2.0 * chirpDurationS
        let invNm1 = 1.0 / Double(n - 1)
        for i in 0..<n {
            let t = Double(i) / sampleRate
            let phase = 2.0 * .pi * (f0 * t + (f1 - f0) * t * t / denom)
            let window = 0.5 * (1.0 - cos(2.0 * .pi * Double(i) * invNm1))
            let sample = Float(sin(phase) * window) * amplitude
            for c in 0..<channels {
                channelData[c][i] = sample
            }
        }
        return buffer
    }

    /// Encode an in-memory float32 PCM stream as a 16-bit mono WAV.
    /// Matches exactly what the server's `wave` module expects (RIFF
    /// little-endian, WAVE format 1 = PCM, 16-bit, 1 channel).
    static func encodeWAV(samples: [Float], sampleRate: Double) -> Data {
        let n = samples.count
        let byteRate = Int(sampleRate) * 2
        let dataBytes = n * 2
        var out = Data(capacity: 44 + dataBytes)

        func appendLE<T: FixedWidthInteger>(_ v: T) {
            var little = v.littleEndian
            withUnsafeBytes(of: &little) { out.append(contentsOf: $0) }
        }
        out.append(contentsOf: "RIFF".utf8)
        appendLE(UInt32(36 + dataBytes))
        out.append(contentsOf: "WAVE".utf8)
        out.append(contentsOf: "fmt ".utf8)
        appendLE(UInt32(16))              // fmt chunk size
        appendLE(UInt16(1))               // PCM format
        appendLE(UInt16(1))               // channels = mono
        appendLE(UInt32(Int(sampleRate))) // sample rate
        appendLE(UInt32(byteRate))        // byte rate
        appendLE(UInt16(2))               // block align
        appendLE(UInt16(16))              // bits per sample
        out.append(contentsOf: "data".utf8)
        appendLE(UInt32(dataBytes))
        // Convert float32 [-1,1] → Int16 PCM.
        out.reserveCapacity(out.count + dataBytes)
        for s in samples {
            let clipped = max(-1.0, min(1.0, s))
            let i16 = Int16(clipped * 32767.0)
            appendLE(i16)
        }
        return out
    }
}
