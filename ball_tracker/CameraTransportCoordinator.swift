import CoreVideo
import Foundation
import QuartzCore
import os

private let transportLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.transport")

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
        let applyMutualSyncThreshold: (Double) -> Void
        let applyHeartbeatInterval: (Double) -> Void
        let applyTrackingExposureCap: (String, Double) -> Void
        let applyServerCaptureHeight: (Int) -> Void
        let startStandbyCapture: () -> Void
        let stopCapture: () -> Void
        let refreshModeLabel: () -> Void
    }

    private let healthMonitor: ServerHealthMonitor
    private let dependencies: Dependencies

    private var uploader: ServerUploader
    private var serverConfig: ServerUploader.ServerConfig
    private var cameraRole: String
    private var ws: ServerWebSocketConnection?
    private var previewUploader: PreviewUploader?
    private var frameDispatcher: LiveFrameDispatcher?
    private var commandRouter: CameraCommandRouter!

    private var lastServerChirpThreshold: Double?
    private var lastServerMutualThreshold: Double?
    private var lastServerHeartbeatInterval: Double?
    private var lastServerTrackingExposureCapMode: ServerUploader.TrackingExposureCapMode?
    private var previewRequestedByServer: Bool = false
    private var calibrationFrameCaptureArmed: Bool = false

    init(
        healthMonitor: ServerHealthMonitor,
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
    var hasPendingCalibrationFrameCaptureRequest: Bool { calibrationFrameCaptureArmed }

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
            calibrationFrameCaptureArmed = false
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

    func consumeCalibrationFrameCaptureRequest(
        whileIn state: CameraViewController.AppState
    ) -> Bool {
        guard calibrationFrameCaptureArmed,
              state != .recording,
              state != .timeSyncWaiting else { return false }
        calibrationFrameCaptureArmed = false
        return true
    }

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
        healthMonitor.sendWSHeartbeat = { [weak self] timeSyncId in
            guard let self else { return }
            var payload: [String: Any] = [
                "type": "heartbeat",
                "cam": self.cameraRole,
                "t_session_s": CACurrentMediaTime(),
                "time_sync_id": timeSyncId as Any,
                "sync_anchor_timestamp_s": self.dependencies.getSyncAnchorTimestampS() as Any,
            ]
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
                mutualSyncThresholdDidPush: { [weak self] threshold in
                    self?.applyPushedMutualSyncThreshold(threshold)
                },
                heartbeatIntervalDidPush: { [weak self] interval in
                    self?.applyPushedHeartbeatInterval(interval)
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
                isCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed ?? false },
                setCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed = $0 }
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

    private func applyPushedMutualSyncThreshold(_ threshold: Double) {
        guard lastServerMutualThreshold != threshold else { return }
        lastServerMutualThreshold = threshold
        dependencies.applyMutualSyncThreshold(threshold)
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
