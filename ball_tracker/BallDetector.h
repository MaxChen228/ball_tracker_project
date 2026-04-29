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
/// min(w,h)/max(w,h) of the CC bounding box. 1.0 = perfectly square
/// (round ball ≈ 1). Server-side selector cost reads this — scale-
/// invariant geometric signal that survives the ball flying near→far.
@property(nonatomic, readonly) CGFloat aspect;
/// area / (w*h). Empirical median for the project blue ball is ~0.68
/// (memory: project_ball_empirical_fill). Also scale-invariant.
@property(nonatomic, readonly) CGFloat fill;
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
/// no ROI tracking), used by the live detection pipeline's worker
/// (`ConcurrentDetectionPool`).
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

NS_ASSUME_NONNULL_END
