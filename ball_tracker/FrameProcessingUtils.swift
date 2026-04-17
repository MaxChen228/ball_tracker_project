import Foundation
import CoreVideo

enum FrameProcessingUtils {
    struct LuminanceStats {
        /// Full-frame mean luminance (0…255 gray).
        let mean: Double
        /// Mean luminance of the single brightest tile. Catches localized flashes
        /// (e.g. torch from another phone occupying <1/16 of the frame) that a
        /// full-frame mean would smear out.
        let maxTile: Double
    }

    /// Sparse-sampled luminance stats over the full frame, tiled into
    /// `tileRows × tileCols` cells. ~40k samples regardless of source
    /// resolution, so cost is constant at 240 fps.
    static func luminanceStats(
        pixelBuffer: CVPixelBuffer,
        tileRows: Int = 4,
        tileCols: Int = 4
    ) -> LuminanceStats {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        guard width > 0, height > 0, tileRows > 0, tileCols > 0 else {
            return LuminanceStats(mean: 0, maxTile: 0)
        }

        let xStep = max(1, width / 200)
        let yStep = max(1, height / 200)
        let tileCount = tileRows * tileCols
        var tileSum = [Double](repeating: 0, count: tileCount)
        var tileSamples = [Int](repeating: 0, count: tileCount)

        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)

        if pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
            pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange {
            guard let yBase = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0) else {
                return LuminanceStats(mean: 0, maxTile: 0)
            }
            let yStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
            let yPtr = yBase.assumingMemoryBound(to: UInt8.self)
            for y in stride(from: 0, to: height, by: yStep) {
                let tileRow = min(tileRows - 1, y * tileRows / height)
                let rowPtr = yPtr.advanced(by: y * yStride)
                for x in stride(from: 0, to: width, by: xStep) {
                    let tileCol = min(tileCols - 1, x * tileCols / width)
                    let idx = tileRow * tileCols + tileCol
                    tileSum[idx] += Double(rowPtr[x])
                    tileSamples[idx] += 1
                }
            }
        } else if pixelFormat == kCVPixelFormatType_32BGRA {
            guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else {
                return LuminanceStats(mean: 0, maxTile: 0)
            }
            let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
            let ptr = base.assumingMemoryBound(to: UInt8.self)
            for y in stride(from: 0, to: height, by: yStep) {
                let tileRow = min(tileRows - 1, y * tileRows / height)
                let rowPtr = ptr.advanced(by: y * bytesPerRow)
                for x in stride(from: 0, to: width, by: xStep) {
                    let tileCol = min(tileCols - 1, x * tileCols / width)
                    let idx = tileRow * tileCols + tileCol
                    let b = Double(rowPtr[x * 4])
                    let g = Double(rowPtr[x * 4 + 1])
                    let r = Double(rowPtr[x * 4 + 2])
                    // ITU-R BT.601 luma
                    tileSum[idx] += 0.299 * r + 0.587 * g + 0.114 * b
                    tileSamples[idx] += 1
                }
            }
        } else {
            return LuminanceStats(mean: 0, maxTile: 0)
        }

        var totalSum = 0.0
        var totalSamples = 0
        var maxTileMean = 0.0
        for i in 0..<tileCount {
            totalSum += tileSum[i]
            totalSamples += tileSamples[i]
            if tileSamples[i] > 0 {
                let m = tileSum[i] / Double(tileSamples[i])
                if m > maxTileMean { maxTileMean = m }
            }
        }
        let mean = totalSamples > 0 ? totalSum / Double(totalSamples) : 0
        return LuminanceStats(mean: mean, maxTile: maxTileMean)
    }
}
