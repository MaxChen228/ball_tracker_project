import AVFoundation
import Foundation
import os

private let recordingLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.recording")

/// Coordinates one armed-session recording lifecycle. Owns:
///
/// 1. Session-scoped bookkeeping (server-minted session id, chirp anchor,
///    first-sample video PTS, capture telemetry) — previously hosted by
///    the standalone `PitchRecorder` class, inlined here since the phone
///    is a pure camera post-PR61 and there was nothing left to share.
/// 2. The `ClipRecorder` AVAssetWriter driving the MOV archive.
/// 3. The `PitchPayloadStore` on-disk cache + `PayloadUploadQueue`.
///
/// Exit path is `forceFinishIfRecording()` (triggered via
/// `handleRemoteDisarm` on dashboard stop / session timeout); it emits a
/// `PitchPayload`, waits for the clip to finalise, then persists + enqueues
/// the upload and returns the VC to standby.
final class CameraRecordingWorkflow {
    struct Dependencies {
        let getCameraRole: () -> String
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let getSyncId: () -> String?
        let getSyncAnchorTimestampS: () -> Double?
        /// Returns `nil` when no real telemetry is available yet (no frames
        /// observed, or AVCaptureDevice callback hasn't fired). The recording
        /// workflow embeds whatever it gets — `nil` flows through to the
        /// pitch payload's optional `capture_telemetry` field rather than a
        /// zero-filled fabrication.
        let currentCaptureTelemetry: (Double) -> ServerUploader.CaptureTelemetry?
        let startCapture: (Double) -> Void
        let resetDetectionState: () -> Void
        let clearRecoveredAnchor: () -> Void
        let dispatchLiveCycleEnd: (String, String) -> Void
        /// Wait until the live-detection backlog is fully drained, then run
        /// `completion` on the workflow's processing queue. Used to defer
        /// `cycle_end` past disarm so the server's `persist_live_frames`
        /// sees every frame iOS produced — including those that were still
        /// in flight on the detection worker when ClipRecorder finished.
        let waitForDetectionDrain: (@escaping () -> Void) -> Void
        let showErrorBanner: (String) -> Void
        let hideBanner: () -> Void
        let setStatusText: (String) -> Void
        let transitionState: (CameraViewController.AppState, Bool, String?) -> Void
        let reconcileStandbyCaptureState: () -> Void
        let refreshUI: () -> Void
    }

    private let dependencies: Dependencies
    private let trackingFps: Double
    private let processingQueue: DispatchQueue
    private let payloadStore = PitchPayloadStore()
    private(set) var payloadUploadQueue: PayloadUploadQueue
    private var clipRecorder: ClipRecorder?

    // Per-cycle bookkeeping (formerly PitchRecorder).
    private var isRecording: Bool = false
    /// Device-local debug counter. Not a pairing key. Survives across
    /// recordings within a single app lifetime; not persisted.
    private var localRecordingIndex: Int = 0
    private var cameraId: String = "A"
    private var currentSessionId: String = ""
    private var currentSyncId: String?
    private var currentSyncAnchorTimestampS: Double?
    private var currentVideoStartPtsS: Double = 0.0
    private var currentCaptureTelemetry: ServerUploader.CaptureTelemetry?

    var onRecordingStarted: ((Int) -> Void)?
    var onCycleCompleted: (() -> Void)?

    /// Whether a recording cycle is in flight — i.e. `startRecorderIfNeeded`
    /// has been called and `forceFinishIfRecording()` has not yet run. Read
    /// by CameraViewController tests and the remote-disarm handler.
    var isRecordingActive: Bool { isRecording }

    init(
        uploader: ServerUploader,
        trackingFps: Double,
        processingQueue: DispatchQueue,
        dependencies: Dependencies
    ) {
        self.dependencies = dependencies
        self.trackingFps = trackingFps
        self.processingQueue = processingQueue
        self.payloadUploadQueue = PayloadUploadQueue(store: payloadStore, uploader: uploader)

        self.cameraId = dependencies.getCameraRole()
    }

    func ensurePersistenceDirectories() throws {
        try payloadStore.ensureDirectory()
    }

    func reloadPendingQueues() {
        try? payloadUploadQueue.reloadPending()
        payloadUploadQueue.processNextIfNeeded()
    }

    func updateUploader(_ uploader: ServerUploader) {
        payloadUploadQueue.updateUploader(uploader)
    }

    func updateCameraRole(_ cameraRole: String) {
        cameraId = cameraRole
    }

