#import "BallDetector.h"

#import <opencv2/opencv2.h>
#import <opencv2/imgproc.hpp>

#import <algorithm>

// ---------------------------------------------------------------------------
// Constants — MUST be kept in lock-step with server/detection.py +
// server/pipeline.py. Any change here MUST also land on the Python side;
// the whole point of the on-device pipeline is byte-for-byte equivalence
// with the server.
// ---------------------------------------------------------------------------

// _MIN_AREA_PX / _MAX_AREA_PX in server/detection.py.
static const int kMinAreaPx = 20;
static const int kMaxAreaPx = 150000;

// Private initialiser for BTBallDetection — the .h only exposes read-only
// properties, but the detector needs to construct instances from C++.
@interface BTBallDetection ()
- (instancetype)initWithPx:(CGFloat)px py:(CGFloat)py
                    areaPx:(NSInteger)areaPx
                    aspect:(CGFloat)aspect
                      fill:(CGFloat)fill;
@end

// ---------------------------------------------------------------------------
// Shared cv::Mat scratch buffers.
//
// Production capture is NV12 VideoRange (4:2:0 bi-planar Y + UV) per
// CameraCaptureRuntime — that's what we ask AVCaptureSession for so the
// chroma resolution matches what server_post sees post H.264 decode.
// `mapPixelBufferToBGR` also accepts BGRA so unit tests can keep
// constructing BGRA fixtures without taking the YUV conversion path.
//
// `bgr` is the converted output that downstream HSV / shape-gate code
// reads. The NV12 path goes through `cv::cvtColorTwoPlane` which reads
// Y and UV in place via stride-aware Mats — no contiguous stitching
// buffer is needed (we previously kept an `nv12` scratch for that;
// removed once cvtColorTwoPlane was wired in). `Mat::create` is a
// no-op when dims + type match across frames so allocations are
// amortized.
// ---------------------------------------------------------------------------
namespace {
struct CVScratch {
    cv::Mat bgr;
    cv::Mat hsv;
    cv::Mat mask;
    cv::Mat labels;
    cv::Mat stats;
    cv::Mat centroids;
};
} // namespace

/// HSV + CC + shape-gate pass operating on an already-mapped BGR
/// `cv::Mat`. Collects every blob passing area+aspect+fill, sorted by
/// area desc. No best-of pick (caller picks). `offsetX` / `offsetY`
/// are added to each centroid so ROI-cropped Mats can be passed and
/// still get image-coordinate centroids back. `outLargestW` /
/// `outLargestH` (nullable) receive the width/height of the
/// largest-area blob.
static NSArray<BTBallDetection *> *detectAllCandidatesScratch(
    const cv::Mat &bgr,
    int hMin, int hMax, int sMin, int sMax, int vMin, int vMax,
    double aspectMin, double fillMin,
    CVScratch &scratch,
    double offsetX, double offsetY,
    int *_Nullable outLargestW, int *_Nullable outLargestH
) {
    cv::cvtColor(bgr, scratch.hsv, cv::COLOR_BGR2HSV);
    if (hMin <= hMax) {
        cv::inRange(scratch.hsv,
                    cv::Scalar(hMin, sMin, vMin),
                    cv::Scalar(hMax, sMax, vMax),
                    scratch.mask);
    } else {
        // Hue wraps 179->0 (red/orange balls). OpenCV hue is 0-179; a single
        // inRange with hMin>hMax requires (h>=hMin AND h<=hMax) per pixel,
        // which is never true -> all-zero mask -> silent zero-detection.
        // Split into [hMin,179] U [0,hMax] sharing S/V bounds, then OR. Kept
        // lockstep with server detection.py. See docs/reference/hue-and-color.md.
        cv::Mat maskHi, maskLo;
        cv::inRange(scratch.hsv,
                    cv::Scalar(hMin, sMin, vMin),
                    cv::Scalar(179, sMax, vMax),
                    maskHi);
        cv::inRange(scratch.hsv,
                    cv::Scalar(0, sMin, vMin),
                    cv::Scalar(hMax, sMax, vMax),
                    maskLo);
        cv::bitwise_or(maskHi, maskLo, scratch.mask);
    }
    int ncomp = cv::connectedComponentsWithStats(
        scratch.mask, scratch.labels, scratch.stats, scratch.centroids, 8, CV_32S
    );
    struct Cand { int area; int w; int h; double cx; double cy; double aspect; double fill; };
    std::vector<Cand> cands;
    cands.reserve(8);
    for (int i = 1; i < ncomp; i++) {
        int area = scratch.stats.at<int>(i, cv::CC_STAT_AREA);
        if (area < kMinAreaPx || area > kMaxAreaPx) { continue; }
        int w = scratch.stats.at<int>(i, cv::CC_STAT_WIDTH);
        int h = scratch.stats.at<int>(i, cv::CC_STAT_HEIGHT);
        if (w <= 0 || h <= 0) { continue; }
        double aspect = (double)std::min(w, h) / (double)std::max(w, h);
        if (aspect < aspectMin) { continue; }
        double fill = (double)area / (double)(w * h);
        if (fill < fillMin) { continue; }
        cands.push_back({
            area, w, h,
            scratch.centroids.at<double>(i, 0) + offsetX,
            scratch.centroids.at<double>(i, 1) + offsetY,
            aspect, fill,
        });
    }
    std::sort(cands.begin(), cands.end(),
              [](const Cand &a, const Cand &b){ return a.area > b.area; });
    if (!cands.empty()) {
        if (outLargestW) *outLargestW = cands[0].w;
        if (outLargestH) *outLargestH = cands[0].h;
    } else {
        if (outLargestW) *outLargestW = 0;
        if (outLargestH) *outLargestH = 0;
    }
    NSMutableArray<BTBallDetection *> *out = [NSMutableArray arrayWithCapacity:cands.size()];
    for (const auto &c : cands) {
        [out addObject:[[BTBallDetection alloc] initWithPx:(CGFloat)c.cx
                                                        py:(CGFloat)c.cy
                                                    areaPx:(NSInteger)c.area
                                                    aspect:(CGFloat)c.aspect
                                                      fill:(CGFloat)c.fill]];
    }
    return out;
}

