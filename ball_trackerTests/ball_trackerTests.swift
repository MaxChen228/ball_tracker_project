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
