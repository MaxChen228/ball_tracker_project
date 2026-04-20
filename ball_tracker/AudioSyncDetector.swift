import AVFoundation
import Accelerate
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sync")

/// Dual-band single-sweep matched filter for mutual chirp sync. Each phone
/// emits a chirp in its own disjoint frequency band (A: 2–4 kHz, B: 5–7 kHz)
/// and simultaneously listens via its mic. This detector runs two matched
/// filters in parallel on the live audio stream and fires a distinct event
/// the first time each band's peak crosses its PSR + threshold gate.
///
/// Design deltas vs. `AudioChirpDetector` (which drives the legacy third-
/// device time-sync flow):
///  - Two reference chirps (one per band), not a paired up/down sequence.
///  - Per-band cooldown instead of pair-latching — once a band fires, it
///    ignores further peaks for a short window so a second partial match
///    within the same emission can't double-fire.
///  - No Doppler-cancel averaging: emission is on-device, distances are
///    O(m), and the mutual-sync algebra tolerates hundreds of μs of
///    per-report jitter. The simpler design is a better fit for the
///    measurement than the pair machinery.
///
/// Both timestamps ride `AVCaptureSession.masterClock` via the same
/// `CMSampleBuffer` PTS pipeline that video samples use. This is what
/// makes the reported (t_self, t_from_other) comparable across phones
/// once the server applies Δ.
///
/// Threading: install as the `AVCaptureAudioDataOutput` sample-buffer
/// delegate with `deliveryQueue`. All state lives on that serial queue.
final class AudioSyncDetector: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    enum Band: String {
        case A
        case B
    }

    struct DetectionEvent {
        let band: Band
        /// Session-clock PTS of the chirp **center** (absolute, same clock
        /// the video frame timestamps live on).
        let centerPTS: Double
        /// Normalized matched-filter peak value in [0, 1]. Kept for logs;
        /// callers typically only need `centerPTS`.
        let peakNorm: Float
    }

    /// One matched-filter sample captured during a sync run. Shipped to
    /// the server so `/sync` can plot sub-threshold peaks — the whole
    /// point of the debug view (long-distance failures never cross the
    /// 0.18 gate so live `onDetection` alone hides the reason).
    struct TraceSample {
        /// Run-relative seconds (sample PTS minus `firstPTS`).
        let t: Double
        /// Same `bestNorm` the gate uses; [0, 1].
        let peak: Float
        /// Peak-to-sidelobe ratio (best / second-best outside exclusion).
        let psr: Float
    }

    let deliveryQueue = DispatchQueue(label: "audio.sync.queue")

    /// Fired on `deliveryQueue` the first time each band crosses threshold.
    /// May be called twice per run (one A, one B) or once (if the other
    /// phone's chirp was inaudible). Caller is responsible for arming/
    /// disarming this detector per sync run; repeat peaks within the
    /// cooldown window are silently dropped.
    var onDetection: ((DetectionEvent) -> Void)?

    private let bandAF0: Double
    private let bandAF1: Double
    private let bandBF0: Double
    private let bandBF1: Double
    private let chirpDurationS: Double
    private let threshold: Float
    private let minPSR: Float
    private let perBandCooldownS: Double

    private var sampleRate: Double = 0
    private var referenceA: [Float] = []
    private var referenceB: [Float] = []
    private var refLen: Int = 0
    private var checkIntervalSamples: Int = 0

    private var ring: [Float] = []
    private var ringLen: Int = 0
    private var writeIndex: Int = 0
    private var totalWritten: Int = 0
    private var firstPTS: CMTime?
    private var samplesSinceCheck: Int = 0
    private var lastTriggerA: Double?
    private var lastTriggerB: Double?
    /// Highest-precision timestamps reported so far for each band, so a
    /// caller collecting both can pull them off synchronously.
    private(set) var latestA: DetectionEvent?
    private(set) var latestB: DetectionEvent?

    /// Per-band rolling trace buffers. Every matched-filter scan appends
    /// one sample BEFORE the threshold/PSR gate so sub-threshold peaks
    /// are visible on the `/sync` debug plot. Capped at
    /// `traceMaxSamples` so a long run doesn't unbounded-grow.
    private var traceA: [TraceSample] = []
    private var traceB: [TraceSample] = []
    private let traceMaxSamples = 600

    init(
        // Bands mirror `server/chirp.py`'s SYNC_BAND_*_F0/F1. If you change
        // one side you MUST update the other — iOS emitter + iOS listener +
        // server constants must agree on the exact f0/f1.
        bandAF0: Double = 2000.0,
        bandAF1: Double = 4000.0,
        bandBF0: Double = 5000.0,
        bandBF1: Double = 7000.0,
        chirpDurationS: Double = 0.1,
        threshold: Float = 0.18,
        // Same value as legacy chirp detector — field-tuned and works for
        // real rooms (AGC + reverb push sidelobes up to ~1.5-2.5×).
        minPSR: Float = 1.5,
        perBandCooldownS: Double = 0.5
    ) {
        self.bandAF0 = bandAF0
        self.bandAF1 = bandAF1
        self.bandBF0 = bandBF0
        self.bandBF1 = bandBF1
        self.chirpDurationS = chirpDurationS
        self.threshold = threshold
        self.minPSR = minPSR
        self.perBandCooldownS = perBandCooldownS
        super.init()
    }

    /// Tear down state + arm for a fresh sync run. Safe from any thread.
    func reset() {
        deliveryQueue.async { [weak self] in
            guard let self else { return }
            self.writeIndex = 0
            self.totalWritten = 0
            self.firstPTS = nil
            self.samplesSinceCheck = 0
            self.lastTriggerA = nil
            self.lastTriggerB = nil
            self.latestA = nil
            self.latestB = nil
            self.traceA.removeAll(keepingCapacity: true)
            self.traceB.removeAll(keepingCapacity: true)
            if !self.ring.isEmpty {
                self.ring = [Float](repeating: 0, count: self.ringLen)
            }
        }
    }

    /// Pull the accumulated traces and reset the buffers. `self_` is the
    /// emitter's own band given the caller's role (role "A" → traceA);
    /// `other` is the cross-band. Safe to call from any thread — blocks
    /// briefly on `deliveryQueue` to snapshot atomically.
    func drainTraces(role: String) -> (self_: [TraceSample], other: [TraceSample]) {
        var out: (self_: [TraceSample], other: [TraceSample]) = ([], [])
        deliveryQueue.sync {
            let a = self.traceA
            let b = self.traceB
            self.traceA.removeAll(keepingCapacity: true)
            self.traceB.removeAll(keepingCapacity: true)
            if role == "A" {
                out = (self_: a, other: b)
            } else {
                out = (self_: b, other: a)
            }
        }
        return out
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // deliveryQueue.
        if let rate = Self.firstSampleRate(sampleBuffer),
           abs(rate - sampleRate) > 0.5 {
            log.info("sync detector rebuilding for sample_rate_hz=\(rate, privacy: .public) (was \(self.sampleRate, privacy: .public))")
            rebuildForSampleRate(rate)
        }
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

        // Ring MUST be fully populated before the matched filter runs. An
        // earlier draft gated on `refLen` (one chirp length) which let
        // `correlate` normalize windows dominated by zero-padding: any lag
        // whose window straddled the unwritten tail had near-zero energy,
        // the `max(energy, 1e-12)` clamp inflated `dot / sqrt(energy)` by
        // ~10⁶, and the resulting `peak=114.6` slipped past the 0.18
        // threshold + PSR gate. The fix matches `AudioChirpDetector`'s
        // original design: only scan once the ring holds `ringLen` real
        // samples end-to-end so every lag's energy is real.
        guard totalWritten >= ringLen else { return }
        if samplesSinceCheck >= checkIntervalSamples {
            runMatchedFilter()
            samplesSinceCheck = 0
        }
    }

    // MARK: - Test injection

    /// Push synthetic samples directly. Mirrors captureOutput's cadence so a
    /// test can drive the dual-band pipeline without AVFoundation.
    func _testFeed(samples: [Float], sampleRate rate: Double, firstPTS pts: CMTime) {
        deliveryQueue.sync {
            if abs(rate - sampleRate) > 0.5 {
                rebuildForSampleRate(rate)
            }
            if firstPTS == nil { firstPTS = pts }
            var offset = 0
            let chunk = max(1, Int(rate * 0.01))
            while offset < samples.count {
                let end = min(offset + chunk, samples.count)
                for i in offset..<end {
                    ring[writeIndex] = samples[i]
                    writeIndex += 1
                    if writeIndex >= ringLen { writeIndex = 0 }
                    totalWritten += 1
                }
                samplesSinceCheck += end - offset
                if totalWritten >= refLen && samplesSinceCheck >= checkIntervalSamples {
                    runMatchedFilter()
                    samplesSinceCheck = 0
                }
                offset = end
            }
        }
    }

    // MARK: - Internals

    private func rebuildForSampleRate(_ rate: Double) {
        sampleRate = rate
        refLen = Int(rate * chirpDurationS)
        // Ring holds ~2 s of audio so the full "A emits → wait → B emits"
        // sequence lands inside a single scan window even with conservative
        // inter-emission gaps.
        ringLen = max(4 * refLen, Int(rate * 2.0))
        referenceA = AudioChirpDetector.makeChirp(
            sampleRate: rate, f0: bandAF0, f1: bandAF1, duration: chirpDurationS
        )
        referenceB = AudioChirpDetector.makeChirp(
            sampleRate: rate, f0: bandBF0, f1: bandBF1, duration: chirpDurationS
        )
        // ~30 Hz scan cadence — same as AudioChirpDetector. Higher than
        // buffer delivery cadence (~100 Hz at 10 ms chunks) would just
        // burn CPU without latency benefit.
        checkIntervalSamples = Int(rate / 30.0)
        ring = [Float](repeating: 0, count: ringLen)
        writeIndex = 0
        totalWritten = 0
        firstPTS = nil
        samplesSinceCheck = 0
        lastTriggerA = nil
        lastTriggerB = nil
    }

    private func runMatchedFilter() {
        // Linearize the ring: oldest sample first.
        var linear = [Float](repeating: 0, count: ringLen)
        let tail = ringLen - writeIndex
        if tail > 0 {
            for i in 0..<tail { linear[i] = ring[writeIndex + i] }
        }
        for i in 0..<writeIndex { linear[tail + i] = ring[i] }

        // Cumulative sum of squares → exact window energy for per-lag
        // normalization without rolling-update drift.
        var cumsq = [Float](repeating: 0, count: ringLen + 1)
        var running: Float = 0
        for i in 0..<ringLen {
            running += linear[i] * linear[i]
            cumsq[i + 1] = running
        }

        let lagHi = ringLen - refLen
        let scanSpan = min(samplesSinceCheck + refLen, lagHi)
        let lagLo = max(1, lagHi - scanSpan)
        guard lagHi > lagLo else { return }

        let resA = correlate(
            linear: linear, cumsq: cumsq,
            reference: referenceA, lagLo: lagLo, lagHi: lagHi
        )
        let resB = correlate(
            linear: linear, cumsq: cumsq,
            reference: referenceB, lagLo: lagLo, lagHi: lagHi
        )

        let now = currentPTSApprox()
        // Append one trace sample per band BEFORE the fire-gate so
        // sub-threshold peaks (the whole reason this buffer exists)
        // reach the debug plot. `t` is run-relative: the chirp center
        // timestamp minus firstPTS, which equals the same expression
        // with firstPTS cancelled.
        appendTrace(.A, result: resA)
        appendTrace(.B, result: resB)
        maybeFire(band: .A, result: resA, now: now)
        maybeFire(band: .B, result: resB, now: now)
    }

    private func appendTrace(_ band: Band, result: CorrResult) {
        let psr: Float = result.secondNorm > 0 ? result.bestNorm / result.secondNorm : 0
        let ringStartGlobal = totalWritten - ringLen
        let chirpCenterGlobal = Double(ringStartGlobal + result.bestLag) + Double(refLen) / 2.0
        let tRel = sampleRate > 0 ? chirpCenterGlobal / sampleRate : 0
        let sample = TraceSample(t: tRel, peak: result.bestNorm, psr: psr)
        switch band {
        case .A:
            traceA.append(sample)
            if traceA.count > traceMaxSamples {
                traceA.removeFirst(traceA.count - traceMaxSamples)
            }
        case .B:
            traceB.append(sample)
            if traceB.count > traceMaxSamples {
                traceB.removeFirst(traceB.count - traceMaxSamples)
            }
        }
    }

    private struct CorrResult {
        let bestLag: Int
        let bestNorm: Float
        let leftNorm: Float
        let rightNorm: Float
        let secondNorm: Float
    }

    private func correlate(
        linear: [Float],
        cumsq: [Float],
        reference: [Float],
        lagLo: Int,
        lagHi: Int
    ) -> CorrResult {
        let count = lagHi - lagLo + 1
        var normValues = [Float](repeating: 0, count: count)

        linear.withUnsafeBufferPointer { buf in
            guard let base = buf.baseAddress else { return }
            for idx in 0..<count {
                let lag = lagLo + idx
                var dot: Float = 0
                vDSP_dotpr(base + lag, 1, reference, 1, &dot, vDSP_Length(refLen))
                let energy = max(cumsq[lag + refLen] - cumsq[lag], 1e-12)
                normValues[idx] = abs(dot) / sqrt(energy)
            }
        }

        var bestIdx = 0
        var bestNorm: Float = normValues[0]
        for idx in 1..<count {
            if normValues[idx] > bestNorm {
                bestNorm = normValues[idx]
                bestIdx = idx
            }
        }
        let bestLag = lagLo + bestIdx
        let leftNorm = bestIdx > 0 ? normValues[bestIdx - 1] : bestNorm
        let rightNorm = bestIdx < count - 1 ? normValues[bestIdx + 1] : bestNorm
        let exclusion = refLen / 2
        var secondNorm: Float = 0
        for idx in 0..<count {
            let lag = lagLo + idx
            if abs(lag - bestLag) <= exclusion { continue }
            if normValues[idx] > secondNorm {
                secondNorm = normValues[idx]
            }
        }
        return CorrResult(
            bestLag: bestLag,
            bestNorm: bestNorm,
            leftNorm: leftNorm,
            rightNorm: rightNorm,
            secondNorm: secondNorm
        )
    }

    private func maybeFire(band: Band, result: CorrResult, now: Double) {
        let psr = result.secondNorm > 0 ? result.bestNorm / result.secondNorm : 0
        guard result.bestNorm > threshold, psr > minPSR else { return }
        // Per-band cooldown — skip if this band already fired recently.
        let lastTrigger: Double?
        switch band {
        case .A: lastTrigger = lastTriggerA
        case .B: lastTrigger = lastTriggerB
        }
        if let lt = lastTrigger, now - lt < perBandCooldownS { return }

        let centerS = timestampForChirpCenter(result: result)
        let event = DetectionEvent(band: band, centerPTS: centerS, peakNorm: result.bestNorm)
        switch band {
        case .A:
            lastTriggerA = now
            latestA = event
        case .B:
            lastTriggerB = now
            latestB = event
        }
        log.info("sync band \(band.rawValue, privacy: .public) fired peak=\(result.bestNorm, privacy: .public) psr=\(psr, privacy: .public) center_s=\(centerS, privacy: .public)")
        onDetection?(event)
    }

    private func timestampForChirpCenter(result: CorrResult) -> Double {
        var fracLag = Double(result.bestLag)
        let denom = result.leftNorm - 2 * result.bestNorm + result.rightNorm
        if denom != 0 {
            let frac = 0.5 * Double(result.leftNorm - result.rightNorm) / Double(denom)
            if frac > -1 && frac < 1 { fracLag += frac }
        }
        let ringStartGlobal = totalWritten - ringLen
        let chirpStartGlobal = Double(ringStartGlobal) + fracLag
        let chirpCenterGlobal = chirpStartGlobal + Double(refLen) / 2.0
        let firstPTSS = firstPTS.map { CMTimeGetSeconds($0) } ?? 0
        return firstPTSS + chirpCenterGlobal / sampleRate
    }

    private func currentPTSApprox() -> Double {
        guard let first = firstPTS else { return 0 }
        return CMTimeGetSeconds(first) + Double(totalWritten) / sampleRate
    }

    // MARK: - PCM extraction (duplicated from AudioChirpDetector)

    private static func firstSampleRate(_ sampleBuffer: CMSampleBuffer) -> Double? {
        guard let fmt = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else {
            return nil
        }
        let rate = asbdPtr.pointee.mSampleRate
        return rate > 0 ? rate : nil
    }

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
        let isFloat = asbd.mFormatID == kAudioFormatLinearPCM &&
            (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0 &&
            asbd.mBitsPerChannel == 32
        let isInt16 = asbd.mFormatID == kAudioFormatLinearPCM &&
            (asbd.mFormatFlags & kAudioFormatFlagIsSignedInteger) != 0 &&
            asbd.mBitsPerChannel == 16
        let interleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0

        if isFloat {
            let srcFloat = dataPtr.withMemoryRebound(to: Float.self, capacity: totalLen / 4) { $0 }
            if channels == 1 {
                return Array(UnsafeBufferPointer(start: srcFloat, count: numSamples))
            }
            if interleaved {
                var out = [Float](repeating: 0, count: numSamples)
                let gain = 1.0 / Float(channels)
                for i in 0..<numSamples {
                    var sum: Float = 0
                    for c in 0..<channels {
                        sum += srcFloat[i * channels + c]
                    }
                    out[i] = sum * gain
                }
                return out
            }
            return Array(UnsafeBufferPointer(start: srcFloat, count: numSamples))
        }
        if isInt16 {
            let srcI16 = dataPtr.withMemoryRebound(to: Int16.self, capacity: totalLen / 2) { $0 }
            var out = [Float](repeating: 0, count: numSamples)
            if channels == 1 || !interleaved {
                for i in 0..<numSamples {
                    out[i] = Float(srcI16[i]) / 32768.0
                }
            } else {
                let gain = 1.0 / (32768.0 * Float(channels))
                for i in 0..<numSamples {
                    var sum: Int32 = 0
                    for c in 0..<channels {
                        sum += Int32(srcI16[i * channels + c])
                    }
                    out[i] = Float(sum) * gain
                }
            }
            return out
        }
        return nil
    }
}
