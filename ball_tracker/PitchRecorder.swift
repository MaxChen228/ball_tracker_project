import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Bookkeeping for one armed-session recording. Holds the server-minted
/// session id, the chirp-anchor timestamp, the first-sample video PTS, and
/// the nominal capture rate — then emits a `PitchPayload` at force-finish.
///
/// No per-frame work lives here any more: the phone is a pure camera, so
/// ball detection runs on the server against the MOV and neither iOS nor
/// this recorder inspects pixel content. `forceFinishIfRecording()` is the
/// sole exit path (dashboard stop or server-side session timeout).
final class PitchRecorder {
    private var isRecording: Bool = false
    /// Device-local debug counter. Not a pairing key. Survives across
    /// recordings within a single app lifetime; not persisted.
    private var localRecordingIndex: Int = 0

    private var syncAnchorTimestampS: Double?
    private var videoStartPtsS: Double = 0.0
    private var sessionId: String = ""
    private var cameraId: String = "A"
    private var captureTelemetry: ServerUploader.CaptureTelemetry?

    var onCycleComplete: ((ServerUploader.PitchPayload) -> Void)?
    var onRecordingStarted: ((Int) -> Void)?

    /// Whether `startRecording(...)` has been called and `forceFinishIfRecording()`
    /// has not yet been invoked. Read by CameraViewController to distinguish
    /// "disarm before any frame arrived" (no-op finish) from the normal
    /// "disarm during recording" path.
    var isActive: Bool { isRecording }

    func setCameraId(_ id: String) {
        cameraId = id
    }

    func reset() {
        isRecording = false
        sessionId = ""
        syncAnchorTimestampS = nil
        videoStartPtsS = 0.0
        captureTelemetry = nil
        // localRecordingIndex intentionally NOT reset — it's a run-of-
        // app counter used only for debug logs.
    }

    /// Called once per armed session, after the ClipRecorder's first sample
    /// arrives. `sessionId` MUST be the server-minted value; `videoStartPtsS`
    /// is the session-clock PTS of the first appended sample (used by the
    /// server to reconstruct absolute PTS per decoded frame).
    func startRecording(
        sessionId: String,
        anchorTimestampS: Double?,
        videoStartPtsS: Double,
        captureTelemetry: ServerUploader.CaptureTelemetry?
    ) {
        guard !isRecording else { return }

        self.sessionId = sessionId
        self.syncAnchorTimestampS = anchorTimestampS
        self.videoStartPtsS = videoStartPtsS
        self.captureTelemetry = captureTelemetry
        isRecording = true
        localRecordingIndex += 1
        log.info("recorder start session=\(sessionId, privacy: .public) cam=\(self.cameraId, privacy: .public) idx=\(self.localRecordingIndex) anchor_ts=\(anchorTimestampS ?? .nan) video_start=\(videoStartPtsS)")
        onRecordingStarted?(localRecordingIndex)
    }

    /// Finish the current cycle. Sole exit path — fired by the dashboard
    /// stop (disarm command) or a server-side session timeout. Emits the
    /// payload to `onCycleComplete` and returns to idle.
    func forceFinishIfRecording() {
        guard isRecording else { return }
        log.info("recorder force finish session=\(self.sessionId, privacy: .public) cam=\(self.cameraId, privacy: .public)")
        isRecording = false

        let payload = ServerUploader.PitchPayload(
            camera_id: cameraId,
            session_id: sessionId,
            sync_anchor_timestamp_s: syncAnchorTimestampS,
            video_start_pts_s: videoStartPtsS,
            local_recording_index: localRecordingIndex,
            frames: [],
            frames_on_device: [],
            capture_telemetry: captureTelemetry
        )
        onCycleComplete?(payload)
        sessionId = ""
        syncAnchorTimestampS = nil
        videoStartPtsS = 0.0
        captureTelemetry = nil
    }
}
