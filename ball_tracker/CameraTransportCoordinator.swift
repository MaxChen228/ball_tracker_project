import CoreVideo
import Foundation
import QuartzCore
import UIKit
import os

private let transportLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.transport")

/// Lifecycle of a single auto-calibration capture cycle. Replaces the
/// older single `calibrationFrameCaptureArmed: Bool` flag because the
/// 12 MP photo-format swap is multi-phase and any settings push that
/// triggers `lockForConfiguration` mid-swap can clobber the rollback
/// target — that's the suspected root cause of the prior 12 MP swap
/// revert (delegate silently never fires). Every WS settings handler
/// gates on `calCaptureState == .idle` so only one configuration mutator
/// runs against the AVCaptureSession at a time.
enum CalCaptureState {
    case idle           // no calibration capture in flight
    case swappingTo     // server requested capture; will swap activeFormat → 12 MP next frame callback
    case capturing      // 12 MP format active, AVCapturePhotoOutput.capturePhoto in flight (runtime owns the swap-back internally)
}

/// Stable per-device identity for keying ChArUco intrinsics server-side.
/// `identifierForVendor` survives app launches on the same device + vendor;
/// reinstalling the app rotates it, which is acceptable (operator reruns
/// ChArUco once after reinstall). `model` is the machine identifier from
/// sysctl (e.g. "iPhone15,3") so the dashboard can show a human-readable
/// hint alongside the UUID.
enum DeviceIdentity {
    static let id: String = UIDevice.current.identifierForVendor?.uuidString
        ?? "unknown-\(UUID().uuidString)"

    static let model: String = {
        var sysinfo = utsname()
        uname(&sysinfo)
        let raw = withUnsafePointer(to: &sysinfo.machine) { ptr in
            ptr.withMemoryRebound(to: CChar.self, capacity: Int(_SYS_NAMELEN)) {
                String(cString: $0)
            }
        }
        return raw.isEmpty ? UIDevice.current.model : raw
    }()
}

final class CameraTransportCoordinator: NSObject {
    struct Dependencies {
        let getState: () -> CameraViewController.AppState
        let getCurrentSessionId: () -> String?
        let getCurrentSessionPaths: () -> Set<ServerUploader.DetectionPath>
        let setCurrentSessionId: (String?) -> Void
        let setCurrentSessionPaths: (Set<ServerUploader.DetectionPath>) -> Void
        let getCurrentTargetFps: () -> Double
        let getCurrentCaptureHeight: () -> Int
        let getSyncId: () -> String?
        let getSyncAnchorTimestampS: () -> Double?
        let getChirpSnapshot: () -> AudioChirpDetector.Snapshot?
        let startTimeSync: (String) -> Void
        let applyMutualSync: (String, [Double], Double) -> Void
        let applyRemoteArm: () -> Void
        let applyRemoteDisarm: () -> Void
        let updateTimeSyncServerState: (Bool, String?) -> Void
        let applyChirpThreshold: (Double) -> Void
        let applyHeartbeatInterval: (Double) -> Void
        let applyHSVRange: (ServerUploader.HSVRangePayload) -> Void
        let applyShapeGate: (ServerUploader.ShapeGatePayload) -> Void
        let applyTrackingExposureCap: (String, Double) -> Void
        let applyServerCaptureHeight: (Int) -> Void
        let startStandbyCapture: () -> Void
        let stopCapture: () -> Void
        let refreshModeLabel: () -> Void
    }

    private let healthMonitor: HeartbeatScheduler
    private let dependencies: Dependencies

    private var uploader: ServerUploader
    private var serverConfig: ServerUploader.ServerConfig
    private var cameraRole: String
    private var ws: ServerWebSocketConnection?
    private var previewUploader: PreviewUploader?
    private var frameDispatcher: LiveFrameDispatcher?
    private var commandRouter: CameraCommandRouter!

    private var lastServerChirpThreshold: Double?
    private var lastServerHeartbeatInterval: Double?
    private var lastServerTrackingExposureCapMode: ServerUploader.TrackingExposureCapMode?
    private var previewRequestedByServer: Bool = false
    private var calCaptureState: CalCaptureState = .idle