/// Convert a CVPixelBuffer to BGR.
///
/// Production capture is NV12 VideoRange (4:2:0 bi-planar Y + UV) per
/// CameraCaptureRuntime. Tests construct BGRA buffers directly. This
/// function accepts both via OpenCV 4.x's two-plane NV12 API (no
/// stitching memcpy needed — pass Y and UV planes as separate
/// stride-aware Mats and let `cv::cvtColorTwoPlane` do the conversion
/// in one pass).
///
/// `out` ends up as a `CV_8UC3` BGR Mat OWNING its data — callers do
/// NOT need to balance an unlock; the unlock happens inside this
/// function once the conversion has completed.
///
/// FullRange NV12 (`...8BiPlanarFullRange`) is **rejected**, not
/// silently converted: cv::cvtColor's NV12 path always assumes
/// VideoRange (Y∈[16,235], UV∈[16,240]) and feeding FullRange
/// (Y∈[0,255]) crushes blacks + clips highlights, shifting saturation
/// of pixels near the HSV gate. AVFoundation honours our VideoRange
/// request in practice, but format negotiation can override at
/// session-preset boundaries — refuse loudly here so the operator
/// sees the error in the iOS console rather than silently degraded
/// detection (project rule: no silent fallback).
///
/// Note on color matrix: cv::cvtColor with `COLOR_YUV2BGR_NV12` uses
/// BT.601 coefficients; iPhone video capture tags streams as BT.709.
/// Quantified 2026-04-30 via `server/chroma_alignment_check.py`:
/// real ball-ROI ΔH ≈ +2 OpenCV units (~4°) p95 = 3, mask Jaccard
/// vs server-side BT.709 path = 0.974 mean / 0.994 p50 on tennis
/// preset. Operationally invisible — both presets in use (`tennis`
/// h∈[25,55], `blue_ball` h∈[105,112]) have enough margin. Switch to
/// libyuv's `H420ToARGB` only if you introduce a preset narrower than
/// 6 OpenCV hue units; re-run the tool first.
static bool mapPixelBufferToBGR(
    CVPixelBufferRef pixelBuffer,
    cv::Mat &out
) {
    if (!pixelBuffer) { return false; }
    const OSType fmt = CVPixelBufferGetPixelFormatType(pixelBuffer);
    if (fmt == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange) {
        // Loud refuse — see docstring rationale. dispatch_once so the
        // log doesn't spam at 240 fps if the session is wedged.
        static dispatch_once_t once;
        dispatch_once(&once, ^{
            NSLog(@"BallDetector: capture delivered NV12 FullRange "
                  @"(Y∈[0,255]) but we requested VideoRange. cv::cvtColor "
                  @"would mis-convert; refusing every FullRange frame. "
                  @"Check AVCaptureSession format negotiation in "
                  @"CameraCaptureRuntime.swift.");
        });
        return false;
    }
    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    bool ok = false;
    if (fmt == kCVPixelFormatType_32BGRA) {
        const size_t w = CVPixelBufferGetWidth(pixelBuffer);
        const size_t h = CVPixelBufferGetHeight(pixelBuffer);
        const size_t row = CVPixelBufferGetBytesPerRow(pixelBuffer);
        void *base = CVPixelBufferGetBaseAddress(pixelBuffer);
        if (base) {
            cv::Mat bgra((int)h, (int)w, CV_8UC4, base, row);
            cv::cvtColor(bgra, out, cv::COLOR_BGRA2BGR);
            ok = true;
        }
    } else if (fmt == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
        const size_t w = CVPixelBufferGetWidth(pixelBuffer);
        const size_t h = CVPixelBufferGetHeight(pixelBuffer);
        const size_t yStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0);
        const size_t uvStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 1);
        const uint8_t *yBase = (const uint8_t *)CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0);
        const uint8_t *uvBase = (const uint8_t *)CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 1);
        if (yBase && uvBase) {
            // cvtColorTwoPlane reads Y and UV in place via their
            // strides — no stitching buffer, ~1 ms saved per frame
            // and the per-thread `nv12Scratch` (~3 MB) goes away.
            cv::Mat yMat((int)h, (int)w, CV_8UC1, (void *)yBase, yStride);
            cv::Mat uvMat((int)h / 2, (int)w / 2, CV_8UC2, (void *)uvBase, uvStride);
            cv::cvtColorTwoPlane(yMat, uvMat, out, cv::COLOR_YUV2BGR_NV12);
            ok = true;
        }
    }
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    return ok;
}

