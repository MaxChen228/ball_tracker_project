#import "CharucoCalibrator.h"

#import <opencv2/opencv2.h>
#import <opencv2/imgproc.hpp>
#import <opencv2/calib3d.hpp>
#import <opencv2/objdetect.hpp>

#import <vector>

// ---------------------------------------------------------------------------
// Board spec — hardcoded to match `server/calibrate_intrinsics.py` defaults
// (5×7 squares, 0.040m square, 0.030m marker, DICT_4X4_50). If you change
// these you MUST also change the CLI defaults or remove the CLI entirely
// before the algorithms diverge.
// ---------------------------------------------------------------------------
static const int kSquaresX = 5;
static const int kSquaresY = 7;
static const float kSquareLengthM = 0.040f;
static const float kMarkerLengthM = 0.030f;

// Bootstrap K used only for solvePnP during live preview evaluation (pose
// fingerprint diversity detection). Assumes ~30° half-FOV horizontal —
// good enough to differentiate "this is a different orientation" but NOT
// a calibrated K. The actual calibrated K comes out of solveWithImageWidth:
// at the end of the flow.
static cv::Mat bootstrapK(int width, int height) {
    const double halfFovRad = 30.0 * M_PI / 180.0;
    const double fx = (double)width / 2.0 / std::tan(halfFovRad);
    cv::Mat K = cv::Mat::eye(3, 3, CV_64F);
    K.at<double>(0, 0) = fx;
    K.at<double>(1, 1) = fx; // square pixels assumption for bootstrap
    K.at<double>(0, 2) = width / 2.0;
    K.at<double>(1, 2) = height / 2.0;
    return K;
}

// ---------------------------------------------------------------------------
// Pixel-buffer → grayscale cv::Mat. Mirrors BallDetector.mm:
// NV12 (preview frames from AVCaptureSession) and BGRA (test fixtures)
// both accepted. NV12's Y plane is already grayscale — no conversion needed,
// which keeps the preview eval hot path light.
// ---------------------------------------------------------------------------
static bool mapPixelBufferToGray(CVPixelBufferRef pb, cv::Mat &out) {
    CVPixelBufferLockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
    const size_t w = CVPixelBufferGetWidth(pb);
    const size_t h = CVPixelBufferGetHeight(pb);
    const OSType fmt = CVPixelBufferGetPixelFormatType(pb);
    bool ok = false;
    if (fmt == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
        fmt == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange) {
        uint8_t *yPlane = (uint8_t *)CVPixelBufferGetBaseAddressOfPlane(pb, 0);
        const size_t yStride = CVPixelBufferGetBytesPerRowOfPlane(pb, 0);
        cv::Mat yMat((int)h, (int)w, CV_8UC1, yPlane, yStride);
        yMat.copyTo(out); // detach from pixel buffer so unlock is safe
        ok = true;
    } else if (fmt == kCVPixelFormatType_32BGRA) {
        uint8_t *base = (uint8_t *)CVPixelBufferGetBaseAddress(pb);
        const size_t stride = CVPixelBufferGetBytesPerRow(pb);
        cv::Mat bgra((int)h, (int)w, CV_8UC4, base, stride);
        cv::cvtColor(bgra, out, cv::COLOR_BGRA2GRAY);
        ok = true;
    }
    CVPixelBufferUnlockBaseAddress(pb, kCVPixelBufferLock_ReadOnly);
    return ok;
}

// ---------------------------------------------------------------------------
// Private interfaces — backing storage for the public read-only properties.
// ---------------------------------------------------------------------------
@interface BTCharucoFrameEval ()
- (instancetype)initWithCornerCount:(NSInteger)cornerCount
                     boardFillRatio:(CGFloat)boardFillRatio
                            rvecXY:(CGPoint)rvecXY
                             rvecZ:(CGFloat)rvecZ
                    tvecMagnitudeM:(CGFloat)tvecMagnitudeM
                          tvecYaw:(CGFloat)tvecYaw
                        tvecPitch:(CGFloat)tvecPitch
                      sharpnessVar:(CGFloat)sharpnessVar;
