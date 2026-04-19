import CoreMedia
import Testing
@testable import ball_tracker

// MARK: - AudioChirpDetector

struct AudioChirpDetectorTests {

    @Test func chirpGeneratorProducesUnitEnergy() {
        let samples = AudioChirpDetector.makeChirp(
            sampleRate: 44100.0, f0: 2000.0, f1: 8000.0, duration: 0.1
        )
        #expect(samples.count == 4410)
        // Unit-energy normalization means sum(x²) ≈ 1.
        let energy = samples.reduce(Float(0)) { $0 + $1 * $1 }
        #expect(abs(energy - 1.0) < 1e-4)
    }

    @Test func chirpGeneratorStartsAndEndsQuiet() {
        // Hann window → first/last samples are near zero.
        let samples = AudioChirpDetector.makeChirp(
            sampleRate: 44100.0, f0: 2000.0, f1: 8000.0, duration: 0.1
        )
        #expect(abs(samples.first ?? 1.0) < 1e-3)
        #expect(abs(samples.last ?? 1.0) < 1e-3)
    }

    @Test func downSweepIsAlsoUnitEnergy() {
        // Dual-chirp anchor relies on both sweeps being unit-energy so the
        // normalized matched-filter peaks are comparable to the same
        // threshold.
        let samples = AudioChirpDetector.makeChirp(
            sampleRate: 44100.0, f0: 8000.0, f1: 2000.0, duration: 0.1
        )
        let energy = samples.reduce(Float(0)) { $0 + $1 * $1 }
        #expect(abs(energy - 1.0) < 1e-4)
    }

    @Test func upAndDownSweepsAreDistinct() {
        // Must not collapse to the same waveform — their cross-correlation
        // should be much smaller than each self-correlation (unit energy).
        let up = AudioChirpDetector.makeChirp(
            sampleRate: 44100.0, f0: 2000.0, f1: 8000.0, duration: 0.1
        )
        let down = AudioChirpDetector.makeChirp(
            sampleRate: 44100.0, f0: 8000.0, f1: 2000.0, duration: 0.1
        )
        var dot: Float = 0
        for i in 0..<up.count { dot += up[i] * down[i] }
        #expect(abs(dot) < 0.2)   // well below the 0.18 trigger threshold
    }

    @Test func defaultConfigStartsDisarmed() {
        let detector = AudioChirpDetector()
        let snap = detector.lastSnapshot
        #expect(snap.armed == false)
        #expect(snap.triggered == false)
        #expect(snap.bufferFillSamples == 0)
    }

    @Test func resetRestoresInitialSnapshot() async {
        let detector = AudioChirpDetector(threshold: 0.42)
        detector.reset()
        // reset bounces onto the delivery queue; give it a tick.
        try? await Task.sleep(nanoseconds: 50_000_000)
        let snap = detector.lastSnapshot
        #expect(snap.armed == false)
        #expect(snap.triggered == false)
        #expect(snap.lastPeak == 0)
        #expect(snap.threshold == 0.42)
        #expect(snap.pendingUp == false)
    }

    @Test func dualChirpPairFiresAtMidpoint() async {
        // End-to-end: synthesize a clean signal containing
        // [silence | up-sweep | gap | down-sweep | silence], feed it through
        // the detector, and assert `onChirpDetected` fires with the anchor
        // timestamp at the midpoint between the two chirp centers.
        //
        // This is the guard against the pending-expiry bug: if the pending
        // up-sweep timed out before its down-sweep partner became
        // correlatable, no event would fire here.
        let sr = 44100.0
        let chirpDur = 0.1
        let gapSilence = 0.05
        let leadingSilence = 0.2
        let trailingSilence = 0.2

        let up = AudioChirpDetector.makeChirp(
            sampleRate: sr, f0: 2000.0, f1: 8000.0, duration: chirpDur
        )
        let down = AudioChirpDetector.makeChirp(
            sampleRate: sr, f0: 8000.0, f1: 2000.0, duration: chirpDur
        )
        let lead = [Float](repeating: 0, count: Int(sr * leadingSilence))
        let midGap = [Float](repeating: 0, count: Int(sr * gapSilence))
        let tail = [Float](repeating: 0, count: Int(sr * trailingSilence))
        let signal = lead + up + midGap + down + tail

        let detector = AudioChirpDetector(sampleRate: sr)
        let received = Locked<[AudioChirpDetector.ChirpEvent]>([])
        detector.onChirpDetected = { event in received.withValue { $0.append(event) } }

        // PTS base = 1.0 s so the anchor comparison is meaningful.
        let basePTS = CMTime(value: 44100, timescale: 44100)
        detector._testFeed(samples: signal, sampleRate: sr, firstPTS: basePTS)

        let events = received.value
        #expect(events.count == 1, "expected exactly one pair-fired event, got \(events.count)")
        guard let event = events.first else { return }

        // Up-sweep center = leadingSilence + chirpDur/2 (seconds from firstPTS).
        // Down-sweep center = leadingSilence + chirpDur + gapSilence + chirpDur/2.
        // Midpoint = leadingSilence + chirpDur + gapSilence/2.
        let expectedAnchorOffset = leadingSilence + chirpDur + gapSilence / 2.0
        let expected = CMTimeGetSeconds(basePTS) + expectedAnchorOffset
        #expect(abs(event.anchorTimestampS - expected) < 0.002)  // <2 ms
    }

