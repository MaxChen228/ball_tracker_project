import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.commands")

final class CameraCommandRouter {
    struct Dependencies {
        let cameraRole: () -> String
        let getState: () -> CameraAppState
        let healthMonitor: ServerHealthMonitor
        let setCurrentSessionId: (String?) -> Void
        let setCurrentSessionPaths: (Set<ServerUploader.DetectionPath>) -> Void
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let currentCaptureHeight: () -> Int
        let updateModeLabel: (String) -> Void
        let handleTrackingExposureCap: (String) -> Void
        let startTimeSync: (String) -> Void
        let applyMutualSync: (String) -> Void
        let applyRemoteArm: () -> Void
        let applyRemoteDisarm: () -> Void
        let applyServerCaptureHeight: (Int) -> Void
        let chirpThresholdDidPush: (Double) -> Void
        let heartbeatIntervalDidPush: (Double) -> Void
        let isPreviewRequested: () -> Bool
        let setPreviewRequested: (Bool) -> Void
        let ensurePreviewUploader: () -> Void
        let resetPreviewUploader: () -> Void
        let startCaptureAtStandby: () -> Void
        let stopCapture: () -> Void
        let isCalibrationFrameCaptureArmed: () -> Bool
        let setCalibrationFrameCaptureArmed: (Bool) -> Void
    }

    private let deps: Dependencies
    private var lastServerChirpThreshold: Double?
    private var lastServerHeartbeatInterval: Double?

    init(dependencies: Dependencies) {
        self.deps = dependencies
    }

    func didConnect() {
        deps.healthMonitor.recordConnectionSuccess(status: "WS_OPEN")
    }

    func didDisconnect() {
        deps.healthMonitor.recordConnectionDrop()
    }

    func handle(message: [String: Any]) {
        guard let type = message["type"] as? String else { return }

        deps.healthMonitor.recordConnectionSuccess(
            status: type == "arm" ? "ARMED (\(message["sid"] as? String ?? "-"))" : "IDLE"
        )

        switch type {
        case "sync_run":
            if let sid = message["sync_id"] as? String {
                DispatchQueue.main.async { self.deps.applyMutualSync(sid) }
            }
        case "sync_command":
            if let cmd = message["command"] as? String, cmd == "start" {
                DispatchQueue.main.async {
                    if self.deps.getState() == .standby,
                       let syncId = message["sync_command_id"] as? String {
                        self.deps.startTimeSync(syncId)
                    }
                }
            }
        case "arm":
            if let sid = message["sid"] as? String { deps.setCurrentSessionId(sid) }
            applyPushedPaths(message["paths"] as? [String])
            if let capStr = message["tracking_exposure_cap"] as? String {
                deps.handleTrackingExposureCap(capStr)
            }
            DispatchQueue.main.async {
                self.refreshModeLabel()
                self.deps.applyRemoteArm()
            }
        case "disarm":
            DispatchQueue.main.async { self.deps.applyRemoteDisarm() }
        case "calibration_updated":
            let changedCam = (message["cam"] as? String) ?? "?"
            let localCam = deps.cameraRole()
            log.info("ws calibration update cam=\(changedCam, privacy: .public) local=\(localCam, privacy: .public)")
            DispatchQueue.main.async { self.deps.healthMonitor.probeNow() }
        case "settings":
            applyPushedPaths(message["paths"] as? [String])
            if let threshold = message["chirp_detect_threshold"] as? Double,
               lastServerChirpThreshold != threshold {
                lastServerChirpThreshold = threshold
                DispatchQueue.main.async { self.deps.chirpThresholdDidPush(threshold) }
            }
            if let interval = message["heartbeat_interval_s"] as? Double,
               lastServerHeartbeatInterval != interval {
                lastServerHeartbeatInterval = interval
                DispatchQueue.main.async { self.deps.heartbeatIntervalDidPush(interval) }
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
            let pushedPrev = message["preview_requested"] as? Bool ?? false
            applyPreviewRequest(pushedPrev)
            let calFrame = message["calibration_frame_requested"] as? Bool ?? false
            if calFrame, !deps.isCalibrationFrameCaptureArmed(), deps.getState() != .recording {
                deps.setCalibrationFrameCaptureArmed(true)
                if deps.getState() == .standby && !deps.isPreviewRequested() {
                    deps.startCaptureAtStandby()
                }
            }
            DispatchQueue.main.async { self.refreshModeLabel() }
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
        if deps.isPreviewRequested() != requested {
            deps.setPreviewRequested(requested)
            if requested {
                deps.ensurePreviewUploader()
                if deps.getState() == .standby {
                    deps.startCaptureAtStandby()
                }
            } else {
                deps.resetPreviewUploader()
                if deps.getState() == .standby {
                    deps.stopCapture()
                }
            }
        }
    }

    private func refreshModeLabel() {
        let pathLabel = deps.getCurrentSessionPaths().map(\.displayLabel).sorted().joined(separator: " + ")
        deps.updateModeLabel("MODE · \(pathLabel.uppercased())")
    }
}
