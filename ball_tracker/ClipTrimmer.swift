import AVFoundation
import CoreMedia
import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Post-recording MOV trim pipeline. Reads encoded H.264 samples from the
/// original clip via `AVAssetReader`, rewrites their PTS/DTS so the first
/// surviving sample sits at 0, and writes them back through `AVAssetWriter`
/// in passthrough mode (no re-encode — just a re-wrap of the elementary
/// stream). End-to-end cost is a few hundred ms for a 10 s clip, regardless
/// of resolution.
///
/// Why not `AVAssetExportSession(preset: passthrough)`?
///   - Export snaps `timeRange.start` to the nearest sync sample (keyframe)
///     internally, but does NOT tell the caller where the output actually
///     starts. We need that offset exactly to stamp a correct
///     `video_start_pts_s` on the uploaded payload — otherwise pair-matching
///     drifts by the GOP distance.
///
/// Reader + writer lets us observe the first output sample's PTS directly
/// and report it back to the caller. Passthrough is preserved because both
/// `outputSettings` are nil.
enum ClipTrimmer {
    struct Output {
        /// New MOV location (owned by the caller; trimmer does not delete it
        /// on success).
        let url: URL
        /// Absolute session-clock PTS of the new MOV's first frame. Must be
        /// written into `PitchPayload.video_start_pts_s` — this is the whole
        /// point of the hand-carved reader/writer pipeline.
        let absoluteStartPtsS: Double
        /// Duration of the trimmed clip in seconds (walltime of the wrapped
        /// samples, not the CMTimeRange we requested).
        let durationS: Double
    }

    enum TrimError: Error {
        case noVideoTrack
        case noFormatDescription
        case readerInitFailed(Error)
        case writerInitFailed(Error)
        case readerRejected
        case writerRejected
        case noSamplesInRange
    }

    private static let trimQueue = DispatchQueue(
        label: "camera.cliptrim.queue", qos: .utility
    )

    /// Trim `source` to `[startOffsetFromMovStartS, +durationS)` and write
    /// the result to `destination`. `originalVideoStartPtsS` is the caller's
    /// `video_start_pts_s` for the untrimmed MOV — needed to compute the
    /// trimmed clip's new absolute start.
    ///
    /// Completion fires on `trimQueue`; caller must dispatch to main for
    /// any UI work. Passes `nil` on any failure — caller should fall back
    /// to uploading the untrimmed clip rather than losing the cycle.
    static func trim(
        source: URL,
        startOffsetFromMovStartS: Double,
        durationS: Double,
        destination: URL,
        originalVideoStartPtsS: Double,
        completion: @escaping (Output?) -> Void
    ) {
        trimQueue.async {
            runTrim(
                source: source,
                startOffsetFromMovStartS: startOffsetFromMovStartS,
                durationS: durationS,
                destination: destination,
                originalVideoStartPtsS: originalVideoStartPtsS,
                completion: completion
            )
        }
    }

    private static func runTrim(
        source: URL,
        startOffsetFromMovStartS: Double,
        durationS: Double,
        destination: URL,
        originalVideoStartPtsS: Double,
        completion: @escaping (Output?) -> Void
    ) {
        let asset = AVURLAsset(url: source)

        // Synchronous property access — we're on a background queue and the
        // file is local, so the blocking wait here is measured in
        // microseconds. Using the async load API would complicate the
        // completion-handler ergonomics without buying anything.
        let videoTracks = asset.tracks(withMediaType: .video)
        guard let videoTrack = videoTracks.first else {
            log.error("clip trim no video track source=\(source.lastPathComponent, privacy: .public)")
            completion(nil); return
        }
        guard let formatHint = videoTrack.formatDescriptions.first else {
            log.error("clip trim no format description source=\(source.lastPathComponent, privacy: .public)")
            completion(nil); return
        }

        let reader: AVAssetReader
        do {
            reader = try AVAssetReader(asset: asset)
        } catch {
            log.error("clip trim reader init failed error=\(error.localizedDescription, privacy: .public)")
            completion(nil); return
        }
        let startTime = CMTime(seconds: max(startOffsetFromMovStartS, 0), preferredTimescale: 600)
        let duration = CMTime(seconds: max(durationS, 0), preferredTimescale: 600)
        reader.timeRange = CMTimeRange(start: startTime, duration: duration)

        let readerOutput = AVAssetReaderTrackOutput(track: videoTrack, outputSettings: nil)
        readerOutput.alwaysCopiesSampleData = false
        guard reader.canAdd(readerOutput) else {
            log.error("clip trim reader cannot add track output")
            completion(nil); return
        }
        reader.add(readerOutput)

        try? FileManager.default.removeItem(at: destination)
        let writer: AVAssetWriter
        do {
            writer = try AVAssetWriter(outputURL: destination, fileType: .mov)
        } catch {
            log.error("clip trim writer init failed error=\(error.localizedDescription, privacy: .public)")
            completion(nil); return
        }
        // outputSettings: nil + sourceFormatHint = passthrough (re-wrap H.264
        // ES without re-encoding). Required for the no-transcode fast path.
        let writerInput = AVAssetWriterInput(
            mediaType: .video,
            outputSettings: nil,
            sourceFormatHint: formatHint as CMFormatDescription
        )
        writerInput.expectsMediaDataInRealTime = false
        guard writer.canAdd(writerInput) else {
            log.error("clip trim writer cannot add video input")
            completion(nil); return
        }
        writer.add(writerInput)

        guard reader.startReading() else {
            log.error("clip trim reader startReading failed status=\(reader.status.rawValue)")
            completion(nil); return
        }
        guard writer.startWriting() else {
            log.error("clip trim writer startWriting failed status=\(writer.status.rawValue)")
            completion(nil); return
        }

        var firstSamplePts: CMTime?
        var lastSamplePts: CMTime?
        let pumpQueue = trimQueue

        writerInput.requestMediaDataWhenReady(on: pumpQueue) {
            while writerInput.isReadyForMoreMediaData {
                guard reader.status == .reading,
                      let sample = readerOutput.copyNextSampleBuffer() else {
                    // End of input — seal the writer.
                    writerInput.markAsFinished()
                    finalize(
                        writer: writer,
                        destination: destination,
                        firstSamplePts: firstSamplePts,
                        lastSamplePts: lastSamplePts,
                        originalVideoStartPtsS: originalVideoStartPtsS,
                        completion: completion
                    )
                    return
                }

                let originalPts = CMSampleBufferGetPresentationTimeStamp(sample)
                if firstSamplePts == nil {
                    firstSamplePts = originalPts
                    writer.startSession(atSourceTime: .zero)
                }
                lastSamplePts = originalPts

                guard let origin = firstSamplePts,
                      let retimed = retime(sample, origin: origin) else {
                    continue
                }
                if !writerInput.append(retimed) {
                    log.error("clip trim writerInput.append failed status=\(writer.status.rawValue) error=\(writer.error?.localizedDescription ?? "nil", privacy: .public)")
                    writerInput.markAsFinished()
                    writer.cancelWriting()
                    try? FileManager.default.removeItem(at: destination)
                    completion(nil)
                    return
                }
            }
        }
    }

