import AVFoundation
import Accelerate
import CoreMedia
import Foundation

/// Single sync mechanism for the tracker: each phone listens for a known
/// linear-chirp signal (2 → 8 kHz Hann-windowed, 100 ms) played from a third
/// device while the two cameras sit side-by-side. Once detected, each phone
/// records the chirp's session-clock PTS as `sync_anchor_timestamp_s`, which
/// then anchors the whole pitch cycle against the other camera.
///
/// Replaces the earlier torch-flash / audio-xcorr / Mac-NTP experiments.
///
/// Detection: normalized matched filter (cross-correlation of the mic stream
/// against the reference chirp, divided by the window's local energy). Peak
/// above `threshold` within `cooldownS` of no prior trigger emits an event.
///
/// Precision: audio sample-rate 44.1 kHz → 22 μs per sample; matched-filter
/// peak + parabolic interpolation ≈ **<100 μs** under clean SNR, versus ~4 ms
/// frame-granularity on the old visual flash detector.
///
/// Threading: the owner installs this as the `AVCaptureAudioDataOutput`
/// sample-buffer delegate with `deliveryQueue`. All state lives on that
/// serial queue — no locks.
final class AudioChirpDetector: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    struct ChirpEvent {
        /// Kept for schema parity with the old flash event. `frameIndex` has
        /// no meaning for an audio anchor — the server pairs A/B by
        /// `anchorTimestampS` alone.
        let anchorFrameIndex: Int
        /// Session-clock time of the chirp's center sample (same time base
        /// as video frame timestamps — they share `AVCaptureSession.masterClock`).
        let anchorTimestampS: Double
    }

    /// Live detector state exposed to the debug HUD.
    struct Snapshot {
        let bufferFillSamples: Int
        let lastPeak: Float
        let threshold: Float
        let armed: Bool
        let triggered: Bool
    }

    /// Install with `AVCaptureAudioDataOutput.setSampleBufferDelegate(_:queue:)`.
    let deliveryQueue = DispatchQueue(label: "audio.chirp.queue")

    /// Fired on `deliveryQueue` when a chirp is detected.
    var onChirpDetected: ((ChirpEvent) -> Void)?

    // Config (immutable after init).
    private let sampleRate: Double
    private let threshold: Float
    private let cooldownS: Double
    private let reference: [Float]
    private let refLen: Int
    private let checkIntervalSamples: Int

    // Mutable state — accessed only on deliveryQueue.
    private var ring: [Float]
    private let ringLen: Int
    private var writeIndex: Int = 0
    private var totalWritten: Int = 0
    private var firstPTS: CMTime?
    private var lastTriggerPTS: Double?
    private var samplesSinceCheck: Int = 0
    private(set) var lastSnapshot: Snapshot

    init(
        sampleRate: Double = 44100.0,
        chirpF0: Double = 2000.0,
        chirpF1: Double = 8000.0,
        chirpDurationS: Double = 0.1,
        // Field-tuned on an external speaker at ~1 m: clean-SNR peaks land
        // around 0.4–0.7, typical living-room playback 0.20–0.35. Threshold
        // 0.18 gives consistent trigger while still being well above
        // stationary noise (<0.05) and ambient speech (~0.08).
        threshold: Float = 0.18,
        cooldownS: Double = 1.0
    ) {
        self.sampleRate = sampleRate
        self.threshold = threshold
        self.cooldownS = cooldownS
        self.refLen = Int(sampleRate * chirpDurationS)
        self.ringLen = 2 * refLen
        self.ring = [Float](repeating: 0, count: 2 * refLen)
        self.reference = Self.makeChirp(
            sampleRate: sampleRate, f0: chirpF0, f1: chirpF1, duration: chirpDurationS
        )
        // ~10 Hz matched-filter pass. Balances latency with CPU on the
        // capture queue (O(N²) in refLen, dominated by vDSP_dotpr).
        self.checkIntervalSamples = Int(sampleRate / 10.0)
        self.lastSnapshot = Snapshot(
            bufferFillSamples: 0,
            lastPeak: 0,
            threshold: threshold,
            armed: false,
            triggered: false
        )
    }

    /// Linear chirp, Hann-windowed, energy-normalized to unit norm so the
    /// matched-filter peak lands in [0, 1] regardless of reference amplitude.
    static func makeChirp(
        sampleRate: Double,
        f0: Double,
        f1: Double,
        duration: Double
    ) -> [Float] {
        let n = Int(sampleRate * duration)
        guard n > 1 else { return [] }
        var out = [Float](repeating: 0, count: n)
        let denom = 2.0 * duration
        let invNm1 = 1.0 / Double(n - 1)
        for i in 0..<n {
            let t = Double(i) / sampleRate
            let phase = 2.0 * .pi * (f0 * t + (f1 - f0) * t * t / denom)
            let window = 0.5 * (1.0 - cos(2.0 * .pi * Double(i) * invNm1))
            out[i] = Float(sin(phase) * window)
        }
        // Unit-energy normalize.
        var energy: Float = 0
        vDSP_svesq(out, 1, &energy, vDSP_Length(n))
        let norm = sqrt(energy)
        if norm > 0 {
            var scale = Float(1.0) / norm
            vDSP_vsmul(out, 1, &scale, &out, 1, vDSP_Length(n))
        }
        return out
    }

    /// Discard all buffered audio and timing state. Safe to call from any
    /// thread (bounces onto deliveryQueue).
    func reset() {
        deliveryQueue.async { [weak self] in
            guard let self else { return }
            self.ring = [Float](repeating: 0, count: self.ringLen)
            self.writeIndex = 0
            self.totalWritten = 0
            self.firstPTS = nil
            self.lastTriggerPTS = nil
            self.samplesSinceCheck = 0
            self.lastSnapshot = Snapshot(
                bufferFillSamples: 0,
                lastPeak: 0,
                threshold: self.threshold,
                armed: false,
                triggered: false
            )
        }
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // Runs on deliveryQueue.
        if firstPTS == nil {
            firstPTS = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        }
        guard let samples = Self.extractMonoFloat(sampleBuffer), !samples.isEmpty else {
            return
        }
        samples.withUnsafeBufferPointer { srcBuf in
            guard let src = srcBuf.baseAddress else { return }
            for i in 0..<samples.count {
                ring[writeIndex] = src[i]
                writeIndex += 1
                if writeIndex >= ringLen { writeIndex = 0 }
                totalWritten += 1
            }
        }
        samplesSinceCheck += samples.count

        guard totalWritten >= ringLen else {
            lastSnapshot = Snapshot(
                bufferFillSamples: totalWritten,
                lastPeak: lastSnapshot.lastPeak,
                threshold: threshold,
                armed: false,
                triggered: false
            )
            return
        }
        if samplesSinceCheck >= checkIntervalSamples {
            samplesSinceCheck = 0
            runMatchedFilter()
        }
    }

    // MARK: - Matched filter

    private func runMatchedFilter() {
        // Linearize the ring: oldest sample first.
        var linear = [Float](repeating: 0, count: ringLen)
        let tail = ringLen - writeIndex
        if tail > 0 {
            for i in 0..<tail {
                linear[i] = ring[writeIndex + i]
            }
        }
        for i in 0..<writeIndex {
            linear[tail + i] = ring[i]
        }

        let resultLen = ringLen - refLen + 1
        var peakNorm: Float = 0
        var peakLag: Int = 0
        var peakUnnorm: Float = 0

        // Rolling window energy — compute once, update incrementally.
        var windowEnergy: Float = 0
        vDSP_svesq(linear, 1, &windowEnergy, vDSP_Length(refLen))

        for lag in 0..<resultLen {
            var dot: Float = 0
            linear.withUnsafeBufferPointer { buf in
                vDSP_dotpr(
                    buf.baseAddress! + lag, 1,
                    reference, 1,
                    &dot,
                    vDSP_Length(refLen)
                )
            }
            let denom = sqrt(max(windowEnergy, 1e-12))
            let norm = abs(dot / denom)
            if norm > peakNorm {
                peakNorm = norm
                peakLag = lag
                peakUnnorm = dot
            }
            if lag + refLen < ringLen {
                let leaving = linear[lag]
                let arriving = linear[lag + refLen]
                windowEnergy += arriving * arriving - leaving * leaving
                if windowEnergy < 0 { windowEnergy = 0 }
            }
        }

        let now_s = currentPTSApprox()
        let armed = (lastTriggerPTS ?? -Double.infinity) + cooldownS <= now_s
        var triggered = false

        if armed && peakNorm > threshold {
            // Sub-sample peak refinement from the unnormalized dot product
            // (parabolic on the three samples around the peak).
            var fracLag = Double(peakLag)
            if peakLag > 0 && peakLag < resultLen - 1 {
                // We don't have the neighbouring normalized values stored; recompute.
                linear.withUnsafeBufferPointer { buf in
                    guard let base = buf.baseAddress else { return }
                    var yL: Float = 0, yR: Float = 0
                    vDSP_dotpr(base + peakLag - 1, 1, reference, 1, &yL, vDSP_Length(refLen))
                    vDSP_dotpr(base + peakLag + 1, 1, reference, 1, &yR, vDSP_Length(refLen))
                    let denom = (yL - 2 * peakUnnorm + yR)
                    if denom != 0 {
                        let frac = 0.5 * Double(yL - yR) / Double(denom)
                        if frac > -1 && frac < 1 {
                            fracLag += frac
                        }
                    }
                }
            }
            let ringStartGlobal = totalWritten - ringLen
            let chirpStartGlobal = Double(ringStartGlobal) + fracLag
            let chirpCenterGlobal = chirpStartGlobal + Double(refLen) / 2.0
            if let first = firstPTS {
                let pts = CMTimeGetSeconds(first) + chirpCenterGlobal / sampleRate
                lastTriggerPTS = pts
                triggered = true
                onChirpDetected?(
                    ChirpEvent(anchorFrameIndex: 0, anchorTimestampS: pts)
                )
            }
        }

        lastSnapshot = Snapshot(
            bufferFillSamples: totalWritten,
            lastPeak: peakNorm,
            threshold: threshold,
            armed: armed,
            triggered: triggered
        )
    }

    private func currentPTSApprox() -> Double {
        guard let first = firstPTS else { return 0 }
        return CMTimeGetSeconds(first) + Double(totalWritten) / sampleRate
    }

    // MARK: - PCM extraction

    /// Pulls a mono Float32 view of the sample buffer. Handles the common
    /// iOS capture formats: Float32 mono/stereo (interleaved or planar) and
    /// Int16 mono/stereo (interleaved). Returns nil for anything else.
    private static func extractMonoFloat(_ sampleBuffer: CMSampleBuffer) -> [Float]? {
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer),
              let fmt = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else {
            return nil
        }
        let asbd = asbdPtr.pointee
        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        if numSamples <= 0 { return [] }

        var totalLen = 0
        var dataPtr: UnsafeMutablePointer<Int8>?
        let status = CMBlockBufferGetDataPointer(
            blockBuffer,
            atOffset: 0,
            lengthAtOffsetOut: nil,
            totalLengthOut: &totalLen,
            dataPointerOut: &dataPtr
        )
        guard status == noErr, let dataPtr else { return nil }

        let channels = Int(asbd.mChannelsPerFrame)
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let bytesPerSample = Int(asbd.mBitsPerChannel) / 8
        let interleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0

        var mono = [Float](repeating: 0, count: numSamples)

        if isFloat && bytesPerSample == 4 {
            let ptr = UnsafeRawPointer(dataPtr).assumingMemoryBound(to: Float.self)
            if channels == 1 {
                for i in 0..<numSamples { mono[i] = ptr[i] }
            } else if interleaved {
                let cF = Float(channels)
                for i in 0..<numSamples {
                    var s: Float = 0
                    for c in 0..<channels { s += ptr[i * channels + c] }
                    mono[i] = s / cF
                }
            } else {
                // Non-interleaved planar: first plane only.
                for i in 0..<numSamples { mono[i] = ptr[i] }
            }
        } else if !isFloat && bytesPerSample == 2 {
            let ptr = UnsafeRawPointer(dataPtr).assumingMemoryBound(to: Int16.self)
            let scale: Float = 1.0 / 32768.0
            if channels == 1 {
                for i in 0..<numSamples { mono[i] = Float(ptr[i]) * scale }
            } else if interleaved {
                let cF = Float(channels)
                for i in 0..<numSamples {
                    var s: Int32 = 0
                    for c in 0..<channels { s += Int32(ptr[i * channels + c]) }
                    mono[i] = (Float(s) * scale) / cF
                }
            } else {
                for i in 0..<numSamples { mono[i] = Float(ptr[i]) * scale }
            }
        } else {
            return nil
        }
        return mono
    }
}
