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
///    per-report jitter.
///
/// I/O is decoupled from `AVCaptureSession`. The owner (`MutualSyncAudio`)
/// installs an `AVAudioEngine` input tap and forwards `AVAudioPCMBuffer`
/// frames via `ingestPCMBuffer(_:at:)`. Timestamps are converted from the
/// tap's mach host ticks (`AVAudioTime.hostTime`) to seconds on the host
/// clock via `CMClockMakeHostTimeFromSystemUnits`. On iOS,
/// `AVCaptureSession.masterClock` is `CMClockGetHostTimeClock()` by default,
/// so the resulting `t_self_s` / `t_from_other_s` remain directly
/// comparable to video-frame PTS — the sole invariant the solver needs.
///
/// Threading: all state lives on `deliveryQueue`. `ingestPCMBuffer` hops
/// to that queue synchronously so callers can deliver buffers from any
/// tap callback without extra plumbing.
final class AudioSyncDetector {
    enum Band: String {
        case A
        case B
    }

    struct DetectionEvent {
        let band: Band
        /// Host-clock seconds at the chirp **center** — directly comparable
        /// to video frame PTS (`AVCaptureSession.masterClock` derives from
        /// the same host clock).
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
        /// Scan-time relative to the run start (seconds since `firstPTS`).
        /// This is **when the matched filter was evaluated**, not the
        /// arrival time of any chirp inside the ring — plotting peak vs
        /// scan-time shows the noise floor with a clean spike where the
        /// chirp lived.
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
    private var threshold: Float
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
    /// First buffer's host-clock seconds. `centerPTS` = `firstPTS` + offset
    /// in samples / sampleRate, which is exactly the same algebra the
    /// legacy capture-session path used with `CMSampleBuffer` PTS.
    private var firstPTS: Double?
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

