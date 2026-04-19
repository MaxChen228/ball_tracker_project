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
/// ball detection. Kept byte-for-byte equivalent with `server/detection.py`
/// so iOS-side on-device detection (used for recording trim decisions)
/// produces the same centroid the server would compute on the uploaded MOV.
///
/// Default HSV range targets a yellow-green tennis ball
/// (h[25,55] s[90,255] v[90,255], OpenCV hue scale 0–179). Override via the
/// expanded initialiser when the dashboard-driven HSV-config lands.
@interface BTBallDetector : NSObject

/// Run detection with the default HSV range (tennis-ball yellow-green).
/// Returns nil when no blob in area [20, 150000] px² survives.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Run detection with a caller-supplied HSV range. Hue in 0–179, sat/val
/// in 0–255 — both use OpenCV's 8-bit HSV convention, same as the server.
+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax;

@end

NS_ASSUME_NONNULL_END
