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
/// `BTBallDetector` is the stateless per-frame path (no background model).
/// For the mode-two pipeline use `BTDetectionSession` which owns the MOG2
/// background subtractor and warmup counter — matching `pipeline.detect_pitch`.
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

/// Stateful mirror of `server/pipeline.detect_pitch`'s MOG2 + detection loop.
///
/// One session = one recording cycle. Owns a `cv::BackgroundSubtractorMOG2`
/// (detectShadows=False) built up across successive frames and a warmup
/// counter — `applyPixelBuffer:` returns nil for the first 30 frames while
/// the per-pixel Gaussian model stabilises, then HSV mask AND fg_mask AND
/// morphological CLOSE gate into the usual area + shape filter.
///
/// Threading: not thread-safe. Create one session per recording cycle and
/// drive it from a single detection queue.
@interface BTDetectionSession : NSObject

/// New session with the default HSV range + 30 frame warmup.
- (instancetype)init;

/// New session with a caller-supplied HSV range + 30 frame warmup.
- (instancetype)initWithHMin:(int)hMin hMax:(int)hMax
                        sMin:(int)sMin sMax:(int)sMax
                        vMin:(int)vMin vMax:(int)vMax;

/// `frameIndex` < warmupFrames means the MOG2 background model isn't yet
/// reliable. `applyPixelBuffer:` during warmup still accumulates into the
/// model but returns nil so callers can gate "valid detection" logic.
@property (nonatomic, readonly) NSInteger warmupFrames;
@property (nonatomic, readonly) NSInteger frameIndex;

/// Feed one frame into the MOG2 model and (post-warmup) run detection
/// on the combined HSV-AND-fg_mask blob mask. Returns the largest blob
/// passing the area + shape gate, or nil if none / still warming up.
- (nullable BTBallDetection *)applyPixelBuffer:(CVPixelBufferRef)pixelBuffer
    NS_SWIFT_NAME(apply(_:));

@end

NS_ASSUME_NONNULL_END