@end

@implementation BTCharucoFrameEval
- (instancetype)initWithCornerCount:(NSInteger)cornerCount
                     boardFillRatio:(CGFloat)boardFillRatio
                            rvecXY:(CGPoint)rvecXY
                             rvecZ:(CGFloat)rvecZ
                    tvecMagnitudeM:(CGFloat)tvecMagnitudeM
                          tvecYaw:(CGFloat)tvecYaw
                        tvecPitch:(CGFloat)tvecPitch
                      sharpnessVar:(CGFloat)sharpnessVar {
    if ((self = [super init])) {
        _cornerCount = cornerCount;
        _boardFillRatio = boardFillRatio;
        _rvecXY = rvecXY;
        _rvecZ = rvecZ;
        _tvecMagnitudeM = tvecMagnitudeM;
        _tvecYaw = tvecYaw;
        _tvecPitch = tvecPitch;
        _sharpnessVar = sharpnessVar;
    }
    return self;
}
@end

@interface BTCharucoSolveResult ()
- (instancetype)initWithFx:(CGFloat)fx fy:(CGFloat)fy cx:(CGFloat)cx cy:(CGFloat)cy
                distortion:(NSArray<NSNumber *> *)distortion
         rmsReprojectionPx:(CGFloat)rmsReprojectionPx
                imageWidth:(NSInteger)imageWidth
               imageHeight:(NSInteger)imageHeight
             numImagesUsed:(NSInteger)numImagesUsed;
@end

@implementation BTCharucoSolveResult
- (instancetype)initWithFx:(CGFloat)fx fy:(CGFloat)fy cx:(CGFloat)cx cy:(CGFloat)cy
                distortion:(NSArray<NSNumber *> *)distortion
         rmsReprojectionPx:(CGFloat)rmsReprojectionPx
                imageWidth:(NSInteger)imageWidth
               imageHeight:(NSInteger)imageHeight
             numImagesUsed:(NSInteger)numImagesUsed {
    if ((self = [super init])) {
        _fx = fx; _fy = fy; _cx = cx; _cy = cy;
        _distortion = [distortion copy];
        _rmsReprojectionPx = rmsReprojectionPx;
        _imageWidth = imageWidth;
        _imageHeight = imageHeight;
        _numImagesUsed = numImagesUsed;
    }
    return self;
}
@end

// ---------------------------------------------------------------------------
@implementation BTCharucoCalibrator {
    cv::Ptr<cv::aruco::Dictionary> _dict;
    cv::Ptr<cv::aruco::CharucoBoard> _board;
    cv::Ptr<cv::aruco::CharucoDetector> _detector;
    std::vector<cv::Mat> _objPts; // per-view object points (Nx1x3, CV_32F)
    std::vector<cv::Mat> _imgPts; // per-view image points  (Nx1x2, CV_32F)
}

- (instancetype)init {
    if ((self = [super init])) {
        cv::aruco::Dictionary d = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50);
        _dict = cv::makePtr<cv::aruco::Dictionary>(d);
        cv::aruco::CharucoBoard b(cv::Size(kSquaresX, kSquaresY),
                                  kSquareLengthM, kMarkerLengthM, *_dict);
        _board = cv::makePtr<cv::aruco::CharucoBoard>(b);
        cv::aruco::CharucoDetector det(*_board);
        _detector = cv::makePtr<cv::aruco::CharucoDetector>(det);
    }
    return self;
}

- (NSInteger)shotCount { return (NSInteger)_objPts.size(); }

- (void)reset {
    _objPts.clear();
    _imgPts.clear();
}

