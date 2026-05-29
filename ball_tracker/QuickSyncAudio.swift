import AVFoundation
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sync.quick")

/// Audio-I/O owner for **quick sync** (single-emitter, N-listener acoustic
/// time alignment). Like `MutualSyncAudio` it runs its own `AVAudioEngine`
/// fully decoupled from `AVCaptureSession`, records the full listening
/// window as 16-bit mono WAV, and hands the bytes + first-sample PTS back
/// to the caller, which POSTs them to `/sync/quick_audio_upload` for
/// server-side matched-filter detection.
///
/// Difference vs mutual:
///   - **One band only.** There is a single physical chirp (band A,
///     2000–4000 Hz). Every cam — emitter included — records and the
///     server matched-filters that one band off each WAV. The emitter's
///     self-hear anchor is the run's zero point.
///   - **Recording is unconditional; emission is opt-in.** A listener
///     passes `isEmitter: false` and never plays; only the emitter
///     schedules the band-A chirp at `emitAtS`. There is no role / band
///     selection — any cam can be the emitter (the N-cam enabler), because
///     the band is fixed.
///
/// `MutualSyncAudio` is deliberately left untouched (hardware-validated,
/// can't run iOS tests locally, slated for Phase-5 deletion). The only
/// shared code is `MutualSyncAudio.encodeWAV` (already `static`); when
/// mutual is deleted that encoder moves here and net duplication is zero.
final class QuickSyncAudio {
    /// Payload handed to the caller once the listen window closes. No
    /// `emissionPtsS` — quick upload never sends it (the server's zero
    /// point is the emitter's self-hear, not a reported emission time).
    struct RecordingResult {
        let wavData: Data
        let sampleRate: Double
        let audioStartPtsS: Double
    }

    /// Band A — fixed for quick sync regardless of which cam emits. Matches
    /// the server reference (`chirp.py` SYNC_BAND_A_F0/F1) and
    /// `MutualSyncAudio`'s role-"A" band so the detector's reference is
    /// spectrally identical.
    private let bandF0: Double = 2000.0
    private let bandF1: Double = 4000.0
    private let chirpDurationS: Double = 0.1
    // Same SPL rationale as MutualSyncAudio: Hann chirp peaks at amplitude
    // × 1.0; `.measurement` mode caps output 6–10 dB, so push close to 1.0.
    private let amplitude: Float = 0.95

    private let emitAtS: [Double]
    private let recordingDurationS: TimeInterval
    private let isEmitter: Bool

    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var tapInstalled = false
    private var running = false
    private let stateQueue = DispatchQueue(label: "audio.quicksync.state")

    private var recordedSamples: [Float] = []
    private var recordingSampleRate: Double = 0
    private var recordingFirstPTS: Double?
    private var completionFired = false

    /// - Parameters:
    ///   - recordingDurationS: Total mic recording window. Must exceed the
    ///         last emission offset + chirp duration + max propagation time.
    ///   - emitAtS: Engine-relative emission offsets. Emitter only; a
    ///         listener passes `[]` (no `precondition` — listeners legitimately
    ///         never emit, unlike mutual where every cam emits).
    ///   - isEmitter: true → schedule the band-A chirp at `emitAtS`; false →
    ///         record only.
    init(recordingDurationS: TimeInterval, emitAtS: [Double], isEmitter: Bool) {
        self.recordingDurationS = recordingDurationS
        self.emitAtS = emitAtS
        self.isEmitter = isEmitter
    }

    /// Start the engine + mic tap. If `isEmitter`, also synthesise the
    /// band-A chirp and schedule it at each `emitAtS` offset.
    /// `onRecordingComplete` fires once `recordingDurationS` elapses from
    /// the first recorded sample; `onError` fires on engine/session failure.
    /// Both run on the main queue. Idempotent: a second call while running
    /// is a no-op.
    func beginSync(
        onRecordingComplete: @escaping (RecordingResult) -> Void,
        onError: @escaping (String) -> Void
    ) {
        stateQueue.sync {
            guard !running else {
                log.warning("quick-sync audio beginSync while already running — ignoring")
                return
            }
            running = true
            recordedSamples.removeAll(keepingCapacity: true)
            recordingFirstPTS = nil
            completionFired = false
        }

        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()

        do {
            // Same session config as MutualSyncAudio: `.measurement` disables
            // AGC / beamforming / voice-processing (flat-response chirp
            // recording), `.defaultToSpeaker` routes the emitter's chirp out
            // the main speaker so clustered peers hear it.
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.defaultToSpeaker, .allowBluetoothA2DP]
            )
            try session.setActive(true, options: [])
            log.info("quick-sync audio session active outputVolume=\(session.outputVolume, privacy: .public) isEmitter=\(self.isEmitter, privacy: .public)")

