#import "ArucoDetector.h"

#import <opencv2/opencv2.h>
#import <opencv2/objdetect/aruco_detector.hpp>
#import <opencv2/imgproc.hpp>
#import <opencv2/calib3d.hpp>

#import <vector>

// MARK: - BTArucoMarker

@implementation BTArucoMarker {
    int _markerId;
    CGPoint _corners[4];
}

- (instancetype)initWithId:(int)markerId corners:(const std::vector<cv::Point2f>&)corners {
    self = [super init];
    if (self) {
        _markerId = markerId;
        for (int i = 0; i < 4 && i < (int)corners.size(); i++) {
            _corners[i] = CGPointMake(corners[i].x, corners[i].y);
        }
    }
    return self;
}

- (int)markerId { return _markerId; }
- (CGPoint)corner0 { return _corners[0]; }
- (CGPoint)corner1 { return _corners[1]; }
- (CGPoint)corner2 { return _corners[2]; }
- (CGPoint)corner3 { return _corners[3]; }
- (CGPoint)center {
    return CGPointMake(
        (_corners[0].x + _corners[1].x + _corners[2].x + _corners[3].x) * 0.25,
        (_corners[0].y + _corners[1].y + _corners[2].y + _corners[3].y) * 0.25
    );
}

@end

// MARK: - BTArucoDetector

@implementation BTArucoDetector

+ (NSArray<BTArucoMarker *> *)detectMarkersInPixelBuffer:(CVPixelBufferRef)pixelBuffer
{
    if (!pixelBuffer) { return @[]; }

    OSType pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer);
    if (pixelFormat != kCVPixelFormatType_32BGRA) {
        // Only BGRA is supported for now — the app pipeline uses BGRA output.
        return @[];
    }

    CVPixelBufferLockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
    const size_t width = CVPixelBufferGetWidth(pixelBuffer);
    const size_t height = CVPixelBufferGetHeight(pixelBuffer);
    const size_t bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer);
    void *base = CVPixelBufferGetBaseAddress(pixelBuffer);
    if (!base) {
        CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);
        return @[];
    }

    cv::Mat bgra((int)height, (int)width, CV_8UC4, base, bytesPerRow);
    cv::Mat gray;
    cv::cvtColor(bgra, gray, cv::COLOR_BGRA2GRAY);
    CVPixelBufferUnlockBaseAddress(pixelBuffer, kCVPixelBufferLock_ReadOnly);

    cv::aruco::Dictionary dict = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_50);
    cv::aruco::DetectorParameters params;
    cv::aruco::ArucoDetector detector(dict, params);

    std::vector<std::vector<cv::Point2f>> markerCorners;
    std::vector<int> markerIds;
    detector.detectMarkers(gray, markerCorners, markerIds);

    NSMutableArray<BTArucoMarker *> *results = [NSMutableArray arrayWithCapacity:markerIds.size()];
    for (size_t i = 0; i < markerIds.size(); i++) {
        [results addObject:[[BTArucoMarker alloc] initWithId:markerIds[i] corners:markerCorners[i]]];
    }
    return results;
}

+ (nullable NSArray<NSNumber *> *)findHomographyFromWorldPoints:(NSArray<NSValue *> *)worldPoints
                                                   imagePoints:(NSArray<NSValue *> *)imagePoints
{
    if (worldPoints.count < 4 || worldPoints.count != imagePoints.count) {
        return nil;
    }

    std::vector<cv::Point2f> src;
    std::vector<cv::Point2f> dst;
    src.reserve(worldPoints.count);
    dst.reserve(imagePoints.count);

    for (NSUInteger i = 0; i < worldPoints.count; i++) {
        CGPoint w = [worldPoints[i] CGPointValue];
        CGPoint p = [imagePoints[i] CGPointValue];
        src.emplace_back((float)w.x, (float)w.y);
        dst.emplace_back((float)p.x, (float)p.y);
    }

    cv::Mat H = cv::findHomography(src, dst, cv::RANSAC, 3.0);
    if (H.empty() || H.rows != 3 || H.cols != 3) { return nil; }

    // Normalize so h33 = 1 (matches iOS-side convention in UserDefaults).
    double h33 = H.at<double>(2, 2);
    if (std::abs(h33) < 1e-12) { return nil; }
    NSMutableArray<NSNumber *> *out = [NSMutableArray arrayWithCapacity:9];
    for (int r = 0; r < 3; r++) {
        for (int c = 0; c < 3; c++) {
            [out addObject:@(H.at<double>(r, c) / h33)];
        }
    }
    return out;
}

@end
