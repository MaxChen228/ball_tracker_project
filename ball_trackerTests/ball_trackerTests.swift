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
    }
}
