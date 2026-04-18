import AVFoundation
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Per-cycle H.264 clip writer that consumes the same `CMSampleBuffer`s the
/// capture queue already dispatches to `CameraViewController.captureOutput`.
/// Wraps `AVAssetWriter` + one video input; audio is intentionally dropped
/// (the chirp anchor is the only audio consumer and it runs elsewhere).
///
/// Phase 1 scope: clip starts at `append(sampleBuffer:)` of the first frame
/// passed in (typically the first ball-detected frame). Pre-roll frames live
/// only in the JSON payload; covering them in video would cost ~1GB of
/// buffered BGRA, which is not worth it for Phase 1.
///
/// Threading: the caller serialises `prepare`/`append`/`finish`/`cancel`. In
/// `CameraViewController` all calls happen on the capture processing queue.
/// `finish` hands its completion to an arbitrary queue (AVAssetWriter's
/// internal queue) — consumers that need main-thread work dispatch from
/// there.
final class ClipRecorder {
    enum ClipRecorderError: Error {
        case cannotAddVideoInput
        case notStarted
    }

    private let outputURL: URL
    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var sessionStarted: Bool = false
    private(set) var droppedFrameCount: Int = 0
    /// Session-clock PTS of the first sample appended. Uploader pairs this
    /// with the JSON payload so the server can reconstruct absolute PTS for
    /// each decoded frame.
    private(set) var firstSamplePTS: CMTime?

    init(outputURL: URL) {
        self.outputURL = outputURL
    }

    /// Open the writer. Must be called before any `append`. Uses H.264 so the
    /// server side (OpenCV / FFmpeg) can decode without special builds.
    func prepare(width: Int, height: Int) throws {
        try? FileManager.default.removeItem(at: outputURL)
        let w: AVAssetWriter
        do {
            w = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
        } catch {
            log.error("clip writer init failed error=\(error.localizedDescription, privacy: .public)")
            throw error
        }
        let input = AVAssetWriterInput(
            mediaType: .video,
            outputSettings: [
                AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: width,
                AVVideoHeightKey: height,
            ]
        )
        input.expectsMediaDataInRealTime = true
        guard w.canAdd(input) else {
            log.error("clip writer cannot add video input width=\(width) height=\(height)")
            throw ClipRecorderError.cannotAddVideoInput
        }
        w.add(input)
        writer = w
        videoInput = input
        sessionStarted = false
        droppedFrameCount = 0
        firstSamplePTS = nil
        log.info("clip writer prepared width=\(width) height=\(height)")
    }

    /// Append one frame. The first appended sample's PTS becomes the writer
    /// session's `atSourceTime`, so downstream decoders see timestamps on the
    /// same session clock as the JSON payload's `timestamp_s`.
    func append(sampleBuffer: CMSampleBuffer) {
        guard let writer, let videoInput else { return }

        if !sessionStarted {
            guard writer.startWriting() else {
                log.error("clip writer startWriting failed status=\(writer.status.rawValue) error=\(writer.error?.localizedDescription ?? "nil", privacy: .public)")
                return
            }
            let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            writer.startSession(atSourceTime: pts)
            firstSamplePTS = pts
            sessionStarted = true
        }

        if videoInput.isReadyForMoreMediaData {
            _ = videoInput.append(sampleBuffer)
        } else {
            droppedFrameCount += 1
            // Log dropped frames at cadence boundaries only — per-frame
            // would flood the subsystem at 240 fps. `.debug` stays off in
            // release builds by default.
            if droppedFrameCount == 1 || droppedFrameCount % 60 == 0 {
                log.debug("clip writer dropped frames count=\(self.droppedFrameCount)")
            }
        }
    }

    /// Finalise and flush. `completion` fires on AVAssetWriter's internal
    /// queue with the clip URL on success, nil on failure (no session, writer
    /// error). The recorder resets its state so a fresh `prepare` is needed
    /// for the next cycle.
    func finish(completion: @escaping (URL?) -> Void) {
        guard let writer, let videoInput else {
            completion(nil)
            return
        }
        guard sessionStarted else {
            // Nothing was appended; tear down without producing a file.
            log.info("clip writer finish no-op (session never started)")
            videoInput.markAsFinished()
            writer.cancelWriting()
            self.writer = nil
            self.videoInput = nil
            try? FileManager.default.removeItem(at: outputURL)
            completion(nil)
            return
        }
        videoInput.markAsFinished()
        let capturedURL = outputURL
        let dropped = droppedFrameCount
        writer.finishWriting { [weak self] in
            let status = writer.status
            self?.writer = nil
            self?.videoInput = nil
            self?.sessionStarted = false
            if status == .completed {
                log.info("clip writer finished url=\(capturedURL.lastPathComponent, privacy: .public) dropped=\(dropped)")
                completion(capturedURL)
            } else {
                log.error("clip writer finish failed status=\(status.rawValue) error=\(writer.error?.localizedDescription ?? "nil", privacy: .public)")
                try? FileManager.default.removeItem(at: capturedURL)
                completion(nil)
            }
        }
    }

    /// Abort the clip without producing a file. Safe to call from any state.
    func cancel() {
        if writer != nil {
            log.info("clip writer cancel session_started=\(self.sessionStarted) dropped=\(self.droppedFrameCount)")
        }
        if let writer, sessionStarted {
            videoInput?.markAsFinished()
            writer.cancelWriting()
        }
        writer = nil
        videoInput = nil
        sessionStarted = false
        try? FileManager.default.removeItem(at: outputURL)
    }
}
