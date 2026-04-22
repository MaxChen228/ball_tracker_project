import Foundation
import UIKit
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.recording")

final class CameraRecordingCoordinator {
    struct Dependencies {
        let cameraRole: () -> String
        let getState: () -> CameraAppState
        let setState: (CameraAppState) -> Void
        let updateFrameState: (CameraAppState, Bool, String?) -> Void
        let currentSessionId: () -> String?
        let lastSyncAnchorTimestampS: () -> Double?
        let currentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let previewRequestedByServer: () -> Bool
        let resetRecorder: () -> Void
        let resetBallDetectionState: () -> Void
        let cancelClipRecorderAsync: () -> Void
        let cancelClipRecorderNow: () -> Void
        let clipRecorderExists: () -> Bool
        let recorderIsActive: () -> Bool
        let forceFinishRecording: () -> Void
        let runOnProcessingQueue: (@escaping () -> Void) -> Void
        let startCapture: (Double) -> Void
        let stopCapture: () -> Void
        let switchCaptureFps: (Double) -> Void
        let updateUIForState: () -> Void
        let setWarning: (String?, UIColor?, Bool) -> Void
        let setLastUploadStatusText: (String) -> Void
        let setReturnToStandbyAfterCycle: (Bool) -> Void
        let armFeedback: () -> Void
        let prepareRecordingFeedback: () -> Void
        let drainDetectedFrames: () -> [ServerUploader.FramePayload]
        let dispatchCycleEnd: (String, String) -> Void
        let payloadStore: PitchPayloadStore
        let uploadQueue: PayloadUploadQueue
        let analysisStore: AnalysisJobStore
        let analysisQueue: AnalysisUploadQueue
    }

    private let standbyFps: Double
    private let trackingFps: Double
    private let deps: Dependencies

    init(
        standbyFps: Double,
        trackingFps: Double,
        dependencies: Dependencies
    ) {
        self.standbyFps = standbyFps
        self.trackingFps = trackingFps
        self.deps = dependencies
    }

    func applyRemoteArm() {
        let stateText = CameraViewController.stateText(self.deps.getState())
        let sessionId = self.deps.currentSessionId() ?? "nil"
        let cameraRole = self.deps.cameraRole()
        log.info("camera received arm command state=\(stateText, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraRole, privacy: .public)")
        switch deps.getState() {
        case .standby:
            enterRecordingMode()
        case .timeSyncWaiting, .mutualSyncing, .recording, .uploading:
            break
        }
    }

    func applyRemoteDisarm(
        cancelTimeSync: @escaping (String) -> Void,
        abortMutualSync: @escaping (String) -> Void
    ) {
        let stateText = CameraViewController.stateText(self.deps.getState())
        let sessionId = self.deps.currentSessionId() ?? "nil"
        let cameraRole = self.deps.cameraRole()
        log.info("camera received disarm command state=\(stateText, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraRole, privacy: .public)")
        switch deps.getState() {
        case .standby:
            break
        case .timeSyncWaiting:
            cancelTimeSync("disarmed")
        case .mutualSyncing:
            abortMutualSync("disarmed")
        case .uploading:
            break
        case .recording:
            deps.setReturnToStandbyAfterCycle(true)
            deps.runOnProcessingQueue {
                let active = self.deps.recorderIsActive()
                log.info("camera disarm while recording: recorder_active=\(active) clip_exists=\(self.deps.clipRecorderExists())")
                if active {
                    self.deps.forceFinishRecording()
                } else {
                    log.warning("camera disarm before first frame: no payload produced — frames never reached captureOutput or clip.append failed")
                    self.deps.cancelClipRecorderNow()
                    DispatchQueue.main.async {
                        self.deps.setReturnToStandbyAfterCycle(false)
                        self.exitRecordingToStandby()
                    }
                }
            }
        }
    }

    func enterRecordingMode() {
        guard deps.getState() == .standby else { return }
        let snapshotSessionId = deps.currentSessionId()
        let cameraRole = self.deps.cameraRole()
        log.info("camera entering recording session=\(snapshotSessionId ?? "nil", privacy: .public) cam=\(cameraRole, privacy: .public)")
        if deps.lastSyncAnchorTimestampS() == nil {
            deps.setWarning("尚未時間校正，將無法三角化", DesignTokens.Colors.ink, false)
            log.warning("arm without time sync anchor — server will skip triangulation")
        } else {
            deps.setWarning(nil, nil, true)
        }
        deps.startCapture(trackingFps)
        deps.resetRecorder()
        deps.resetBallDetectionState()
        deps.setState(.recording)
        deps.updateFrameState(.recording, true, snapshotSessionId)
        deps.updateUIForState()
        deps.armFeedback()
        deps.prepareRecordingFeedback()
    }

