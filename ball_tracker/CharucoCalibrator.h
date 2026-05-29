#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreVideo/CoreVideo.h>

NS_ASSUME_NONNULL_BEGIN

/// Lightweight per-frame ChArUco evaluation for the live preview path.
/// Drives the auto-trigger state machine in `IntrinsicsCaptureController`:
/// no accumulation, no calibrateCamera call. Pose fields use a bootstrap K
/// derived from a 30° half-FOV guess — fine for pose-fingerprint diversity
/// detection, NOT a calibrated result.
@interface BTCharucoFrameEval : NSObject
@property(nonatomic, readonly) NSInteger cornerCount;
/// convex-hull(corners) area / image area. Cheap proxy for "is the board
/// large enough to give useful calibration signal".
@property(nonatomic, readonly) CGFloat boardFillRatio;
/// Rodrigues rotation vector (radians) from solvePnP with bootstrap K.
@property(nonatomic, readonly) CGPoint rvecXY; // x,y component
@property(nonatomic, readonly) CGFloat rvecZ;
/// Translation vector (meters) from solvePnP with bootstrap K.
@property(nonatomic, readonly) CGFloat tvecMagnitudeM;
@property(nonatomic, readonly) CGFloat tvecYaw;     // atan2(tx, tz)
@property(nonatomic, readonly) CGFloat tvecPitch;   // atan2(ty, tz)
/// variance-of-Laplacian on the grayscale board ROI. Scale of value depends
/// on lighting — use rolling-median × ratio in the state machine, never
/// a fixed threshold.
@property(nonatomic, readonly) CGFloat sharpnessVar;
@end

/// Result of `cv::calibrateCamera` ported from
/// `server/calibrate_intrinsics.py:172-191`. Distortion is truncated to
/// exactly 5 coefficients to match the server-side `_validate_intrinsics_payload`
/// gate at `server/routes/calibration_intrinsics.py:157-164`.
@interface BTCharucoSolveResult : NSObject
@property(nonatomic, readonly) CGFloat fx;
@property(nonatomic, readonly) CGFloat fy;
@property(nonatomic, readonly) CGFloat cx;
@property(nonatomic, readonly) CGFloat cy;
@property(nonatomic, readonly) NSArray<NSNumber *> *distortion; // count == 5
@property(nonatomic, readonly) CGFloat rmsReprojectionPx;
@property(nonatomic, readonly) NSInteger imageWidth;
@property(nonatomic, readonly) NSInteger imageHeight;
@property(nonatomic, readonly) NSInteger numImagesUsed;
@end

/// Obj-C++ bridge around `cv::aruco::CharucoDetector` (preview eval) and
/// `cv::calibrateCamera` (solve). 1:1 port of `server/calibrate_intrinsics.py`
/// — same board spec (5×7, square 0.040m, marker 0.030m, DICT_4X4_50),
/// same `matchImagePoints` dtype rule (CV_32F corners + CV_32S ids), same
/// 5-coeff distortion default (no RATIONAL_MODEL).
@interface BTCharucoCalibrator : NSObject

- (instancetype)init;

/// Preview-rate evaluation. NV12 or BGRA `CVPixelBufferRef` both accepted
/// (same convention as BallDetector.mm). Returns `nil` if no board detected.
- (nullable BTCharucoFrameEval *)evaluatePreviewFrame:(CVPixelBufferRef)pixelBuffer;

/// Heavy: decode high-res JPEG (4032×3024), detect ChArUco, run
/// `matchImagePoints`, append the (objPts, imgPts) pair to the internal
/// accumulator if successful. Returns YES on accepted, NO otherwise.
/// Caller decides whether to trust the shot — wrapper just reports
/// "have valid correspondences".
- (BOOL)acceptShotFromJPEG:(NSData *)jpeg
                imageWidth:(NSInteger *)outWidth
               imageHeight:(NSInteger *)outHeight
                     error:(NSError * _Nullable * _Nullable)error;

/// Number of accepted shots accumulated so far.
@property(nonatomic, readonly) NSInteger shotCount;

/// Resets accumulator. Does not change board spec.
- (void)reset;

/// Runs cv::calibrateCamera on accumulated shots. `imageWidth/Height`
/// must match the JPEG dims passed to acceptShotFromJPEG (asserted).
/// Returns nil + error if shotCount < 4 or solver fails.
- (nullable BTCharucoSolveResult *)solveWithImageWidth:(NSInteger)imageWidth
                                           imageHeight:(NSInteger)imageHeight
                                                 error:(NSError * _Nullable * _Nullable)error;

@end

NS_ASSUME_NONNULL_END
