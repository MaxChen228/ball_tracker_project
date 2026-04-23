import AVFoundation
import Foundation
import os

private let recordingLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.recording")

final class CameraRecordingWorkflow {
    struct Dependencies {
        let getCameraRole: () -> String
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let getCurrentCaptureMode: () -> ServerUploader.CaptureMode
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
    private let analysisStore = AnalysisJobStore()
    private let recorder = PitchRecorder()
    private(set) var payloadUploadQueue: PayloadUploadQueue
    private(set) var analysisUploadQueue: AnalysisUploadQueue
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
        self.analysisUploadQueue = AnalysisUploadQueue(store: analysisStore, uploader: uploader)

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
        try analysisStore.ensureDirectory()
    }

    func reloadPendingQueues() {
        try? payloadUploadQueue.reloadPending()
        payloadUploadQueue.processNextIfNeeded()
        try? analysisUploadQueue.reloadPending()
        analysisUploadQueue.processNextIfNeeded()
    }

    func updateUploader(_ uploader: ServerUploader) {
        payloadUploadQueue.updateUploader(uploader)
        analysisUploadQueue.updateUploader(uploader)
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
        recordingLog.info("camera first frame, starting recorder session=\(sessionId, privacy: .public) mode=\(self.dependencies.getCurrentCaptureMode().rawValue, privacy: .public) video_start_pts=\(timestampS) anchor=\(self.dependencies.getSyncAnchorTimestampS() ?? .nan)")
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
        let advisoryFrames = dependencies.drainDetectedFrames()
        let paths = dependencies.getCurrentSessionPaths()
        recordingLog.info("cycle complete session=\(enriched.session_id, privacy: .public) paths=\(paths.map(\.rawValue).sorted().joined(separator: ","), privacy: .public) advisory_frames=\(advisoryFrames.count) ball_frames=\(advisoryFrames.filter { $0.ball_detected }.count) has_video=\(videoURL != nil)")
        let payload = enriched.withPaths(Array(paths))

        guard let videoURL else {
            handleFallbackCycleWithoutVideo(
                enriched: payload,
                advisoryFrames: advisoryFrames,
                paths: paths
            )
            return
        }

        let analysisVideoURL: URL?
        if paths.contains(.iosPost) {
            analysisVideoURL = paths.contains(.serverPost)
                ? duplicateVideoForAnalysis(from: videoURL)
                : videoURL
        } else {
            analysisVideoURL = nil
        }

        if paths.contains(.serverPost) {
            persistCompletedCycle(payload, videoURL: videoURL)
        }
        if paths.contains(.iosPost) {
            let uploadMode: AnalysisJobStore.Job.UploadMode = paths.contains(.serverPost)
                ? .dualSidecar
                : .onDevicePrimary
            if let analysisVideoURL {
                persistAnalysisJob(
                    payload: payload,
                    videoURL: analysisVideoURL,
                    uploadMode: uploadMode
                )
            } else {
                recordingLog.error("camera failed to prepare clip for iOS post-pass session=\(payload.session_id, privacy: .public)")
            }
        }
        if paths.contains(.live) {
            dependencies.dispatchLiveCycleEnd(payload.session_id, "disarmed")
        }
        if !paths.contains(.serverPost) && !paths.contains(.iosPost) {
            persistCompletedCycle(payload, videoURL: videoURL)
        }
    }

    private func handleFallbackCycleWithoutVideo(
        enriched: ServerUploader.PitchPayload,
        advisoryFrames: [ServerUploader.FramePayload],
        paths: Set<ServerUploader.DetectionPath>
    ) {
        if paths.contains(.serverPost) {
            persistCompletedCycle(enriched, videoURL: nil)
        } else {
            persistCompletedCycle(enriched.withFrames(advisoryFrames), videoURL: nil)
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

    private func persistAnalysisJob(
        payload: ServerUploader.PitchPayload,
        videoURL: URL,
        uploadMode: AnalysisJobStore.Job.UploadMode
    ) {
        let job = AnalysisJobStore.Job(uploadMode: uploadMode, pitch: payload)
        do {
            let fileURL = try analysisStore.save(job, videoURL: videoURL)
            DispatchQueue.main.async {
                self.dependencies.setStatusText("暫存完成 · 等待錄後分析")
                self.analysisUploadQueue.enqueue(fileURL)
                self.exitRecordingToStandby(currentSessionId: payload.session_id, currentState: .recording)
            }
        } catch {
            recordingLog.error("camera analysis persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            try? FileManager.default.removeItem(at: videoURL)
            DispatchQueue.main.async {
                self.dependencies.setStatusText("錄後分析暫存失敗 · \(error.localizedDescription)")
                self.exitRecordingToStandby(currentSessionId: payload.session_id, currentState: .recording)
            }
        }
    }

    private func duplicateVideoForAnalysis(from sourceURL: URL) -> URL? {
        let ext = sourceURL.pathExtension.isEmpty ? "mov" : sourceURL.pathExtension
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("analysis_\(UUID().uuidString).\(ext)")
        do {
            try FileManager.default.copyItem(at: sourceURL, to: dest)
            return dest
        } catch {
            recordingLog.error("camera analysis clip copy failed src=\(sourceURL.lastPathComponent, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            return nil
        }
    }
}