// MARK: - BTBallDetection

@implementation BTBallDetection {
    CGFloat _px;
    CGFloat _py;
    NSInteger _areaPx;
    CGFloat _aspect;
    CGFloat _fill;
}

- (instancetype)initWithPx:(CGFloat)px py:(CGFloat)py
                    areaPx:(NSInteger)areaPx
                    aspect:(CGFloat)aspect
                      fill:(CGFloat)fill {
    self = [super init];
    if (self) {
        _px = px;
        _py = py;
        _areaPx = areaPx;
        _aspect = aspect;
        _fill = fill;
    }
    return self;
}

- (CGFloat)px { return _px; }
- (CGFloat)py { return _py; }
- (NSInteger)areaPx { return _areaPx; }
- (CGFloat)aspect { return _aspect; }
- (CGFloat)fill { return _fill; }

@end

// MARK: - BTBallDetector (stateless)

@implementation BTBallDetector

+ (NSArray<BTBallDetection *> *)detectAllCandidatesInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                                            hMin:(int)hMin hMax:(int)hMax
                                                            sMin:(int)sMin sMax:(int)sMax
                                                            vMin:(int)vMin vMax:(int)vMax
                                                       aspectMin:(double)aspectMin
                                                         fillMin:(double)fillMin
{
    thread_local CVScratch scratch;
    if (!mapPixelBufferToBGR(pixelBuffer, scratch.bgr)) { return @[]; }
    NSArray<BTBallDetection *> *cands = detectAllCandidatesScratch(
        scratch.bgr, hMin, hMax, sMin, sMax, vMin, vMax, aspectMin, fillMin, scratch,
        /*offsetX=*/0.0, /*offsetY=*/0.0,
        /*outLargestW=*/nullptr, /*outLargestH=*/nullptr
    );
    return cands;
}

@end
