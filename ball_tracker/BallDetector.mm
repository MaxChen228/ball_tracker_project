#import "BallDetector.h"

#import <opencv2/opencv2.h>
#import <opencv2/imgproc.hpp>

// Defaults mirror server/detection.py `HSVRange.default()` — a yellow-green
// tennis ball under typical rig lighting. Area bounds match the server's
// `_MIN_AREA_PX` / `_MAX_AREA_PX`: a close-range ball can occupy a lot of
// pixels, far smaller than 20 px² is noise.
static const int kDefaultHMin = 25;
static const int kDefaultHMax = 55;
static const int kDefaultSMin = 90;
static const int kDefaultSMax = 255;
static const int kDefaultVMin = 90;
static const int kDefaultVMax = 255;
static const int kMinAreaPx = 20;
static const int kMaxAreaPx = 150000;

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

// MARK: - BTBallDetector

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
    if (!pixelBuffer) { return nil; }
    OSType pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer);
    if (pixelFormat != kCVPixelFormatType_32BGRA) {
        // CameraViewController's videoOutput is configured for BGRA; if we
        // ever add a second output with a different format the caller must
        // convert before passing it in.
        return nil;
    }

    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    const size_t width = CVPixelBufferGetWidth(pixelBuffer);
    const size_t height = CVPixelBufferGetHeight(pixelBuffer);
    const size_t bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *base = CVPixelBufferGetBaseAddress(pixelBuffer);
    if (!base) {
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return nil;
    }

    // `bgra` is a zero-copy view onto the CVPixelBuffer — we convert to an
    // owned HSV Mat before unlocking so the pixel buffer can be released.
    cv::Mat bgra((int)height, (int)width, CV_8UC4, base, bytesPerRow);
    cv::Mat hsv;
    cv::cvtColor(bgra, hsv, cv::COLOR_BGRA2BGR);  // bgra view → owned BGR
    cv::cvtColor(hsv, hsv, cv::COLOR_BGR2HSV);    // BGR → HSV in place
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);

    cv::Mat mask;
    cv::inRange(hsv,
                cv::Scalar(hMin, sMin, vMin),
                cv::Scalar(hMax, sMax, vMax),
                mask);

    cv::Mat labels, stats, centroids;
    int ncomp = cv::connectedComponentsWithStats(
        mask, labels, stats, centroids, 8, CV_32S
    );

    // Pick the largest in-range blob. Label 0 is the background.
    int bestLabel = -1;
    int bestArea = 0;
    for (int i = 1; i < ncomp; i++) {
        int area = stats.at<int>(i, cv::CC_STAT_AREA);
        if (area < kMinAreaPx || area > kMaxAreaPx) { continue; }
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

@end
