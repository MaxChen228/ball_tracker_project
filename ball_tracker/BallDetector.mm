#import "BallDetector.h"

#import <opencv2/opencv2.h>
#import <opencv2/imgproc.hpp>
#import <opencv2/video/background_segm.hpp>

// ---------------------------------------------------------------------------
// Constants — kept in lock-step with server/detection.py + server/pipeline.py.
// Any change to the HSV range, area bounds, or shape-gate thresholds below
// MUST also land on the Python side: this file's responsibility is keeping
// those *constants* byte-for-byte identical across the two implementations.
//
// This is NOT a claim of pipeline-level equivalence — the iOS live path is
// stateless HSV + CC + shape-gate, while `server/pipeline.py` additionally
// runs an MOG2 warmup that suppresses the opening frames. See the header
// comment in `BallDetector.h` for the full asymmetry note; do not duplicate
// that discussion here.
// ---------------------------------------------------------------------------

// HSVRange.default() in server/detection.py.
static const int kDefaultHMin = 25;
static const int kDefaultHMax = 55;
static const int kDefaultSMin = 90;
static const int kDefaultSMax = 255;
static const int kDefaultVMin = 90;
static const int kDefaultVMax = 255;

// _MIN_AREA_PX / _MAX_AREA_PX in server/detection.py.
static const int kMinAreaPx = 20;
static const int kMaxAreaPx = 150000;

// _MIN_ASPECT / _MIN_FILL in server/detection.py. Calibrated from real
// pitch sessions (see memory: project_ball_empirical_fill, project_ball_shape_invariance).
static const double kMinAspect = 0.75;
static const double kMinFill = 0.60;

// Private initialiser for BTBallDetection — the .h only exposes read-only
// properties, but the detector needs to construct instances from C++.
@interface BTBallDetection ()
- (instancetype)initWithPx:(CGFloat)px py:(CGFloat)py areaPx:(NSInteger)areaPx;
@end

// MARK: - Core detection (shared between stateless + session paths)

/// Core HSV + CC + shape gate pass. `fgMask`, when non-null, is AND'd into
/// the HSV mask before connected-components analysis — same as the server's
/// `detect_ball(..., fg_mask=...)` path. Returns largest blob centroid +
/// area, or nil when no blob passes all filters.
static BTBallDetection *_Nullable detectBallCore(
    const cv::Mat &bgra,
    int hMin, int hMax, int sMin, int sMax, int vMin, int vMax,
    const cv::Mat *fgMask
) {
    cv::Mat hsv;
    cv::cvtColor(bgra, hsv, cv::COLOR_BGRA2BGR);
    cv::cvtColor(hsv, hsv, cv::COLOR_BGR2HSV);

    cv::Mat mask;
    cv::inRange(hsv,
                cv::Scalar(hMin, sMin, vMin),
                cv::Scalar(hMax, sMax, vMax),
                mask);

    if (fgMask != nullptr && !fgMask->empty()) {
        cv::bitwise_and(mask, *fgMask, mask);
    }

    cv::Mat labels, stats, centroids;
    int ncomp = cv::connectedComponentsWithStats(
        mask, labels, stats, centroids, 8, CV_32S
    );

    int bestLabel = -1;
    int bestArea = 0;
    for (int i = 1; i < ncomp; i++) {
        int area = stats.at<int>(i, cv::CC_STAT_AREA);
        if (area < kMinAreaPx || area > kMaxAreaPx) { continue; }
        int w = stats.at<int>(i, cv::CC_STAT_WIDTH);
        int h = stats.at<int>(i, cv::CC_STAT_HEIGHT);
        if (w <= 0 || h <= 0) { continue; }
        double aspect = (double)std::min(w, h) / (double)std::max(w, h);
        if (aspect < kMinAspect) { continue; }
        double fill = (double)area / (double)(w * h);
        if (fill < kMinFill) { continue; }
        if (area > bestArea) {
            bestArea = area;
            bestLabel = i;
        }
    }
    if (bestLabel < 0) { return nil; }

    double cx = centroids.at<double>(bestLabel, 0);
    double cy = centroids.at<double>(bestLabel, 1);
    return [[BTBallDetection alloc] initWithPx:(CGFloat)cx
                                            py:(CGFloat)cy
                                        areaPx:(NSInteger)bestArea];
}

/// Lock a CVPixelBuffer for read + wrap its base address in a zero-copy
/// cv::Mat. Returns false when the buffer can't be mapped (nil, wrong
/// pixel format, or no base address). Caller MUST call
/// CVPixelBufferUnlockBaseAddress before the returned Mat goes out of scope.
static bool mapBGRAPixelBuffer(CVPixelBufferRef pixelBuffer, cv::Mat &out) {
    if (!pixelBuffer) { return false; }
    if (CVPixelBufferGetPixelFormatType(pixelBuffer) != kCVPixelFormatType_32BGRA) {
        return false;
    }
    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    const size_t w = CVPixelBufferGetWidth(pixelBuffer);
    const size_t h = CVPixelBufferGetHeight(pixelBuffer);
    const size_t row = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *base = CVPixelBufferGetBaseAddress(pixelBuffer);
    if (!base) {
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return false;
    }
    out = cv::Mat((int)h, (int)w, CV_8UC4, base, row);
    return true;
}

// MARK: - BTBallDetection

@implementation BTBallDetection {
    CGFloat _px;
    CGFloat _py;
    NSInteger _areaPx;
}

- (instancetype)initWithPx:(CGFloat)px py:(CGFloat)py areaPx:(NSInteger)areaPx {
    self = [super init];
    if (self) {
        _px = px;
        _py = py;
        _areaPx = areaPx;
    }
    return self;
}

- (CGFloat)px { return _px; }
- (CGFloat)py { return _py; }
- (NSInteger)areaPx { return _areaPx; }

@end

// MARK: - BTBallDetector (stateless)

@implementation BTBallDetector

+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
{
    return [self detectInPixelBuffer:pixelBuffer
                                hMin:kDefaultHMin hMax:kDefaultHMax
                                sMin:kDefaultSMin sMax:kDefaultSMax
                                vMin:kDefaultVMin vMax:kDefaultVMax];
}

+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax
{
    cv::Mat bgra;
    if (!mapBGRAPixelBuffer(pixelBuffer, bgra)) { return nil; }

    BTBallDetection *detection = detectBallCore(
        bgra, hMin, hMax, sMin, sMax, vMin, vMax, nullptr
    );
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    return detection;
}

@end