- (nullable BTCharucoFrameEval *)evaluatePreviewFrame:(CVPixelBufferRef)pixelBuffer {
    cv::Mat gray;
    if (!mapPixelBufferToGray(pixelBuffer, gray)) return nil;
    if (gray.empty()) return nil;

    cv::Mat charucoCorners, charucoIds;
    std::vector<std::vector<cv::Point2f>> markerCorners;
    std::vector<int> markerIds;
    _detector->detectBoard(gray, charucoCorners, charucoIds, markerCorners, markerIds);
    if (charucoCorners.empty() || charucoIds.empty()) return nil;
    const int n = charucoCorners.rows;
    if (n < 4) return nil;

    // Board fill ratio = convex hull area / image area.
    std::vector<cv::Point2f> pts;
    pts.reserve(n);
    for (int i = 0; i < n; ++i) {
        pts.push_back(charucoCorners.at<cv::Point2f>(i, 0));
    }
    std::vector<cv::Point2f> hull;
    cv::convexHull(pts, hull);
    const double hullArea = cv::contourArea(hull);
    const double imgArea = (double)gray.cols * (double)gray.rows;
    const double fillRatio = imgArea > 0 ? hullArea / imgArea : 0.0;

    // Sharpness — variance of Laplacian on board ROI.
    cv::Rect bbox = cv::boundingRect(pts);
    bbox &= cv::Rect(0, 0, gray.cols, gray.rows);
    double sharpness = 0.0;
    if (bbox.area() > 64) {
        cv::Mat lap;
        cv::Laplacian(gray(bbox), lap, CV_64F);
        cv::Scalar mean, stddev;
        cv::meanStdDev(lap, mean, stddev);
        sharpness = stddev[0] * stddev[0];
    }

    // Pose estimation with bootstrap K — for fingerprint only.
    CGPoint rvecXY = CGPointMake(0, 0);
    CGFloat rvecZ = 0;
    CGFloat tvecMag = 0, tvecYaw = 0, tvecPitch = 0;
    cv::Mat corners32, ids32;
    charucoCorners.convertTo(corners32, CV_32F);
    charucoIds.convertTo(ids32, CV_32S);
    cv::Mat objPts, imgPts;
    _board->matchImagePoints(corners32, ids32, objPts, imgPts);
    if (!objPts.empty() && objPts.rows >= 4) {
        cv::Mat K = bootstrapK(gray.cols, gray.rows);
        cv::Mat dist = cv::Mat::zeros(5, 1, CV_64F);
        cv::Mat rvec, tvec;
        bool ok = cv::solvePnP(objPts, imgPts, K, dist, rvec, tvec,
                               false, cv::SOLVEPNP_ITERATIVE);
        if (ok) {
            rvecXY = CGPointMake(rvec.at<double>(0), rvec.at<double>(1));
            rvecZ = rvec.at<double>(2);
            const double tx = tvec.at<double>(0);
            const double ty = tvec.at<double>(1);
            const double tz = tvec.at<double>(2);
            tvecMag = std::sqrt(tx*tx + ty*ty + tz*tz);
            tvecYaw = std::atan2(tx, tz);
            tvecPitch = std::atan2(ty, tz);
        }
    }

    return [[BTCharucoFrameEval alloc]
            initWithCornerCount:n
                 boardFillRatio:fillRatio
                        rvecXY:rvecXY
                         rvecZ:rvecZ
                tvecMagnitudeM:tvecMag
                      tvecYaw:tvecYaw
                    tvecPitch:tvecPitch
                  sharpnessVar:sharpness];
}