    init(
        healthMonitor: HeartbeatScheduler,
        uploader: ServerUploader,
        serverConfig: ServerUploader.ServerConfig,
        cameraRole: String,
        dependencies: Dependencies
    ) {
        self.healthMonitor = healthMonitor
        self.uploader = uploader
        self.serverConfig = serverConfig
        self.cameraRole = cameraRole
        self.dependencies = dependencies
        super.init()
        self.commandRouter = buildCommandRouter()
        wireHealthMonitorHeartbeat()
    }

    var isPreviewRequested: Bool { previewRequestedByServer }
    var hasPendingCalibrationFrameCaptureRequest: Bool { calCaptureState != .idle }

    func connect() {
        guard let baseURL = webSocketURL() else { return }
        if ws == nil {
            let connection = ServerWebSocketConnection(baseURL: baseURL, cameraId: cameraRole)
            connection.delegate = self
            ws = connection
            frameDispatcher = LiveFrameDispatcher(
                connection: connection,
                cameraId: cameraRole,
                currentSessionId: { [weak self] in self?.dependencies.getCurrentSessionId() },
                currentPaths: { [weak self] in self?.dependencies.getCurrentSessionPaths() ?? [] }
            )
        }
        ws?.connect(initialHello: [
            "type": "hello",
            "cam": cameraRole,
            "device_id": DeviceIdentity.id,
            "device_model": DeviceIdentity.model,
            "session_id": dependencies.getCurrentSessionId() as Any,
            "time_sync_id": dependencies.getSyncId() as Any,
            "sync_anchor_timestamp_s": dependencies.getSyncAnchorTimestampS() as Any,
        ])
    }

    func disconnect() {
        ws?.disconnect()
        ws = nil
        frameDispatcher = nil
    }

    func updateConnection(
        serverConfig: ServerUploader.ServerConfig,
        uploader: ServerUploader,
        cameraRole: String,
        reconnect: Bool
    ) {
        let endpointChanged = self.serverConfig.serverIP != serverConfig.serverIP
            || self.serverConfig.serverPort != serverConfig.serverPort
        let roleChanged = self.cameraRole != cameraRole

        self.serverConfig = serverConfig
        self.uploader = uploader
        self.cameraRole = cameraRole
        previewUploader?.updateUploader(uploader)
        if roleChanged {
            previewUploader = nil
            calCaptureState = .idle
        }

        if endpointChanged || roleChanged {
            disconnect()
            commandRouter = buildCommandRouter()
            if reconnect {
                connect()
            }
        }
    }

    func pushPreviewFrame(_ pixelBuffer: CVPixelBuffer) {
        previewUploader?.pushFrame(pixelBuffer)
    }

    func dispatchLiveFrame(_ frame: ServerUploader.FramePayload) {
        frameDispatcher?.dispatchFrame(frame)
    }

    func dispatchLiveCycleEnd(sessionId: String, reason: String) {
        frameDispatcher?.dispatchCycleEnd(sessionId: sessionId, reason: reason)
    }

    /// WS settings push has armed a fresh calibration capture. Returns
    /// false if a previous cycle is still in flight or the device is in
    /// a state that forbids the swap (recording, time-sync waiting); the
    /// router should drop the request rather than queue it (server retries).
    func armCalibrationCapture(whileIn state: CameraViewController.AppState) -> Bool {
        guard calCaptureState == .idle,
              state != .recording,
              state != .timeSyncWaiting else { return false }
        calCaptureState = .swappingTo
        return true
    }

    /// Frame callback asks: should we kick off the photo-format swap NOW?
    /// Returns true exactly once per armed cycle, transitioning to
    /// `.capturing` so concurrent frame callbacks can't double-fire.
    func consumeCalibrationFrameCaptureRequest(
        whileIn state: CameraViewController.AppState
    ) -> Bool {
        guard calCaptureState == .swappingTo,
              state != .recording,
              state != .timeSyncWaiting else { return false }
        calCaptureState = .capturing
        return true
    }

