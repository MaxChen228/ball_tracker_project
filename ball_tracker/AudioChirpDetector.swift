import AVFoundation
import Accelerate
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Single sync mechanism for the tracker: each phone listens for a known
/// dual-chirp signal (up-sweep 2→8 kHz, 50 ms silence, down-sweep 8→2 kHz;
/// each chirp 100 ms Hann-windowed) played from a third device while the two
/// cameras sit side-by-side. Once both sweeps are detected **and** the gap
/// between their centers matches the expected 150 ms within ±5 ms, the phone
/// records the mid-point session-clock PTS as `sync_anchor_timestamp_s`, which
/// anchors the whole pitch cycle against the other camera.
///
/// Dual-sweep design cancels first-order Doppler (if the speaker moves, the
/// up-sweep's apparent center shifts one way and the down-sweep's shifts the
/// opposite way — the average is invariant) and gives us a built-in
/// consistency check: a stray spike that looks like one chirp still has to
/// produce a second chirp ~150 ms later with the opposite sweep direction to
/// trigger a false anchor.
///
/// Robustness mechanics:
///  - Exact window energy via cumulative-sum-of-squares (no rolling-update
///    drift); normalized matched-filter peak lands in [0, 1].
///  - PSR (peak-to-sidelobe ratio) gate rejects flat/noisy correlations.
///  - Parabolic sub-sample refinement on **normalized** peaks (not raw dot
///    products) so a local energy ramp can't skew the anchor.
///  - Scan only the newly-arrived samples per pass — each buffer contributes
///    exactly once to any lag.
///
/// Precision (clean SNR): ~20 μs per chirp end-point, Doppler-free after
/// averaging up + down — target anchor precision <50 μs.
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
        /// Session-clock time of the anchor (= mid-point between up-sweep
        /// center and down-sweep center, which equals the start of the 50 ms
        /// inter-chirp silence). Same time base as video frame timestamps
        /// since both share `AVCaptureSession.masterClock`.
        let anchorTimestampS: Double
    }

    /// Live detector state exposed to the debug HUD. `lastPeak` is the most
    /// recent up-sweep normalized peak; `pendingUp` indicates an accepted
    /// up-sweep is currently waiting for its down-sweep partner.
    struct Snapshot {
        let bufferFillSamples: Int
        let lastPeak: Float
        let threshold: Float
        let armed: Bool
        let triggered: Bool
        let lastDownPeak: Float
        let lastPSR: Float
        let pendingUp: Bool
    }

    /// Install with `AVCaptureAudioDataOutput.setSampleBufferDelegate(_:queue:)`.
    let deliveryQueue = DispatchQueue(label: "audio.chirp.queue")

    /// Fired on `deliveryQueue` when a valid up+down pair is detected.
    var onChirpDetected: ((ChirpEvent) -> Void)?

    // Config.
    /// Current sample rate the references + ring are built against. Starts at
    /// the init default (44.1 kHz for tests) and is overwritten on the first
    /// `captureOutput` buffer via `rebuildForSampleRate(_:)` when the real
    /// mic ASBD reports a different rate (iPhone delivers 48 kHz natively).
    private var sampleRate: Double
    /// Mutable so the user can tune from Settings without rebuilding the
    /// capture session. Reads on deliveryQueue, writes bounced through
    /// `setThreshold(_:)`.
    private var threshold: Float
    private let cooldownS: Double
    /// Peak-to-sidelobe-ratio floor. Measured as the best normalized peak
    /// divided by the best normalized value outside a ±refLen/2 exclusion
    /// zone around the peak. Clean field chirps yield PSR 3-10×; ambient
    /// noise / near-matches sit near 1.0. 2.0 comfortably separates them.
    private let minPSR: Float
    /// Expected time between up-sweep center and down-sweep center, derived
    /// from the source chirp WAV: two 100 ms sweeps with a 50 ms silence
    /// between → 150 ms center-to-center.
    private let chirpGapCenterS: Double
    /// ± tolerance applied to the gap check. 5 ms is ~30× the target anchor
    /// precision, so a valid pair always passes, while stray same-band
    /// transients almost never land on the exact 150 ms offset.
    private let chirpPairToleranceS: Double
    /// Chirp parameters kept around for lazy rebuild on rate change.
    private let chirpF0: Double
    private let chirpF1: Double
    private let chirpDurationS: Double
    private var referenceUp: [Float]
    private var referenceDown: [Float]
    private var refLen: Int
    private var checkIntervalSamples: Int

    // Mutable state — accessed only on deliveryQueue.
    private var ring: [Float]
    private var ringLen: Int
    private var writeIndex: Int = 0
    private var totalWritten: Int = 0
    private var firstPTS: CMTime?
    private var lastTriggerPTS: Double?
    private var samplesSinceCheck: Int = 0
    /// An accepted up-sweep awaiting its down-sweep partner. Expires once it
    /// ages past `chirpGapCenterS + chirpPairToleranceS` without a match.
    private var pendingUp: (centerS: Double, peak: Float)?
    private(set) var lastSnapshot: Snapshot

    /// CFAR noise-floor estimators, one per sweep. Feed every non-chirp
    /// scan's bestNorm; fire gate multiplies the estimate by
    /// `cfarNoiseMultiplier` to check "signal really above noise" on top
    /// of the absolute threshold. Makes quick-chirp self-calibrate across
    /// phones with different mic sensitivity / AGC — same robust design
    /// the mutual-sync detector uses. Both sync modalities now share one
    /// detection policy so tuning on one rig stays valid on the other.
    private var cfarUp = CFARNoiseFloor()
    private var cfarDown = CFARNoiseFloor()
    private var cfarNoiseMultiplier: Float = 5.0
    private let cfarMinSamples: Int = 8

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
        cooldownS: Double = 1.0,
        // PSR floor was 2.0 — too strict for real rooms. Mic AGC + room
        // reverb pushes sidelobes up: clean lab PSR 5-10×, real living
        // room 1.5-2.5×. Pure noise sits at PSR ≈ 1.0, so 1.5 is
        // comfortably above noise while letting through realistic chirps.
        // The down-sweep additionally bypasses PSR when a pending up
        // exists (timing prior is enough — see runMatchedFilter).
        minPSR: Float = 1.5,
        chirpGapCenterS: Double = 0.15,
        // 5 ms was sample-accurate-WAV tight. Real audio playback through
        // CoreAudio / Bluetooth / browser stacks introduces ms-scale
        // scheduling jitter. 10 ms still rules out same-band transients
        // (which essentially never land on the exact 150 ms offset).
        chirpPairToleranceS: Double = 0.010
    ) {
        self.sampleRate = 0
        self.threshold = threshold
        self.cooldownS = cooldownS
        self.minPSR = minPSR
        self.chirpGapCenterS = chirpGapCenterS
        self.chirpPairToleranceS = chirpPairToleranceS
        self.chirpF0 = chirpF0
        self.chirpF1 = chirpF1
        self.chirpDurationS = chirpDurationS
        self.referenceUp = []
        self.referenceDown = []
        self.refLen = 0
        self.checkIntervalSamples = 0
        self.ring = []
        self.ringLen = 0
        self.lastSnapshot = Snapshot(
            bufferFillSamples: 0,
            lastPeak: 0,
            threshold: threshold,
            armed: false,
            triggered: false,
            lastDownPeak: 0,
            lastPSR: 0,
            pendingUp: false
        )
        super.init()
        rebuildForSampleRate(sampleRate)
    }

    /// Regenerate the reference chirps + ring buffer for a new sample rate
    /// and reset all timing state. Ring size is sized so both up-sweep and
    /// down-sweep plus their 50 ms gap fit inside the most recent window —
    /// lets a single matched-filter pass see both chirps when they arrive in
    /// the same buffer batch.
    private func rebuildForSampleRate(_ rate: Double) {
        self.sampleRate = rate
        self.refLen = Int(rate * chirpDurationS)
        // Need to hold at least: up-sweep + gap + down-sweep = 2·refLen + gap
        // samples. Round up to 4·refLen (~400 ms @ 100 ms chirps) to give
        // the scan region margin for late buffers.
        let gapSamples = Int(rate * (chirpGapCenterS - chirpDurationS))
        let minRing = 2 * refLen + gapSamples + refLen
        self.ringLen = max(4 * refLen, minRing)
        self.referenceUp = Self.makeChirp(
            sampleRate: rate, f0: chirpF0, f1: chirpF1, duration: chirpDurationS
        )
        self.referenceDown = Self.makeChirp(
            sampleRate: rate, f0: chirpF1, f1: chirpF0, duration: chirpDurationS
        )
        // ~30 Hz matched-filter pass. Higher than the old 10 Hz so the scan
        // region per pass is smaller and latency to trigger drops; still
        // comfortably under audio buffer delivery cadence (iOS hands 10 ms
        // chunks at 48 kHz).
        self.checkIntervalSamples = Int(rate / 30.0)
        self.ring = [Float](repeating: 0, count: ringLen)
        self.writeIndex = 0
        self.totalWritten = 0
        self.firstPTS = nil
        self.lastTriggerPTS = nil
        self.samplesSinceCheck = 0
        self.pendingUp = nil
        self.lastSnapshot = Snapshot(
            bufferFillSamples: 0,
            lastPeak: 0,
            threshold: threshold,
            armed: false,
            triggered: false,
            lastDownPeak: 0,
            lastPSR: 0,
            pendingUp: false
        )
    }

    /// Linear chirp, Hann-windowed, energy-normalized to unit norm so the
    /// matched-filter peak lands in [0, 1] regardless of reference amplitude.
    /// `f0 > f1` gives a down-sweep (same phase formula handles either sign).
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
        var energy: Float = 0
        vDSP_svesq(out, 1, &energy, vDSP_Length(n))
        let norm = sqrt(energy)
        if norm > 0 {
            var scale = Float(1.0) / norm
            vDSP_vsmul(out, 1, &scale, &out, 1, vDSP_Length(n))
        }
        return out
    }

    /// Update the matched-filter trigger threshold. Safe to call from any
    /// thread; the new value is applied on the next `runMatchedFilter` pass.
    func setThreshold(_ value: Float) {
        log.info("chirp threshold updated value=\(value, privacy: .public)")
        deliveryQueue.async { [weak self] in
            guard let self else { return }
            self.threshold = value
        }
    }

    /// Hot-apply CFAR multiplier from dashboard. Mirrors the shape on
    /// AudioSyncDetector so the two detectors expose the same control
    /// surface.
    func setCFARMultiplier(_ value: Float) {
        deliveryQueue.async { [weak self] in
            self?.cfarNoiseMultiplier = max(2.0, min(12.0, value))
        }
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
            self.pendingUp = nil
            self.cfarUp.reset()
            self.cfarDown.reset()
            self.lastSnapshot = Snapshot(
                bufferFillSamples: 0,
                lastPeak: 0,
                threshold: self.threshold,
                armed: false,
                triggered: false,
                lastDownPeak: 0,
                lastPSR: 0,
                pendingUp: false
            )
        }
    }

    // MARK: - Test injection

    /// Push synthetic samples directly into the ring on the delivery queue,
    /// bypassing CMSampleBuffer plumbing. Mirrors the per-buffer path of
    /// `captureOutput` — including the `samplesSinceCheck` cadence gate —
    /// so tests can exercise the full matched-filter + pairing pipeline
    /// end-to-end without AVFoundation. Blocks until the feed drains so a
    /// test can assert on callback delivery synchronously.
    func _testFeed(samples: [Float], sampleRate rate: Double, firstPTS pts: CMTime) {
        deliveryQueue.sync {
            if abs(rate - sampleRate) > 0.5 {
                rebuildForSampleRate(rate)
            }
            if firstPTS == nil { firstPTS = pts }

            var offset = 0
            // Match the real capture cadence: iOS hands ~10 ms chunks at a
            // time. Feeding one big array in a single pass would never cross
            // the `samplesSinceCheck >= checkIntervalSamples` gate more than
            // once, so split it.
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
                if totalWritten >= ringLen && samplesSinceCheck >= checkIntervalSamples {
                    runMatchedFilter()
                    samplesSinceCheck = 0
                }
                offset = end
            }
        }
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // Runs on deliveryQueue.
        if let rate = Self.firstSampleRate(sampleBuffer),
           abs(rate - sampleRate) > 0.5 {
            log.info("chirp detector rebuilding for sample_rate_hz=\(rate, privacy: .public) (was \(self.sampleRate, privacy: .public))")
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

        guard totalWritten >= ringLen else {
            lastSnapshot = Snapshot(
                bufferFillSamples: totalWritten,
                lastPeak: lastSnapshot.lastPeak,
                threshold: threshold,
                armed: false,
                triggered: false,
                lastDownPeak: lastSnapshot.lastDownPeak,
                lastPSR: lastSnapshot.lastPSR,
                pendingUp: pendingUp != nil
            )
            return
        }
        if samplesSinceCheck >= checkIntervalSamples {
            runMatchedFilter()
            samplesSinceCheck = 0
        }
    }

    // MARK: - Matched filter

    /// Correlation result for one reference over a lag range.
    private struct CorrResult {
        let bestLag: Int
        let bestNorm: Float
        /// Raw normalized value one sample below `bestLag` (for parabolic fit).
        let leftNorm: Float
        /// Raw normalized value one sample above `bestLag`.
        let rightNorm: Float
        /// Best normalized value seen outside ±exclusion of `bestLag`.
        let secondNorm: Float
    }

    private func runMatchedFilter() {
        // Linearize the ring: oldest sample first.
        var linear = [Float](repeating: 0, count: ringLen)
        let tail = ringLen - writeIndex
        if tail > 0 {
            for i in 0..<tail { linear[i] = ring[writeIndex + i] }
        }
        for i in 0..<writeIndex { linear[tail + i] = ring[i] }

        // Cumulative sum of squares → exact window energy in O(1) per lag,
        // no rolling-update drift.
        var cumsq = [Float](repeating: 0, count: ringLen + 1)
        var running: Float = 0
        for i in 0..<ringLen {
            running += linear[i] * linear[i]
            cumsq[i + 1] = running
        }

        // Scan only lags whose chirp END falls in the newly-arrived region.
        // A chirp ending exactly at the newest sample sits at lag = ringLen -
        // refLen; one ending `samplesSinceCheck` samples ago sits at lag =
        // ringLen - refLen - samplesSinceCheck. Margin of refLen lets the
        // parabolic-fit neighbours exist, and covers the jitter window the
        // gap tolerance implies.
        let lagHi = ringLen - refLen
        let scanSpan = min(samplesSinceCheck + refLen, lagHi)
        let lagLo = max(1, lagHi - scanSpan)  // lag ≥ 1 so leftNorm exists

        guard lagHi > lagLo else {
            lastSnapshot = Snapshot(
                bufferFillSamples: totalWritten,
                lastPeak: lastSnapshot.lastPeak,
                threshold: threshold,
                armed: false,
                triggered: false,
                lastDownPeak: lastSnapshot.lastDownPeak,
                lastPSR: lastSnapshot.lastPSR,
                pendingUp: pendingUp != nil
            )
            return
        }

        let upResult = correlate(
            linear: linear, cumsq: cumsq,
            reference: referenceUp,
            lagLo: lagLo, lagHi: lagHi
        )
        let downResult = correlate(
            linear: linear, cumsq: cumsq,
            reference: referenceDown,
            lagLo: lagLo, lagHi: lagHi
        )
        // Feed every scan's bestNorm into the CFAR estimator. The
        // saturationCap inside CFARNoiseFloor excludes the chirp itself
        // (peaks > ~0.15 are assumed signal) so our own chirp can't
        // bias the noise floor upward.
        cfarUp.observe(upResult.bestNorm)
        cfarDown.observe(downResult.bestNorm)

        let now_s = currentPTSApprox()
        let armed = (lastTriggerPTS ?? -Double.infinity) + cooldownS <= now_s

        // Expire stale pending up-sweep. `now_s` tracks the newest sample,
        // but a down-sweep only becomes correlatable once its END sits in the
        // ring — i.e. `now_s ≥ up_center + chirpGap + chirpDuration/2`. The
        // expiry bound therefore has to hold the pending across that full
        // detection latency plus the pair-gap tolerance, not just the
        // center-to-center gap. An earlier version used just
        // `chirpGapCenterS + chirpPairToleranceS` and cleared pending right
        // before the legitimate down-sweep check — pairing never fired.
        let pendingHoldS = chirpGapCenterS + chirpDurationS + chirpPairToleranceS
        if let pu = pendingUp, now_s - pu.centerS > pendingHoldS {
            pendingUp = nil
        }

        // PSR denominator = best normalized value outside the ±exclusion zone
        // around the peak. On short startup scans the exclusion can blanket
        // the whole scan → no sidelobe samples → reject (PSR = 0) rather
        // than auto-accepting; subsequent passes with more samples will
        // re-evaluate.
        let upPSR = upResult.secondNorm > 0 ? upResult.bestNorm / upResult.secondNorm : 0
        let downPSR = downResult.secondNorm > 0 ? downResult.bestNorm / downResult.secondNorm : 0

        // CFAR gates: signal must also be multiple × current noise floor
        // on top of the absolute threshold. Before warm-up (minSamples),
        // CFAR stays transparent — absolute-threshold + PSR alone arbitrate.
        let upCFARReady = cfarUp.sampleCount >= cfarMinSamples
        let downCFARReady = cfarDown.sampleCount >= cfarMinSamples
        let upCFARGate = !upCFARReady
            || upResult.bestNorm > cfarNoiseMultiplier * cfarUp.estimate
        let downCFARGate = !downCFARReady
            || downResult.bestNorm > cfarNoiseMultiplier * cfarDown.estimate

        let upValid = armed && upResult.bestNorm > threshold
            && upPSR > minPSR && upCFARGate
        // Down-sweep validation has TWO tiers:
        //  - Strict (no pending): full PSR + threshold gate, same as up.
        //  - Loose (pending up waiting): the 150 ms timing prior already
        //    rules out random spikes, so PSR is dropped — a clean down with
        //    weak sidelobes (common when AGC clips after the up burst)
        //    still pairs. Standard "preamble-gated loose threshold" — what
        //    makes the dual-sweep design actually fire in real rooms.
        // CFAR gate applies in BOTH tiers — even a pending-latched down
        // has to rise above the noise floor. Otherwise an ambient hiss
        // peak at the right 150 ms offset would trigger a false pair.
        let downValid = armed && downResult.bestNorm > threshold
            && downCFARGate
            && (pendingUp != nil || downPSR > minPSR)

        var triggered = false

        if upValid {
            let upCenterS = timestampForChirpCenter(result: upResult)
            let isNewLatch = (pendingUp == nil)
            if isNewLatch
                || abs(upCenterS - (pendingUp?.centerS ?? 0)) > 0.010
                || upResult.bestNorm > (pendingUp?.peak ?? 0) {
                pendingUp = (centerS: upCenterS, peak: upResult.bestNorm)
                if isNewLatch {
                    log.info("chirp up latched peak=\(upResult.bestNorm, privacy: .public) psr=\(upPSR, privacy: .public) cfar_floor=\(self.cfarUp.estimate, privacy: .public) cfar_k=\(self.cfarNoiseMultiplier, privacy: .public) center_s=\(upCenterS, privacy: .public)")
                }
            }
        }

        if downValid, let pu = pendingUp {
            let downCenterS = timestampForChirpCenter(result: downResult)
            let gap = downCenterS - pu.centerS
            if abs(gap - chirpGapCenterS) < chirpPairToleranceS {
                // Doppler-free anchor: average of the two chirp centers.
                let anchorS = (pu.centerS + downCenterS) / 2.0
                lastTriggerPTS = anchorS
                pendingUp = nil
                triggered = true
                log.info("chirp pair detected up_peak=\(pu.peak, privacy: .public) down_peak=\(downResult.bestNorm, privacy: .public) down_psr=\(downPSR, privacy: .public) cfar_up_floor=\(self.cfarUp.estimate, privacy: .public) cfar_down_floor=\(self.cfarDown.estimate, privacy: .public) cfar_k=\(self.cfarNoiseMultiplier, privacy: .public) gap_s=\(gap, privacy: .public) anchor_s=\(anchorS, privacy: .public)")
                onChirpDetected?(
                    ChirpEvent(anchorFrameIndex: 0, anchorTimestampS: anchorS)
                )
            } else {
                // Down candidate found but the gap is wrong — log so the
                // operator sees how far off playback timing actually is.
                log.info("chirp down candidate rejected gap_s=\(gap, privacy: .public) expected_s=\(self.chirpGapCenterS, privacy: .public) tol_s=\(self.chirpPairToleranceS, privacy: .public) down_peak=\(downResult.bestNorm, privacy: .public)")
            }
        } else if downResult.bestNorm > threshold && pendingUp == nil {
            // Down arrived without a preceding up — usually means the up
            // was rejected (PSR/threshold) or playback was truncated.
            log.info("chirp down-only seen peak=\(downResult.bestNorm, privacy: .public) psr=\(downPSR, privacy: .public) (no pending up)")
        }

        lastSnapshot = Snapshot(
            bufferFillSamples: totalWritten,
            lastPeak: upResult.bestNorm,
            threshold: threshold,
            armed: armed,
            triggered: triggered,
            lastDownPeak: downResult.bestNorm,
            lastPSR: min(upPSR, downPSR),
            pendingUp: pendingUp != nil
        )
    }

    /// Dot-product sweep + normalization + PSR. Called once per reference per
    /// matched-filter pass.
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

        // Best normalized peak in scan range.
        var bestIdx = 0
        var bestNorm: Float = normValues[0]
        for idx in 1..<count {
            if normValues[idx] > bestNorm {
                bestNorm = normValues[idx]
                bestIdx = idx
            }
        }
        let bestLag = lagLo + bestIdx

        // Parabolic neighbours — clamp to scan edges if needed.
        let leftNorm = bestIdx > 0 ? normValues[bestIdx - 1] : bestNorm
        let rightNorm = bestIdx < count - 1 ? normValues[bestIdx + 1] : bestNorm

        // Second-best outside ±refLen/2 exclusion zone → PSR denominator.
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

    /// Absolute session-clock PTS of the chirp **center**, derived from the
    /// integer peak lag plus parabolic sub-sample refinement on normalized
    /// values (the raw-dot variant leaked local-energy ramps into the fit).
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

    // MARK: - PCM extraction

    /// Read the sample buffer's native sample rate from its ASBD. Returns nil
    /// when the buffer lacks a format description (shouldn't happen on
    /// `AVCaptureAudioDataOutput`, but stay defensive).
    private static func firstSampleRate(_ sampleBuffer: CMSampleBuffer) -> Double? {
        guard let fmt = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else {
            return nil
        }
        let rate = asbdPtr.pointee.mSampleRate
        return rate > 0 ? rate : nil
    }

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
