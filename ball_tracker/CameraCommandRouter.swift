import Foundation
import os

private let commandLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.commands")

final class CameraCommandRouter {
    struct Dependencies {
        let getState: () -> CameraViewController.AppState
        let getCameraRole: () -> String
        let healthMonitor: HeartbeatScheduler
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
        let heartbeatIntervalDidPush: (Double) -> Void
        let hsvRangeDidPush: (ServerUploader.HSVRangePayload) -> Void
        let shapeGateDidPush: (ServerUploader.ShapeGatePayload) -> Void
        let handleTrackingExposureCap: (String) -> Void
        let currentCaptureHeight: () -> Int
        let applyServerCaptureHeight: (Int) -> Void
        let isPreviewRequested: () -> Bool
        let setPreviewRequested: (Bool) -> Void
        let ensurePreviewUploader: () -> Void
        let resetPreviewUploader: () -> Void
        let startStandbyCapture: () -> Void
        let stopCapture: () -> Void
        let getCalCaptureState: () -> CalCaptureState
        let armCalibrationCapture: () -> Bool
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

        // Settings handlers that touch AVCaptureDevice.lockForConfiguration
        // (capture-height swap, tracking exposure cap) MUST NOT run while
        // a calibration capture cycle is mid-swap, otherwise two parallel
        // activeFormat swaps interleave on sessionQueue and either deadlock
        // or leave activeFormat in an inconsistent state — the suspected
        // root cause of the prior 12 MP swap revert. sync_run starts an
        // audio chirp window; chirp detection is dead while the AV session
        // is stopped for the swap, so reject it for the same reason.
        let calIdle = deps.getCalCaptureState() == .idle

        switch type {
        case "sync_run":
            guard calIdle else {
                commandLog.warning("ws sync_run dropped: calibration capture in flight (state=\(String(describing: self.deps.getCalCaptureState()), privacy: .public))")
                return
            }
            guard let sid = message["sync_id"] as? String,
                  let emitAtS = message["emit_at_s"] as? [Double],
                  let recordDurationS = message["record_duration_s"] as? Double
            else {
                commandLog.error("ws sync_run missing required fields sid=\(message["sync_id"] as? String ?? "-", privacy: .public)")
                return
            }
            DispatchQueue.main.async {
                self.deps.applyMutualSync(sid, emitAtS, recordDurationS)
            }
        case "sync_command":
            if let cmd = message["command"] as? String, cmd == "start" {
                DispatchQueue.main.async {
                    guard let syncId = message["sync_command_id"] as? String else { return }
                    let state = self.deps.getState()
                    // Accept new sync_command from .standby OR from
                    // .timeSyncWaiting (operator re-fired Quick chirp
                    // before the prior 15 s timeout expired). beginTimeSync
                    // cancels the old timeout work and resets pending+anchor
                    // so the swap is clean. Reject during .recording /
                    // .mutualSyncing — those states own the audio pipeline.
                    guard state == .standby || state == .timeSyncWaiting else { return }
                    self.deps.startTimeSync(syncId)
                }
            }
        case "arm":
            if let sid = message["sid"] as? String {
                deps.setCurrentSessionId(sid)
            }
            applyPushedPaths(message["paths"] as? [String])
            if let capStr = message["tracking_exposure_cap"] as? String {
                if calIdle {
                    deps.handleTrackingExposureCap(capStr)
                } else {
                    // Same lock-conflict vector as the settings-path gate;
                    // arm-time exposure cap also calls lockForConfiguration
                    // and would race against an in-flight calibration swap.
                    commandLog.warning("ws arm tracking_exposure_cap dropped: calibration capture in flight")
                }
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
            guard let pushedTimeSync = message["device_time_synced"] as? Bool else {
                commandLog.error("ws settings missing device_time_synced")
                return
            }
            let pushedTimeSyncId = message["device_time_sync_id"] as? String
            deps.updateTimeSyncServerState(pushedTimeSync, pushedTimeSyncId)
            applyPushedPaths(message["paths"] as? [String])
            if let threshold = message["chirp_detect_threshold"] as? Double {
                deps.chirpThresholdDidPush(threshold)
            }
            if let interval = message["heartbeat_interval_s"] as? Double {
                deps.heartbeatIntervalDidPush(interval)
            }
            if let hsv = message["hsv_range"] as? [String: Any],
               let hMin = hsv["h_min"] as? Int,
               let hMax = hsv["h_max"] as? Int,
               let sMin = hsv["s_min"] as? Int,
               let sMax = hsv["s_max"] as? Int,
               let vMin = hsv["v_min"] as? Int,
               let vMax = hsv["v_max"] as? Int {
                deps.hsvRangeDidPush(
                    ServerUploader.HSVRangePayload(
                        h_min: hMin,
                        h_max: hMax,
                        s_min: sMin,
                        s_max: sMax,
                        v_min: vMin,
                        v_max: vMax
                    )
                )
            }
            if let gate = message["shape_gate"] as? [String: Any],
               let aspectMin = gate["aspect_min"] as? Double,
               let fillMin = gate["fill_min"] as? Double {
                deps.shapeGateDidPush(
                    ServerUploader.ShapeGatePayload(
                        aspect_min: aspectMin,
                        fill_min: fillMin
                    )
                )
            }
            if let capStr = message["tracking_exposure_cap"] as? String {
                if calIdle {
                    deps.handleTrackingExposureCap(capStr)
                } else {
                    commandLog.warning("ws tracking_exposure_cap dropped: calibration capture in flight")
                }
            }
            if let pushedH = message["capture_height_px"] as? Int,
               pushedH != deps.currentCaptureHeight() {
                if calIdle {
                    DispatchQueue.main.async {
                        guard self.deps.getState() == .standby else { return }
                        self.deps.applyServerCaptureHeight(pushedH)
                    }
                } else {
                    commandLog.warning("ws capture_height_px=\(pushedH) dropped: calibration capture in flight")
                }
            }
            // Preview-request handler stops/starts AVCaptureSession via
            // startStandbyCapture / stopCapture. A toggle landing while a
            // calibration cycle is mid-swap would interleave session
            // mutations on sessionQueue with pauseAndCaptureHighResStill,
            // which is the most plausible reproduction of the prior
            // "delegate silently never fires" failure mode.
            guard let previewRequested = message["preview_requested"] as? Bool,
                  let calFrameRequested = message["calibration_frame_requested"] as? Bool
            else {
                commandLog.error("ws settings missing preview_requested/calibration_frame_requested")
                return
            }
            if calIdle {
                applyPreviewRequest(previewRequested)
            } else {
                commandLog.warning("ws preview_requested dropped: calibration capture in flight")
            }
            applyCalibrationFrameRequest(calFrameRequested)
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
        guard requested else { return }
        // armCalibrationCapture enforces:
        //   - calCaptureState == .idle (no double-arm during in-flight swap)
        //   - state ∉ {.recording, .timeSyncWaiting} (defense in depth with
        //     consume-time check; arm-time block is the cleanest UX since
        //     the operator gets a single rejection point per request)
        guard deps.armCalibrationCapture() else {
            commandLog.warning("ws calibration_frame_requested rejected: state=\(String(describing: self.deps.getCalCaptureState()), privacy: .public) appState=\(String(describing: self.deps.getState()), privacy: .public)")
            return
        }
        if deps.getState() == .standby && !deps.isPreviewRequested() {
            deps.startStandbyCapture()
        }
    }
}
