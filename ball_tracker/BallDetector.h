#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreVideo/CoreVideo.h>

NS_ASSUME_NONNULL_BEGIN

/// One ball detection — image-pixel centroid + blob area. Mirrors what
/// server/detection.py returns (tuple[float, float] for the centroid, area
/// kept as int for debugging). Pixel coords are in the same grid as the
/// pixel buffer passed to the detector, i.e. match whichever capture
/// resolution is active.
@interface BTBallDetection : NSObject
@property(nonatomic, readonly) CGFloat px;
@property(nonatomic, readonly) CGFloat py;
@property(nonatomic, readonly) NSInteger areaPx;
@end

/// Obj-C++ OpenCV wrapper for HSV-threshold + connectedComponentsWithStats
/// ball detection.
///
/// Shared-constants contract with the server: same default HSV range
/// (yellow-green h[25,55] s[90,255] v[90,255]), same area bounds
/// ([20, 150000] px²), same shape gate (aspect ≥ 0.75, fill ≥ 0.60).
///
/// IMPORTANT — the two pipelines are NOT byte-for-byte equivalent:
///   - iOS (`BTBallDetector`): stateless per-frame HSV + connected-components
///     + shape-gate only. No temporal smoothing, no background subtraction.
///   - Server (`server/pipeline.py`): runs the same HSV + CC + shape-gate,
///     BUT by default also runs an OpenCV MOG2 background subtractor with a
///     30-frame warmup window (≈125 ms at 240 fps) during which detection
///     is FORCED to return `None`.
///
/// Consequence: for the first ~125 ms of each take the `live` path (iOS)
/// may produce detections while the `server_post` path (server re-decode)
/// cannot. This is a known, deliberate asymmetry introduced to suppress
/// static-background false positives on the archival path — not a bug and
/// not something to "fix" by silently tuning one side.
///
/// TODO: if strict symmetry is ever required, either (a) add a matching
/// MOG2 warmup to `ConcurrentDetectionPool` so the live path swallows the
/// same opening frames, or (b) flip the server's MOG2 default off. Both
/// are product decisions, not detector-local tweaks.
///
/// `BTBallDetector` is the stateless per-frame path, used by the live
/// detection pipeline (`ConcurrentDetectionPool`).
@interface BTBallDetector : NSObject

/// Run detection with the default HSV range.
/// Returns nil when no blob passes area + shape gating.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Run detection with a caller-supplied HSV range. Hue in 0–179, sat/val
/// in 0–255 — OpenCV's 8-bit HSV convention, same as the server.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax;

@end

NS_ASSUME_NONNULL_END
