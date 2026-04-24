import XCTest
import CoreVideo
import CoreGraphics
@testable import ball_tracker

/// Unit tests for the Obj-C++ HSV ball detector.
///
/// We synthesise BGRA CVPixelBuffers with a filled yellow-green circle at
/// known coords and assert the detector recovers the centroid within a
/// tight pixel tolerance. The goal is to lock behaviour on:
///   - happy path centroid accuracy on a bright yellow-green disc,
///   - blank-image rejection,
///   - ROI-tracking hit after a small offset follow-up,
///   - loud ROI-miss → full-frame fallback path.
final class BallDetectorTests: XCTestCase {

    // MARK: - Synthetic buffer helpers

    /// 1920×1080 BGRA buffer with a filled circle of the given colour at
    /// (cx, cy). Background is near-black so HSV V gate kills everything
    /// outside the disc.
    private func makeBGRA(
        width: Int = 1920,
        height: Int = 1080,
        circleCenter: CGPoint?,
        radius: CGFloat = 30,
        rgb: (r: UInt8, g: UInt8, b: UInt8) = (210, 235, 40) // yellow-green
    ) -> CVPixelBuffer {
        var pb: CVPixelBuffer?
        let attrs: [CFString: Any] = [
            kCVPixelBufferCGImageCompatibilityKey: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey: true
        ]
        let status = CVPixelBufferCreate(
            kCFAllocatorDefault, width, height,
            kCVPixelFormatType_32BGRA,
            attrs as CFDictionary, &pb
        )
        precondition(status == kCVReturnSuccess, "CVPixelBufferCreate failed: \(status)")
        let buffer = pb!
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

        let base = CVPixelBufferGetBaseAddress(buffer)!
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)

        // Zero = black background, alpha = 0xFF.
        for y in 0..<height {
            let row = base.advanced(by: y * rowBytes).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width {
                let i = x * 4
                row[i + 0] = 0      // B
                row[i + 1] = 0      // G
                row[i + 2] = 0      // R
                row[i + 3] = 0xFF   // A
            }
        }

        guard let center = circleCenter else { return buffer }
        let r2 = radius * radius
        let minX = max(0, Int(center.x - radius))
        let maxX = min(width - 1, Int(center.x + radius))
        let minY = max(0, Int(center.y - radius))
        let maxY = min(height - 1, Int(center.y + radius))
        for y in minY...maxY {
            let row = base.advanced(by: y * rowBytes).assumingMemoryBound(to: UInt8.self)
            for x in minX...maxX {
                let dx = CGFloat(x) - center.x
                let dy = CGFloat(y) - center.y
                if dx * dx + dy * dy <= r2 {
                    let i = x * 4
                    row[i + 0] = rgb.b
                    row[i + 1] = rgb.g
                    row[i + 2] = rgb.r
                    row[i + 3] = 0xFF
                }
            }
        }
        return buffer
    }

    // MARK: - Stateless path

    func testStatelessDetectsYellowGreenCircleAtKnownCenter() {
        let center = CGPoint(x: 960, y: 540)
        let buf = makeBGRA(circleCenter: center, radius: 28)
        let detection = BTBallDetector.detect(in: buf)
        XCTAssertNotNil(detection, "Detector should find the synthetic disc")
        guard let d = detection else { return }
        XCTAssertEqual(Double(d.px), Double(center.x), accuracy: 5.0)
        XCTAssertEqual(Double(d.py), Double(center.y), accuracy: 5.0)
    }

    func testStatelessReturnsNilOnBlankImage() {
        let buf = makeBGRA(circleCenter: nil)
        let detection = BTBallDetector.detect(in: buf)
        XCTAssertNil(detection, "Blank image must not produce a detection")
    }

    // MARK: - Stateful + ROI

    func testStatefulTracksAcrossSmallOffsetHit() {
        let detector = BTStatefulBallDetector()
        let c0 = CGPoint(x: 960, y: 540)
        let buf0 = makeBGRA(circleCenter: c0, radius: 28)
        let d0 = detector.detect(in: buf0)
        XCTAssertNotNil(d0, "Full-frame pass should find first frame")

        // ±100 px offset — inside the 3× radius ROI crop, so the ROI path
        // should still hit without a fallback log.
        let c1 = CGPoint(x: c0.x + 80, y: c0.y - 60)
        let buf1 = makeBGRA(circleCenter: c1, radius: 28)
        let d1 = detector.detect(in: buf1)
        XCTAssertNotNil(d1, "ROI pass should still hit on small offset")
        if let d1 = d1 {
            XCTAssertEqual(Double(d1.px), Double(c1.x), accuracy: 5.0)
            XCTAssertEqual(Double(d1.py), Double(c1.y), accuracy: 5.0)
        }
    }

    func testStatefulFallsBackToFullFrameAfterROIMiss() {
        let detector = BTStatefulBallDetector()
        // First frame: ball at far-left corner.
        let c0 = CGPoint(x: 120, y: 120)
        let buf0 = makeBGRA(circleCenter: c0, radius: 28)
        XCTAssertNotNil(detector.detect(in: buf0))

        // Second frame: ball jumps to far-right — way outside the ROI
        // crop around c0. ROI pass misses → full-frame fallback should
        // recover the new position.
        let c1 = CGPoint(x: 1800, y: 960)
        let buf1 = makeBGRA(circleCenter: c1, radius: 28)
        let d1 = detector.detect(in: buf1)
        XCTAssertNotNil(d1, "Fallback full-frame pass should recover distant hit")
        if let d1 = d1 {
            XCTAssertEqual(Double(d1.px), Double(c1.x), accuracy: 5.0)
            XCTAssertEqual(Double(d1.py), Double(c1.y), accuracy: 5.0)
        }
    }

    func testResetTrackingDropsROIState() {
        let detector = BTStatefulBallDetector()
        let c0 = CGPoint(x: 400, y: 400)
        XCTAssertNotNil(detector.detect(in: makeBGRA(circleCenter: c0, radius: 28)))

        detector.resetTracking()

        // After reset, a ball at a distant location must still be found
        // (via the full-frame path, with no ROI bias from the prior hit).
        let c1 = CGPoint(x: 1600, y: 900)
        let buf1 = makeBGRA(circleCenter: c1, radius: 28)
        let d1 = detector.detect(in: buf1)
        XCTAssertNotNil(d1)
        if let d1 = d1 {
            XCTAssertEqual(Double(d1.px), Double(c1.x), accuracy: 5.0)
            XCTAssertEqual(Double(d1.py), Double(c1.y), accuracy: 5.0)
        }
    }

    func testStatefulReturnsNilOnBlankImage() {
        let detector = BTStatefulBallDetector()
        let buf = makeBGRA(circleCenter: nil)
        XCTAssertNil(detector.detect(in: buf))
    }
}
