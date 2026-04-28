import CoreVideo
import Foundation

/// Per-frame ball-detection engine. Mirrors `server/detection_engine.py`'s
/// `DetectionEngine` Protocol — the abstraction lets a future ML engine
/// drop in alongside the existing HSV pipeline without touching
/// `ConcurrentDetectionPool` or `LiveFrameDispatcher`.
///
/// Implementations are NOT required to be thread-safe. The pool calls
/// every method (detect / resetTracking / engine-specific config) from a
/// single serial detection queue, so the engine is queue-confined for its
/// entire lifetime.
///
/// Versioning convention matches the server side: `<family>@<version>`.
/// iOS-side HSV is `hsv@ios.1.0` — distinct from the server-side
/// `hsv@1.0` because the input domain differs (BGRA sensor direct vs
/// H.264-decoded BGR), so archived data should remember which side ran.
protocol BallDetectionEngine: AnyObject {
    /// Stable identity stamped onto every produced FramePayload. Bumping
    /// the algorithm or weights ⇒ bump the version suffix.
    var name: String { get }

    /// Detect ball candidates in `pixelBuffer`. Returns every blob that
    /// passed the engine's gates, sorted descending by area. Empty array
    /// → no detection. Caller (the pool) owns area-score normalisation +
    /// FramePayload construction.
    func detect(in pixelBuffer: CVPixelBuffer) -> [BTBallDetection]

    /// Drop any cached per-frame tracking state — called on session
    /// boundaries (arm / disarm) so a stale anchor from the prior arm
    /// window doesn't bias the first frame of the next.
    func resetTracking()
}

/// Pre-defined engine identifiers used by the iOS-side detection paths.
/// Kept in one place so the wire stamp + the engine implementation can't
/// drift apart.
enum BallDetectionEngineID {
    static let hsvIOS = "hsv@ios.1.0"
}

/// HSV pipeline behind the `BallDetectionEngine` Protocol. Wraps the
/// existing `BTStatefulBallDetector` (Obj-C++ OpenCV implementation) so
/// the engine identity + config plumbing live in Swift while the inner
/// loop stays in C++.
///
/// HSV range and shape gate are runtime-tunable from the dashboard. The
/// pool snapshots them under its own lock and calls `applyConfig` from
/// the detection queue right before each `detect`, so settings updates
/// don't race the C++ detector's non-thread-safe setters.
final class HSVDetectionEngine: BallDetectionEngine {

    private let detector = BTStatefulBallDetector()

    var name: String { BallDetectionEngineID.hsvIOS }

    func detect(in pixelBuffer: CVPixelBuffer) -> [BTBallDetection] {
        return detector.detectAllCandidates(in: pixelBuffer)
    }

    func resetTracking() {
        detector.resetTracking()
    }

    /// Apply a fresh HSV range + shape gate snapshot. MUST be called from
    /// the same serial queue as `detect` — `BTStatefulBallDetector` is
    /// documented "Not thread-safe."
    func applyConfig(
        hsv: ServerUploader.HSVRangePayload,
        shape: ServerUploader.ShapeGatePayload
    ) {
        detector.setHMin(
            Int32(hsv.h_min),
            hMax: Int32(hsv.h_max),
            sMin: Int32(hsv.s_min),
            sMax: Int32(hsv.s_max),
            vMin: Int32(hsv.v_min),
            vMax: Int32(hsv.v_max)
        )
        detector.setAspectMin(shape.aspect_min, fillMin: shape.fill_min)
    }
}