            engine.attach(player)
            let mixerFormat = engine.mainMixerNode.outputFormat(forBus: 0)
            engine.connect(player, to: engine.mainMixerNode, format: mixerFormat)

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
            log.error("quick-sync audio beginSync failed: \(error.localizedDescription, privacy: .public)")
            let message = error.localizedDescription
            self.teardown()
            stateQueue.sync { running = false }
            DispatchQueue.main.async { onError(message) }
            return
        }

        self.engine = engine
        self.player = player

        if isEmitter {
            // Pre-build to fail loud before scheduling if the format is bad.
            guard makeBandAChirpBuffer(
                format: engine.mainMixerNode.outputFormat(forBus: 0)
            ) != nil else {
                log.error("quick-sync audio failed to build band-A chirp buffer")
                let err = "failed to build band-A chirp buffer"
                self.teardown()
                stateQueue.sync { running = false }
                DispatchQueue.main.async { onError(err) }
                return
            }
            scheduleEmissions()
        }

        armRecordingWatchdog(onRecordingComplete: onRecordingComplete)
    }

    /// Early teardown (operator cancel / watchdog timeout). Does NOT fire
    /// `onRecordingComplete`. Idempotent.
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

    private func scheduleEmissions() {
        for (idx, offset) in emitAtS.enumerated() {
            DispatchQueue.main.asyncAfter(deadline: .now() + offset) { [weak self] in
                guard let self else { return }
                guard let player = self.player, let engine = self.engine, engine.isRunning else { return }
                guard let buf = self.makeBandAChirpBuffer(
                    format: engine.mainMixerNode.outputFormat(forBus: 0)
                ) else { return }
                player.scheduleBuffer(buf, at: nil, options: [], completionHandler: nil)
                if !player.isPlaying { player.play() }
                log.info("quick-sync chirp emitted idx=\(idx) offset_s=\(offset, privacy: .public)")
            }
        }
    }

    private func ingestTapBuffer(_ buffer: AVAudioPCMBuffer, at audioTime: AVAudioTime) {
        let rate = buffer.format.sampleRate
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0, rate > 0,
              let channelData = buffer.floatChannelData else { return }

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

        // Fail-loud on invalid host time — identical rationale to
        // MutualSyncAudio: a wall-clock fallback would inject unbounded skew
        // and corrupt the anchor. Drop the buffer so recordingFirstPTS stays
        // nil and the coordinator's watchdog times the sync out instead.
        guard audioTime.isHostTimeValid else {
            assertionFailure("quick-sync audio: AVAudioTime.isHostTimeValid == false; dropping buffer (host-clock fallback would corrupt anchor)")
            log.error("quick-sync audio buffer dropped: hostTime invalid — anchor would be unreliable, letting watchdog time out")
            return
        }
        let hostCMTime = CMClockMakeHostTimeFromSystemUnits(audioTime.hostTime)
        let bufferStartS = CMTimeGetSeconds(hostCMTime)

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
        func pollForFirstSample() {
            stateQueue.async { [weak self] in
                guard let self, self.running else { return }
                if let first = self.recordingFirstPTS {
                    let elapsed = CMTimeGetSeconds(
                        CMClockGetTime(CMClockGetHostTimeClock())
                    ) - first
                    let remaining = max(0.05, self.recordingDurationS - elapsed)
                    DispatchQueue.main.asyncAfter(deadline: .now() + remaining) { [weak self] in
                        self?.finishRecording(onRecordingComplete: onRecordingComplete)
                    }
                    return
                }
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
            guard let firstPTS = recordingFirstPTS, recordingSampleRate > 0 else { return }
            completionFired = true
            let wav = MutualSyncAudio.encodeWAV(
                samples: recordedSamples, sampleRate: recordingSampleRate
            )
            payload = RecordingResult(
                wavData: wav,
                sampleRate: recordingSampleRate,
                audioStartPtsS: firstPTS
            )
            running = false
        }
        if let payload {
            log.info("quick-sync recording complete samples=\(self.recordedSamples.count, privacy: .public) wav_bytes=\(payload.wavData.count, privacy: .public)")
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
            log.info("quick-sync audio deactivate skipped: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Build the band-A Hann-windowed linear chirp in the given output
    /// format. Same phase + window math as the server reference and
    /// `MutualSyncAudio`'s role-"A" chirp so emission and detector reference
    /// are spectrally identical.
    private func makeBandAChirpBuffer(format: AVAudioFormat) -> AVAudioPCMBuffer? {
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
            let phase = 2.0 * .pi * (bandF0 * t + (bandF1 - bandF0) * t * t / denom)
            let window = 0.5 * (1.0 - cos(2.0 * .pi * Double(i) * invNm1))
            let sample = Float(sin(phase) * window) * amplitude
            for c in 0..<channels {
                channelData[c][i] = sample
            }
        }
        return buffer
    }
}
