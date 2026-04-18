import XCTest
@testable import ball_tracker

/// XCTest suite for `PitchRecorder` — the pre-roll + cycle-assembly state
/// machine. Exercises the documented thresholds:
///   preRollMaxFrames = 120
///   minFramesAfterStart = 24
///   endWhenNoBallFrames = 24
///   maxCycleDurationS = 5.0
///
/// All timestamps are synthetic — driven purely by `FramePayload.timestamp_s`
/// values we pass in. Nothing here calls `Date()` or sleeps on a queue.
final class PitchRecorderTests: XCTestCase {

    // MARK: 1. Pre-roll buffer cap

    func testPreRollBufferCapsAt120Frames() {
        let recorder = PitchRecorder()

        // Feed 150 ball-absent frames — pre-roll must hold only the most
        // recent 120 of them.
        for i in 0..<150 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: false))
        }

        // Kick the recorder into a recording with no NEW frame, so the
        // emitted payload frames == the retained pre-roll contents.
        var emitted: ServerUploader.PitchPayload?
        recorder.onCycleComplete = { emitted = $0 }
        recorder.startRecording(
            sessionId: "s_test01",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 150,
            startTimestampS: 150.0 / 240.0
        )
        recorder.forceFinishIfRecording()

        XCTAssertNotNil(emitted)
        // All pre-roll frames were ball-absent → startRecording copies them
        // into cycleFrames; nothing else was appended before forceFinish.
        XCTAssertEqual(emitted?.frames.count, 120, "Pre-roll buffer must be capped to 120 frames")
        // First retained frame should be index 30 (we fed 0..<150; keeps last 120).
        XCTAssertEqual(emitted?.frames.first?.frame_index, 30)
        XCTAssertEqual(emitted?.frames.last?.frame_index, 149)
    }

    // MARK: 2. startRecording includes pre-roll frames

    func testStartRecordingIncludesPreRollInEmittedPayload() {
        let recorder = PitchRecorder()

        // 50 pre-roll frames (all ball-absent, indices 0..49).
        for i in 0..<50 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: false))
        }

        var emitted: ServerUploader.PitchPayload?
        recorder.onCycleComplete = { emitted = $0 }

        // Start recording at frame 50.
        recorder.startRecording(
            sessionId: "s_test02",
            anchorFrameIndex: 10,
            anchorTimestampS: 10.0 / 240.0,
            startFrameIndex: 50,
            startTimestampS: 50.0 / 240.0
        )

        // One ball-detected frame at index 50 sets hasSeenBallSinceStart.
        recorder.handleFrame(makeFrame(index: 50, timestamp: 50.0 / 240.0, ballDetected: true))

        // 24 ball-absent frames at 51..74. The 24th (index 74) satisfies
        // noBallStreak >= 24 AND capturedFrames = 74-50+1 = 25 >= 24.
        for i in 51...74 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: false))
        }

        XCTAssertNotNil(emitted, "naturalEnd should have fired finishCycle")
        // 50 pre-roll + 1 ball-detected + 24 ball-absent = 75 frames.
        XCTAssertEqual(emitted?.frames.count, 75)
        // Pre-roll frames are at the head.
        XCTAssertEqual(emitted?.frames.first?.frame_index, 0)
        XCTAssertEqual(emitted?.frames[49].frame_index, 49)
        // Ball-detected frame follows.
        XCTAssertEqual(emitted?.frames[50].frame_index, 50)
        XCTAssertEqual(emitted?.frames[50].ball_detected, true)
        // Tail is the 24-frame absence streak.
        XCTAssertEqual(emitted?.frames.last?.frame_index, 74)
        XCTAssertEqual(emitted?.frames.last?.ball_detected, false)
        // Session / anchor passthroughs.
        XCTAssertEqual(emitted?.session_id, "s_test02")
        XCTAssertEqual(emitted?.sync_anchor_frame_index, 10)
    }

    // MARK: 3. naturalEnd triggers finishCycle exactly once

    func testNaturalEndTriggersFinishCycleOnceAndClearsCycleFrames() {
        let recorder = PitchRecorder()
        var fireCount = 0
        var capturedCount = -1
        recorder.onCycleComplete = { payload in
            fireCount += 1
            capturedCount = payload.frames.count
        }

        recorder.startRecording(
            sessionId: "s_test03",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 0,
            startTimestampS: 0.0
        )

        // 30 ball-detected frames (indices 0..29).
        for i in 0..<30 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: true))
        }
        // 24 ball-absent frames (indices 30..53). The 24th absent frame
        // (index 53) satisfies the naturalEnd predicate.
        for i in 30..<54 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: false))
        }

        XCTAssertEqual(fireCount, 1, "onCycleComplete must fire exactly once on naturalEnd")
        // 30 + 24 = 54 frames emitted (no pre-roll populated in this test).
        XCTAssertEqual(capturedCount, 54)

        // Further frames after finish should NOT refire the callback —
        // isRecording is false, so they just land in pre-roll.
        for i in 54..<60 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: false))
        }
        XCTAssertEqual(fireCount, 1, "Callback must not refire after finishCycle")
    }

    // MARK: 4. hardCap triggers finishCycle even if ball still present

    func testHardCapTriggersFinishCycleAtMaxCycleDuration() {
        let recorder = PitchRecorder()
        var fireCount = 0
        var capturedDuration: Double = 0.0
        recorder.onCycleComplete = { payload in
            fireCount += 1
            // Duration of the last recorded frame relative to pitchStart.
            if let last = payload.frames.last {
                capturedDuration = last.timestamp_s
            }
        }

        recorder.startRecording(
            sessionId: "s_test04",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 0,
            startTimestampS: 0.0
        )

        // Feed up to 240 ball-detected frames at 240 fps, spanning ~1.0s
        // per 240 frames. We want to hit 5.0s before running out. Use a
        // slightly slower rate so duration crosses 5.0s around frame 240.
        // Timestamps: t_i = i * (5.1 / 239) so frame 239 sits at 5.1s.
        let maxIdx = 239
        let dt = 5.1 / Double(maxIdx)
        var fireIndex = -1
        for i in 0...maxIdx {
            let t = Double(i) * dt
            recorder.handleFrame(makeFrame(index: i, timestamp: t, ballDetected: true))
            if fireCount == 1 && fireIndex == -1 {
                fireIndex = i
            }
        }

        XCTAssertEqual(fireCount, 1, "hardCap should fire onCycleComplete even with ball present")
        // Must have fired on or before the frame whose timestamp first
        // reaches 5.0s.
        XCTAssertGreaterThanOrEqual(capturedDuration, 5.0, "hardCap fires once duration >= 5.0s")
        // Should not have needed the entire 240-frame stream; the recorder
        // stops ingesting past finishCycle.
        XCTAssertLessThan(fireIndex, maxIdx)
    }

    // MARK: 5. forceFinishIfRecording while recording emits payload

    func testForceFinishWhileRecordingEmitsCycle() {
        let recorder = PitchRecorder()
        var fireCount = 0
        recorder.onCycleComplete = { _ in fireCount += 1 }

        recorder.startRecording(
            sessionId: "s_test05",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 0,
            startTimestampS: 0.0
        )
        // A handful of frames — not enough to naturally end.
        for i in 0..<5 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: true))
        }

        recorder.forceFinishIfRecording()
        XCTAssertEqual(fireCount, 1)

        // Calling it again after finish must be a no-op.
        recorder.forceFinishIfRecording()
        XCTAssertEqual(fireCount, 1)
    }

    // MARK: 6. forceFinishIfRecording while NOT recording is a no-op

    func testForceFinishWhileNotRecordingDoesNotFire() {
        let recorder = PitchRecorder()
        var fireCount = 0
        recorder.onCycleComplete = { _ in fireCount += 1 }

        // No startRecording call.
        recorder.forceFinishIfRecording()
        XCTAssertEqual(fireCount, 0)
    }

    // MARK: 7. reset clears state but NOT localRecordingIndex

    func testResetClearsStateButLocalRecordingIndexSurvives() {
        let recorder = PitchRecorder()

        // Capture localRecordingIndex emitted by onRecordingStarted.
        var startedIndices: [Int] = []
        recorder.onRecordingStarted = { idx in startedIndices.append(idx) }
        var emitted: ServerUploader.PitchPayload?
        recorder.onCycleComplete = { emitted = $0 }

        // Populate pre-roll + a live recording.
        recorder.startRecording(
            sessionId: "s_test07a",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 0,
            startTimestampS: 0.0
        )
        for i in 0..<50 {
            recorder.handleFrame(makeFrame(index: i, timestamp: Double(i) / 240.0, ballDetected: true))
        }

        XCTAssertEqual(startedIndices, [1], "First startRecording yields localRecordingIndex == 1")

        recorder.reset()

        // Trigger another recording — there is no way to directly observe
        // preRollBuffer / isRecording, but if reset() cleared them
        // correctly, the next payload's `frames` will not include stale
        // pre-roll carryover from the first session, and startRecording
        // will succeed (isRecording was cleared).
        recorder.startRecording(
            sessionId: "s_test07b",
            anchorFrameIndex: 0,
            anchorTimestampS: 0.0,
            startFrameIndex: 1000,
            startTimestampS: 1000.0 / 240.0
        )
        recorder.forceFinishIfRecording()

        XCTAssertNotNil(emitted)
        // Pre-roll cleared by reset → no carryover frames in the second cycle.
        XCTAssertEqual(emitted?.frames.count, 0, "reset() must clear preRollBuffer")
        // localRecordingIndex survived reset → second start emits index 2.
        XCTAssertEqual(startedIndices, [1, 2],
                       "localRecordingIndex must NOT be reset by reset()")
        // Session id reflects the second startRecording.
        XCTAssertEqual(emitted?.session_id, "s_test07b")
        // local_recording_index piggy-backs onto the payload too.
        XCTAssertEqual(emitted?.local_recording_index, 2)
    }
}

// MARK: - Helpers

private extension PitchRecorderTests {
    /// Minimal FramePayload stub; irrelevant fields stay nil.
    func makeFrame(
        index: Int,
        timestamp: Double,
        ballDetected: Bool
    ) -> ServerUploader.FramePayload {
        return ServerUploader.FramePayload(
            frame_index: index,
            timestamp_s: timestamp,
            theta_x_rad: nil,
            theta_z_rad: nil,
            px: nil,
            py: nil,
            ball_detected: ballDetected
        )
    }
}
