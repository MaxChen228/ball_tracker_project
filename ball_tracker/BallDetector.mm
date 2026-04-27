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
- (instancetype)initWithPx:(CGFloat)px py:(CGFloat)py areaPx:(NSInteger)areaPx;
@end

// ---------------------------------------------------------------------------
// Shared cv::Mat scratch buffers.
//
// We intentionally do NOT merge BGRA→HSV into one cvtColor call — OpenCV
// has no COLOR_BGRA2HSV flag, so we go BGRA→BGR→HSV (two passes). We tried
// the Accelerate/vImage route (vImageConvert_BGRA8888toRGB888 + manual HSV)
// but rejected it to avoid introducing a new framework link dependency in
// ball_tracker.xcodeproj (only OpenCV is linked today). Instead we reuse
// cv::Mat buffers across calls so the cvtColor writes land in pre-allocated
// memory — Mat::create is a no-op when dims + type already match.
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

/// Core HSV + CC + shape gate pass, operating on an already-mapped BGRA
/// cv::Mat (possibly a ROI slice). Writes intermediates into the provided
/// scratch buffers — the caller owns their lifetime, which lets us reuse
/// them across frames on the stateful path and across concurrent calls
/// (thread-local) on the stateless path.
///
/// `offsetX` / `offsetY` are added to the returned centroid so callers can
/// pass a ROI crop and still get image-coordinate output.
static BTBallDetection *_Nullable detectBallCoreScratch(
    const cv::Mat &bgra,
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
    cv::cvtColor(bgra, scratch.bgr, cv::COLOR_BGRA2BGR);
    cv::cvtColor(scratch.bgr, scratch.hsv, cv::COLOR_BGR2HSV);

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
                                        areaPx:(NSInteger)bestArea];
}

/// Multi-candidate variant of detectBallCoreScratch — collects every
/// blob passing area+aspect+fill, sorted by area desc. No best-of pick
/// (caller picks). `offsetX` / `offsetY` are added to each centroid so
/// ROI-cropped Mats can be passed and still get image-coordinate
/// centroids back. `outLargestW` / `outLargestH` (nullable) receive the
/// width/height of the largest-area blob — used by the stateful path
/// to update its ROI radius hint after a multi-candidate hit.
static NSArray<BTBallDetection *> *detectAllCandidatesScratch(
    const cv::Mat &bgra,
    int hMin, int hMax, int sMin, int sMax, int vMin, int vMax,
    double aspectMin, double fillMin,
    CVScratch &scratch,
    double offsetX, double offsetY,
    int *_Nullable outLargestW, int *_Nullable outLargestH
) {
    cv::cvtColor(bgra, scratch.bgr, cv::COLOR_BGRA2BGR);
    cv::cvtColor(scratch.bgr, scratch.hsv, cv::COLOR_BGR2HSV);
    cv::inRange(scratch.hsv,
                cv::Scalar(hMin, sMin, vMin),
                cv::Scalar(hMax, sMax, vMax),
                scratch.mask);
    int ncomp = cv::connectedComponentsWithStats(
        scratch.mask, scratch.labels, scratch.stats, scratch.centroids, 8, CV_32S
    );
    struct Cand { int area; int w; int h; double cx; double cy; };
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
                                                    areaPx:(NSInteger)c.area]];
    }
    return out;
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
    cv::Mat bgra;
    if (!mapBGRAPixelBuffer(pixelBuffer, bgra)) { return nil; }

    // thread_local scratch: the stateless path is used by
    // ConcurrentDetectionPool with up to `maxConcurrency` workers; giving
    // each thread its own buffers avoids allocator churn without needing
    // a lock.
    thread_local CVScratch scratch;
#if BT_DETECTOR_TIMING
    double hsvMs = 0, ccMs = 0;
#endif
    BTBallDetection *detection = detectBallCoreScratch(
        bgra, hMin, hMax, sMin, sMax, vMin, vMax,
        aspectMin, fillMin,
        scratch, /*offsetX=*/0, /*offsetY=*/0,
        /*outBestW=*/nullptr, /*outBestH=*/nullptr
#if BT_DETECTOR_TIMING
        , &hsvMs, &ccMs
#endif
    );
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);

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
    cv::Mat bgra;
    if (!mapBGRAPixelBuffer(pixelBuffer, bgra)) { return @[]; }
    thread_local CVScratch scratch;
    NSArray<BTBallDetection *> *cands = detectAllCandidatesScratch(
        bgra, hMin, hMax, sMin, sMax, vMin, vMax, aspectMin, fillMin, scratch,
        /*offsetX=*/0.0, /*offsetY=*/0.0,
        /*outLargestW=*/nullptr, /*outLargestH=*/nullptr
    );
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
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
    cv::Mat bgra;
    if (!mapBGRAPixelBuffer(pixelBuffer, bgra)) { return nil; }

    BTBallDetection *result = nil;
    @try {
        result = [self detectInBGRA:bgra];
    } @catch (NSException *e) {
        NSLog(@"BallDetector: exception during detection: %@", e);
        result = nil;
    }

    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    return result;
}

- (nullable BTBallDetection *)detectInBGRA:(const cv::Mat &)bgra {
    const int W = bgra.cols;
    const int H = bgra.rows;

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
            cv::Mat crop = bgra(roi);
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
        bgra, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
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
    cv::Mat bgra;
    if (!mapBGRAPixelBuffer(pixelBuffer, bgra)) { return @[]; }

    NSArray<BTBallDetection *> *result = @[];
    @try {
        result = [self detectAllCandidatesInBGRA:bgra];
    } @catch (NSException *e) {
        NSLog(@"BallDetector: exception during multi-candidate detection: %@", e);
        result = @[];
    }

    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    return result;
}

- (NSArray<BTBallDetection *> *)detectAllCandidatesInBGRA:(const cv::Mat &)bgra {
    const int W = bgra.cols;
    const int H = bgra.rows;

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
            cv::Mat crop = bgra(roi);
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
        bgra, _hMin, _hMax, _sMin, _sMax, _vMin, _vMax,
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
