import XCTest
import CoreVideo
import CoreGraphics
import CoreImage
@testable import ball_tracker

/// Cross-language parity fixture for the HSV ball detector.
///
/// This mirrors `server/test_detection_parity.py` frame-for-frame — same
/// image dimensions, same yellow-green BGR fill (HSV(40,210,210) →
/// BGR(37,210,152)), same radius / center / noise seed, same distracter —
/// and asserts that `BTBallDetector` (Obj-C++ / OpenCV) recovers the
/// ball at the same location within the same tolerance as the Python
/// side. Any drift here means the two detection paths (iOS `live` vs
/// server `server_post`) are no longer running the same pipeline, which
/// violates CLAUDE.md's "HSV / area / shape constants kept lock-step"
/// contract.
///
/// Python reference (server/test_detection_parity.py):
///   - `_yg_bgr()` = HSV(40,210,210) → BGR(37,210,152)
///   - clean     : 1280×720 bg=(30,30,30), circle (640,360) r=22
///   - blur      : same, then cv2.GaussianBlur ksize=11 σ=3 at (800,400) r=22
///   - cluttered : 1280×720 noise 0-49, ball (500,300) r=20, blue rect
///                 (900..1100, 500..700) BGR(200,50,50) distracter
///
/// On Swift side we build BGRA CVPixelBuffers directly (OpenCV's 8-bit
/// HSV convention, 0-179 hue, matches the detector's internal cvtColor),
/// apply Core Image's CIGaussianBlur for the blur scene (numerically
/// close to cv2.GaussianBlur at σ=3), and feed each to `BTBallDetector`.
final class BallDetectorParityTests: XCTestCase {

    // HSV(40, 210, 210) → BGR(37, 210, 152); RGB tuple for pixel write is
    // (r=152, g=210, b=37). Mirrors _yg_bgr() in the Python fixture.
    private static let yellowGreenRGB: (r: UInt8, g: UInt8, b: UInt8) = (152, 210, 37)
    // Distracter colour used in the cluttered scene: BGR(200, 50, 50) →
    // RGB(50, 50, 200). Hue sits far from yellow-green so the HSV gate
    // rejects it cleanly with no prior needed.
    private static let distracterRGB: (r: UInt8, g: UInt8, b: UInt8) = (50, 50, 200)

    // MARK: - BGRA helpers

    /// Allocate a width×height BGRA pixel buffer, alpha opaque.
    private func makeBlankBGRA(width: Int, height: Int) -> CVPixelBuffer {
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
        precondition(status == kCVReturnSuccess, "CVPixelBufferCreate failed \(status)")
        let buffer = pb!
        CVPixelBufferLockBaseAddress(buffer, [])
        let base = CVPixelBufferGetBaseAddress(buffer)!
        let row = CVPixelBufferGetBytesPerRow(buffer)
        for y in 0..<height {
            let p = base.advanced(by: y * row).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width {
                let i = x * 4
                p[i + 0] = 0    // B
                p[i + 1] = 0    // G
                p[i + 2] = 0    // R
                p[i + 3] = 0xFF // A
            }
        }
        CVPixelBufferUnlockBaseAddress(buffer, [])
        return buffer
    }