    @Test func dualChirpFiresUnderNoiseAndAttenuatedDown() {
        // Mimics the field failure mode that the clean integration test
        // missed: the down-sweep arrives weaker than the up-sweep (mic AGC
        // pulled gain after the up burst) and is buried in white noise that
        // raises sidelobes. Without the "drop PSR for down when pendingUp
        // is set" rule, this case fails the down-side PSR gate and never
        // pairs.
        let sr = 44100.0
        let chirpDur = 0.1
        let gapSilence = 0.05
        let leadingSilence = 0.2
        let trailingSilence = 0.2

        let up = AudioChirpDetector.makeChirp(
            sampleRate: sr, f0: 2000.0, f1: 8000.0, duration: chirpDur
        )
        var down = AudioChirpDetector.makeChirp(
            sampleRate: sr, f0: 8000.0, f1: 2000.0, duration: chirpDur
        )
        // Halve the down-sweep amplitude — emulates AGC suppressing the
        // second burst. The unit-energy reference still cross-correlates,
        // but the normalized peak is lower and sidelobes look proportionally
        // larger relative to it.
        for i in 0..<down.count { down[i] *= 0.5 }

        let lead = [Float](repeating: 0, count: Int(sr * leadingSilence))
        let midGap = [Float](repeating: 0, count: Int(sr * gapSilence))
        let tail = [Float](repeating: 0, count: Int(sr * trailingSilence))
        var signal: [Float] = []
        signal.reserveCapacity(lead.count + up.count + midGap.count + down.count + tail.count)
        signal.append(contentsOf: lead)
        signal.append(contentsOf: up)
        signal.append(contentsOf: midGap)
        signal.append(contentsOf: down)
        signal.append(contentsOf: tail)

        // Add deterministic pseudo-random white noise across the full track
        // so PSR-relevant sidelobes are non-zero everywhere — including the
        // down-sweep window. Amplitude tuned to put SNR around the realistic
        // field range (peaks 0.3-0.5).
        var rng: UInt32 = 0x1357_9bdf
        for i in 0..<signal.count {
            rng = rng &* 1_103_515_245 &+ 12_345
            let r = Float(Int32(bitPattern: rng)) / Float(Int32.max)
            signal[i] += 0.04 * r
        }

        let detector = AudioChirpDetector(sampleRate: sr)
        let received = Locked<[AudioChirpDetector.ChirpEvent]>([])
        detector.onChirpDetected = { event in received.withValue { $0.append(event) } }
        detector._testFeed(
            samples: signal, sampleRate: sr,
            firstPTS: CMTime(value: 44100, timescale: 44100)
        )

        #expect(received.value.count == 1, "noisy + attenuated down should still pair")
    }
}

/// Tiny async-safe box for collecting callback values in Swift Testing tests.
private final class Locked<T>: @unchecked Sendable {
    private var _value: T
    private let lock = NSLock()
    init(_ initial: T) { self._value = initial }
    var value: T { lock.lock(); defer { lock.unlock() }; return _value }
    func withValue(_ body: (inout T) -> Void) {
        lock.lock(); defer { lock.unlock() }
        body(&_value)
    }
}