    /// Runtime advances state to `.idle` when its completion fires —
    /// either delegate-success or timeout-rollback path. The runtime
    /// owns the swap-back internally (rollbackToFormat), so callers
    /// only see two transitions visible at the coordinator boundary:
    /// .swappingTo → .capturing (consume) and .capturing → .idle (mark).
    func markCalIdle() { calCaptureState = .idle }

    private func webSocketURL() -> URL? {
        guard
            let host = serverConfig.serverIP.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed),
            !host.isEmpty
        else { return nil }
        var comps = URLComponents()
        comps.scheme = "ws"
        comps.host = host
        comps.port = Int(serverConfig.serverPort)
        return comps.url
    }

    private func wireHealthMonitorHeartbeat() {
        // Enable battery monitoring so UIDevice.batteryLevel returns real
        // readings (defaults to -1 with monitoring off). Idempotent; safe to
        // call every time the coordinator wires up.
        UIDevice.current.isBatteryMonitoringEnabled = true
        healthMonitor.sendWSHeartbeat = { [weak self] timeSyncId in
            guard let self else { return }
            let anchorTs = self.dependencies.getSyncAnchorTimestampS()
            // Active diagnostic: surface every heartbeat's sync state so
            // Console.app logs reveal whether the iOS side is "forgetting"
            // the anchor across state transitions. Kept on while
            // Quick-Chirp anchor persistence is still being tuned.
            transportLog.info("heartbeat cam=\(self.cameraRole, privacy: .public) time_sync_id=\(timeSyncId ?? "nil", privacy: .public) anchor_ts=\(anchorTs ?? .nan)")
            var payload: [String: Any] = [
                "type": "heartbeat",
                "cam": self.cameraRole,
                "device_id": DeviceIdentity.id,
                "device_model": DeviceIdentity.model,
                "t_session_s": CACurrentMediaTime(),
                "time_sync_id": timeSyncId as Any,
                "sync_anchor_timestamp_s": anchorTs as Any,
            ]
            let device = UIDevice.current
            let level = device.batteryLevel
            if level >= 0 {
                payload["battery_level"] = Double(level)
            }
            switch device.batteryState {
            case .unknown:   payload["battery_state"] = "unknown"
            case .unplugged: payload["battery_state"] = "unplugged"
            case .charging:  payload["battery_state"] = "charging"
            case .full:      payload["battery_state"] = "full"
            @unknown default: break
            }
            if self.dependencies.getState() == .timeSyncWaiting,
               let s = self.dependencies.getChirpSnapshot() {
                payload["sync_telemetry"] = [
                    "mode": "quick_chirp",
                    "armed": s.armed,
                    "input_rms": s.inputRMS,
                    "input_peak": s.inputPeak,
                    "up_peak": s.lastPeak,
                    "down_peak": s.lastDownPeak,
                    "cfar_up_floor": s.cfarUpFloor,
                    "cfar_down_floor": s.cfarDownFloor,
                    "threshold": s.threshold,
                    "pending_up": s.pendingUp,
                ]
            }
            self.ws?.send(payload)
        }
    }

    private func buildCommandRouter() -> CameraCommandRouter {
        CameraCommandRouter(
            dependencies: .init(
                getState: { [weak self] in self?.dependencies.getState() ?? .standby },
                getCameraRole: { [weak self] in self?.cameraRole ?? "?" },
                healthMonitor: healthMonitor,
                getCurrentSessionPaths: { [weak self] in self?.dependencies.getCurrentSessionPaths() ?? [] },
                setCurrentSessionId: { [weak self] in self?.dependencies.setCurrentSessionId($0) },
                setCurrentSessionPaths: { [weak self] in self?.dependencies.setCurrentSessionPaths($0) },
                refreshModeLabel: { [weak self] in self?.dependencies.refreshModeLabel() },
                startTimeSync: { [weak self] syncId in self?.dependencies.startTimeSync(syncId) },
                applyMutualSync: { [weak self] syncId, emitAtS, recordDurationS in self?.dependencies.applyMutualSync(syncId, emitAtS, recordDurationS) },
                applyRemoteArm: { [weak self] in self?.dependencies.applyRemoteArm() },
                applyRemoteDisarm: { [weak self] in self?.dependencies.applyRemoteDisarm() },
                updateTimeSyncServerState: { [weak self] confirmed, syncId in
                    self?.dependencies.updateTimeSyncServerState(confirmed, syncId)
                },
                chirpThresholdDidPush: { [weak self] threshold in
                    self?.applyPushedChirpThreshold(threshold)
                },
                heartbeatIntervalDidPush: { [weak self] interval in
                    self?.applyPushedHeartbeatInterval(interval)
                },
                hsvRangeDidPush: { [weak self] hsvRange in
                    self?.dependencies.applyHSVRange(hsvRange)
                },
                shapeGateDidPush: { [weak self] shapeGate in
                    self?.dependencies.applyShapeGate(shapeGate)
                },
                handleTrackingExposureCap: { [weak self] cap in
                    guard let self else { return }
                    self.applyPushedTrackingExposureCap(cap)
                },
                currentCaptureHeight: { [weak self] in
                    self?.dependencies.getCurrentCaptureHeight() ?? AppSettings.captureHeightFixed
                },
                applyServerCaptureHeight: { [weak self] height in
                    self?.dependencies.applyServerCaptureHeight(height)
                },
                isPreviewRequested: { [weak self] in self?.previewRequestedByServer ?? false },
                setPreviewRequested: { [weak self] in self?.previewRequestedByServer = $0 },
                ensurePreviewUploader: { [weak self] in self?.ensurePreviewUploader() },
                resetPreviewUploader: { [weak self] in self?.previewUploader?.reset() },
                startStandbyCapture: { [weak self] in self?.dependencies.startStandbyCapture() },
                stopCapture: { [weak self] in self?.dependencies.stopCapture() },
                getCalCaptureState: { [weak self] in self?.calCaptureState ?? .idle },
                armCalibrationCapture: { [weak self] in
                    guard let self else { return false }
                    return self.armCalibrationCapture(whileIn: self.dependencies.getState())
                }
            )
        )
    }

    private func ensurePreviewUploader() {
        if previewUploader == nil {
            previewUploader = PreviewUploader(uploader: uploader, cameraId: cameraRole)
        }
    }

    private func applyPushedChirpThreshold(_ threshold: Double) {
        guard lastServerChirpThreshold != threshold else { return }
        lastServerChirpThreshold = threshold
        DispatchQueue.main.async {
            self.dependencies.applyChirpThreshold(threshold)
            transportLog.info("quick-chirp threshold hot-applied from server: \(threshold)")
        }
    }

    private func applyPushedHeartbeatInterval(_ interval: Double) {
        guard lastServerHeartbeatInterval != interval else { return }
        lastServerHeartbeatInterval = interval
        DispatchQueue.main.async {
            self.dependencies.applyHeartbeatInterval(interval)
            transportLog.info("heartbeat interval hot-applied: \(interval)s")
        }
    }

    private func applyPushedTrackingExposureCap(_ modeStr: String) {
        let exposureMode = ServerUploader.TrackingExposureCapMode(rawValue: modeStr) ?? .frameDuration
        guard lastServerTrackingExposureCapMode != exposureMode else { return }
        lastServerTrackingExposureCapMode = exposureMode
        dependencies.applyTrackingExposureCap(modeStr, dependencies.getCurrentTargetFps())
    }
}

extension CameraTransportCoordinator: ServerWebSocketDelegate {
    func webSocketDidConnect(_ connection: ServerWebSocketConnection) {}

    func webSocketDidDisconnect(_ connection: ServerWebSocketConnection, reason: String?) {
        // Guard against stale callbacks: when role changes we nil `ws` and
        // immediately create a new connection. The old connection's close
        // notification arrives async — ignore it so it can't reset state
        // (previewRequested, previewUploader) that the new connection has
        // already configured.
        guard connection === ws else { return }
        commandRouter.didDisconnect()
    }

    func webSocket(_ connection: ServerWebSocketConnection, didReceive message: [String : Any]) {
        commandRouter.handle(message: message)
    }
}
