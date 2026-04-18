import Testing
import CoreVideo
import Foundation
@testable import ball_tracker

// MARK: - BallDetector
//
// These tests exercise BallDetector on synthetic 320x240 BGRA frames. The
// detector downsamples by step = max(2, min(w,h)/300) so at 320x240 the grid
// step is 2; a 40x40 opaque square covers ~400 grid cells (~1600 px² after
// the step² area approximation), well above the 20 px² area floor and below
// the 5000 px² ceiling.
//
// Deep-blue fixture: BGRA (255, 0, 0, 255) renders as RGB (0, 0, 255). RGB→HSV
// gives H=240° (OpenCV H=120, inside default 100..130), S=1.0 (255 inside
// 140..255), V=1.0 (255 inside 40..255) — so the square lights up the mask
// under the spec's default thresholds.

private let defaultRange = BallDetector.HSVRange(
    hMin: 100, hMax: 130,
    sMin: 140, sMax: 255,
    vMin: 40, vMax: 255
)

private func makeBGRAPixelBuffer(
    width: Int,
    height: Int,
    fill: (Int, Int) -> (b: UInt8, g: UInt8, r: UInt8, a: UInt8)
) -> CVPixelBuffer {
    var pb: CVPixelBuffer?
    let attrs: [CFString: Any] = [kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA]
    CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, attrs as CFDictionary, &pb)
    guard let buf = pb else { fatalError("CVPixelBufferCreate failed") }
    CVPixelBufferLockBaseAddress(buf, [])
    defer { CVPixelBufferUnlockBaseAddress(buf, []) }
    let bytesPerRow = CVPixelBufferGetBytesPerRow(buf)
    let base = CVPixelBufferGetBaseAddress(buf)!.assumingMemoryBound(to: UInt8.self)
    for y in 0..<height {
        for x in 0..<width {
            let (b, g, r, a) = fill(x, y)
            let o = y * bytesPerRow + x * 4
            base[o] = b
            base[o + 1] = g
            base[o + 2] = r
            base[o + 3] = a
        }
    }
    return buf
}

/// BGRA for a solid-grey frame (no saturation — fails S filter).
private func greyFill(_ x: Int, _ y: Int) -> (b: UInt8, g: UInt8, r: UInt8, a: UInt8) {
    return (128, 128, 128, 255)
}

/// Fill that paints a solid deep-blue square (BGRA 255,0,0,255 → RGB 0,0,255)
/// covering [x0, x0+size) × [y0, y0+size) and solid grey elsewhere.
private func blueSquareFill(x0: Int, y0: Int, size: Int)
    -> (Int, Int) -> (b: UInt8, g: UInt8, r: UInt8, a: UInt8)
{
    return { x, y in
        if x >= x0 && x < x0 + size && y >= y0 && y < y0 + size {
            return (255, 0, 0, 255)
        }
        return (128, 128, 128, 255)
    }
}

struct BallDetectorTests {

    @Test func noBallReturnsFalse() {
        let buf = makeBGRAPixelBuffer(width: 320, height: 240, fill: greyFill)
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: nil)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: 1.0
        )
        #expect(result.ballDetected == false)
        #expect(result.thetaXRad == nil)
        #expect(result.thetaZRad == nil)
        #expect(result.centroidX == nil)
        #expect(result.centroidY == nil)
    }

    @Test func blueBallAtCentreHasCentroidNearCentre() {
        // 40×40 square at (140..179, 100..139) — exactly centred on (160, 120).
        let buf = makeBGRAPixelBuffer(
            width: 320, height: 240,
            fill: blueSquareFill(x0: 140, y0: 100, size: 40)
        )
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: nil)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: 1.0
        )
        #expect(result.ballDetected == true)
        guard let cx = result.centroidX, let cy = result.centroidY else {
            Issue.record("centroid unset after positive detection")
            return
        }
        // Grid step is 2, so sampled cell centres average to (159, 119) —
        // 1 px off the true centre. Allow a 5-px slack.
        #expect(abs(cx - 160.0) <= 5.0)
        #expect(abs(cy - 120.0) <= 5.0)
    }

    @Test func blueBallOffCentreHasCorrectCentroid() {
        // 40×40 square at (80..119, 60..99) — true centre (99.5, 79.5).
        let buf = makeBGRAPixelBuffer(
            width: 320, height: 240,
            fill: blueSquareFill(x0: 80, y0: 60, size: 40)
        )
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: nil)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: 1.0
        )
        #expect(result.ballDetected == true)
        guard let cx = result.centroidX, let cy = result.centroidY else {
            Issue.record("centroid unset after positive detection")
            return
        }
        #expect(abs(cx - 100.0) <= 5.0)
        #expect(abs(cy - 80.0) <= 5.0)
    }

    @Test func tinyBallRejectedByAreaFilter() {
        // 3×3 square → raw area 9 px², below the 20 px² floor.
        let buf = makeBGRAPixelBuffer(
            width: 320, height: 240,
            fill: blueSquareFill(x0: 158, y0: 118, size: 3)
        )
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: nil)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: 1.0
        )
        #expect(result.ballDetected == false)
    }

    @Test func anglesUseCalibratedIntrinsics() {
        // 40×40 square at (180..219, 120..159) — with step=2 the sampled grid
        // centres average to (199, 139). Intrinsics fx=fz=500, cx=160, cy=120
        // give θx = atan2(199-160, 500), θz = atan2(139-120, 500).
        let buf = makeBGRAPixelBuffer(
            width: 320, height: 240,
            fill: blueSquareFill(x0: 180, y0: 120, size: 40)
        )
        let intr = BallDetector.Intrinsics(cx: 160, cy: 120, fx: 500, fz: 500)
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: intr)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: 1.0
        )
        #expect(result.ballDetected == true)
        guard let cx = result.centroidX,
              let cy = result.centroidY,
              let tx = result.thetaXRad,
              let tz = result.thetaZRad else {
            Issue.record("angles unset after positive detection")
            return
        }
        let expectedTx = atan2(cx - 160.0, 500.0)
        let expectedTz = atan2(cy - 120.0, 500.0)
        #expect(abs(tx - expectedTx) < 1e-9)
        #expect(abs(tz - expectedTz) < 1e-9)
    }

    @Test func fovFallbackDerivesFocalLengthsWhenIntrinsicsNil() {
        // No intrinsics → detector falls back to (w/2) / tan(fov/2) for fx and
        // derives fz from aspect-aware vertical FOV.
        let buf = makeBGRAPixelBuffer(
            width: 320, height: 240,
            fill: blueSquareFill(x0: 180, y0: 120, size: 40)
        )
        let fov = 1.0
        let detector = BallDetector(hsvRange: defaultRange, intrinsics: nil)
        let result = detector.detect(
            pixelBuffer: buf,
            imageWidth: 320,
            imageHeight: 240,
            horizontalFovRadians: fov
        )
        #expect(result.ballDetected == true)
        guard let cx = result.centroidX,
              let cy = result.centroidY,
              let tx = result.thetaXRad,
              let tz = result.thetaZRad else {
            Issue.record("angles unset after positive detection")
            return
        }
        let fx = (320.0 / 2.0) / tan(fov / 2.0)
        let vfov = 2.0 * atan(tan(fov / 2.0) * (240.0 / 320.0))
        let fz = (240.0 / 2.0) / tan(vfov / 2.0)
        let expectedTx = atan2(cx - 160.0, fx)
        let expectedTz = atan2(cy - 120.0, fz)
        #expect(abs(tx - expectedTx) < 1e-9)
        #expect(abs(tz - expectedTz) < 1e-9)
    }
}
