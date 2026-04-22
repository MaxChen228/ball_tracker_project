import Foundation
import os

private let commandLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.commands")

final class CameraCommandRouter {
    struct Dependencies {
        let getState: () -> CameraViewController.AppState
        let getCameraRole: () -> String
        let healthMonitor: ServerHealthMonitor
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let setCurrentSessionId: (String?) -> Void
        let setCurrentSessionPaths: (Set<ServerUploader.DetectionPath>) -> Void
        let refreshModeLabel: () -> Void
        let startTimeSync: (String) -> Void
        let applyMutualSync: (String, [Double], Double) -> Void
        let applyRemoteArm: () -> Void
        let applyRemoteDisarm: () -> Void
        let updateTimeSyncServerState: (Bool, String?) -> Void
        let chirpThresholdDidPush: (Double) -> Void
        let mutualSyncThresholdDidPush: (Double) -> Void
        let heartbeatIntervalDidPush: (Double) -> Void
        let handleTrackingExposureCap: (String) -> Void
        let currentCaptureHeight: () -> Int
        let applyServerCaptureHeight: (Int) -> Void
        let isPreviewRequested: () -> Bool
        let setPreviewRequested: (Bool) -> Void
        let ensurePreviewUploader: () -> Void
        let resetPreviewUploader: () -> Void
        let startStandbyCapture: () -> Void
        let stopCapture: () -> Void
        let isCalibrationFrameCaptureArmed: () -> Bool
        let setCalibrationFrameCaptureArmed: (Bool) -> Void
    }

    private let deps: Dependencies

    init(dependencies: Dependencies) {
        self.deps = dependencies
    }

    func didDisconnect() {
        deps.healthMonitor.recordConnectionDrop()
        deps.setPreviewRequested(false)
        deps.resetPreviewUploader()
        if deps.getState() == .standby {
            deps.stopCapture()
        }
    }

    func handle(message: [String: Any]) {
        guard let type = message["type"] as? String else { return }

        deps.healthMonitor.recordConnectionSuccess(
            status: type == "arm" ? "ARMED (\(message["sid"] as? String ?? "-"))" : "IDLE"
        )

        switch type {
        case "sync_run":
            if let sid = message["sync_id"] as? String {
                let emitAtS = (message["emit_at_s"] as? [Double]) ?? [0.3]
                let recordDurationS = (message["record_duration_s"] as? Double) ?? 3.0
                DispatchQueue.main.async {
                    self.deps.applyMutualSync(sid, emitAtS: emitAtS, recordDurationS: recordDurationS)
                }
            }
        case "sync_command":
            if let cmd = message["command"] as? String, cmd == "start" {
                DispatchQueue.main.async {
                    guard self.deps.getState() == .standby,
                          let syncId = message["sync_command_id"] as? String else { return }
                    self.deps.startTimeSync(syncId)
                }
            }
        case "arm":
            if let sid = message["sid"] as? String {
                deps.setCurrentSessionId(sid)
            }
            applyPushedPaths(message["paths"] as? [String])
            if let capStr = message["tracking_exposure_cap"] as? String {
                deps.handleTrackingExposureCap(capStr)
            }
            DispatchQueue.main.async {
                self.deps.refreshModeLabel()
                self.deps.applyRemoteArm()
            }
        case "disarm":
            DispatchQueue.main.async { self.deps.applyRemoteDisarm() }
        case "calibration_updated":
            let changedCam = (message["cam"] as? String) ?? "?"
            commandLog.info(
                "ws calibration update cam=\(changedCam, privacy: .public) local=\(self.deps.getCameraRole(), privacy: .public)"
            )
            DispatchQueue.main.async { self.deps.healthMonitor.probeNow() }
        case "settings":
            let pushedTimeSync = message["device_time_synced"] as? Bool ?? false
            let pushedTimeSyncId = message["device_time_sync_id"] as? String
            deps.updateTimeSyncServerState(pushedTimeSync, pushedTimeSyncId)
            applyPushedPaths(message["paths"] as? [String])
            if let threshold = message["chirp_detect_threshold"] as? Double {
                deps.chirpThresholdDidPush(threshold)
            }
            if let mThreshold = message["mutual_sync_threshold"] as? Double {
                deps.mutualSyncThresholdDidPush(mThreshold)
            }
            if let interval = message["heartbeat_interval_s"] as? Double {
                deps.heartbeatIntervalDidPush(interval)
            }
            if let capStr = message["tracking_exposure_cap"] as? String {
                deps.handleTrackingExposureCap(capStr)
            }
            if let pushedH = message["capture_height_px"] as? Int,
               pushedH != deps.currentCaptureHeight() {
                DispatchQueue.main.async {
                    guard self.deps.getState() == .standby else { return }
                    self.deps.applyServerCaptureHeight(pushedH)
                }
            }
            applyPreviewRequest(message["preview_requested"] as? Bool ?? false)
            applyCalibrationFrameRequest(message["calibration_frame_requested"] as? Bool ?? false)
            DispatchQueue.main.async { self.deps.refreshModeLabel() }
        default:
            break
        }
    }

    private func applyPushedPaths(_ rawPaths: [String]?) {
        guard let rawPaths else { return }
        let parsed = Set(rawPaths.compactMap(ServerUploader.DetectionPath.init(rawValue:)))
        if !parsed.isEmpty {
            deps.setCurrentSessionPaths(parsed)
        }
    }

    private func applyPreviewRequest(_ requested: Bool) {
        guard deps.isPreviewRequested() != requested else { return }
        deps.setPreviewRequested(requested)
        if requested {
            deps.ensurePreviewUploader()
            if deps.getState() == .standby {
                deps.startStandbyCapture()
            }
        } else {
            deps.resetPreviewUploader()
            if deps.getState() == .standby {
                deps.stopCapture()
            }
        }
    }

    private func applyCalibrationFrameRequest(_ requested: Bool) {
        guard requested,
              !deps.isCalibrationFrameCaptureArmed(),
              deps.getState() != .recording else { return }
        deps.setCalibrationFrameCaptureArmed(true)
        if deps.getState() == .standby && !deps.isPreviewRequested() {
            deps.startStandbyCapture()
        }
    }
}
