import AVFoundation
import Accelerate
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sync")

/// Plays a single Hann-windowed linear chirp in the phone's own sync band
/// (A or B), once per mutual-sync run. Uses `AVAudioEngine` +
/// `AVAudioPlayerNode` so output lands through the device speaker routed
/// alongside the existing `AVCaptureAudioDataOutput` mic capture.
///
/// **Emission-time precision is deliberately NOT a concern**. The mutual-
/// sync algebra reads the **mic-side** timestamp (`AVCaptureSession.masterClock`
/// PTS of the self-heard peak detected by `AudioSyncDetector`) — not the
/// scheduled playback time. AVAudioEngine's output-latency jitter (~5–20 ms,
/// device-dependent) therefore has zero effect on the solved Δ. The emitter
/// only needs to actually get sound into the air.
///
/// `AVAudioSession` is configured once at first use with
/// `.playAndRecord` + `.measurement`. `.measurement` disables AGC +
/// beamforming, which is what the matched filter wants on the mic side.
/// `.defaultToSpeaker` forces playback through the main speaker instead
/// of the earpiece so the peer phone 2-3 m away can actually hear it.
final class ChirpEmitter {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var bufferA: AVAudioPCMBuffer?
    private var bufferB: AVAudioPCMBuffer?
    private var engineRunning = false

    /// Output amplitude scale applied on buffer synthesis. 0.7 gives plenty
    /// of SPL at typical speaker levels while staying below digital clip —
    /// clipped self-hear buffers would collapse matched-filter PSR.
    private let amplitude: Float = 0.7

    private let chirpDurationS: Double
    private let bandAF0: Double
    private let bandAF1: Double
    private let bandBF0: Double
    private let bandBF1: Double

    init(
        chirpDurationS: Double = 0.1,
        bandAF0: Double = 2000.0,
        bandAF1: Double = 4000.0,
        bandBF0: Double = 5000.0,
        bandBF1: Double = 7000.0
    ) {
        self.chirpDurationS = chirpDurationS
        self.bandAF0 = bandAF0
        self.bandAF1 = bandAF1
        self.bandBF0 = bandBF0
        self.bandBF1 = bandBF1
    }

    /// Emit the chirp corresponding to this phone's role. Idempotent with
    /// respect to engine setup; safe to call on the main thread.
    func emit(role: String, completion: (() -> Void)? = nil) {
        guard role == "A" || role == "B" else {
            log.error("chirp emit rejected role=\(role, privacy: .public)")
            completion?()
            return
        }
        do {
            try ensureEngineRunning()
        } catch {
            log.error("chirp emit engine start failed: \(error.localizedDescription, privacy: .public)")
            completion?()
            return
        }
        guard let buffer = bufferFor(role: role) else {
            log.error("chirp emit buffer missing for role=\(role, privacy: .public)")
            completion?()
            return
        }
        // Empty completion-handler block keeps the buffer alive for the
        // duration of playback (otherwise AVAudioEngine can release it
        // early on a subsequent schedule).
        player.scheduleBuffer(buffer, at: nil, options: []) {
            completion?()
        }
        if !player.isPlaying {
            player.play()
        }
        log.info("chirp emit role=\(role, privacy: .public) duration_s=\(self.chirpDurationS, privacy: .public)")
    }

    /// Stop the player node and tear the engine down. Call after a sync
    /// run completes so we're not holding `.playAndRecord` while idle —
    /// that category forces the lower-power `measurement` audio route and
    /// can interfere with other audio sources on the device.
    func shutdown() {
        if player.isPlaying {
            player.stop()
        }
        if engine.isRunning {
            engine.stop()
        }
        engineRunning = false
    }

    // MARK: - Private

    private func ensureEngineRunning() throws {
        if engineRunning { return }

        // `.playAndRecord` is needed to share the mic with
        // `AVCaptureAudioDataOutput`; `.measurement` mode turns off AGC
        // and directional beamforming so the mic stream stays flat —
        // critical for the matched filter. `.defaultToSpeaker` routes
        // output to the main speaker instead of the earpiece.
        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(
            .playAndRecord,
            mode: .measurement,
            options: [.defaultToSpeaker, .allowBluetoothA2DP]
        )
        try audioSession.setActive(true, options: [])

        // Engine graph: player → mainMixer → output. Attach + connect is
        // idempotent as long as the nodes only get attached once; we
        // guard on engineRunning so repeat calls are safe.
        if engine.attachedNodes.contains(player) == false {
            engine.attach(player)
            let format = engine.mainMixerNode.outputFormat(forBus: 0)
            engine.connect(player, to: engine.mainMixerNode, format: format)
            rebuildBuffers(format: format)
        }

        if !engine.isRunning {
            try engine.start()
        }
        engineRunning = true
    }

    private func rebuildBuffers(format: AVAudioFormat) {
        bufferA = Self.makeChirpBuffer(
            format: format,
            durationS: chirpDurationS,
            f0: bandAF0,
            f1: bandAF1,
            amplitude: amplitude
        )
        bufferB = Self.makeChirpBuffer(
            format: format,
            durationS: chirpDurationS,
            f0: bandBF0,
            f1: bandBF1,
            amplitude: amplitude
        )
    }

    private func bufferFor(role: String) -> AVAudioPCMBuffer? {
        switch role {
        case "A": return bufferA
        case "B": return bufferB
        default: return nil
        }
    }

    /// Build an `AVAudioPCMBuffer` containing one Hann-windowed linear
    /// chirp at the given `[f0, f1]` band. Same phase + window math as
    /// `AudioChirpDetector.makeChirp` so the reference and emission are
    /// spectrally identical.
    private static func makeChirpBuffer(
        format: AVAudioFormat,
        durationS: Double,
        f0: Double,
        f1: Double,
        amplitude: Float
    ) -> AVAudioPCMBuffer? {
        let sampleRate = format.sampleRate
        let n = Int(sampleRate * durationS)
        guard n > 1, let buffer = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(n)
        ) else { return nil }
        buffer.frameLength = AVAudioFrameCount(n)
        guard let channelData = buffer.floatChannelData else { return nil }
        let channels = Int(format.channelCount)
        let denom = 2.0 * durationS
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
