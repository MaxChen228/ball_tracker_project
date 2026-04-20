import AVFoundation
import Accelerate
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sync")

/// Owner of all audio-I/O for mutual chirp sync. Runs its own
/// `AVAudioEngine` (input tap + `AVAudioPlayerNode`) and is **fully
/// decoupled from `AVCaptureSession`**. This is the whole point of the
/// file: sync startup no longer has to wait for the capture session to
/// cold-boot, so we go from 14–23 s to ~1 s from `beginSync` to the first
/// mic buffer reaching the detector.
///
/// Lifecycle per sync run:
///   `beginSync` → activate AVAudioSession (.playAndRecord, .measurement,
///     .defaultToSpeaker) → engine.prepare → install input tap → engine.start
///     → schedule chirp playback (~300 ms later so the tap is live)
///   `endSync` → stop player, uninstall tap, stop engine, deactivate session
///
/// The engine is rebuilt per run rather than kept warm: `.measurement` mode
/// forces a specific hardware route and leaving the category active forever
/// interferes with other audio on the phone (Music, ringer, etc.). One-shot
/// activation per 5-second run is cheap and well-behaved.
///
/// Both the input tap and the player node live on the same engine instance.
/// `AVAudioEngine` explicitly supports an input node + attached player node
/// simultaneously — this is the canonical "listen while you speak" graph.
final class MutualSyncAudio {
    private let detector: AudioSyncDetector
    private let emissionDelayS: TimeInterval
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

    init(
        detector: AudioSyncDetector,
        emissionDelayS: TimeInterval = 0.3,
        chirpDurationS: Double = 0.1,
        bandAF0: Double = 2000.0,
        bandAF1: Double = 4000.0,
        bandBF0: Double = 5000.0,
        bandBF1: Double = 7000.0
    ) {
        self.detector = detector
        self.emissionDelayS = emissionDelayS
        self.chirpDurationS = chirpDurationS
        self.bandAF0 = bandAF0
        self.bandAF1 = bandAF1
        self.bandBF0 = bandBF0
        self.bandBF1 = bandBF1
    }

    /// Start the engine, install the mic tap, and schedule one chirp in
    /// `emittedBand`. `onEmitted` fires after the player has scheduled the
    /// buffer (not after it finishes — caller generally doesn't care). No
    /// callback is wired here for detections; the caller reads those
    /// directly via `detector.onDetection`.
    ///
    /// Idempotent: calling twice without `endSync` in between is a no-op
    /// on the second call.
    func beginSync(
        emittedRole: String,
        onEmitted: (() -> Void)? = nil
    ) {
        stateQueue.sync {
            guard !running else {
                log.warning("mutual-sync audio beginSync while already running — ignoring")
                return
            }
            running = true
        }

        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()

        do {
            // Configure the audio session first so the engine picks up the
            // mic + speaker route we actually want. `.measurement` disables
            // AGC / beamforming (critical for flat matched-filter input).
            // `.defaultToSpeaker` routes output through the main speaker so
            // the peer 2-3 m away can hear the chirp — the earpiece is
            // too quiet for cross-device reception.
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.defaultToSpeaker, .allowBluetoothA2DP]
            )
            try session.setActive(true, options: [])

            // Build the graph: player → mainMixer → output (speaker).
            // The input node is implicit — we attach a tap to it directly.
            engine.attach(player)
            let mixerFormat = engine.mainMixerNode.outputFormat(forBus: 0)
            engine.connect(player, to: engine.mainMixerNode, format: mixerFormat)

            // Install the mic tap. Buffer size 1024 at 44.1/48 kHz is ~21-23
            // ms per buffer — well below the ~33 ms matched-filter scan
            // period. Format `nil` = use the tap bus's native format.
            let inputNode = engine.inputNode
            let inputFormat = inputNode.outputFormat(forBus: 0)
            let weakDetector = self.detector
            inputNode.installTap(
                onBus: 0,
                bufferSize: 1024,
                format: inputFormat
            ) { buffer, when in
                weakDetector.ingestPCMBuffer(buffer, at: when)
            }
            self.tapInstalled = true

            try engine.start()
        } catch {
            log.error("mutual-sync audio beginSync failed: \(error.localizedDescription, privacy: .public)")
            self.teardown()
            stateQueue.sync { running = false }
            return
        }

        self.engine = engine
        self.player = player

        // Synthesise the emission buffer in the player's connected format so
        // `scheduleBuffer` doesn't need an extra converter stage. Player
        // gets the node's output format (= mainMixer output format).
        guard let chirpBuffer = makeChirpBuffer(
            role: emittedRole,
            format: engine.mainMixerNode.outputFormat(forBus: 0)
        ) else {
            log.error("mutual-sync audio failed to build chirp buffer role=\(emittedRole, privacy: .public)")
            onEmitted?()
            return
        }

        // Emission is deliberately delayed so the input tap has a handful of
        // buffers in the ring before the self-hear lands. The matched filter
        // only fires once the ring is fully populated (~2 s at 44.1 kHz);
        // at 300 ms we're well short, but the self-hear peak itself arrives
        // with enough preceding audio to correlate cleanly.
        DispatchQueue.main.asyncAfter(deadline: .now() + emissionDelayS) { [weak self] in
            guard let self else { return }
            guard let player = self.player, let engine = self.engine, engine.isRunning else {
                onEmitted?()
                return
            }
            player.scheduleBuffer(chirpBuffer, at: nil, options: [], completionHandler: nil)
            if !player.isPlaying { player.play() }
            log.info("mutual-sync chirp emitted role=\(emittedRole, privacy: .public) duration_s=\(self.chirpDurationS, privacy: .public)")
            onEmitted?()
        }
    }

    /// Stop the player, uninstall the tap, stop the engine, deactivate the
    /// audio session. Idempotent — safe to call in both success and abort
    /// paths.
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

        // Release the session so the phone can idle without `.measurement`
        // locking the route. `notifyOthersOnDeactivation` is the polite bit
        // that wakes Music/etc. back up if they were ducked.
        do {
            try AVAudioSession.sharedInstance().setActive(
                false,
                options: [.notifyOthersOnDeactivation]
            )
        } catch {
            log.error("mutual-sync audio deactivate failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Build a Hann-windowed linear chirp buffer in the given output format.
    /// Same phase + window math as `AudioChirpDetector.makeChirp` so the
    /// emission and the detector's reference are spectrally identical.
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
}
