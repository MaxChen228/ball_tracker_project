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
/// ball detection. Kept lock-step with `server/detection.py`: same default
/// HSV range (yellow-green h[25,55] s[90,255] v[90,255]), same area bounds
/// ([20, 150000] px²), same shape gate (aspect ≥ 0.70, fill ≥ 0.55).
///
/// IMPORTANT: these thresholds (aspect / fill / area / HSV default) MUST be
/// kept in lock-step with `server/detection.py`. Any change here MUST also
/// land on the Python side — the whole point of the on-device pipeline is
/// byte-for-byte equivalence with the server.
///
/// `BTBallDetector` is the stateless per-frame path (no background model,
/// no ROI tracking), used by the live detection pipeline's concurrent pool
/// (`ConcurrentDetectionPool`). For a single-threaded stateful pipeline
/// (ROI tracking across frames), use `BTStatefulBallDetector` instead.
@interface BTBallDetector : NSObject

/// Run detection with the default HSV range.
/// Returns nil when no blob passes area + shape gating.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Run detection with a caller-supplied HSV range. Hue in 0–179, sat/val
/// in 0–255 — OpenCV's 8-bit HSV convention, same as the server.
/// Uses default shape gate (aspect ≥ 0.70, fill ≥ 0.55).
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax;

/// Run detection with caller-supplied HSV + shape gate thresholds.
/// `aspectMin` = min(w,h)/max(w,h) lower bound; `fillMin` = area/(w*h)
/// lower bound. Both ∈ [0, 1]. Match `ShapeGate` on the server so the
/// live path rejects the same blobs server_post would.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax
                                        aspectMin:(double)aspectMin
                                          fillMin:(double)fillMin;

/// Multi-candidate variant: return ALL blobs passing area+aspect+fill,
/// sorted by area desc. Empty array → no candidates. Used by the live
/// path to ship every survivor to the server (`schemas.BlobCandidate`),
/// which then applies the temporal-prior selector before pairing.
/// Single-blob callers can take the first element of the array.
+ (NSArray<BTBallDetection *> *)detectAllCandidatesInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                                            hMin:(int)hMin hMax:(int)hMax
                                                            sMin:(int)sMin sMax:(int)sMax
                                                            vMin:(int)vMin vMax:(int)vMax
                                                       aspectMin:(double)aspectMin
                                                         fillMin:(double)fillMin;

@end

/// Stateful per-frame detector that reuses work across frames:
///
/// - Keeps an internal ROI around the last successful hit (±3 × blob
///   radius, clamped to image bounds, minimum 256×256). HSV threshold +
///   connected-components run on that crop only, cutting ~95% of pixels
///   on a full-screen follow.
/// - On ROI miss, falls back to a full-frame pass **loudly** (NSLog
///   "BallDetector: ROI miss, falling back to full frame") — NOT a silent
///   fallback. After 10 consecutive misses the ROI state is dropped.
/// - Reuses cv::Mat scratch buffers (BGR intermediate + HSV + mask +
///   labels/stats/centroids) across frames so the 1080p path doesn't
///   re-alloc ~8 MB per frame.
///
/// Not thread-safe. Intended for a single capture-queue worker.
@interface BTStatefulBallDetector : NSObject

/// Default HSV range (yellow-green tennis ball). Mirrors
/// `HSVRange.default()` in server/detection.py.
- (instancetype)init;

/// Update the HSV range in place — no allocation, no state reset.
- (void)setHMin:(int)hMin hMax:(int)hMax
           sMin:(int)sMin sMax:(int)sMax
           vMin:(int)vMin vMax:(int)vMax;

/// Update the shape gate in place — no allocation, no state reset.
/// `aspectMin` ∈ [0, 1]; `fillMin` ∈ [0, 1]. Mirrors server `ShapeGate`.
- (void)setAspectMin:(double)aspectMin fillMin:(double)fillMin;

/// Run one frame through the ROI-assisted pipeline.
/// Returns nil when no blob passes area + shape gating on either the ROI
/// pass or the full-frame fallback.
- (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Multi-candidate ROI variant of `detectInPixelBuffer:`. Same ROI gating
/// as the single-best path but returns ALL blobs passing area+aspect+fill
/// in the winning region (ROI crop on hit, full-frame fallback on ROI
/// miss), sorted by area desc. Empty array → no candidates anywhere.
///
/// Tracking state is updated from the largest blob (`firstObject`) on
/// hit, mirroring the single-best detector. The server's temporal-prior
/// candidate selector may pick a smaller candidate as the actual ball,
/// but iOS doesn't get that decision back over the WS, so ROI follows
/// largest-blob with the same `kROIMaxConsecutiveMisses` recovery.
- (NSArray<BTBallDetection *> *)detectAllCandidatesInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Drop any cached ROI tracking state — call on session boundaries
/// (arm / disarm / re-entry to capture) so a stale hit from a prior
/// recording doesn't bias the first frame's crop.
- (void)resetTracking;

@end

NS_ASSUME_NONNULL_END
