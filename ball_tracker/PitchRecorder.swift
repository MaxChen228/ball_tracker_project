import Foundation

/// Realtime per-frame pitch buffering:
/// - Maintain a pre-roll circular buffer (120 frames @ 240 fps ≈ 0.5 s)
/// - Start one pitch recording when the ball first appears
/// - Tag the cycle with a sync anchor (chirp-detected session-clock PTS)
/// - Keep ingesting frames until the ball has been absent for a while
final class PitchRecorder {
    private let preRollMaxFrames: Int = 120
    private let minFramesAfterStart: Int = 24
    private let endWhenNoBallFrames: Int = 24

    private var preRollBuffer: [ServerUploader.FramePayload] = []
    private var isRecording: Bool = false
    private var hasSeenBallSinceStart: Bool = false
    private var noBallStreakFrames: Int = 0
    private var pitchStartFrameIndex: Int = 0
    private var pitchNumber: Int = 1

    private var syncAnchorFrameIndex: Int = 0
    private var syncAnchorTimestampS: Double = 0.0
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
        pitchNumber = 1
        cycleFrames.removeAll(keepingCapacity: true)
    }

    /// Called when the ball first appears during sync mode.
    func startRecording(anchorFrameIndex: Int, anchorTimestampS: Double, startFrameIndex: Int) {
        guard !isRecording else { return }

        self.syncAnchorFrameIndex = anchorFrameIndex
        self.syncAnchorTimestampS = anchorTimestampS
        self.pitchStartFrameIndex = startFrameIndex

        isRecording = true
        hasSeenBallSinceStart = false
        noBallStreakFrames = 0

        // Include pre-trigger data in payload.
        cycleFrames = preRollBuffer
        onRecordingStarted?(pitchNumber)
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
            if hasSeenBallSinceStart
                && noBallStreakFrames >= endWhenNoBallFrames
                && capturedFrames >= minFramesAfterStart {
                finishCycle()
            }
        } else {
            preRollBuffer.append(frame)
            if preRollBuffer.count > preRollMaxFrames {
                preRollBuffer.removeFirst(preRollBuffer.count - preRollMaxFrames)
            }
        }
    }

    private func finishCycle() {
        isRecording = false

        let payload = ServerUploader.PitchPayload(
            camera_id: cameraId,
            sync_anchor_frame_index: syncAnchorFrameIndex,
            sync_anchor_timestamp_s: syncAnchorTimestampS,
            cycle_number: pitchNumber,
            frames: cycleFrames,
            intrinsics: nil,
            homography: nil,
            image_width_px: nil,
            image_height_px: nil
        )

        onCycleComplete?(payload)
        pitchNumber += 1
        cycleFrames.removeAll(keepingCapacity: true)
        hasSeenBallSinceStart = false
        noBallStreakFrames = 0
        pitchStartFrameIndex = 0
        // preRollBuffer survives so the next pitch can include pre-trigger frames.
    }

    func currentCycleNumber() -> Int {
        return pitchNumber
    }
}
