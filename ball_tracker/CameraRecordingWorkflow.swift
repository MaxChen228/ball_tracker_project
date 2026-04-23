import AVFoundation
import Foundation
import os

private let recordingLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.recording")

final class CameraRecordingWorkflow {
    struct Dependencies {
        let getCameraRole: () -> String
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let getSyncId: () -> String?
        let getSyncAnchorTimestampS: () -> Double?
        let currentCaptureTelemetry: (Double) -> ServerUploader.CaptureTelemetry
        let startCapture: (Double) -> Void
        let resetDetectionState: () -> Void
        let drainDetectedFrames: () -> [ServerUploader.FramePayload]
        let clearRecoveredAnchor: () -> Void
        let dispatchLiveCycleEnd: (String, String) -> Void
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
    private let recorder = PitchRecorder()
    private(set) var payloadUploadQueue: PayloadUploadQueue
    private var clipRecorder: ClipRecorder?

    var onRecordingStarted: ((Int) -> Void)?
    var onCycleCompleted: (() -> Void)?

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

        recorder.setCameraId(dependencies.getCameraRole())
        recorder.onRecordingStarted = { [weak self] idx in
            self?.onRecordingStarted?(idx)
        }
        recorder.onCycleComplete = { [weak self] payload in
            self?.handleCycleComplete(payload)
        }
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
        recorder.setCameraId(cameraRole)
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
        recorder.reset()
        dependencies.resetDetectionState()
        dependencies.transitionState(.recording, true, sessionId)
        dependencies.refreshUI()
    }

    func exitRecordingToStandby(currentSessionId: String?, currentState: CameraViewController.AppState) {
        recordingLog.info("camera exit recording → standby session=\(currentSessionId ?? "nil", privacy: .public) cam=\(self.dependencies.getCameraRole(), privacy: .public) state=\(CameraViewController.stateText(currentState), privacy: .public)")
        dependencies.transitionState(.standby, false, nil)
        recorder.reset()
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
            let active = self.recorder.isActive
            recordingLog.info("camera disarm while recording: recorder_active=\(active) clip_exists=\(self.clipRecorder != nil)")
            if active {
                self.recorder.forceFinishIfRecording()
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
            try recorder.prepare(width: width, height: height)
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

    func startRecorderIfNeeded(sessionId: String, timestampS: TimeInterval) {
        guard !recorder.isActive else { return }
        let pathsLabel = self.dependencies.getCurrentSessionPaths().map { $0.rawValue }.sorted().joined(separator: ",")
        recordingLog.info("camera first frame, starting recorder session=\(sessionId, privacy: .public) paths=\(pathsLabel, privacy: .public) video_start_pts=\(timestampS) anchor=\(self.dependencies.getSyncAnchorTimestampS() ?? .nan)")
        recorder.startRecording(
            sessionId: sessionId,
            syncId: dependencies.getSyncId(),
            anchorTimestampS: dependencies.getSyncAnchorTimestampS(),
            videoStartPtsS: timestampS,
            captureTelemetry: dependencies.currentCaptureTelemetry(trackingFps)
        )
    }

    private func handleCycleComplete(_ payload: ServerUploader.PitchPayload) {
        let finishingClip = clipRecorder
        clipRecorder = nil
        if payload.sync_id != nil {
            dependencies.clearRecoveredAnchor()
            DispatchQueue.main.async {
                self.dependencies.refreshUI()
            }
        }
        recordingLog.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
        DispatchQueue.main.async {
            self.onCycleCompleted?()
        }
        if let finishingClip {
            finishingClip.finish { [weak self] videoURL in
                // `droppedFrameCount` only stabilises after finish — fold it
                // into the telemetry so the uploaded payload carries the
                // real encoder-pressure number (not the stale zero from
                // first-frame stamping).
                let dropped = finishingClip.droppedFrameCount
                let enriched = Self.payloadWithDroppedFrameCount(payload, dropped: dropped)
                self?.handleFinishedClip(enriched: enriched, videoURL: videoURL)
            }
        } else {
            handleFinishedClip(enriched: payload, videoURL: nil)
        }
    }

    /// Clone `payload` with `capture_telemetry.dropped_frame_count = dropped`.
    /// Falls back to a minimal telemetry record when the payload has none,
    /// so operators still see dropped counts on legacy code paths.
    private static func payloadWithDroppedFrameCount(
        _ payload: ServerUploader.PitchPayload,
        dropped: Int
    ) -> ServerUploader.PitchPayload {
        let telemetry: ServerUploader.CaptureTelemetry
        if let existing = payload.capture_telemetry {
            telemetry = ServerUploader.CaptureTelemetry(
                width_px: existing.width_px,
                height_px: existing.height_px,
                target_fps: existing.target_fps,
                applied_fps: existing.applied_fps,
                format_fov_deg: existing.format_fov_deg,
                format_index: existing.format_index,
                is_video_binned: existing.is_video_binned,
                tracking_exposure_cap: existing.tracking_exposure_cap,
                applied_max_exposure_s: existing.applied_max_exposure_s,
                dropped_frame_count: dropped
            )
        } else {
            telemetry = ServerUploader.CaptureTelemetry(
                width_px: 0,
                height_px: 0,
                target_fps: 0,
                applied_fps: nil,
                format_fov_deg: nil,
                format_index: nil,
                is_video_binned: nil,
                tracking_exposure_cap: nil,
                applied_max_exposure_s: nil,
                dropped_frame_count: dropped
            )
        }
        return payload.withCaptureTelemetry(telemetry)
    }

    private func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let advisoryFrames = dependencies.drainDetectedFrames()
        let paths = dependencies.getCurrentSessionPaths()
        recordingLog.info("cycle complete session=\(enriched.session_id, privacy: .public) paths=\(paths.map(\.rawValue).sorted().joined(separator: ","), privacy: .public) advisory_frames=\(advisoryFrames.count) ball_frames=\(advisoryFrames.filter { $0.ball_detected }.count) has_video=\(videoURL != nil)")
        let payload = enriched.withPaths(Array(paths))

        if paths.contains(.live) {
            dependencies.dispatchLiveCycleEnd(payload.session_id, "disarmed")
        }

        if videoURL != nil || paths.contains(.serverPost) {
            persistCompletedCycle(payload, videoURL: videoURL)
        } else {
            // No MOV and server_post is not selected — persist the payload
            // with the live advisory frames so the session still has a record.
            persistCompletedCycle(payload.withFrames(advisoryFrames), videoURL: nil)
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