    func exitRecordingToStandby() {
        let sessionId = self.deps.currentSessionId() ?? "nil"
        let cameraRole = self.deps.cameraRole()
        let stateText = CameraViewController.stateText(self.deps.getState())
        log.info("camera exit recording → standby session=\(sessionId, privacy: .public) cam=\(cameraRole, privacy: .public) state=\(stateText, privacy: .public)")
        deps.setState(.standby)
        deps.updateFrameState(.standby, false, nil)
        deps.resetRecorder()
        deps.resetBallDetectionState()
        deps.cancelClipRecorderAsync()
        deps.setWarning(nil, nil, true)
        if deps.previewRequestedByServer() {
            deps.switchCaptureFps(standbyFps)
        } else {
            deps.stopCapture()
        }
        deps.updateUIForState()
    }

    func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let advisoryFrames = deps.drainDetectedFrames()
        let paths = deps.currentSessionPaths()
        log.info("cycle complete session=\(enriched.session_id, privacy: .public) paths=\(paths.map(\.rawValue).sorted().joined(separator: ","), privacy: .public) advisory_frames=\(advisoryFrames.count) ball_frames=\(advisoryFrames.filter { $0.ball_detected }.count) has_video=\(videoURL != nil)")
        let payload = enriched.withPaths(Array(paths))

        guard let videoURL else {
            handleFallbackCycleWithoutVideo(
                enriched: payload,
                advisoryFrames: advisoryFrames,
                paths: paths
            )
            return
        }

        if paths.contains(.serverPost) {
            handleCameraOnlyCycle(enriched: payload, videoURL: videoURL)
        }
        if paths.contains(.iosPost) {
            let uploadMode: AnalysisJobStore.Job.UploadMode = paths.contains(.serverPost) ? .dualSidecar : .onDevicePrimary
            let analysisVideoURL = paths.contains(.serverPost) ? duplicateVideoForAnalysis(from: videoURL) : videoURL
            if let analysisVideoURL {
                persistAnalysisJob(
                    payload: payload,
                    videoURL: analysisVideoURL,
                    uploadMode: uploadMode
                )
            } else {
                log.error("camera failed to prepare clip for iOS post-pass session=\(payload.session_id, privacy: .public)")
            }
        }
        if paths.contains(.live) {
            deps.dispatchCycleEnd(payload.session_id, "disarmed")
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

    private func handleCameraOnlyCycle(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        persistCompletedCycle(enriched, videoURL: videoURL)
    }

    private func persistCompletedCycle(
        _ payload: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        do {
            let fileURL = try deps.payloadStore.save(payload, videoURL: videoURL)
            DispatchQueue.main.async {
                self.deps.setLastUploadStatusText("暫存完成 · 等待上傳")
                self.deps.uploadQueue.enqueue(fileURL)
                self.deps.setReturnToStandbyAfterCycle(false)
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera cycle persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            if let videoURL {
                try? FileManager.default.removeItem(at: videoURL)
            }
            DispatchQueue.main.async {
                self.deps.setLastUploadStatusText("暫存失敗 · \(error.localizedDescription)")
                self.deps.setReturnToStandbyAfterCycle(false)
                self.exitRecordingToStandby()
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
            let fileURL = try deps.analysisStore.save(job, videoURL: videoURL)
            DispatchQueue.main.async {
                self.deps.setLastUploadStatusText("暫存完成 · 等待錄後分析")
                self.deps.analysisQueue.enqueue(fileURL)
                self.deps.setReturnToStandbyAfterCycle(false)
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera analysis persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            try? FileManager.default.removeItem(at: videoURL)
            DispatchQueue.main.async {
                self.deps.setLastUploadStatusText("錄後分析暫存失敗 · \(error.localizedDescription)")
                self.deps.setReturnToStandbyAfterCycle(false)
                self.exitRecordingToStandby()
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
            log.error("camera analysis clip copy failed src=\(sourceURL.lastPathComponent, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            return nil
        }
    }
}
