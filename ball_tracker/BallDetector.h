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
/// ([20, 150000] px²), same shape gate (aspect ≥ 0.75, fill ≥ 0.60).
///
/// `BTBallDetector` is the stateless per-frame path (no background model),
/// used by the live detection pipeline (`ConcurrentDetectionPool`).
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
