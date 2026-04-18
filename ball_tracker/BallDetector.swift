import Foundation
import CoreVideo
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Deep-blue ball detector (HSV masking) - skeleton implementation.
/// The spec requires per-frame HSV thresholding + contour filtering + centroid -> angle calculation.
final class BallDetector {
    struct Intrinsics {
        let cx: Double
        let cy: Double
        let fx: Double
        let fz: Double
    }

    struct HSVRange {
        var hMin: Int
        var hMax: Int
        var sMin: Int
        var sMax: Int
        var vMin: Int
        var vMax: Int
    }

    struct DetectionResult {
        var ballDetected: Bool
        var thetaXRad: Double?
        var thetaZRad: Double?
        var centroidX: Double?
        var centroidY: Double?
    }

    private let hsvRange: HSVRange
    private let intrinsics: Intrinsics?

    init(hsvRange: HSVRange, intrinsics: Intrinsics? = nil) {
        self.hsvRange = hsvRange
        self.intrinsics = intrinsics
        let source = intrinsics != nil ? "calibrated" : "fov_approx"
        log.info("detector initialized intrinsics=\(source, privacy: .public)")
    }

    /// Returns whether a deep-blue ball is detected and (if so) its angular offsets.
    ///
    /// Note: This is a prototype implementation that computes a centroid from HSV-thresholded pixels.
    /// It does not perform full contour extraction; later we can replace it with an OpenCV bridge.
    func detect(
        pixelBuffer: CVPixelBuffer,
        imageWidth: Int,
        imageHeight: Int,
        horizontalFovRadians: Double
    ) -> DetectionResult {
        // Direct BGRA plane access. CameraViewController pins videoSettings to
        // kCVPixelFormatType_32BGRA so the sample buffer we receive already
        // has the layout we want — no CIContext.render round-trip needed.
        // The previous implementation rasterized the full 1920×1080 frame to
        // an RGBA scratch bitmap every call (~50–80 ms) which capped effective
        // tracking throughput at ~14 fps regardless of the 240 fps capture
        // format. Reading the base address + skipping by `step` keeps the
        // per-frame cost proportional to the sampled grid (~230k reads) not
        // the full buffer (~2M pixels + GPU→CPU copy).
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        guard let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else {
            return DetectionResult(ballDetected: false, thetaXRad: nil, thetaZRad: nil, centroidX: nil, centroidY: nil)
        }
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
        let bgra = baseAddress.assumingMemoryBound(to: UInt8.self)

        let step = max(2, min(imageWidth, imageHeight) / 300) // reduce compute load
        let defaultCx = Double(imageWidth) / 2.0
        let defaultCy = Double(imageHeight) / 2.0
        let cx = intrinsics?.cx ?? defaultCx
        let cy = intrinsics?.cy ?? defaultCy

        // Connected-component approach on a downsampled grid (approx for "largest contour").
        let gridW = max(1, (imageWidth + step - 1) / step)
        let gridH = max(1, (imageHeight + step - 1) / step)
        var mask = [UInt8](repeating: 0, count: gridW * gridH)

        // Spec HSV ranges correspond to OpenCV-ish scale:
        // - H: 0..179 (spec says 100..130)
        // - S/V: 0..255
        for gy in 0..<gridH {
            let y = min(imageHeight - 1, gy * step)
            let rowBase = y * bytesPerRow
            for gx in 0..<gridW {
                let x = min(imageWidth - 1, gx * step)
                let idx = rowBase + x * 4
                // 32BGRA byte order on Apple platforms: B G R A.
                let b = Double(bgra[idx]) / 255.0
                let g = Double(bgra[idx + 1]) / 255.0
                let r = Double(bgra[idx + 2]) / 255.0

                let hsv = rgbToHSV(r: r, g: g, b: b)
                let hOpenCV = Int(round(hsv.hDegrees / 2.0)) // 0..179
                let s255 = Int(round(hsv.s * 255.0))
                let v255 = Int(round(hsv.v * 255.0))

                if hOpenCV < hsvRange.hMin || hOpenCV > hsvRange.hMax { continue }
                if s255 < hsvRange.sMin || s255 > hsvRange.sMax { continue }
                if v255 < hsvRange.vMin || v255 > hsvRange.vMax { continue }

                mask[gy * gridW + gx] = 1
            }
        }

        var visited = [UInt8](repeating: 0, count: gridW * gridH)
        var bestAreaCells: Int = 0
        var bestSumX: Double = 0
        var bestSumY: Double = 0

        let neighborOffsets = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

        for startIdx in 0..<(gridW * gridH) {
            if mask[startIdx] == 0 || visited[startIdx] == 1 { continue }

            // BFS over connected component in 8-neighborhood.
            var queue: [Int] = [startIdx]
            visited[startIdx] = 1
            var front = 0

            var areaCells = 0
            var sumX: Double = 0
            var sumY: Double = 0

            while front < queue.count {
                let cur = queue[front]
                front += 1

                let gx = cur % gridW
                let gy = cur / gridW
                let x = min(imageWidth - 1, gx * step)
                let y = min(imageHeight - 1, gy * step)

                areaCells += 1
                sumX += Double(x)
                sumY += Double(y)

                for (dx, dy) in neighborOffsets {
                    let nx = gx + dx
                    let ny = gy + dy
                    if nx < 0 || ny < 0 || nx >= gridW || ny >= gridH { continue }
                    let ni = ny * gridW + nx
                    if mask[ni] == 1 && visited[ni] == 0 {
                        visited[ni] = 1
                        queue.append(ni)
                    }
                }
            }

            let areaApproxPx2 = Double(areaCells) * Double(step * step)
            if areaApproxPx2 < 20.0 || areaApproxPx2 > 5000.0 {
                continue
            }

            if areaCells > bestAreaCells {
                bestAreaCells = areaCells
                bestSumX = sumX
                bestSumY = sumY
            }
        }

        if bestAreaCells <= 0 {
            return DetectionResult(ballDetected: false, thetaXRad: nil, thetaZRad: nil, centroidX: nil, centroidY: nil)
        }

        // Centroid (in pixels) using the largest connected component that passes area filter.
        let px = bestSumX / Double(bestAreaCells)
        let py = bestSumY / Double(bestAreaCells)

        // 3) Angle calculation using focal lengths in pixels.
        // If intrinsics are calibrated, use them; otherwise approximate via field-of-view.
        let fx: Double
        let fz: Double
        if let intr = intrinsics {
            fx = intr.fx
            fz = intr.fz
        } else {
            fx = (Double(imageWidth) / 2.0) / tan(horizontalFovRadians / 2.0)
            // Approx vertical FOV from aspect ratio to derive fz.
            let verticalFov = 2.0 * atan(tan(horizontalFovRadians / 2.0) * (Double(imageHeight) / Double(imageWidth)))
            fz = (Double(imageHeight) / 2.0) / tan(verticalFov / 2.0)
        }

        let thetaX = atan2(px - cx, fx)
        let thetaZ = atan2(py - cy, fz)

        return DetectionResult(ballDetected: true, thetaXRad: thetaX, thetaZRad: thetaZ, centroidX: px, centroidY: py)
    }

    // MARK: - Helpers

    private func rgbToHSV(r: Double, g: Double, b: Double) -> (hDegrees: Double, s: Double, v: Double) {
        let maxV = max(r, max(g, b))
        let minV = min(r, min(g, b))
        let delta = maxV - minV

        let v = maxV
        let s = maxV == 0 ? 0 : (delta / maxV)

        var h: Double = 0
        if delta == 0 {
            h = 0
        } else if maxV == r {
            h = 60.0 * ( (g - b) / delta ).truncatingRemainder(dividingBy: 6.0)
        } else if maxV == g {
            h = 60.0 * ( (b - r) / delta + 2.0 )
        } else {
            h = 60.0 * ( (r - g) / delta + 4.0 )
        }
        if h < 0 { h += 360.0 }
        return (hDegrees: h, s: s, v: v)
    }

}

