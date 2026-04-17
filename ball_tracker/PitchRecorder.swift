import Foundation

/// Realtime per-recording buffering:
/// - Maintain a pre-roll circular buffer (120 frames @ 240 fps ≈ 0.5 s)
/// - Start one recording when the ball first appears
/// - Tag the recording with the server-minted session id + sync anchor
/// - Keep ingesting frames until the ball has been absent for a while,
///   OR the hard max-duration cap is hit (handles "ball stopped in frame"),
///   OR the caller force-finishes (dashboard sent `disarm` mid-recording).
///
/// NOTE: this recorder does NOT mint any pairing identifier. Server
/// `POST /sessions/arm` allocates `session_id`; CameraViewController reads
/// it off the heartbeat and passes it in at `startRecording`. The local
/// recording counter (`localRecordingIndex`) is strictly for operator
/// debug logs.
final class PitchRecorder {
    private let preRollMaxFrames: Int = 120
    private let minFramesAfterStart: Int = 24
    private let endWhenNoBallFrames: Int = 24
    /// Hard cap from first recorded frame to emergency recording end.
    /// Covers the edge case where the ball lands in a spot still visible
    /// to the camera — `noBallStreakFrames` would never reach the
    /// threshold, so a timer keeps the state machine from stalling.
    private let maxCycleDurationS: Double = 5.0

    private var preRollBuffer: [ServerUploader.FramePayload] = []
    private var isRecording: Bool = false
    private var hasSeenBallSinceStart: Bool = false
    private var noBallStreakFrames: Int = 0
    private var pitchStartFrameIndex: Int = 0
    private var pitchStartTimestampS: Double = 0.0
    /// Device-local debug counter. Not a pairing key. Survives across
    /// recordings within a single app lifetime; not persisted.
    private var localRecordingIndex: Int = 0

    private var syncAnchorFrameIndex: Int = 0
    private var syncAnchorTimestampS: Double = 0.0
    private var sessionId: String = ""
    private var cameraId: String = "A"

    private var cycleFrames: [ServerUploader.FramePayload] = []

    var onCycleComplete: ((ServerUploader.PitchPayload) -> Void)?
    var onRecordingStarted: ((Int) -> Void)?

    func setCameraId(_ id: String) {
        cameraId = id
    }

    func reset() {
        preRollBuffer.removeAll(keepingCapacity: true)
        isRecording = false
        hasSeenBallSinceStart = false
        noBallStreakFrames = 0
        pitchStartFrameIndex = 0
        pitchStartTimestampS = 0.0
        sessionId = ""
        cycleFrames.removeAll(keepingCapacity: true)
        // localRecordingIndex intentionally NOT reset — it's a run-of-
        // app counter used only for debug logs.
    }

    /// Called when the ball first appears inside an armed session.
    /// `sessionId` MUST be the server-minted value for the currently
    /// armed session (from the most recent heartbeat response or the
    /// `/sessions/arm` reply).
    func startRecording(
        sessionId: String,
        anchorFrameIndex: Int,
        anchorTimestampS: Double,
        startFrameIndex: Int,
        startTimestampS: Double
    ) {
        guard !isRecording else { return }

        self.sessionId = sessionId
        self.syncAnchorFrameIndex = anchorFrameIndex
        self.syncAnchorTimestampS = anchorTimestampS
        self.pitchStartFrameIndex = startFrameIndex
        self.pitchStartTimestampS = startTimestampS

        isRecording = true
        hasSeenBallSinceStart = false
        noBallStreakFrames = 0

        // Include pre-trigger data in payload.
        cycleFrames = preRollBuffer
        localRecordingIndex += 1
        onRecordingStarted?(localRecordingIndex)
    }

    /// Ingest one frame. `FramePayload` includes `ball_detected` and
    /// optional angles / pixel coords.
    func handleFrame(_ frame: ServerUploader.FramePayload) {
        if isRecording {
            cycleFrames.append(frame)
            if frame.ball_detected {
                hasSeenBallSinceStart = true
                noBallStreakFrames = 0
            } else if hasSeenBallSinceStart {
                noBallStreakFrames += 1
            }

            let capturedFrames = frame.frame_index - pitchStartFrameIndex + 1
            let capturedDurationS = frame.timestamp_s - pitchStartTimestampS
            let naturalEnd = hasSeenBallSinceStart
                && noBallStreakFrames >= endWhenNoBallFrames
                && capturedFrames >= minFramesAfterStart
            let hardCap = hasSeenBallSinceStart
                && capturedDurationS >= maxCycleDurationS
            if naturalEnd || hardCap {
                finishCycle()
            }
        } else {
            preRollBuffer.append(frame)
            if preRollBuffer.count > preRollMaxFrames {
                preRollBuffer.removeFirst(preRollBuffer.count - preRollMaxFrames)
            }
        }
    }

    /// Emergency finish used when the dashboard sends `disarm` mid-
    /// recording (or the session times out server-side). Flushes whatever
    /// frames are buffered as a short cycle — same callback path as a
    /// natural end, so the upload queue sees no special case.
    func forceFinishIfRecording() {
        guard isRecording else { return }
        finishCycle()
    }

    private func finishCycle() {
        isRecording = false

        let payload = ServerUploader.PitchPayload(
            camera_id: cameraId,
            session_id: sessionId,
            sync_anchor_frame_index: syncAnchorFrameIndex,
            sync_anchor_timestamp_s: syncAnchorTimestampS,
            local_recording_index: localRecordingIndex,
            frames: cycleFrames,
            intrinsics: nil,
            homography: nil,
            image_width_px: nil,
            image_height_px: nil
        )

        onCycleComplete?(payload)
        cycleFrames.removeAll(keepingCapacity: true)
        hasSeenBallSinceStart = false
        noBallStreakFrames = 0
        pitchStartFrameIndex = 0
        sessionId = ""
        // preRollBuffer survives so the next recording can include pre-
        // trigger frames.
    }
}
