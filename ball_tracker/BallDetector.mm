#import "BallDetector.h"

#import <opencv2/opencv2.h>
#import <opencv2/imgproc.hpp>
#import <opencv2/video/background_segm.hpp>

#import <algorithm>
#import <mach/mach_time.h>

// ---------------------------------------------------------------------------
// Constants — MUST be kept in lock-step with server/detection.py +
// server/pipeline.py. Any change here MUST also land on the Python side;
// the whole point of the on-device pipeline is byte-for-byte equivalence
// with the server.
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
// pitch sessions (see memory: project_ball_empirical_fill,
// project_ball_shape_invariance). Loosened 2026-04 (0.75→0.70, 0.60→0.55)
// after ROI-cropped HSV masks showed median fill 0.63–0.70 — the prior
// 0.60 floor was clipping real hits on tennis-ball rotations.
static const double kMinAspect = 0.70;
static const double kMinFill = 0.55;

// ROI tracking parameters (stateful detector only).
static const int kROIRadiusMultiplier = 3;     // crop = lastRadius × this, both sides
static const int kROIMinSide = 256;            // never crop tighter than 256×256
static const int kROIMaxConsecutiveMisses = 10; // reset tracking after N misses

// ---------------------------------------------------------------------------
// Timing harness. Off in release; on in DEBUG. Reports median over the last
// 240 frames, so we can verify the "12–18 ms → 3–6 ms" goal in the field.
// ---------------------------------------------------------------------------
#if DEBUG
#define BT_DETECTOR_TIMING 1
#else
#define BT_DETECTOR_TIMING 0
#endif

#if BT_DETECTOR_TIMING
namespace {
struct TimingRing {
    static constexpr size_t kCapacity = 240;
    double hsvMs[kCapacity] = {0};
    double ccMs[kCapacity] = {0};
    size_t count = 0;
    size_t head = 0;

    void add(double hsv, double cc) {
        hsvMs[head] = hsv;
        ccMs[head] = cc;
        head = (head + 1) % kCapacity;
        if (count < kCapacity) count++;
    }

    static double median(const double *src, size_t n) {
        double tmp[kCapacity];
        std::copy(src, src + n, tmp);
        std::nth_element(tmp, tmp + n / 2, tmp + n);
        return tmp[n / 2];
    }
};

static double machToMs(uint64_t delta) {
    static mach_timebase_info_data_t tb = {0, 0};
    if (tb.denom == 0) mach_timebase_info(&tb);
    return (double)delta * (double)tb.numer / (double)tb.denom / 1.0e6;
}

static TimingRing gStatelessTiming;
static TimingRing gStatefulTiming;
static dispatch_once_t gTimingLogOnce;

static void reportIfDue(TimingRing &ring, const char *tag) {
    if (ring.count < TimingRing::kCapacity) return;
    double medHSV = TimingRing::median(ring.hsvMs, ring.count);
    double medCC = TimingRing::median(ring.ccMs, ring.count);
    NSLog(@"BallDetector[%s]: median over %zu frames → HSV %.2f ms, CC+gate %.2f ms (total %.2f ms)",
          tag, ring.count, medHSV, medCC, medHSV + medCC);
    ring.count = 0; // roll next bucket
    ring.head = 0;
}
} // namespace
#define BT_TIMING_START(name) uint64_t name = mach_absolute_time()
#define BT_TIMING_ELAPSED_MS(start) machToMs(mach_absolute_time() - (start))
#else
#define BT_TIMING_START(name) (void)0
#define BT_TIMING_ELAPSED_MS(start) 0.0
#endif

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

/// Core HSV + CC + shape gate pass, operating on an already-mapped BGR
/// cv::Mat (possibly a ROI slice). Writes intermediates into the provided
/// scratch buffers — the caller owns their lifetime, which lets us reuse
/// them across frames on the stateful path and across concurrent calls
/// (thread-local) on the stateless path.
///
/// `offsetX` / `offsetY` are added to the returned centroid so callers can
/// pass a ROI crop and still get image-coordinate output.
static BTBallDetection *_Nullable detectBallCoreScratch(
    const cv::Mat &bgr,
    int hMin, int hMax, int sMin, int sMax, int vMin, int vMax,
    double aspectMin, double fillMin,
    CVScratch &scratch,
    double offsetX, double offsetY,
    int *_Nullable outBestW, int *_Nullable outBestH
#if BT_DETECTOR_TIMING
    , double *outHsvMs, double *outCcMs
#endif
) {
    BT_TIMING_START(tHsv);
    cv::cvtColor(bgr, scratch.hsv, cv::COLOR_BGR2HSV);

    cv::inRange(scratch.hsv,
                cv::Scalar(hMin, sMin, vMin),
                cv::Scalar(hMax, sMax, vMax),
                scratch.mask);
#if BT_DETECTOR_TIMING
    if (outHsvMs) *outHsvMs = BT_TIMING_ELAPSED_MS(tHsv);
#endif

    BT_TIMING_START(tCc);
    int ncomp = cv::connectedComponentsWithStats(
        scratch.mask, scratch.labels, scratch.stats, scratch.centroids, 8, CV_32S
    );

    int bestLabel = -1;
    int bestArea = 0;
    int bestW = 0, bestH = 0;
    double bestAspect = 0.0, bestFill = 0.0;
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
        if (area > bestArea) {
            bestArea = area;
            bestLabel = i;
            bestW = w;
            bestH = h;
            bestAspect = aspect;
            bestFill = fill;
        }
    }