    /// Hot-apply a new matched-filter threshold. Mirrors the legacy
    /// AudioChirpDetector's `setThreshold(_:)` so the dashboard slider's
    /// pushed value reaches this detector too (previously mutual sync
    /// ignored the dashboard threshold and always used 0.18, forcing the
    /// operator to compile-time-edit the default to run a quieter rig).
    /// Thread-safe — writes happen on `deliveryQueue`.
    func setThreshold(_ value: Float) {
        deliveryQueue.async { [weak self] in
            self?.threshold = max(0.01, value)
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

    // MARK: - AVAudioEngine input-tap ingest

    /// Called by the owner's `AVAudioEngine` input-tap closure with one
    /// PCM buffer and its capture host-time. Hops to `deliveryQueue` and
    /// executes the same ring-buffer + matched-filter work the legacy
    /// `captureOutput` delegate did — the only difference is the clock
    /// conversion below.
    ///
    /// Host-tick → seconds conversion uses `CMClockMakeHostTimeFromSystemUnits`,
    /// which is the canonical bridge from mach ticks to `CMTime` on the
    /// host clock. `AVCaptureSession.masterClock` on iOS is by default the
    /// same `CMClockGetHostTimeClock()`, so the resulting PTS is identical
    /// to what `CMSampleBufferGetPresentationTimeStamp` returned in the
    /// old path — modulo scheduling jitter of the tap vs. the capture
    /// callback (both sub-ms).
    func ingestPCMBuffer(_ buffer: AVAudioPCMBuffer, at audioTime: AVAudioTime) {
        let rate = buffer.format.sampleRate
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0, rate > 0,
              let channelData = buffer.floatChannelData else { return }

        // Flatten to mono on the caller's thread — cheap (≤1024 samples per
        // buffer typical), avoids holding the AVAudioPCMBuffer past the tap
        // closure (its storage is reused by the engine).
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

        // Host ticks → host-clock seconds. `audioTime.hostTime` is in mach
        // absolute-time units; `CMClockMakeHostTimeFromSystemUnits` is the
        // documented bridge and returns a `CMTime` on the host clock.
        let bufferStartS: Double
        if audioTime.isHostTimeValid {
            let hostCMTime = CMClockMakeHostTimeFromSystemUnits(audioTime.hostTime)
            bufferStartS = CMTimeGetSeconds(hostCMTime)
        } else {
            // Fall back to the current host time if the tap ever hands us
            // an invalid timestamp — jitter bounded by one buffer duration.
            let hostCMTime = CMClockGetTime(CMClockGetHostTimeClock())
            bufferStartS = CMTimeGetSeconds(hostCMTime)
        }

        deliveryQueue.async { [weak self] in
            guard let self else { return }
            if abs(rate - self.sampleRate) > 0.5 {
                log.info("sync detector rebuilding for sample_rate_hz=\(rate, privacy: .public) (was \(self.sampleRate, privacy: .public))")
                self.rebuildForSampleRate(rate)
            }
            if self.firstPTS == nil {
                self.firstPTS = bufferStartS
            }
            for i in 0..<frameCount {
                self.ring[self.writeIndex] = mono[i]
                self.writeIndex += 1
                if self.writeIndex >= self.ringLen { self.writeIndex = 0 }
                self.totalWritten += 1
            }
            self.samplesSinceCheck += frameCount

            // Ring MUST be fully populated before the matched filter runs.
            // See original design note in rebuildForSampleRate — a zero-
            // padded tail inflates normalized peaks via the energy clamp.
            guard self.totalWritten >= self.ringLen else { return }
            if self.samplesSinceCheck >= self.checkIntervalSamples {
                self.runMatchedFilter()
                self.samplesSinceCheck = 0
            }
        }
    }

    // MARK: - Test injection

    /// Push synthetic samples directly. Mirrors `ingestPCMBuffer`'s cadence
    /// so a test can drive the dual-band pipeline without AVFoundation.
    /// `firstPTS` is seconds on whatever clock the test asserts against
    /// (the detector itself doesn't care — it only adds offsets).
    func _testFeed(samples: [Float], sampleRate rate: Double, firstPTS pts: CMTime) {
        deliveryQueue.sync {
            if abs(rate - sampleRate) > 0.5 {
                rebuildForSampleRate(rate)
            }
            if firstPTS == nil { firstPTS = CMTimeGetSeconds(pts) }
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
        // reach the debug plot.
        appendTrace(.A, result: resA, scanTime: now)
        appendTrace(.B, result: resB, scanTime: now)
        maybeFire(band: .A, result: resA, now: now)
        maybeFire(band: .B, result: resB, now: now)
    }

    private func appendTrace(_ band: Band, result: CorrResult, scanTime: Double) {
        let psr: Float = result.secondNorm > 0 ? result.bestNorm / result.secondNorm : 0
        // The trace's `t` is **scan time relative to the first buffer**
        // (i.e. "when did this correlation measurement happen"), NOT the
        // chirp-arrival time inside the ring. Earlier versions stored the
        // latter which produced a misleading drift-line on the debug plot:
        // once the real chirp fell out of the ring, `bestLag` wandered with
        // the loudest noise spike and drew a downward-sloping line all the
        // way across the x-axis. Plotting peak-vs-scan-time instead gives
        // the operator the actual noise-floor-with-spike profile they want.
        let firstPTSSnapshot = firstPTS ?? scanTime
        let tRel = scanTime - firstPTSSnapshot
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

        // Effective silence floor: if the windowed signal's energy is
        // below this, the ring is essentially zeros (mic not producing
        // samples, or denormals from an un-initialised path). The
        // previous 1e-12 floor was low enough that `abs(dot) / sqrt(1e-12)`
        // could explode to peaks of 1000+ on pure numerical noise —
        // producing "I heard something huge" false positives that then
        // got reported as real detections. Matched filter's bestNorm is
        // bounded by 1.0 for unit-energy reference; anything above that
        // is pathology.
        let silenceFloor: Float = 1e-6
        linear.withUnsafeBufferPointer { buf in
            guard let base = buf.baseAddress else { return }
            for idx in 0..<count {
                let lag = lagLo + idx
                let rawEnergy = cumsq[lag + refLen] - cumsq[lag]
                if rawEnergy < silenceFloor {
                    normValues[idx] = 0
                    continue
                }
                var dot: Float = 0
                vDSP_dotpr(base + lag, 1, reference, 1, &dot, vDSP_Length(refLen))
                // Mathematical ceiling is 1.0 (Cauchy-Schwarz). Clamp as a
                // belt-and-braces guard against numerical overshoot.
                normValues[idx] = min(1.0, abs(dot) / sqrt(rawEnergy))
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
        let firstPTSS = firstPTS ?? 0
        return firstPTSS + chirpCenterGlobal / sampleRate
    }

    private func currentPTSApprox() -> Double {
        guard let first = firstPTS else { return 0 }
        return first + Double(totalWritten) / sampleRate
    }
}
