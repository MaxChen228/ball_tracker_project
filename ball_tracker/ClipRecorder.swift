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
/// Threading: the caller serialises `prepare`/`append`/`finish`/`cancel` onto
/// `CameraViewController.processingQueue`. However, `finish` hands its
/// completion to AVAssetWriter's internal queue, so a `cancel` arriving on
/// the processing queue can race with an in-flight `finishWriting`. The
/// explicit `lifecycle` state machine below guards that crossing — once
/// `finish` flips us to `.finishing`, any subsequent `cancel` is a no-op
/// and absolutely will not delete the just-finalised file.
final class ClipRecorder {
    enum ClipRecorderError: Error {
        case cannotAddVideoInput
        case notStarted
    }

    /// Lifecycle phases tracked under `stateLock`.
    /// - `idle`: `prepare` succeeded but no frame appended yet.
    /// - `writing`: at least one sample has started the writer session.
    /// - `finishing`: `finish` is in flight; `finishWriting` callback pending.
    /// - `finished`: terminal success path; file may exist at `outputURL`.
    /// - `cancelled`: terminal cancel path; file deleted.
    private enum State {
        case idle
        case writing
        case finishing
        case finished
        case cancelled
    }

    private let outputURL: URL
    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private(set) var droppedFrameCount: Int = 0
    /// Session-clock PTS of the first sample appended. Uploader pairs this
    /// with the JSON payload so the server can reconstruct absolute PTS for
    /// each decoded frame.
    private(set) var firstSamplePTS: CMTime?

    private var lifecycle: State = .idle
    private let stateLock = NSLock()

    init(outputURL: URL) {
        self.outputURL = outputURL
    }

    private func withLock<T>(_ body: () -> T) -> T {
        stateLock.lock()
        defer { stateLock.unlock() }
        return body()
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
        // Lock so a racing cancel/finish on AVAssetWriter's queue sees the
        // fresh lifecycle immediately.
        withLock {
            writer = w
            videoInput = input
            droppedFrameCount = 0
            firstSamplePTS = nil
            lifecycle = .idle
        }
        log.info("clip writer prepared width=\(width) height=\(height)")
    }

    /// Append one frame. The first appended sample's PTS becomes the writer
    /// session's `atSourceTime`, so downstream decoders see timestamps on the
    /// same session clock as the JSON payload's `timestamp_s`.
    func append(sampleBuffer: CMSampleBuffer) {
        // Snapshot under lock; we only proceed if we still hold a writer and
        // are in a state that accepts samples.
        let snapshot: (writer: AVAssetWriter, input: AVAssetWriterInput, started: Bool)? = withLock {
            guard let writer, let videoInput else { return nil }
            switch lifecycle {
            case .idle:
                return (writer, videoInput, false)
            case .writing:
                return (writer, videoInput, true)
            case .finishing, .finished, .cancelled:
                return nil
            }
        }
        guard let (writer, videoInput, sessionStarted) = snapshot else { return }

        if !sessionStarted {
            guard writer.startWriting() else {
                log.error("clip writer startWriting failed status=\(writer.status.rawValue) error=\(writer.error?.localizedDescription ?? "nil", privacy: .public)")
                return
            }
            let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            writer.startSession(atSourceTime: pts)
            withLock {
                firstSamplePTS = pts
                lifecycle = .writing
            }
        }

        if videoInput.isReadyForMoreMediaData {
            _ = videoInput.append(sampleBuffer)
        } else {
            let dropped: Int = withLock {
                droppedFrameCount += 1
                return droppedFrameCount
            }
            // Log dropped frames at cadence boundaries only — per-frame
            // would flood the subsystem at 240 fps. `.debug` stays off in
            // release builds by default.
            if dropped == 1 || dropped % 60 == 0 {
                log.debug("clip writer dropped frames count=\(dropped)")
            }
        }
    }

    /// Finalise and flush. `completion` fires on AVAssetWriter's internal
    /// queue with the clip URL on success, nil on failure (no session, writer
    /// error). The recorder transitions to `.finishing` (then `.finished`)
    /// so any racing `cancel()` becomes a no-op and cannot delete the
    /// produced file.
    func finish(completion: @escaping (URL?) -> Void) {
        // Decide the next move atomically. We may be:
        //  - .idle: prepared but no frames; tear down and return nil (no
        //    file produced). Mark .finished so a racing cancel is a no-op.
        //  - .writing: real flush via finishWriting.
        //  - .finishing/.finished/.cancelled: already terminal — nil.
        enum Decision {
            case noOpReturnNil                                       // already terminal
            case tearDownEmpty(AVAssetWriter, AVAssetWriterInput)    // .idle path
            case finalize(AVAssetWriter, AVAssetWriterInput, Int)    // .writing path
        }
        let decision: Decision = withLock {
            guard let w = writer, let input = videoInput else { return .noOpReturnNil }
            switch lifecycle {
            case .idle:
                lifecycle = .finishing
                return .tearDownEmpty(w, input)
            case .writing:
                lifecycle = .finishing
                return .finalize(w, input, droppedFrameCount)
            case .finishing, .finished, .cancelled:
                return .noOpReturnNil
            }
        }

        switch decision {
        case .noOpReturnNil:
            completion(nil)
        case .tearDownEmpty(let w, let input):
            log.info("clip writer finish no-op (session never started)")
            input.markAsFinished()
            w.cancelWriting()
            withLock {
                writer = nil
                videoInput = nil
                lifecycle = .finished
            }
            try? FileManager.default.removeItem(at: outputURL)
            completion(nil)
        case .finalize(let w, let input, let dropped):
            input.markAsFinished()
            let capturedURL = outputURL
            w.finishWriting { [weak self] in
                let status = w.status
                if let self {
                    self.withLock {
                        self.writer = nil
                        self.videoInput = nil
                        self.lifecycle = .finished
                    }
                }
                if status == .completed {
                    log.info("clip writer finished url=\(capturedURL.lastPathComponent, privacy: .public) dropped=\(dropped)")
                    completion(capturedURL)
                } else {
                    log.error("clip writer finish failed status=\(status.rawValue) error=\(w.error?.localizedDescription ?? "nil", privacy: .public)")
                    try? FileManager.default.removeItem(at: capturedURL)
                    completion(nil)
                }
            }
        }
    }

    /// Abort the clip without producing a file. Safe to call from any state,
    /// but if `finish` is already in flight (or has completed) this is a
    /// no-op — we will NEVER delete a file `finish` is trying to produce or
    /// has just produced.
    func cancel() {
        enum Action {
            case noOp(State)                                              // already terminal/finishing
            case abort(AVAssetWriter?, AVAssetWriterInput?, Bool, Int)    // .idle / .writing
        }
        let action: Action = withLock {
            switch lifecycle {
            case .finishing, .finished:
                return .noOp(lifecycle)
            case .cancelled:
                return .noOp(.cancelled)
            case .idle, .writing:
                let started = (lifecycle == .writing)
                let w = writer
                let input = videoInput
                lifecycle = .cancelled
                writer = nil
                videoInput = nil
                return .abort(w, input, started, droppedFrameCount)
            }
        }

        switch action {
        case .noOp(let state):
            // Important: don't touch the output file — finish() owns it.
            log.warning("clip writer cancel ignored (lifecycle=\(String(describing: state), privacy: .public))")
        case .abort(let w, let input, let started, let dropped):
            if w != nil {
                log.info("clip writer cancel session_started=\(started) dropped=\(dropped)")
            }
            if let w, started {
                input?.markAsFinished()
                w.cancelWriting()
            }
            try? FileManager.default.removeItem(at: outputURL)
        }
    }
}