    func enterRecordingMode(sessionId: String?, serverTimeSyncConfirmed: Bool) {
        recordingLog.info("camera entering recording session=\(sessionId ?? "nil", privacy: .public) cam=\(self.dependencies.getCameraRole(), privacy: .public)")
        if !serverTimeSyncConfirmed {
            dependencies.showErrorBanner("尚未時間校正，將無法三角化")
            recordingLog.warning("arm without server-confirmed time sync — server will skip triangulation")
        } else {
            dependencies.hideBanner()
        }
        dependencies.startCapture(trackingFps)
        resetRecordingState()
        dependencies.resetDetectionState()
        dependencies.transitionState(.recording, true, sessionId)
        dependencies.refreshUI()
    }

    func exitRecordingToStandby(currentSessionId: String?, currentState: CameraViewController.AppState) {
        recordingLog.info("camera exit recording → standby session=\(currentSessionId ?? "nil", privacy: .public) cam=\(self.dependencies.getCameraRole(), privacy: .public) state=\(CameraViewController.stateText(currentState), privacy: .public)")
        dependencies.transitionState(.standby, false, nil)
        resetRecordingState()
        dependencies.resetDetectionState()
        processingQueue.async { [weak self] in
            self?.clipRecorder?.cancel()
            self?.clipRecorder = nil
        }
        dependencies.hideBanner()
        dependencies.reconcileStandbyCaptureState()
        dependencies.refreshUI()
    }

    func handleRemoteDisarm(currentSessionId: String?, currentState: CameraViewController.AppState) {
        recordingLog.info("camera disarm while recording session=\(currentSessionId ?? "nil", privacy: .public) cam=\(self.dependencies.getCameraRole(), privacy: .public)")
        processingQueue.async { [weak self] in
            guard let self else { return }
            let active = self.isRecording
            recordingLog.info("camera disarm while recording: recorder_active=\(active) clip_exists=\(self.clipRecorder != nil)")
            if active {
                self.forceFinishIfRecording()
            } else {
                recordingLog.warning("camera disarm before first frame: no payload produced — frames never reached captureOutput or clip.append failed")
                self.clipRecorder?.cancel()
                self.clipRecorder = nil
                DispatchQueue.main.async {
                    self.exitRecordingToStandby(currentSessionId: currentSessionId, currentState: currentState)
                }
            }
        }
    }

    @discardableResult
    func bootstrapClipRecorder(width: Int, height: Int, sessionId: String?) -> Bool {
        recordingLog.info("camera clip bootstrap start width=\(width) height=\(height) sid=\(sessionId ?? "nil", privacy: .public)")
        let tmpURL = payloadStore.makeTempVideoURL()
        let recorder = ClipRecorder(outputURL: tmpURL)
        do {
            // Pass capture fps so AVAssetWriter sizes its encoder budget
            // for 240 Hz instead of the ~30 Hz default — without it the
            // H.264 hardware encoder back-pressures and drops the vast
            // majority of frames (see ClipRecorder.prepare comment).
            try recorder.prepare(width: width, height: height, expectedFps: Int(trackingFps.rounded()))
            clipRecorder = recorder
            recordingLog.info("camera clip bootstrap ok session=\(sessionId ?? "nil", privacy: .public)")
            return true
        } catch {
            recordingLog.error("camera clip recorder prepare failed error=\(error.localizedDescription, privacy: .public)")
            clipRecorder = nil
            return false
        }
    }

    func appendSample(_ sampleBuffer: CMSampleBuffer) {
        clipRecorder?.append(sampleBuffer: sampleBuffer)
    }

    /// Begin the bookkeeping side of a recording cycle. `sessionId` MUST be
    /// the server-minted value; `timestampS` is the session-clock PTS of
    /// the first appended sample (used by the server to reconstruct
    /// absolute PTS per decoded frame). Idempotent — a second call while
    /// already recording is a no-op.
    func startRecorderIfNeeded(sessionId: String, timestampS: TimeInterval) {
        guard !isRecording else { return }

        currentSessionId = sessionId
        currentSyncId = dependencies.getSyncId()
        currentSyncAnchorTimestampS = dependencies.getSyncAnchorTimestampS()
        currentVideoStartPtsS = timestampS
        currentCaptureTelemetry = dependencies.currentCaptureTelemetry(trackingFps)
        isRecording = true
        localRecordingIndex += 1

        recordingLog.info("camera first frame, starting recorder session=\(sessionId, privacy: .public) mode=camera_only video_start_pts=\(timestampS) anchor=\(self.currentSyncAnchorTimestampS ?? .nan) idx=\(self.localRecordingIndex) sync_id=\(self.currentSyncId ?? "nil", privacy: .public)")
        onRecordingStarted?(localRecordingIndex)
    }

