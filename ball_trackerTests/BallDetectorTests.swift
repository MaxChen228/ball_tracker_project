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

    // MARK: - Stateful + ROI multi-candidate

    /// Adds a second filled circle to an existing buffer at `extraCenter`,
    /// so a multi-candidate test can assert the detector ships both blobs.
    /// The second circle uses the same yellow-green colour by default —
    /// caller can override via `rgb` if a test wants different magnitudes.
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

    func testStatefulMultiCandidateReturnsAllOnFullFrame() {
        // First arm of the detector — no prior ROI, so the multi-cand path
        // takes the full-frame branch and should return both discs sorted
        // by area desc.
        let detector = BTStatefulBallDetector()
        let bigCenter = CGPoint(x: 600, y: 400)
        let smallCenter = CGPoint(x: 1200, y: 700)
        let buf = makeBGRA(circleCenter: bigCenter, radius: 32)
        addCircle(buf, center: smallCenter, radius: 18)

        let cands = detector.detectAllCandidates(in: buf)
        XCTAssertEqual(cands.count, 2, "Both yellow-green discs must pass the gates")
        // Largest first.
        XCTAssertEqual(Double(cands[0].px), Double(bigCenter.x), accuracy: 5.0)
        XCTAssertEqual(Double(cands[0].py), Double(bigCenter.y), accuracy: 5.0)
        XCTAssertGreaterThan(cands[0].areaPx, cands[1].areaPx)
    }

    func testStatefulMultiCandidateUsesROIAfterPriorHit() {
        // Frame 0: full-frame run anchors the ROI on the big disc.
        let detector = BTStatefulBallDetector()
        let bigCenter = CGPoint(x: 600, y: 400)
        let buf0 = makeBGRA(circleCenter: bigCenter, radius: 32)
        let cands0 = detector.detectAllCandidates(in: buf0)
        XCTAssertEqual(cands0.count, 1)

        // Frame 1: same big disc moved slightly (within 3× radius ROI), plus
        // a far-away decoy that's OUTSIDE the ROI. The ROI pass should
        // ignore the decoy entirely → exactly one candidate returned.
        let bigMoved = CGPoint(x: bigCenter.x + 50, y: bigCenter.y - 30)
        let decoyCenter = CGPoint(x: 1700, y: 950)
        let buf1 = makeBGRA(circleCenter: bigMoved, radius: 32)
        addCircle(buf1, center: decoyCenter, radius: 18)
        let cands1 = detector.detectAllCandidates(in: buf1)
        XCTAssertEqual(cands1.count, 1, "ROI should crop out the far decoy")
        XCTAssertEqual(Double(cands1[0].px), Double(bigMoved.x), accuracy: 5.0)
        XCTAssertEqual(Double(cands1[0].py), Double(bigMoved.y), accuracy: 5.0)
    }

    func testStatefulMultiCandidateFallsBackOnROIMiss() {
        // Frame 0: anchor ROI on top-left.
        let detector = BTStatefulBallDetector()
        let c0 = CGPoint(x: 120, y: 120)
        XCTAssertEqual(detector.detectAllCandidates(in: makeBGRA(circleCenter: c0, radius: 28)).count, 1)

        // Frame 1: ball jumps far outside ROI; full-frame fallback must
        // recover it (and update tracking from the new largest blob).
        let c1 = CGPoint(x: 1800, y: 960)
        let buf1 = makeBGRA(circleCenter: c1, radius: 32)
        addCircle(buf1, center: CGPoint(x: 1500, y: 800), radius: 18)
        let cands1 = detector.detectAllCandidates(in: buf1)
        XCTAssertEqual(cands1.count, 2, "Both visible blobs survive the gate")
        XCTAssertEqual(Double(cands1[0].px), Double(c1.x), accuracy: 5.0)
    }

    func testStatefulMultiCandidateReturnsEmptyOnBlankImage() {
        let detector = BTStatefulBallDetector()
        let buf = makeBGRA(circleCenter: nil)
        XCTAssertEqual(detector.detectAllCandidates(in: buf).count, 0)
    }

    func testStatefulMultiCandidateSharesROIStateWithSingleBest() {
        // Single-best `detect` and multi `detectAllCandidates` must read /
        // write the same ROI state — calling one then the other on
        // overlapping frames should keep the cropping consistent.
        let detector = BTStatefulBallDetector()
        let c0 = CGPoint(x: 800, y: 500)
        XCTAssertNotNil(detector.detect(in: makeBGRA(circleCenter: c0, radius: 28)))

        // Far decoy now ignored by both APIs since they share `_hasPrev`.
        let cMoved = CGPoint(x: 850, y: 480)
        let buf = makeBGRA(circleCenter: cMoved, radius: 28)
        addCircle(buf, center: CGPoint(x: 1700, y: 100), radius: 18)
        let cands = detector.detectAllCandidates(in: buf)
        XCTAssertEqual(cands.count, 1, "ROI from prior single-best call should still crop the decoy out")
    }
}
