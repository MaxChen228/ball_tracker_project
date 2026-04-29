import XCTest
import AVFoundation
import CoreMedia
import CoreVideo
@testable import ball_tracker

/// XCTest suite for `ClipRecorder` — the per-cycle `AVAssetWriter` wrapper
/// that turns capture-queue `CMSampleBuffer`s into an H.264 .mov on disk.
/// We construct synthetic sample buffers from `CVPixelBuffer` so the suite
/// runs on the simulator (and in CI) without needing the real camera.
///
/// Threading note: production code drives `prepare`/`append`/`finish`/
/// `cancel` from `processingQueue`. The XCTest dispatcher is single-
/// threaded per test, which mirrors that serial-access invariant —
/// every call here is from the test's main thread.
final class ClipRecorderStateTests: XCTestCase {

    // MARK: 1. prepare() resets state and removes any stale output file.

    func testPrepareRemovesStaleOutputAndZerosState() throws {
        let outputURL = Self.makeTempOutputURL()
        // Pre-seed a stale file at outputURL. prepare() must remove it.
        try Data("stale".utf8).write(to: outputURL)
        XCTAssertTrue(FileManager.default.fileExists(atPath: outputURL.path))

        let recorder = ClipRecorder(outputURL: outputURL)
        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)

        // Stale file is gone; AVAssetWriter only finalises the URL on
        // finishWriting, so post-prepare the file should NOT exist.
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputURL.path),
                       "prepare() must remove any pre-existing file at outputURL")
        XCTAssertNil(recorder.firstSamplePTS,
                     "firstSamplePTS must be nil before any append")
        XCTAssertEqual(recorder.droppedFrameCount, 0,
                       "droppedFrameCount must be zero after prepare")
    }

    // MARK: 2. cancel() before any append leaves no file behind.
    //
    // (Note: `finish()` before any append is currently load-bearing inside
    // CameraViewController's force-finish path, but it triggers
    // `markAsFinished` on a writer whose status is still `.unknown(0)` —
    // AVAssetWriter throws an NSException in that case, which we cannot
    // catch from Swift. The recommended cleanup path for the
    // never-appended branch is `cancel()`. A future ClipRecorder
    // refactor (unit 2 worker) is expected to consolidate this so the
    // suite can exercise both paths uniformly.)

    func testCancelBeforeAnyAppendLeavesNoFile() throws {
        let outputURL = Self.makeTempOutputURL()
        let recorder = ClipRecorder(outputURL: outputURL)
        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)
        recorder.cancel()
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputURL.path),
                       "cancel() before append must not produce a clip file")
        XCTAssertNil(recorder.firstSamplePTS,
                     "Cancel without append leaves firstSamplePTS nil")
    }

    // MARK: 3. cancel() removes the output file and subsequent appends are no-ops.

    func testCancelTearsDownAndAppendBecomesNoop() throws {
        let outputURL = Self.makeTempOutputURL()
        let recorder = ClipRecorder(outputURL: outputURL)
        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)

        recorder.cancel()
        XCTAssertFalse(FileManager.default.fileExists(atPath: outputURL.path),
                       "cancel() must remove the output file if any was created")

        // After cancel, append should be a silent no-op (writer/videoInput
        // are nil) — test by appending a synthetic sample and verifying
        // state remains at zero.
        let sample = try Self.makeSampleBuffer(ptsSeconds: 0.0)
        recorder.append(sampleBuffer: sample)
        XCTAssertEqual(recorder.droppedFrameCount, 0,
                       "append after cancel must NOT touch droppedFrameCount")
        XCTAssertNil(recorder.firstSamplePTS,
                     "append after cancel must NOT set firstSamplePTS")
    }

    // MARK: 4. prepare → append → finish produces a non-nil URL and file on disk.

    func testHappyPathProducesFileAndURL() throws {
        let outputURL = Self.makeTempOutputURL()
        let recorder = ClipRecorder(outputURL: outputURL)
        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)

        // Append one synthetic frame at PTS 0.5 s (within the writer's
        // accepted PTS space).
        let sample = try Self.makeSampleBuffer(ptsSeconds: 0.5)
        recorder.append(sampleBuffer: sample)
        XCTAssertNotNil(recorder.firstSamplePTS,
                        "First append must record firstSamplePTS")
        if let firstPTS = recorder.firstSamplePTS {
            XCTAssertEqual(firstPTS.seconds, 0.5, accuracy: 1e-3)
        }

        let exp = expectation(description: "finish completes")
        var receivedURL: URL?
        recorder.finish { url in
            receivedURL = url
            exp.fulfill()
        }
        wait(for: [exp], timeout: 5.0)

        XCTAssertNotNil(receivedURL,
                        "finish() after at least one append must produce a URL")
        XCTAssertTrue(FileManager.default.fileExists(atPath: outputURL.path),
                      "Output file must exist on disk after a successful finish")
        // Cleanup so a re-run of the suite starts clean.
        try? FileManager.default.removeItem(at: outputURL)
    }

    // MARK: 5. Calling prepare() twice resets firstSamplePTS / droppedFrameCount.

    func testPrepareTwiceResetsState() throws {
        let outputURL = Self.makeTempOutputURL()
        let recorder = ClipRecorder(outputURL: outputURL)

        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)
        let sample = try Self.makeSampleBuffer(ptsSeconds: 1.0)
        recorder.append(sampleBuffer: sample)
        XCTAssertNotNil(recorder.firstSamplePTS)

        // A fresh prepare() — meant for the next cycle — must wipe state
        // back to zero so the previous cycle's PTS does not bleed into the
        // new clip.
        try recorder.prepare(width: 1920, height: 1080, expectedFps: 240)
        XCTAssertNil(recorder.firstSamplePTS,
                     "prepare() on a used recorder must reset firstSamplePTS")
        XCTAssertEqual(recorder.droppedFrameCount, 0,
                       "prepare() on a used recorder must reset droppedFrameCount")
    }

    // MARK: - Helpers

    private static func makeTempOutputURL() -> URL {
        return FileManager.default.temporaryDirectory
            .appendingPathComponent("clip_test_\(UUID().uuidString).mov")
    }

    /// Build a minimal valid `CMSampleBuffer` from a synthetic 16x16 BGRA
    /// pixel buffer. PTS is encoded with a 1/600 timescale so half-second
    /// values land cleanly. Width/height are tiny so the suite is fast on
    /// CI; the writer is configured for 1920x1080 but accepts smaller
    /// frames (it scales / pads at compress time).
    static func makeSampleBuffer(ptsSeconds: Double) throws -> CMSampleBuffer {
        let width = 16
        let height = 16
        var pixelBuffer: CVPixelBuffer?
        let attrs: [CFString: Any] = [
            kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary,
        ]
        let status = CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32BGRA,
            attrs as CFDictionary,
            &pixelBuffer
        )
        guard status == kCVReturnSuccess, let pb = pixelBuffer else {
            throw NSError(domain: "ClipRecorderStateTests", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "CVPixelBufferCreate failed"])
        }

        // Zero-fill so the buffer has a defined value (BGRA = all 0).
        CVPixelBufferLockBaseAddress(pb, [])
        defer { CVPixelBufferUnlockBaseAddress(pb, []) }
        if let base = CVPixelBufferGetBaseAddress(pb) {
            let bytesPerRow = CVPixelBufferGetBytesPerRow(pb)
            memset(base, 0, bytesPerRow * height)
        }

        var formatDesc: CMVideoFormatDescription?
        let fdStatus = CMVideoFormatDescriptionCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pb,
            formatDescriptionOut: &formatDesc
        )
        guard fdStatus == noErr, let fd = formatDesc else {
            throw NSError(domain: "ClipRecorderStateTests", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "format desc failed"])
        }

        let pts = CMTime(value: CMTimeValue(ptsSeconds * 600.0), timescale: 600)
        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: 240),
            presentationTimeStamp: pts,
            decodeTimeStamp: .invalid
        )
        var sampleBuffer: CMSampleBuffer?
        let sbStatus = CMSampleBufferCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pb,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: fd,
            sampleTiming: &timing,
            sampleBufferOut: &sampleBuffer
        )
        guard sbStatus == noErr, let sb = sampleBuffer else {
            throw NSError(domain: "ClipRecorderStateTests", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "sample buffer create failed"])
        }
        return sb
    }
}
