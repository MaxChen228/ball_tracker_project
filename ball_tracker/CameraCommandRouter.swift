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
        /// Quick sync (single-emitter, N-listener) record+upload trigger.
        /// `(syncId, isEmitter, emitAtS, recordDurationS)`.
        let applyQuickSync: (String, Bool, [Double], Double) -> Void
        /// Adopt the server-solved quick-sync anchor pushed via
        /// `quick_sync_applied`. `(syncId, anchorTimestampS)`.
        let adoptQuickSyncAnchor: (String, Double) -> Void
        let applyRemoteArm: () -> Void
        let applyRemoteDisarm: () -> Void
        let updateTimeSyncServerState: (Bool, String?) -> Void
        let chirpThresholdDidPush: (Double) -> Void
        let heartbeatIntervalDidPush: (Double) -> Void
        let hsvRangeDidPush: (ServerUploader.HSVRangePayload) -> Void
        let shapeGateDidPush: (ServerUploader.ShapeGatePayload) -> Void
        let handleTrackingExposureCap: (String) -> Void
        let currentCaptureHeight: () -> Int?
        let applyServerCaptureHeight: (Int) -> Void
        let isPreviewRequested: () -> Bool
        let setPreviewRequested: (Bool) -> Void
        let ensurePreviewUploader: () -> Void
        let resetPreviewUploader: () -> Void
        let startStandbyCapture: () -> Void
        let stopCapture: () -> Void
        let getCalCaptureState: () -> CalCaptureState
        /// Mirror of `getCalCaptureState != .idle` for the ChArUco
        /// IntrinsicsCalibrationViewController flow — same WS commands
        /// must be rejected while the VC owns the camera. Independent
        /// flag (not folded into CalCaptureState) so the 12 MP swap
        /// state machine isn't accidentally driven by VC presentation.
        let getIntrinsicsCalibrationActive: () -> Bool
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
        let calIdle = deps.getCalCaptureState() == .idle && !deps.getIntrinsicsCalibrationActive()

        switch type {
        case "sync_run":
            guard calIdle else {
                commandLog.warning("ws sync_run dropped: calibration capture in flight (state=\(String(describing: self.deps.getCalCaptureState()), privacy: .public))")
                return
            }
            guard let sid = message["sync_id"] as? String else {
                commandLog.error("ws sync_run missing required field sync_id")
                return
            }
            var missing: [String] = []
            let emitAtS = message["emit_at_s"] as? [Double]
            let recordDurationS = message["record_duration_s"] as? Double
            if emitAtS == nil { missing.append("emit_at_s") }
            if recordDurationS == nil { missing.append("record_duration_s") }
            guard let emitAtS, let recordDurationS, missing.isEmpty else {
                commandLog.error("ws sync_run sid=\(sid, privacy: .public) missing required fields: \(missing.joined(separator: ","), privacy: .public)")
                return
            }
            // Atomic-drop on out-of-contract values. server is the single
            // source of truth for sync params; an empty emit_at_s or a
            // record_duration_s < 1.0 is a server bug, not a thing we
            // silently clamp on the device side (which would mask the bug
            // and quietly run with different timing than the server logs
            // claim). Fail loud at the route layer, same pattern as the
            // other missing-key drops above.
            guard !emitAtS.isEmpty else {
                commandLog.error("ws sync_run sid=\(sid, privacy: .public) atomic-drop: emit_at_s is empty (server bug)")
                return
            }
            guard recordDurationS >= 1.0 else {
                commandLog.error("ws sync_run sid=\(sid, privacy: .public) atomic-drop: record_duration_s=\(recordDurationS) below 1.0 floor")
                return
            }
            DispatchQueue.main.async {
                self.deps.applyMutualSync(sid, emitAtS, recordDurationS)
            }
        case "sync_quick_run":
            // Same calibration-capture gate as sync_run: quick sync opens an
            // audio chirp window; chirp detection is dead while the AV
            // session is stopped for a 12 MP swap.
            guard calIdle else {
                commandLog.warning("ws sync_quick_run dropped: calibration capture in flight (state=\(String(describing: self.deps.getCalCaptureState()), privacy: .public))")
                return
            }
            guard let sid = message["sync_id"] as? String else {
                commandLog.error("ws sync_quick_run missing required field sync_id")
                return
            }
            var missing: [String] = []
            let isEmitter = message["is_emitter"] as? Bool
            let emitAtS = message["emit_at_s"] as? [Double]
            let recordDurationS = message["record_duration_s"] as? Double
            if isEmitter == nil { missing.append("is_emitter") }
            if emitAtS == nil { missing.append("emit_at_s") }
            if recordDurationS == nil { missing.append("record_duration_s") }
            guard let isEmitter, let emitAtS, let recordDurationS, missing.isEmpty else {
                commandLog.error("ws sync_quick_run sid=\(sid, privacy: .public) missing required fields: \(missing.joined(separator: ","), privacy: .public)")
                return
            }
            // Atomic-drop on out-of-contract values (same as sync_run). The
            // server pushes emit_at_s = emit_a_at_s (non-empty) to every cam
            // regardless of role, so an empty array is a server bug — fail
            // loud rather than silently running with no emission schedule.
            guard !emitAtS.isEmpty else {
                commandLog.error("ws sync_quick_run sid=\(sid, privacy: .public) atomic-drop: emit_at_s is empty (server bug)")
                return
            }
            guard recordDurationS >= 1.0 else {
                commandLog.error("ws sync_quick_run sid=\(sid, privacy: .public) atomic-drop: record_duration_s=\(recordDurationS) below 1.0 floor")
                return
            }
            DispatchQueue.main.async {
                self.deps.applyQuickSync(sid, isEmitter, emitAtS, recordDurationS)
            }
        case "quick_sync_applied":
            // Server solved the quick sync and is pushing this cam its own
            // anchor (chirp-arrival PTS on this cam's clock) so iOS adopts
            // it as lastSyncAnchor. Atomic-drop on missing required fields.
            //
            // Gate against calibration capture + audio-owning states:
            // adopting an anchor here writes lastSyncAnchor + updateTimeSyncId
            // which the next heartbeat reports. While `.mutualSyncing` /
            // `.timeSyncWaiting` are in flight, those flows hold the
            // invariant that heartbeats report nil until they resolve
            // (startMutualSync L212 / beginTimeSync L141 clear anchor
            // explicitly). Overwriting from a stray quick_sync_applied
            // would silently break that invariant — the next heartbeat
            // would advertise an anchor for a different sync_id than the
            // active flow expects. Atomic-drop here; the operator can
            // re-apply once standby/recording.
            guard let sid = message["sync_id"] as? String else {
                commandLog.error("ws quick_sync_applied missing required field sync_id")
                return
            }
            guard let anchorTs = message["sync_anchor_timestamp_s"] as? Double else {
                commandLog.error("ws quick_sync_applied sid=\(sid, privacy: .public) missing required field sync_anchor_timestamp_s")
                return
            }
            guard calIdle else {
                commandLog.warning("ws quick_sync_applied dropped: calibration capture in flight (state=\(String(describing: self.deps.getCalCaptureState()), privacy: .public))")
                return
            }
            let appState = deps.getState()
            guard appState != .mutualSyncing && appState != .timeSyncWaiting else {
                commandLog.warning("ws quick_sync_applied dropped: app state=\(String(describing: appState), privacy: .public) owns the anchor; would break the active flow's nil-heartbeat invariant")
                return
            }
            DispatchQueue.main.async {
                self.deps.adoptQuickSyncAnchor(sid, anchorTs)
            }
        case "sync_command":
            // server lockstep: every sync_command WS push carries
            // {command, sync_command_id}. Missing/unknown values are
            // schema drift, not legacy variants — atomic-drop and fail
            // loud, same pattern as sync_run above.
            guard let cmd = message["command"] as? String else {
                commandLog.error("ws sync_command missing required field command")
                return
            }
            guard cmd == "start" else {
                commandLog.error("ws sync_command unknown command=\(cmd, privacy: .public) — atomic-drop")
                return
            }
            guard let syncId = message["sync_command_id"] as? String else {
                commandLog.error("ws sync_command cmd=start missing required field sync_command_id")
                return
            }
            DispatchQueue.main.async {
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
            let camId = message["camera_id"] as? String ?? "unknown"
            // server lockstep: main.py::_settings_message_for unconditionally writes device_time_synced (default false). Missing key = schema drift, drop the whole settings update.
            guard let pushedTimeSync = message["device_time_synced"] as? Bool else {
                commandLog.error("ws settings missing device_time_synced cam=\(camId, privacy: .public)")
                return
            }
            // Preview-request handler stops/starts AVCaptureSession via
            // startStandbyCapture / stopCapture. A toggle landing while a
            // calibration cycle is mid-swap would interleave session
            // mutations on sessionQueue with pauseAndCaptureHighResStill,
            // which is the most plausible reproduction of the prior
            // "delegate silently never fires" failure mode. Guard at the
            // top of the handler so missing server-required scaffolding
            // fields (device_time_synced + preview_requested +
            // calibration_frame_requested — all three written
            // unconditionally by main.py::_settings_message_for alongside hsv_range and
            // shape_gate which are gated below) atomic-drop the whole
            // settings update — never apply paths/exposure/hsv half-way
            // before bailing out. Detection-critical hsv_range and
            // shape_gate get the same atomic-drop treatment further down;
            // the remaining tunables (chirp / heartbeat / exposure / etc.)
            // stay opt-in so legacy / partial pushes don't break.
            guard let previewRequested = message["preview_requested"] as? Bool,
                  let calFrameRequested = message["calibration_frame_requested"] as? Bool
            else {
                commandLog.error("ws settings missing preview_requested/calibration_frame_requested cam=\(camId, privacy: .public)")
                return
            }
            // Detection-critical fail-loud: hsv_range and shape_gate are
            // unconditionally written by the server and define the
            // detection contract (PR #93 alignment scorecard). Missing or
            // malformed → atomic-drop the whole settings update; do not
            // silently keep stale local values.
            guard let hsvDict = message["hsv_range"] as? [String: Any],
                  let hsvHMin = hsvDict["h_min"] as? Int,
                  let hsvHMax = hsvDict["h_max"] as? Int,
                  let hsvSMin = hsvDict["s_min"] as? Int,
                  let hsvSMax = hsvDict["s_max"] as? Int,
                  let hsvVMin = hsvDict["v_min"] as? Int,
                  let hsvVMax = hsvDict["v_max"] as? Int
            else {
                commandLog.error("ws settings missing/malformed hsv_range cam=\(camId, privacy: .public)")
                return
            }
            guard let gateDict = message["shape_gate"] as? [String: Any],
                  let gateAspectMin = gateDict["aspect_min"] as? Double,
                  let gateFillMin = gateDict["fill_min"] as? Double
            else {
                commandLog.error("ws settings missing/malformed shape_gate cam=\(camId, privacy: .public)")
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
            // hsv_range / shape_gate validated up-front; apply unconditionally.
            deps.hsvRangeDidPush(
                ServerUploader.HSVRangePayload(
                    h_min: hsvHMin,
                    h_max: hsvHMax,
                    s_min: hsvSMin,
                    s_max: hsvSMax,
                    v_min: hsvVMin,
                    v_max: hsvVMax
                )
            )
            deps.shapeGateDidPush(
                ServerUploader.ShapeGatePayload(
                    aspect_min: gateAspectMin,
                    fill_min: gateFillMin
                )
            )
            if let capStr = message["tracking_exposure_cap"] as? String {
                if calIdle {
                    deps.handleTrackingExposureCap(capStr)
                } else {
                    commandLog.warning("ws tracking_exposure_cap dropped: calibration capture in flight")
                }
            }
            if let pushedH = message["capture_height_px"] as? Int {
                // Skip the drift check entirely when we can't read our own
                // current height — substituting `AppSettings.captureHeightFixed`
                // would silently mask real drift between server and device.
                if let currentH = deps.currentCaptureHeight(), pushedH != currentH {
                    if calIdle {
                        DispatchQueue.main.async {
                            guard self.deps.getState() == .standby else { return }
                            self.deps.applyServerCaptureHeight(pushedH)
                        }
                    } else {
                        commandLog.warning("ws capture_height_px=\(pushedH) dropped: calibration capture in flight")
                    }
                } else if deps.currentCaptureHeight() == nil {
                    commandLog.warning("ws capture_height_px=\(pushedH) dropped: current capture height unavailable")
                }
            }
            if calIdle {
                applyPreviewRequest(previewRequested)
            } else {
                commandLog.warning("ws preview_requested dropped: calibration capture in flight")
            }
            applyCalibrationFrameRequest(calFrameRequested)
            DispatchQueue.main.async { self.deps.refreshModeLabel() }
        case "cam_id_pending":
            // Phase 0 PR3 device-uuid handshake: server has no assignment
            // for this device yet. iOS sits idle on the WS until the
            // dashboard operator promotes us via /devices/assign and the
            // server sends `cam_id_assigned`. No state mutation needed —
            // the post-handshake flow (settings / arm / etc.) simply
            // hasn't started yet. Log for diagnosis.
            let uuid = (message["device_uuid"] as? String) ?? "?"
            commandLog.info("ws cam_id_pending device_uuid=\(uuid, privacy: .public) — awaiting dashboard assignment")
        case "cam_id_assigned":
            // Server resolved (or just resolved) device_uuid → camera_id.
            // The cached `cameraRole` UserDefaults value is the local
            // label; if it disagrees with what the server assigned, the
            // server's value is authoritative for the live session. This
            // log lets operators spot a mismatch before relabelling the
            // device. Future PR: thread the assigned value back into the
            // coordinator so the cameraRole field becomes server-driven
            // rather than UserDefaults-driven.
            let assignedCam = (message["camera_id"] as? String) ?? "?"
            let uuid = (message["device_uuid"] as? String) ?? "?"
            commandLog.info("ws cam_id_assigned device_uuid=\(uuid, privacy: .public) camera_id=\(assignedCam, privacy: .public) local=\(self.deps.getCameraRole(), privacy: .public)")
        default:
            break
        }
    }

    private func applyPushedPaths(_ rawPaths: [String]?) {
        guard let rawPaths else { return }
        let parsed = Set(rawPaths.compactMap(ServerUploader.DetectionPath.init(rawValue:)))
        guard !parsed.isEmpty else {
            commandLog.error("ws paths schema drift: rawPaths=\(rawPaths, privacy: .public) — atomic-drop")
            return
        }
        guard parsed.count == rawPaths.count else {
            commandLog.error("ws paths partial-unknown rawPaths=\(rawPaths, privacy: .public) parsed=\(parsed.count) — atomic-drop")
            return
        }
        deps.setCurrentSessionPaths(parsed)
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