    private static func finalize(
        writer: AVAssetWriter,
        destination: URL,
        firstSamplePts: CMTime?,
        lastSamplePts: CMTime?,
        originalVideoStartPtsS: Double,
        completion: @escaping (Output?) -> Void
    ) {
        guard let firstSamplePts, let lastSamplePts else {
            // The requested [start, start+duration) window didn't contain
            // any samples (e.g. start past the end of the clip). Drop the
            // empty output file and bail.
            writer.cancelWriting()
            try? FileManager.default.removeItem(at: destination)
            log.warning("clip trim no samples in range — falling back to full clip")
            completion(nil)
            return
        }
        writer.finishWriting {
            if writer.status == .completed {
                let firstS = firstSamplePts.seconds
                let lastS = lastSamplePts.seconds
                let absStart = originalVideoStartPtsS + firstS
                let dur = max(lastS - firstS, 0)
                log.info("clip trim complete dur=\(dur) first_mov_pts=\(firstS) abs_start=\(absStart) out=\(destination.lastPathComponent, privacy: .public)")
                completion(Output(url: destination, absoluteStartPtsS: absStart, durationS: dur))
            } else {
                log.error("clip trim finishWriting failed status=\(writer.status.rawValue) error=\(writer.error?.localizedDescription ?? "nil", privacy: .public)")
                try? FileManager.default.removeItem(at: destination)
                completion(nil)
            }
        }
    }

    /// Shift PTS/DTS of `sample` so it sits on a timeline where `origin` is 0.
    /// Returns nil if CoreMedia rejects the rebuild (shouldn't happen for
    /// well-formed samples).
    private static func retime(_ sample: CMSampleBuffer, origin: CMTime) -> CMSampleBuffer? {
        var count: CMItemCount = 0
        var status = CMSampleBufferGetSampleTimingInfoArray(
            sample, entryCount: 0, arrayToFill: nil, entriesNeededOut: &count
        )
        if status != noErr || count == 0 {
            return nil
        }
        var timingInfo = Array(
            repeating: CMSampleTimingInfo(
                duration: .invalid,
                presentationTimeStamp: .invalid,
                decodeTimeStamp: .invalid
            ),
            count: Int(count)
        )
        status = CMSampleBufferGetSampleTimingInfoArray(
            sample, entryCount: count, arrayToFill: &timingInfo, entriesNeededOut: nil
        )
        if status != noErr { return nil }
        for i in 0..<Int(count) {
            if timingInfo[i].presentationTimeStamp.isValid {
                timingInfo[i].presentationTimeStamp = CMTimeSubtract(
                    timingInfo[i].presentationTimeStamp, origin
                )
            }
            if timingInfo[i].decodeTimeStamp.isValid {
                timingInfo[i].decodeTimeStamp = CMTimeSubtract(
                    timingInfo[i].decodeTimeStamp, origin
                )
            }
        }
        var retimed: CMSampleBuffer?
        let createStatus = CMSampleBufferCreateCopyWithNewTiming(
            allocator: kCFAllocatorDefault,
            sampleBuffer: sample,
            sampleTimingEntryCount: count,
            sampleTimingArray: &timingInfo,
            sampleBufferOut: &retimed
        )
        if createStatus != noErr { return nil }
        return retimed
    }
}