    /// Finish the current cycle. Sole exit path — fired by the dashboard
    /// stop (disarm command) or a server-side session timeout. Emits the
    /// payload, finalises the clip, persists, and hands off to the upload
    /// queue.
    func forceFinishIfRecording() {
        guard isRecording else { return }
        recordingLog.info("recorder force finish session=\(self.currentSessionId, privacy: .public) cam=\(self.cameraId, privacy: .public)")
        isRecording = false

        let payload = ServerUploader.PitchPayload(
            camera_id: cameraId,
            session_id: currentSessionId,
            sync_id: currentSyncId,
            sync_anchor_timestamp_s: currentSyncAnchorTimestampS,
            video_start_pts_s: currentVideoStartPtsS,
            local_recording_index: localRecordingIndex,
            paths: nil,
            capture_telemetry: currentCaptureTelemetry
        )

        currentSessionId = ""
        currentSyncId = nil
        currentSyncAnchorTimestampS = nil
        currentVideoStartPtsS = 0.0
        currentCaptureTelemetry = nil

        handleCycleComplete(payload)
    }

    /// Clear transient cycle state without touching `localRecordingIndex`
    /// (which is a run-of-app debug counter by design).
    private func resetRecordingState() {
        isRecording = false
        currentSessionId = ""
        currentSyncId = nil
        currentSyncAnchorTimestampS = nil
        currentVideoStartPtsS = 0.0
        currentCaptureTelemetry = nil
    }

    private func handleCycleComplete(_ payload: ServerUploader.PitchPayload) {
        let finishingClip = clipRecorder
        clipRecorder = nil
        // Do NOT clear the recovered anchor here. Quick Chirp's anchor is
        // the two phones' clock-offset — it's a property of the clocks
        // themselves, stable across recordings. Re-calibrating every
        // pitch was legacy one-shot behavior that forced the operator
        // to re-run Quick Chirp before every session.
        recordingLog.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
        DispatchQueue.main.async {
            self.onCycleCompleted?()
        }
        if let finishingClip {
            finishingClip.finish { [weak self] videoURL in
                self?.handleFinishedClip(enriched: payload, videoURL: videoURL)
            }
        } else {
            handleFinishedClip(enriched: payload, videoURL: nil)
        }
    }

    private func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let paths = dependencies.getCurrentSessionPaths()
        recordingLog.info("cycle complete session=\(enriched.session_id, privacy: .public) paths=\(paths.map(\.rawValue).sorted().joined(separator: ","), privacy: .public) has_video=\(videoURL != nil)")
        let payload = enriched.withPaths(Array(paths))

        if paths.contains(.live) {
            // Capture has stopped (ClipRecorder finished), but the
            // detection worker can still hold a few hundred ms of
            // backlog — frames whose pixel buffers were retained by the
            // pool while their HSV+CC pass was running. Defer cycle_end
            // and the standby transition until that backlog drains; the
            // server's `cycle_end` handler triggers `persist_live_frames`
            // which writes the live bucket to disk in a single shot, so
            // late frames must arrive BEFORE that fires or they'd never
            // make it onto the pitch JSON.
            dependencies.waitForDetectionDrain { [weak self] in
                guard let self else { return }
                self.dependencies.dispatchLiveCycleEnd(payload.session_id, "disarmed")
                self.persistCompletedCycle(payload, videoURL: videoURL)
            }
        } else {
            // No live path → no detection backlog to drain.
            persistCompletedCycle(payload, videoURL: videoURL)
        }
    }

    private func persistCompletedCycle(
        _ payload: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        do {
            let fileURL = try payloadStore.save(payload, videoURL: videoURL)
            DispatchQueue.main.async {
                self.dependencies.setStatusText("暫存完成 · 等待上傳")
                self.payloadUploadQueue.enqueue(fileURL)
                self.exitRecordingToStandby(currentSessionId: payload.session_id, currentState: .recording)
            }
        } catch {
            recordingLog.error("camera cycle persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            if let videoURL {
                try? FileManager.default.removeItem(at: videoURL)
            }
            DispatchQueue.main.async {
                self.dependencies.setStatusText("暫存失敗 · \(error.localizedDescription)")
                self.exitRecordingToStandby(currentSessionId: payload.session_id, currentState: .recording)
            }
        }
    }

}
