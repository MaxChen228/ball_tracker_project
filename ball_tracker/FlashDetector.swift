import Foundation

/// Flash (torch/LED) detector. Triggers on a rapid rise in scene luminance that
/// also clears a multiplicative threshold above a rolling *median* baseline.
///
/// Why a median baseline: AVCaptureDevice auto-exposure reacts within 1–2 frames
/// when torch turns on, so a *mean* baseline contaminated by those frames
/// collapses the ratio. Median over 30 frames is robust to 2–6 flash frames
/// embedded in the window.
///
/// Why the rising-edge gate: environmental slow-varying brightness (cloud,
/// someone walking past) can drive the ratio up without being a flash. Requiring
/// a same-frame absolute rise (`current - previous > minRiseAbsolute`) filters
/// those out. The torch produces a sharp step; ambient drift does not.
///
/// Upstream should also lock the capture device's `exposureMode` while the
/// detector is active; otherwise AE still partially neutralizes the flash.
final class FlashDetector {
    struct FlashEvent {
        let flashFrameIndex: Int
        let flashTimestampS: Double
    }

    /// Live state exposed to a debug HUD. Written every `process` call so the
    /// UI can show why the detector is (or isn't) triggering.
    struct Snapshot {
        let currentLuminance: Double
        let baselineMedian: Double
        let ratio: Double         // current / baselineMedian (0 when baseline empty)
        let rise: Double          // current - previousLuminance (0 on first frame)
        let requiredRatio: Double
        let requiredRise: Double
        let armed: Bool           // baseline full and not in cooldown
        let triggered: Bool       // this frame produced a FlashEvent
    }

    // Config
    private let thresholdMultiplier: Double
    private let minRiseAbsolute: Double
    private let baselineWindowSize: Int
    private let cooldownSeconds: Double

    // Mutable state
    private var baseline: [Double] = []
    private var previousLuminance: Double?
    private var lastFlashTimestampS: Double?
    private(set) var lastSnapshot: Snapshot

    init(
        thresholdMultiplier: Double = 1.8,
        minRiseAbsolute: Double = 8.0,
        baselineWindowSize: Int = 30,
        cooldownSeconds: Double = 1.0
    ) {
        self.thresholdMultiplier = thresholdMultiplier
        self.minRiseAbsolute = minRiseAbsolute
        self.baselineWindowSize = baselineWindowSize
        self.cooldownSeconds = cooldownSeconds
        self.lastSnapshot = Snapshot(
            currentLuminance: 0, baselineMedian: 0, ratio: 0, rise: 0,
            requiredRatio: thresholdMultiplier, requiredRise: minRiseAbsolute,
            armed: false, triggered: false
        )
    }

    func reset() {
        baseline.removeAll(keepingCapacity: true)
        previousLuminance = nil
        lastFlashTimestampS = nil
        lastSnapshot = Snapshot(
            currentLuminance: 0, baselineMedian: 0, ratio: 0, rise: 0,
            requiredRatio: thresholdMultiplier, requiredRise: minRiseAbsolute,
            armed: false, triggered: false
        )
    }

    @discardableResult
    func process(sampleLuminance: Double, frameIndex: Int, timestampS: Double) -> FlashEvent? {
        baseline.append(sampleLuminance)
        if baseline.count > baselineWindowSize {
            baseline.removeFirst()
        }

        let median = Self.median(of: baseline)
        let rise = previousLuminance.map { sampleLuminance - $0 } ?? 0
        let ratio = median > 0 ? sampleLuminance / median : 0

        let inCooldown: Bool
        if let last = lastFlashTimestampS {
            inCooldown = (timestampS - last) < cooldownSeconds
        } else {
            inCooldown = false
        }
        let armed = baseline.count == baselineWindowSize && !inCooldown

        var triggered = false
        var event: FlashEvent? = nil
        if armed && ratio > thresholdMultiplier && rise > minRiseAbsolute {
            lastFlashTimestampS = timestampS
            triggered = true
            event = FlashEvent(flashFrameIndex: frameIndex, flashTimestampS: timestampS)
        }

        lastSnapshot = Snapshot(
            currentLuminance: sampleLuminance,
            baselineMedian: median,
            ratio: ratio,
            rise: rise,
            requiredRatio: thresholdMultiplier,
            requiredRise: minRiseAbsolute,
            armed: armed,
            triggered: triggered
        )
        previousLuminance = sampleLuminance
        return event
    }

    private static func median(of values: [Double]) -> Double {
        if values.isEmpty { return 0 }
        let sorted = values.sorted()
        let mid = sorted.count / 2
        if sorted.count.isMultiple(of: 2) {
            return (sorted[mid - 1] + sorted[mid]) / 2.0
        }
        return sorted[mid]
    }
}
