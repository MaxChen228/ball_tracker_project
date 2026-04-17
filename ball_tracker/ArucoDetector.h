#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreVideo/CoreVideo.h>

NS_ASSUME_NONNULL_BEGIN

/// One detected ArUco marker. Corners are in image pixel coords, ordered
/// top-left → top-right → bottom-right → bottom-left per OpenCV convention.
@interface BTArucoMarker : NSObject
@property(nonatomic, readonly) int markerId;
@property(nonatomic, readonly) CGPoint corner0;
@property(nonatomic, readonly) CGPoint corner1;
@property(nonatomic, readonly) CGPoint corner2;
@property(nonatomic, readonly) CGPoint corner3;
/// Mean of the four corners — useful as a stable "center" correspondence.
@property(nonatomic, readonly) CGPoint center;
@end

/// Thin Obj-C++ wrapper around cv::aruco::ArucoDetector (OpenCV 4.7+ main
/// objdetect module). Hardcoded to DICT_4X4_50 — the only dictionary the fixture
/// needs. Detection only; homography solving is a separate class method.
@interface BTArucoDetector : NSObject

/// Detect ArUco markers in a BGRA CVPixelBuffer (the format used by
/// `kCVPixelFormatType_32BGRA` video output). Converts to grayscale internally.
/// Returns an empty array when no markers are found or the buffer is invalid.
+ (NSArray<BTArucoMarker *> *)detectMarkersInPixelBuffer:(CVPixelBufferRef)pixelBuffer;

/// Solve a homography from (world X, Y) → (image u, v) pixel via least-squares
/// with RANSAC outlier rejection. `worldPoints` and `imagePoints` must be the
/// same length (≥4) of CGPoint NSValues. Returns a length-9 NSArray<NSNumber *>
/// (row-major, h33 normalized to 1) or nil on failure.
+ (nullable NSArray<NSNumber *> *)findHomographyFromWorldPoints:(NSArray<NSValue *> *)worldPoints
                                                   imagePoints:(NSArray<NSValue *> *)imagePoints;

@end

NS_ASSUME_NONNULL_END