    /// Fill the entire buffer with one BGR colour (alpha untouched).
    private func fillSolid(_ buffer: CVPixelBuffer, rgb: (r: UInt8, g: UInt8, b: UInt8)) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let row = CVPixelBufferGetBytesPerRow(buffer)
        let base = CVPixelBufferGetBaseAddress(buffer)!
        for y in 0..<height {
            let p = base.advanced(by: y * row).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width {
                let i = x * 4
                p[i + 0] = rgb.b
                p[i + 1] = rgb.g
                p[i + 2] = rgb.r
            }
        }
    }

    /// Paint a filled axis-aligned disc (cx, cy, r) in BGR.
    private func drawCircle(
        _ buffer: CVPixelBuffer,
        cx: Int, cy: Int, radius: Int,
        rgb: (r: UInt8, g: UInt8, b: UInt8)
    ) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let row = CVPixelBufferGetBytesPerRow(buffer)
        let base = CVPixelBufferGetBaseAddress(buffer)!
        let r2 = radius * radius
        let minX = max(0, cx - radius)
        let maxX = min(width - 1, cx + radius)
        let minY = max(0, cy - radius)
        let maxY = min(height - 1, cy + radius)
        for y in minY...maxY {
            let p = base.advanced(by: y * row).assumingMemoryBound(to: UInt8.self)
            for x in minX...maxX {
                let dx = x - cx
                let dy = y - cy
                if dx * dx + dy * dy <= r2 {
                    let i = x * 4
                    p[i + 0] = rgb.b
                    p[i + 1] = rgb.g
                    p[i + 2] = rgb.r
                }
            }
        }
    }

    /// Paint a filled axis-aligned rectangle (inclusive bounds) in BGR.
    private func drawRect(
        _ buffer: CVPixelBuffer,
        x0: Int, y0: Int, x1: Int, y1: Int,
        rgb: (r: UInt8, g: UInt8, b: UInt8)
    ) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let row = CVPixelBufferGetBytesPerRow(buffer)
        let base = CVPixelBufferGetBaseAddress(buffer)!
        let xa = max(0, x0), xb = min(width - 1, x1)
        let ya = max(0, y0), yb = min(height - 1, y1)
        guard xa <= xb, ya <= yb else { return }
        for y in ya...yb {
            let p = base.advanced(by: y * row).assumingMemoryBound(to: UInt8.self)
            for x in xa...xb {
                let i = x * 4
                p[i + 0] = rgb.b
                p[i + 1] = rgb.g
                p[i + 2] = rgb.r
            }
        }
    }

    /// Overwrite every pixel with uniform noise in [0, maxValue) per
    /// channel, driven by a deterministic seeded LCG so the scene is
    /// reproducible across runs. The Python fixture uses
    /// `np.random.default_rng(seed=0)`; the exact bytes differ but the
    /// statistical properties (uniform low-intensity) match — the
    /// detector's HSV V gate (≥90) rejects this noise anyway, so parity
    /// is preserved at the behavioural level.
    private func fillSeededNoise(
        _ buffer: CVPixelBuffer,
        maxValue: UInt32 = 50,
        seed: UInt64 = 1
    ) {
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let row = CVPixelBufferGetBytesPerRow(buffer)
        let base = CVPixelBufferGetBaseAddress(buffer)!
        var state = seed
        // Simple LCG (Numerical Recipes constants) — deterministic.
        func next() -> UInt32 {
            state = state &* 1664525 &+ 1013904223
            return UInt32(truncatingIfNeeded: state)
        }
        for y in 0..<height {
            let p = base.advanced(by: y * row).assumingMemoryBound(to: UInt8.self)
            for x in 0..<width {
                let i = x * 4
                p[i + 0] = UInt8(next() % maxValue) // B
                p[i + 1] = UInt8(next() % maxValue) // G
                p[i + 2] = UInt8(next() % maxValue) // R
            }
        }
    }

    /// Apply a CIGaussianBlur-equivalent pass to a BGRA buffer in place
    /// via Core Image. Returns a fresh BGRA buffer of the same size so
    /// the detector still receives the BGRA format it needs.
    /// Numerically close to `cv2.GaussianBlur(ksize=(11,11), sigmaX=3)`
    /// for the detector's purposes (fill-rate & centroid accuracy), even
    /// though the kernel truncation is different.
    private func gaussianBlur(_ input: CVPixelBuffer, radius: Double) -> CVPixelBuffer {
        let ci = CIImage(cvPixelBuffer: input)
        let filter = CIFilter(name: "CIGaussianBlur")!
        filter.setValue(ci, forKey: kCIInputImageKey)
        filter.setValue(radius, forKey: kCIInputRadiusKey)
        let out = filter.outputImage!.cropped(to: ci.extent)

        let width = CVPixelBufferGetWidth(input)
        let height = CVPixelBufferGetHeight(input)
        var pb: CVPixelBuffer?
        let attrs: [CFString: Any] = [
            kCVPixelBufferCGImageCompatibilityKey: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey: true,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        CVPixelBufferCreate(
            kCFAllocatorDefault, width, height,
            kCVPixelFormatType_32BGRA,
            attrs as CFDictionary, &pb
        )
        let context = CIContext(options: nil)
        context.render(out, to: pb!)
        return pb!
    }

    // MARK: - Tests — mirror server/test_detection_parity.py

    /// Python: test_clean_scene — circle (640, 360) r=22 on bg=(30,30,30),
    /// tolerance ±2 px.
    func testCleanScene() {
        let buf = makeBlankBGRA(width: 1280, height: 720)
        fillSolid(buf, rgb: (r: 30, g: 30, b: 30))
        drawCircle(buf, cx: 640, cy: 360, radius: 22, rgb: Self.yellowGreenRGB)

        let d = BTBallDetector.detect(in: buf)
        XCTAssertNotNil(d, "Clean scene should yield a detection (parity with Python)")
        guard let d else { return }
        XCTAssertEqual(Double(d.px), 640, accuracy: 2.0)
        XCTAssertEqual(Double(d.py), 360, accuracy: 2.0)
    }

    /// Python: test_motion_blur_scene — 11×11 Gaussian σ=3 at (800, 400)
    /// r=22, tolerance ±4 px.
    func testMotionBlurScene() {
        let raw = makeBlankBGRA(width: 1280, height: 720)
        fillSolid(raw, rgb: (r: 30, g: 30, b: 30))
        drawCircle(raw, cx: 800, cy: 400, radius: 22, rgb: Self.yellowGreenRGB)
        // Core Image's radius ≈ sigma; match Python's sigmaX=3.0.
        let blurred = gaussianBlur(raw, radius: 3.0)

        let d = BTBallDetector.detect(in: blurred)
        XCTAssertNotNil(d, "Blurred scene should still yield a detection")
        guard let d else { return }
        XCTAssertEqual(Double(d.px), 800, accuracy: 4.0)
        XCTAssertEqual(Double(d.py), 400, accuracy: 4.0)
    }

    /// Python: test_cluttered_scene_without_prior — noise 0-49, ball
    /// (500, 300) r=20, blue distracter rect (900..1100, 500..700).
    /// HSV mask rejects the distracter; noise's V ≤49 < 90 is gated out.
    /// Tolerance ±3 px.
    func testClutteredScene() {
        let buf = makeBlankBGRA(width: 1280, height: 720)
        fillSeededNoise(buf, maxValue: 50, seed: 1)
        drawCircle(buf, cx: 500, cy: 300, radius: 20, rgb: Self.yellowGreenRGB)
        drawRect(buf, x0: 900, y0: 500, x1: 1100, y1: 700, rgb: Self.distracterRGB)

        let d = BTBallDetector.detect(in: buf)
        XCTAssertNotNil(d, "Cluttered scene should still find the yellow-green ball")
        guard let d else { return }
        XCTAssertEqual(Double(d.px), 500, accuracy: 3.0)
        XCTAssertEqual(Double(d.py), 300, accuracy: 3.0)
    }
}
