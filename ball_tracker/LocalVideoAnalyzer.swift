import AVFoundation
import CoreMedia
import CoreVideo
import Foundation

final class LocalVideoAnalyzer {
    enum AnalysisError: Error {
        case cannotOpenAsset
        case missingVideoTrack
        case cannotStartReader
    }

    func analyze(
        videoURL: URL,
        videoStartPtsS: Double
    ) throws -> [ServerUploader.FramePayload] {
        let asset = AVAsset(url: videoURL)
        guard let track = asset.tracks(withMediaType: .video).first else {
            throw AnalysisError.missingVideoTrack
        }

        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(
            track: track,
            outputSettings: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            ]
        )
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else {
            throw AnalysisError.cannotOpenAsset
        }
        reader.add(output)
        guard reader.startReading() else {
            throw AnalysisError.cannotStartReader
        }

        let detector = BTDetectionSession()
        var frames: [ServerUploader.FramePayload] = []
        var frameIndex = 0
        while reader.status == .reading, let sample = output.copyNextSampleBuffer() {
            defer { CMSampleBufferInvalidate(sample) }
            guard let pixelBuffer = CMSampleBufferGetImageBuffer(sample) else {
                continue
            }
            let pts = CMSampleBufferGetPresentationTimeStamp(sample)
            let ts = videoStartPtsS + CMTimeGetSeconds(pts)
            if !ts.isFinite {
                continue
            }
            let detection = detector.apply(pixelBuffer)
            frames.append(
                ServerUploader.FramePayload(
                    frame_index: frameIndex,
                    timestamp_s: ts,
                    px: detection.map { Double($0.px) },
                    py: detection.map { Double($0.py) },
                    ball_detected: detection != nil
                )
            )
            frameIndex += 1
        }

        if reader.status == .failed {
            throw reader.error ?? AnalysisError.cannotOpenAsset
        }
        return frames
    }
}
