import Foundation
import CoreVideo

enum FrameProcessingUtils {
    /// Compute mean luminance from pixel buffer.
    /// - If pixel format is bi-planar YUV, reads Y plane directly.
    /// - If pixel format is BGRA, uses BT.601 coefficients.
    static func meanLuminance(pixelBuffer: CVPixelBuffer, roiHeightFraction: Double = 0.25) -> Double {
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let roiEndY = max(1, Int(Double(height) * max(0.0, min(1.0, roiHeightFraction))))

        let xStep = max(1, width / 200)
        let yStep = max(1, roiEndY / 200)

        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        let pixelFormat = CVPixelBufferGetPixelFormatType(pixelBuffer)
        var sumLum = 0.0
        var count = 0.0

        if pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
            pixelFormat == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange {
            guard let yBase = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0) else { return 0.0 }
            let yStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
            let yPtr = yBase.assumingMemoryBound(to: UInt8.self)

            for y in stride(from: 0, to: roiEndY, by: yStep) {
                let rowPtr = yPtr.advanced(by: y * yStride)
                for x in stride(from: 0, to: width, by: xStep) {
                    sumLum += Double(rowPtr[x])
                    count += 1.0
                }
            }
        } else if pixelFormat == kCVPixelFormatType_32BGRA {
            guard let base = CVPixelBufferGetBaseAddress(pixelBuffer) else { return 0.0 }
            let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)
            let ptr = base.assumingMemoryBound(to: UInt8.self)

            for y in stride(from: 0, to: roiEndY, by: yStep) {
                let rowPtr = ptr.advanced(by: y * bytesPerRow)
                for x in stride(from: 0, to: width, by: xStep) {
                    let b = Double(rowPtr[x * 4])
                    let g = Double(rowPtr[x * 4 + 1])
                    let r = Double(rowPtr[x * 4 + 2])
                    let lum = 0.299 * r + 0.587 * g + 0.114 * b
                    sumLum += lum
                    count += 1.0
                }
            }
        } else {
            return 0.0
        }

        guard count > 0 else { return 0.0 }
        return sumLum / count
    }
}

