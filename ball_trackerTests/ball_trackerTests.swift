import Testing
@testable import ball_tracker

// MARK: - FlashDetector

struct FlashDetectorTests {

    private func fillBaseline(_ det: FlashDetector, lum: Double, count: Int, startIndex: Int = 0) {
        for i in 0..<count {
            _ = det.process(
                sampleLuminance: lum,
                frameIndex: startIndex + i,
                timestampS: Double(startIndex + i) * 0.004
            )
        }
    }

    @Test func triggersOnSharpSpikeAfterWarmup() {
        let det = FlashDetector(thresholdMultiplier: 1.8, minRiseAbsolute: 8, baselineWindowSize: 10)
        fillBaseline(det, lum: 50, count: 10)
        let event = det.process(sampleLuminance: 150, frameIndex: 10, timestampS: 0.040)
        #expect(event != nil)
        #expect(event?.flashFrameIndex == 10)
        #expect(det.lastSnapshot.triggered == true)
    }

    @Test func doesNotTriggerDuringWarmup() {
        let det = FlashDetector(baselineWindowSize: 30)
        fillBaseline(det, lum: 50, count: 29)
        let event = det.process(sampleLuminance: 300, frameIndex: 29, timestampS: 0.116)
        #expect(event == nil)
        #expect(det.lastSnapshot.armed == false)
    }

    @Test func rejectsInsufficientRatio() {
        let det = FlashDetector(thresholdMultiplier: 2.0, minRiseAbsolute: 8, baselineWindowSize: 10)
        fillBaseline(det, lum: 50, count: 10)
        // ratio = 90 / 50 = 1.8 < 2.0
        let event = det.process(sampleLuminance: 90, frameIndex: 10, timestampS: 0.040)
        #expect(event == nil)
    }

    @Test func rejectsInsufficientRise() {
        let det = FlashDetector(thresholdMultiplier: 1.5, minRiseAbsolute: 50, baselineWindowSize: 10)
        fillBaseline(det, lum: 50, count: 10)
        // ratio = 80 / 50 = 1.6 ✓, but rise = 80 - 50 = 30 < 50
        let event = det.process(sampleLuminance: 80, frameIndex: 10, timestampS: 0.040)
        #expect(event == nil)
    }

    @Test func cooldownBlocksSecondFlashWithinWindow() {
        let det = FlashDetector(
            thresholdMultiplier: 1.8,
            minRiseAbsolute: 8,
            baselineWindowSize: 10,
            cooldownSeconds: 1.0
        )
        fillBaseline(det, lum: 50, count: 10)
        let first = det.process(sampleLuminance: 150, frameIndex: 10, timestampS: 0.040)
        #expect(first != nil)
        // 0.5 s later: still in cooldown
        let second = det.process(sampleLuminance: 150, frameIndex: 11, timestampS: 0.540)
        #expect(second == nil)
        #expect(det.lastSnapshot.armed == false)
    }

    @Test func medianBaselineResistsSingleOutlier() {
        // Even if one frame of the baseline window is a flash-contaminated
        // outlier, median is unaffected, so the next real flash still triggers.
        let det = FlashDetector(
            thresholdMultiplier: 1.8,
            minRiseAbsolute: 8,
            baselineWindowSize: 10
        )
        for i in 0..<10 {
            let lum: Double = (i == 5) ? 250 : 50
            _ = det.process(sampleLuminance: lum, frameIndex: i, timestampS: Double(i) * 0.004)
        }
        #expect(det.lastSnapshot.baselineMedian == 50)
    }

    @Test func slowAmbientDriftDoesNotTrigger() {
        // Luminance rises by +4 each frame — under minRiseAbsolute=8.
        // Even as luminance doubles over time, the rising-edge gate blocks it.
        let det = FlashDetector(
            thresholdMultiplier: 1.8,
            minRiseAbsolute: 8,
            baselineWindowSize: 10
        )
        for i in 0..<30 {
            let lum = 50.0 + Double(i) * 4.0
            let e = det.process(sampleLuminance: lum, frameIndex: i, timestampS: Double(i) * 0.004)
            #expect(e == nil)
        }
    }

    @Test func resetClearsState() {
        let det = FlashDetector(baselineWindowSize: 5)
        fillBaseline(det, lum: 50, count: 5)
        #expect(det.lastSnapshot.armed == true)
        det.reset()
        #expect(det.lastSnapshot.armed == false)
        #expect(det.lastSnapshot.baselineMedian == 0)
    }

    @Test func snapshotReflectsRequiredThresholds() {
        let det = FlashDetector(thresholdMultiplier: 2.1, minRiseAbsolute: 12)
        _ = det.process(sampleLuminance: 50, frameIndex: 0, timestampS: 0)
        #expect(det.lastSnapshot.requiredRatio == 2.1)
        #expect(det.lastSnapshot.requiredRise == 12)
    }
}