#if BT_DETECTOR_TIMING
    if (outCcMs) *outCcMs = BT_TIMING_ELAPSED_MS(tCc);
#endif

    if (bestLabel < 0) { return nil; }

    double cx = scratch.centroids.at<double>(bestLabel, 0) + offsetX;
    double cy = scratch.centroids.at<double>(bestLabel, 1) + offsetY;
    if (outBestW) *outBestW = bestW;
    if (outBestH) *outBestH = bestH;
    return [[BTBallDetection alloc] initWithPx:(CGFloat)cx
                                            py:(CGFloat)cy
                                        areaPx:(NSInteger)bestArea
                                        aspect:(CGFloat)bestAspect
                                          fill:(CGFloat)bestFill];
}

/// Multi-candidate variant of detectBallCoreScratch — collects every
/// blob passing area+aspect+fill, sorted by area desc. No best-of pick
/// (caller picks). `offsetX` / `offsetY` are added to each centroid so
/// ROI-cropped Mats can be passed and still get image-coordinate
/// centroids back. `outLargestW` / `outLargestH` (nullable) receive the
/// width/height of the largest-area blob — used by the stateful path
/// to update its ROI radius hint after a multi-candidate hit.
static NSArray<BTBallDetection *> *detectAllCandidatesScratch(
    const cv::Mat &bgr,
    int hMin, int hMax, int sMin, int sMax, int vMin, int vMax,
    double aspectMin, double fillMin,
    CVScratch &scratch,
    double offsetX, double offsetY,
    int *_Nullable outLargestW, int *_Nullable outLargestH
) {
    cv::cvtColor(bgr, scratch.hsv, cv::COLOR_BGR2HSV);
    cv::inRange(scratch.hsv,
                cv::Scalar(hMin, sMin, vMin),
                cv::Scalar(hMax, sMax, vMax),
                scratch.mask);
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
/// Hue offset on saturated colours is ~3-4 in OpenCV's 0-179 hue
/// units (~6-8° in 0-360° space) — fine for the blue-ball preset
/// (h∈[100,130] gives ±15 units margin) but tighter for the `tennis`
/// preset (h∈[25,55], ±15 units total). Re-validate the tennis preset
/// before relying on it post-NV12. Switch to libyuv's `H420ToARGB`
/// for precise BT.709 conversion if that ever matters.
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

+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
{
    return [self detectInPixelBuffer:pixelBuffer
                                hMin:kDefaultHMin hMax:kDefaultHMax
                                sMin:kDefaultSMin sMax:kDefaultSMax
                                vMin:kDefaultVMin vMax:kDefaultVMax
                           aspectMin:kMinAspect fillMin:kMinFill];
}

+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax
{
    return [self detectInPixelBuffer:pixelBuffer
                                hMin:hMin hMax:hMax
                                sMin:sMin sMax:sMax
                                vMin:vMin vMax:vMax
                           aspectMin:kMinAspect fillMin:kMinFill];
}

+ (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer
                                             hMin:(int)hMin hMax:(int)hMax
                                             sMin:(int)sMin sMax:(int)sMax
                                             vMin:(int)vMin vMax:(int)vMax
                                        aspectMin:(double)aspectMin
                                          fillMin:(double)fillMin
{
    // thread_local scratch: a leftover from when the stateless path was
    // dispatched onto a concurrent worker pool. Post-Phase-3 the live
    // path uses BTStatefulBallDetector on a serial queue, but this
    // stateless variant is still callable from anywhere (tests, parity
    // fixtures), so per-thread scratch keeps it allocator-cheap on
    // whichever thread happens to call.
    thread_local CVScratch scratch;
    if (!mapPixelBufferToBGR(pixelBuffer, scratch.bgr)) { return nil; }

#if BT_DETECTOR_TIMING
    double hsvMs = 0, ccMs = 0;
#endif
    BTBallDetection *detection = detectBallCoreScratch(
        scratch.bgr, hMin, hMax, sMin, sMax, vMin, vMax,
        aspectMin, fillMin,
        scratch, /*offsetX=*/0, /*offsetY=*/0,
        /*outBestW=*/nullptr, /*outBestH=*/nullptr
#if BT_DETECTOR_TIMING
        , &hsvMs, &ccMs
#endif
    );

#if BT_DETECTOR_TIMING
    // ConcurrentDetectionPool has its own lock; the ring buffer here is
    // racy on concurrent updates but we only use it for rough field-log
    // stats, not a timing contract. Worst case: a few dropped samples.
    gStatelessTiming.add(hsvMs, ccMs);
    reportIfDue(gStatelessTiming, "stateless");
#endif
    return detection;
}

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

// MARK: - BTStatefulBallDetector

@implementation BTStatefulBallDetector {
    int _hMin, _hMax, _sMin, _sMax, _vMin, _vMax;
    double _aspectMin, _fillMin;
    CVScratch _scratch;

    // ROI tracking state
    bool _hasPrev;
    cv::Point2f _lastHitCenter;
    float _lastHitRadius;
    int _consecutiveMisses;
}

- (instancetype)init {
    self = [super init];
    if (self) {
        _hMin = kDefaultHMin; _hMax = kDefaultHMax;
        _sMin = kDefaultSMin; _sMax = kDefaultSMax;
        _vMin = kDefaultVMin; _vMax = kDefaultVMax;
        _aspectMin = kMinAspect; _fillMin = kMinFill;
        _hasPrev = false;
        _lastHitCenter = cv::Point2f(0, 0);
        _lastHitRadius = 0;
        _consecutiveMisses = 0;
    }
    return self;
}

- (void)setHMin:(int)hMin hMax:(int)hMax
           sMin:(int)sMin sMax:(int)sMax
           vMin:(int)vMin vMax:(int)vMax {
    _hMin = hMin; _hMax = hMax;
    _sMin = sMin; _sMax = sMax;
    _vMin = vMin; _vMax = vMax;
}

- (void)setAspectMin:(double)aspectMin fillMin:(double)fillMin {
    _aspectMin = aspectMin;
    _fillMin = fillMin;
}

- (void)resetTracking {
    _hasPrev = false;
    _consecutiveMisses = 0;
    _lastHitRadius = 0;
}

- (nullable BTBallDetection *)detectInPixelBuffer:(CVPixelBufferRef)pixelBuffer {
    if (!mapPixelBufferToBGR(pixelBuffer, _scratch.bgr)) { return nil; }

    BTBallDetection *result = nil;
    @try {
        result = [self detectInBGR:_scratch.bgr];
    } @catch (NSException *e) {
        NSLog(@"BallDetector: exception during detection: %@", e);
        result = nil;
    }
    return result;
}

- (nullable BTBallDetection *)detectInBGR:(const cv::Mat &)bgr {
    const int W = bgr.cols;
    const int H = bgr.rows;

#if BT_DETECTOR_TIMING
    double hsvMs = 0, ccMs = 0;
#endif

    // --- ROI pass, if we have a prior hit ------------------------------
    if (_hasPrev && _lastHitRadius > 0.0f) {
        int side = std::max(kROIMinSide, (int)std::ceil(2.0f * kROIRadiusMultiplier * _lastHitRadius));
        int half = side / 2;
        int x0 = std::max(0, (int)std::round(_lastHitCenter.x) - half);
        int y0 = std::max(0, (int)std::round(_lastHitCenter.y) - half);
        int x1 = std::min(W, x0 + side);
        int y1 = std::min(H, y0 + side);
        // re-snap left/top if we clipped against the right/bottom
        x0 = std::max(0, x1 - side);
        y0 = std::max(0, y1 - side);
        if (x1 > x0 && y1 > y0) {
            cv::Rect roi(x0, y0, x1 - x0, y1 - y0);
            cv::Mat crop = bgr(roi);
            int bestW = 0, bestH = 0;
            BTBallDetection *hit = detectBallCoreScratch(
                crop, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
                _aspectMin, _fillMin,
                _scratch, /*offsetX=*/(double)x0, /*offsetY=*/(double)y0,
                &bestW, &bestH
#if BT_DETECTOR_TIMING
                , &hsvMs, &ccMs
#endif
            );
            if (hit) {
                _lastHitCenter = cv::Point2f((float)hit.px, (float)hit.py);
                _lastHitRadius = 0.5f * (float)std::max(bestW, bestH);
                _consecutiveMisses = 0;
#if BT_DETECTOR_TIMING
                gStatefulTiming.add(hsvMs, ccMs);
                reportIfDue(gStatefulTiming, "stateful-roi");
#endif
                return hit;
            }
            // ROI miss — announce the fallback loudly (per project rule:
            // NO silent fallbacks). Full-frame retry follows below.
            NSLog(@"BallDetector: ROI miss at (%.1f,%.1f r=%.1f), falling back to full frame",
                  _lastHitCenter.x, _lastHitCenter.y, _lastHitRadius);
        }
    }

    // --- Full-frame pass -----------------------------------------------
    int bestW = 0, bestH = 0;
    BTBallDetection *hit = detectBallCoreScratch(
        bgr, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
        _aspectMin, _fillMin,
        _scratch, /*offsetX=*/0, /*offsetY=*/0,
        &bestW, &bestH
#if BT_DETECTOR_TIMING
        , &hsvMs, &ccMs
#endif
    );
#if BT_DETECTOR_TIMING
    gStatefulTiming.add(hsvMs, ccMs);
    reportIfDue(gStatefulTiming, "stateful-full");
#endif
    if (hit) {
        _lastHitCenter = cv::Point2f((float)hit.px, (float)hit.py);
        _lastHitRadius = 0.5f * (float)std::max(bestW, bestH);
        _hasPrev = true;
        _consecutiveMisses = 0;
        return hit;
    }

    // Full-frame miss too — bump miss counter; drop tracking after N.
    _consecutiveMisses++;
    if (_consecutiveMisses >= kROIMaxConsecutiveMisses) {
        if (_hasPrev) {
            NSLog(@"BallDetector: %d consecutive misses, dropping ROI tracking",
                  _consecutiveMisses);
        }
        _hasPrev = false;
        _lastHitRadius = 0;
    }
    return nil;
}

- (NSArray<BTBallDetection *> *)detectAllCandidatesInPixelBuffer:(CVPixelBufferRef)pixelBuffer {
    if (!mapPixelBufferToBGR(pixelBuffer, _scratch.bgr)) { return @[]; }

    NSArray<BTBallDetection *> *result = @[];
    @try {
        result = [self detectAllCandidatesInBGR:_scratch.bgr];
    } @catch (NSException *e) {
        NSLog(@"BallDetector: exception during multi-candidate detection: %@", e);
        result = @[];
    }
    return result;
}

- (NSArray<BTBallDetection *> *)detectAllCandidatesInBGR:(const cv::Mat &)bgr {
    const int W = bgr.cols;
    const int H = bgr.rows;

    // --- ROI pass, if we have a prior hit ------------------------------
    if (_hasPrev && _lastHitRadius > 0.0f) {
        int side = std::max(kROIMinSide, (int)std::ceil(2.0f * kROIRadiusMultiplier * _lastHitRadius));
        int half = side / 2;
        int x0 = std::max(0, (int)std::round(_lastHitCenter.x) - half);
        int y0 = std::max(0, (int)std::round(_lastHitCenter.y) - half);
        int x1 = std::min(W, x0 + side);
        int y1 = std::min(H, y0 + side);
        x0 = std::max(0, x1 - side);
        y0 = std::max(0, y1 - side);
        if (x1 > x0 && y1 > y0) {
            cv::Rect roi(x0, y0, x1 - x0, y1 - y0);
            cv::Mat crop = bgr(roi);
            int largestW = 0, largestH = 0;
            NSArray<BTBallDetection *> *cands = detectAllCandidatesScratch(
                crop, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
                _aspectMin, _fillMin,
                _scratch,
                /*offsetX=*/(double)x0, /*offsetY=*/(double)y0,
                &largestW, &largestH
            );
            if (cands.count > 0) {
                BTBallDetection *largest = cands.firstObject;
                _lastHitCenter = cv::Point2f((float)largest.px, (float)largest.py);
                _lastHitRadius = 0.5f * (float)std::max(largestW, largestH);
                _consecutiveMisses = 0;
                return cands;
            }
            // ROI miss — same loud fallback as the single-best path.
            NSLog(@"BallDetector: ROI miss (multi) at (%.1f,%.1f r=%.1f), falling back to full frame",
                  _lastHitCenter.x, _lastHitCenter.y, _lastHitRadius);
        }
    }

    // --- Full-frame pass -----------------------------------------------
    int largestW = 0, largestH = 0;
    NSArray<BTBallDetection *> *cands = detectAllCandidatesScratch(
        bgr, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
        _aspectMin, _fillMin,
        _scratch,
        /*offsetX=*/0.0, /*offsetY=*/0.0,
        &largestW, &largestH
    );
    if (cands.count > 0) {
        BTBallDetection *largest = cands.firstObject;
        _lastHitCenter = cv::Point2f((float)largest.px, (float)largest.py);
        _lastHitRadius = 0.5f * (float)std::max(largestW, largestH);
        _hasPrev = true;
        _consecutiveMisses = 0;
        return cands;
    }

    _consecutiveMisses++;
    if (_consecutiveMisses >= kROIMaxConsecutiveMisses) {
        if (_hasPrev) {
            NSLog(@"BallDetector: %d consecutive misses (multi), dropping ROI tracking",
                  _consecutiveMisses);
        }
        _hasPrev = false;
        _lastHitRadius = 0;
    }
    return @[];
}

@end
