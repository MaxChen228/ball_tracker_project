import XCTest
@testable import ball_tracker

/// XCTest suite for `PitchRecorder` — the thin bookkeeping layer between the
/// camera-controller state machine and the upload queue. The recorder no
/// longer does per-frame work: the phone is a pure capture client, so this
/// suite only exercises the payload shape + single-exit invariant
/// (`forceFinishIfRecording()` is the sole path to `onCycleComplete`).
final class PitchRecorderTests: XCTestCase {

    // MARK: 1. forceFinishIfRecording emits a payload with the right shape

    func testForceFinishEmitsPayloadWithAllPassthroughFields() {
        let recorder = PitchRecorder()
        recorder.setCameraId("B")

        var emitted: ServerUploader.PitchPayload?
        recorder.onCycleComplete = { emitted = $0 }

        var startedIndices: [Int] = []
        recorder.onRecordingStarted = { startedIndices.append($0) }

        recorder.startRecording(
            sessionId: "s_test01",
            syncId: nil,
            anchorTimestampS: 12.345,
            videoStartPtsS: 42.125,
            captureTelemetry: nil
        )
        XCTAssertTrue(recorder.isActive)
        XCTAssertEqual(startedIndices, [1])

        recorder.forceFinishIfRecording()
        XCTAssertFalse(recorder.isActive)

        XCTAssertNotNil(emitted)
        XCTAssertEqual(emitted?.camera_id, "B")
        XCTAssertEqual(emitted?.session_id, "s_test01")
        XCTAssertEqual(emitted?.sync_anchor_timestamp_s, 12.345)
        XCTAssertEqual(emitted?.video_start_pts_s, 42.125)
        XCTAssertEqual(emitted?.local_recording_index, 1)
    }

    // MARK: 2. Anchor-less recording still emits (server flags the session)

    func testAnchorlessRecordingProducesPayloadWithNilAnchor() {
        let recorder = PitchRecorder()
        var emitted: ServerUploader.PitchPayload?
        recorder.onCycleComplete = { emitted = $0 }

        recorder.startRecording(
            sessionId: "s_noanc",
            syncId: nil,
            anchorTimestampS: nil,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        recorder.forceFinishIfRecording()

        XCTAssertNotNil(emitted)
        XCTAssertNil(emitted?.sync_anchor_timestamp_s)
    }

    // MARK: 3. forceFinish while NOT recording is a no-op

    func testForceFinishWhileNotRecordingDoesNotFire() {
        let recorder = PitchRecorder()
        var fireCount = 0
        recorder.onCycleComplete = { _ in fireCount += 1 }

        recorder.forceFinishIfRecording()
        XCTAssertEqual(fireCount, 0)
        XCTAssertFalse(recorder.isActive)
    }

    // MARK: 4. Start then start is idempotent (no double-start)

    func testDoubleStartIsIgnored() {
        let recorder = PitchRecorder()
        var startedIndices: [Int] = []
        recorder.onRecordingStarted = { startedIndices.append($0) }

        recorder.startRecording(
            sessionId: "s_once",
            syncId: nil,
            anchorTimestampS: 1.0,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        recorder.startRecording(
            sessionId: "s_twice",
            syncId: nil,
            anchorTimestampS: 2.0,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        XCTAssertEqual(startedIndices, [1], "Second startRecording while active must be ignored")
    }

    // MARK: 5. forceFinish twice is a no-op second time

    func testForceFinishTwiceOnlyFiresOnce() {
        let recorder = PitchRecorder()
        var fireCount = 0
        recorder.onCycleComplete = { _ in fireCount += 1 }

        recorder.startRecording(
            sessionId: "s_dbl",
            syncId: nil,
            anchorTimestampS: 0.0,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        recorder.forceFinishIfRecording()
        recorder.forceFinishIfRecording()
        XCTAssertEqual(fireCount, 1)
    }

    // MARK: 6. localRecordingIndex survives reset()

    func testResetPreservesLocalRecordingIndex() {
        let recorder = PitchRecorder()
        var startedIndices: [Int] = []
        recorder.onRecordingStarted = { startedIndices.append($0) }

        recorder.startRecording(
            sessionId: "s_a",
            syncId: nil,
            anchorTimestampS: 0.0,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        recorder.forceFinishIfRecording()
        XCTAssertEqual(startedIndices, [1])

        recorder.reset()

        recorder.startRecording(
            sessionId: "s_b",
            syncId: nil,
            anchorTimestampS: 0.0,
            videoStartPtsS: 0.0,
            captureTelemetry: nil
        )
        XCTAssertEqual(startedIndices, [1, 2], "localRecordingIndex must NOT reset")
    }
}
