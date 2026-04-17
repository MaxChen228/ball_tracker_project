import Foundation

/// Flash (torch/LED) detector using rolling luminance baseline.
/// Spec logic: baseline last 30 frames, trigger when current > baseline * thresholdMultiplier,
/// then ignore further spikes for 1 second to avoid double-triggering.
final class FlashDetector {
    struct FlashEvent {
        let flashFrameIndex: Int
        let flashTimestampS: Double
    }

    private var baseline: [Double] = []
    private let baselineWindowSize: Int = 30

    private let thresholdMultiplier: Double
    private let ignoreAfterSeconds: Double = 1.0

    private var lastFlashTimestampS: Double? = nil

    init(thresholdMultiplier: Double = 2.5) {
        self.thresholdMultiplier = thresholdMultiplier
    }

    func reset() {
        baseline.removeAll(keepingCapacity: true)
        lastFlashTimestampS = nil
    }

    func process(meanLuminance: Double, frameIndex: Int, timestampS: Double) -> FlashEvent? {
        baseline.append(meanLuminance)
        if baseline.count > baselineWindowSize {
            baseline.removeFirst()
        }

        guard baseline.count == baselineWindowSize else {
            return nil
        }

        if let last = lastFlashTimestampS, (timestampS - last) < ignoreAfterSeconds {
            return nil
        }

        let baselineMean = baseline.reduce(0.0, +) / Double(baseline.count)
        if meanLuminance > baselineMean * thresholdMultiplier {
            lastFlashTimestampS = timestampS
            return FlashEvent(flashFrameIndex: frameIndex, flashTimestampS: timestampS)
        }
        return nil
    }
}