- (BOOL)acceptShotFromJPEG:(NSData *)jpeg
                imageWidth:(NSInteger *)outWidth
               imageHeight:(NSInteger *)outHeight
                     error:(NSError * _Nullable * _Nullable)error {
    if (jpeg.length == 0) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:1
                                            userInfo:@{NSLocalizedDescriptionKey: @"empty JPEG data"}];
        return NO;
    }
    std::vector<uint8_t> buf((const uint8_t *)jpeg.bytes,
                             (const uint8_t *)jpeg.bytes + jpeg.length);
    cv::Mat gray = cv::imdecode(buf, cv::IMREAD_GRAYSCALE);
    if (gray.empty()) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:2
                                            userInfo:@{NSLocalizedDescriptionKey: @"JPEG decode failed"}];
        return NO;
    }
    if (outWidth) *outWidth = gray.cols;
    if (outHeight) *outHeight = gray.rows;

    cv::Mat charucoCorners, charucoIds;
    std::vector<std::vector<cv::Point2f>> markerCorners;
    std::vector<int> markerIds;
    _detector->detectBoard(gray, charucoCorners, charucoIds, markerCorners, markerIds);
    if (charucoCorners.empty() || charucoIds.empty()) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:3
                                            userInfo:@{NSLocalizedDescriptionKey: @"no board detected in shot"}];
        return NO;
    }
    if (charucoCorners.rows < 6) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:4
                                            userInfo:@{NSLocalizedDescriptionKey:
                [NSString stringWithFormat:@"only %d corners (< 6)", charucoCorners.rows]}];
        return NO;
    }

    // dtype rule: matchImagePoints asserts CV_32F corners + CV_32S ids
    // (calibrate_intrinsics.py:151-152). Hard-throws inside OpenCV with
    // no silent coercion if wrong.
    cv::Mat corners32, ids32;
    charucoCorners.convertTo(corners32, CV_32F);
    charucoIds.convertTo(ids32, CV_32S);
    cv::Mat objPts, imgPts;
    _board->matchImagePoints(corners32, ids32, objPts, imgPts);
    if (objPts.empty() || imgPts.empty() || objPts.rows < 6) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:5
                                            userInfo:@{NSLocalizedDescriptionKey:
                @"matchImagePoints returned too few points"}];
        return NO;
    }
    _objPts.push_back(objPts);
    _imgPts.push_back(imgPts);
    return YES;
}

- (nullable BTCharucoSolveResult *)solveWithImageWidth:(NSInteger)imageWidth
                                           imageHeight:(NSInteger)imageHeight
                                                 error:(NSError * _Nullable * _Nullable)error {
    if ((NSInteger)_objPts.size() < 4) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:10
                                            userInfo:@{NSLocalizedDescriptionKey:
            [NSString stringWithFormat:@"only %lu usable shot(s); need ≥4 for stable K",
             (unsigned long)_objPts.size()]}];
        return nil;
    }
    cv::Size imgSize((int)imageWidth, (int)imageHeight);
    cv::Mat K, dist;
    std::vector<cv::Mat> rvecs, tvecs;
    double rms = 0.0;
    try {
        rms = cv::calibrateCamera(_objPts, _imgPts, imgSize,
                                   K, dist, rvecs, tvecs);
    } catch (const cv::Exception &e) {
        if (error) *error = [NSError errorWithDomain:@"BTCharucoCalibrator" code:11
                                            userInfo:@{NSLocalizedDescriptionKey:
            [NSString stringWithFormat:@"calibrateCamera threw: %s", e.what()]}];
        return nil;
    }

    // Truncate to 5 coefficients (k1,k2,p1,p2,k3) to match server
    // `_validate_intrinsics_payload` (routes/calibration_intrinsics.py:157).
    cv::Mat distFlat = dist.reshape(1, dist.total());
    NSMutableArray<NSNumber *> *distArr = [NSMutableArray arrayWithCapacity:5];
    for (int i = 0; i < 5; ++i) {
        double v = (i < (int)distFlat.total()) ? distFlat.at<double>(i) : 0.0;
        [distArr addObject:@(v)];
    }

    return [[BTCharucoSolveResult alloc]
            initWithFx:K.at<double>(0, 0)
                    fy:K.at<double>(1, 1)
                    cx:K.at<double>(0, 2)
                    cy:K.at<double>(1, 2)
            distortion:distArr
     rmsReprojectionPx:rms
            imageWidth:imageWidth
           imageHeight:imageHeight
         numImagesUsed:(NSInteger)_objPts.size()];
}

@end
