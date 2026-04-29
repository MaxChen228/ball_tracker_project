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
///   - multi-candidate returns sorted by area desc.
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

    /// Adds a second filled circle to an existing buffer at `extraCenter`,
    /// so a multi-candidate test can assert the detector ships both blobs.
    private func addCircle(
        _ buffer: CVPixelBuffer,
        center: CGPoint,
        radius: CGFloat,
        rgb: (r: UInt8, g: UInt8, b: UInt8) = (210, 235, 40)
    ) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let base = CVPixelBufferGetBaseAddress(buffer)!
        let rowBytes = CVPixelBufferGetBytesPerRow(buffer)
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
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
    }

    // MARK: - Stateless single-best path

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

    // MARK: - Stateless multi-candidate path

    func testStatelessMultiCandidateReturnsAllSortedByAreaDesc() {
        let bigCenter = CGPoint(x: 600, y: 400)
        let smallCenter = CGPoint(x: 1200, y: 700)
        let buf = makeBGRA(circleCenter: bigCenter, radius: 32)
        addCircle(buf, center: smallCenter, radius: 18)

        let cands = BTBallDetector.detectAllCandidates(
            in: buf,
            hMin: Int32(25), hMax: Int32(55),
            sMin: Int32(90), sMax: Int32(255),
            vMin: Int32(90), vMax: Int32(255),
            aspectMin: 0.70, fillMin: 0.55
        )
        XCTAssertEqual(cands.count, 2, "Both yellow-green discs must pass the gates")
        XCTAssertEqual(Double(cands[0].px), Double(bigCenter.x), accuracy: 5.0)
        XCTAssertEqual(Double(cands[0].py), Double(bigCenter.y), accuracy: 5.0)
        XCTAssertGreaterThan(cands[0].areaPx, cands[1].areaPx)
    }

    func testStatelessMultiCandidateReturnsEmptyOnBlankImage() {
        let buf = makeBGRA(circleCenter: nil)
        let cands = BTBallDetector.detectAllCandidates(
            in: buf,
            hMin: Int32(25), hMax: Int32(55),
            sMin: Int32(90), sMax: Int32(255),
            vMin: Int32(90), vMax: Int32(255),
            aspectMin: 0.70, fillMin: 0.55
        )
        XCTAssertEqual(cands.count, 0)
    }
}
