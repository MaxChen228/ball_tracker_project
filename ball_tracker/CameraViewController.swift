import UIKit
import AVFoundation
import AudioToolbox
import CoreMedia
import CoreVideo
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Main camera view. State machine:
/// - STANDBY: live preview, chirp detector off, no recording
/// - TIME_SYNC_WAITING: listens on the mic for the reference chirp, saves
///   session-clock PTS of the peak as the anchor (manual 時間校正 only)
/// - RECORDING: dashboard-armed; H.264 clip being written to disk
/// - UPLOADING: cycle persisted, handed to the upload queue
///
/// The phone is a pure capture client — no ball detection runs on-device.
/// Dashboard arm goes straight to `.recording`; dashboard stop (or server
/// session timeout) is the sole exit back to standby. Server ingests the
/// MOV and does HSV detection + triangulation.
///
/// Heavy side concerns live in dedicated helpers:
/// - `ServerHealthMonitor` owns the 1 Hz heartbeat, backoff, and
///   "last contact" tick timer.
/// - `PayloadUploadQueue` owns the cached-pitch upload worker.
final class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    enum AppState {
        case standby
        case timeSyncWaiting
        case mutualSyncing
        case recording
        case uploading
    }

    // Adaptive capture rate. Idle / time-sync runs at 60 fps to keep the
    // sensor + ISP cool and save battery; `.recording` switches to 240 fps
    // so the 8 ms A/B pair window gets sub-frame resolution server-side.
    // The format swap costs ~300-500 ms (stopRunning → activeFormat →
    // startRunning) so we only do it at state boundaries.
    private let standbyFps: Double = 60
    private let trackingFps: Double = 240

    // `.userInitiated` QoS is required for 240 fps frame delivery. With default
    // QoS the queue can be throttled by the scheduler which amplifies any
    // detection stalls into dropped-frame cascades (AVCaptureVideoDataOutput
    // has `alwaysDiscardsLateVideoFrames = true`).
    private let processingQueue = DispatchQueue(label: "camera.frame.queue", qos: .userInitiated)
    nonisolated(unsafe) private let frameStateBox = FrameStateBox()
    nonisolated(unsafe) private let frameProcessingState = CameraFrameProcessingState()
    nonisolated(unsafe) private var captureQueueCameraRole: String = "A"
    nonisolated(unsafe) private var captureQueueUploader: ServerUploader!
    nonisolated(unsafe) private var captureQueueRecordingWorkflow: CameraRecordingWorkflow!
    nonisolated(unsafe) private var captureQueueTransportCoordinator: CameraTransportCoordinator!
    private var captureRuntime: CameraCaptureRuntime!
    private var stateController: CameraStateController!
    private var state: AppState { stateController.currentState }

    // Collaborators.
    private var settings: AppSettings!
    private var uploader: ServerUploader!
    private var serverConfig: ServerUploader.ServerConfig!
    private var healthMonitor: ServerHealthMonitor!
    private var syncCoordinator: CameraSyncCoordinator!
    private var statusPresenter: CameraStatusPresenter!
    private var recordingWorkflow: CameraRecordingWorkflow!
    private var transportCoordinator: CameraTransportCoordinator!
    private var overlayView: CameraMonitorOverlayView!

    // UI state.
    private var lastUploadStatusText: String = ""

    // The `.recording` bootstrap flag and the arm-time session snapshot both
    // live inside `stateController`; duplicating them here would create a
    // second source of truth across queues.

    // Remote-control state (driven by WS heartbeat / settings traffic).
    /// Last (command, sync_id) tuple we acted on. Plain "arm" / "disarm"
    /// use a nil sync_id; `"sync_run"` carries the server-minted run id so
    /// back-to-back runs (same command string, different run) still fire.
    /// Server-minted pairing key for the currently armed session. Read
    /// off WS arm/settings traffic; tagged onto every recording that starts
    /// while the session is armed. Nil when the server has no active
    /// session. iPhones never mint this themselves.
    private var currentSessionId: String?

    private let captureTelemetryLock = NSLock()
    private var appliedCaptureWidthPx: Int = 1920
    private var appliedCaptureHeightPx: Int = 1080
    private var appliedCaptureFps: Double = 60
    private var appliedFormatFovDeg: Double?
    private var appliedFormatIndex: Int?
    private var appliedFormatIsVideoBinned: Bool?
    private var appliedMaxExposureS: Double?

    // Live detection is advisory only. It still feeds WS streaming and the
    // local fallback path if clip writing fails.
    nonisolated(unsafe) private let detectionPool = ConcurrentDetectionPool(maxConcurrency: 3)
    private let detectionStateLock = NSLock()
    /// Accumulated per-frame detection results for the current cycle.
    /// Drained at cycle-complete and either discarded (mode-one) or
    /// attached to the upload payload (mode-two).
    private var detectionFramesBuffer: [ServerUploader.FramePayload] = []

    // Most recently recovered chirp anchor — session-clock PTS of the
    // chirp peak from the mic matched filter. Stamped onto outgoing
    // payloads as `sync_anchor_timestamp_s`. Nil until the user completes
    // a 時間校正; server rejects unpaired sessions whose anchor is nil.
    private var serverTimeSyncConfirmed: Bool = false
    private var serverTimeSyncId: String?

    // UI containers. Preview stays full-screen; a small overlay panel exposes
    // role, link, and preview status.
    /// Last-known capture mode from the server. Defaults to camera-only so a
    /// network-unreachable launch still records and uploads video.
    private var currentCaptureMode: ServerUploader.CaptureMode = .cameraOnly
    private var currentSessionPaths: Set<ServerUploader.DetectionPath> = [.serverPost]
    private var currentHSVRange: ServerUploader.HSVRangePayload = .tennis

    // Haptic feedback generators. Kept as properties so prepare() is
    // honored (trigger latency drops from ~100 ms to <20 ms).
    private let armHaptic = UIImpactFeedbackGenerator(style: .light)
    private let startRecHaptic = UIImpactFeedbackGenerator(style: .medium)
    private let endRecHaptic = UINotificationFeedbackGenerator()

    // System-sound IDs used at recording start/end. 1113 / 1114 are short
    // iOS system tones with audibly different pitches, so the operator
    // can distinguish start vs end without looking at the screen.
    private let startRecSoundID: SystemSoundID = 1113
    private let endRecSoundID: SystemSoundID = 1114

    // MARK: - Lifecycle

    // Camera preview assumes sensor long-edge → horizontal pitcher-to-plate
    // direction; switching to portrait would flip the axes and invalidate
    // the ChArUco intrinsic calibration captured in landscape.
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        [.landscapeLeft, .landscapeRight]
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        overlayView = CameraMonitorOverlayView()

        detectionPool.onFrame = { [weak self] frame in
            self?.handleDetectedFrame(frame)
        }
        stateController = CameraStateController(frameStateBox: frameStateBox, onStateChanged: { [weak self] in
            self?.updateUIForState()
        })

        settings = AppSettingsStore.load()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: AppSettings.serverPortFixed)

        uploader = ServerUploader(config: serverConfig)
        recordingWorkflow = CameraRecordingWorkflow(
            uploader: uploader,
            trackingFps: trackingFps,
            processingQueue: processingQueue,
            dependencies: .init(
                getCameraRole: { [weak self] in self?.settings.cameraRole ?? "?" },
                getCurrentSessionPaths: { [weak self] in self?.currentSessionPaths ?? [] },
                getCurrentCaptureMode: { [weak self] in self?.currentCaptureMode ?? .cameraOnly },
                getSyncId: { [weak self] in self?.syncCoordinator.lastSyncId },
                getSyncAnchorTimestampS: { [weak self] in self?.syncCoordinator.lastSyncAnchorTimestampS },
                currentCaptureTelemetry: { [weak self] fps in
                    self?.currentCaptureTelemetry(targetFps: fps)
                        ?? ServerUploader.CaptureTelemetry(width_px: 0, height_px: 0, target_fps: fps, applied_fps: 0, format_fov_deg: nil, format_index: nil, is_video_binned: nil, tracking_exposure_cap: nil, applied_max_exposure_s: nil)
                },
                startCapture: { [weak self] fps in self?.startCapture(at: fps) },
                resetDetectionState: { [weak self] in self?.resetBallDetectionState() },
                drainDetectedFrames: { [weak self] in self?.drainDetectedFrames() ?? [] },
                clearRecoveredAnchor: { [weak self] in self?.syncCoordinator.clearRecoveredAnchor() },
                dispatchLiveCycleEnd: { [weak self] sessionId, reason in
                    self?.transportCoordinator?.dispatchLiveCycleEnd(sessionId: sessionId, reason: reason)
                },
                showErrorBanner: { [weak self] text in self?.showErrorBanner(text) },
                hideBanner: { [weak self] in self?.hideBanner() },
                setStatusText: { [weak self] text in
                    self?.lastUploadStatusText = text
                    self?.updateUIForState()
                },
                transitionState: { [weak self] newState, pendingBootstrap, sessionId in
                    self?.transitionState(to: newState, pendingBootstrap: pendingBootstrap, sessionId: sessionId)
                },
                reconcileStandbyCaptureState: { [weak self] in self?.reconcileStandbyCaptureState() },
                refreshUI: { [weak self] in self?.updateUIForState() }
            )
        )
        recordingWorkflow.onRecordingStarted = { [weak self] idx in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.serverTimeSyncConfirmed {
                    self.hideBanner()
                }
                AudioServicesPlaySystemSound(self.startRecSoundID)
                self.startRecHaptic.impactOccurred()
            }
        }
        recordingWorkflow.onCycleCompleted = { [weak self] in
            guard let self else { return }
            AudioServicesPlaySystemSound(self.endRecSoundID)
            self.endRecHaptic.notificationOccurred(.success)
        }
        captureQueueRecordingWorkflow = recordingWorkflow

        do {
            try recordingWorkflow.ensurePersistenceDirectories()
        } catch {
            lastUploadStatusText = "暫存初始化失敗 · \(error.localizedDescription)"
        }

        captureRuntime = CameraCaptureRuntime(
            standbyFps: standbyFps,
            trackingFps: trackingFps,
            initialCaptureHeight: AppSettings.captureHeightFixed,
            trackingExposureCapMode: .frameDuration,
            onTelemetryUpdated: { [weak self] telemetry in
                self?.setAppliedCaptureTelemetry(
                    widthPx: telemetry.widthPx,
                    heightPx: telemetry.heightPx,
                    appliedFps: telemetry.appliedFps,
                    formatFovDeg: telemetry.formatFovDeg,
                    formatIndex: telemetry.formatIndex,
                    isVideoBinned: telemetry.isVideoBinned,
                    appliedMaxExposureS: telemetry.appliedMaxExposureS
                )
            },
            onErrorBanner: { [weak self] text in
                self?.showErrorBanner(text)
            },
            onStatusText: { [weak self] text in
                self?.lastUploadStatusText = text
                self?.updateUIForState()
            }
        )

        wireUploadQueueCallbacks()
        wireAnalysisQueueCallbacks()
        recordingWorkflow.reloadPendingQueues()

        healthMonitor = ServerHealthMonitor(
            baseIntervalS: 1.0
        )
        wireHealthMonitorStatusCallbacks()
        syncCoordinator = buildSyncCoordinator()
        transportCoordinator = buildTransportCoordinator()
        captureQueueTransportCoordinator = transportCoordinator

        setupUI()
        captureRuntime.configureCaptureGraph(
            in: view,
            bounds: view.bounds,
            videoDelegate: self,
            processingQueue: processingQueue
        )
        captureRuntime.requestAudioCaptureAccess(cameraRole: settings.cameraRole)
        captureQueueCameraRole = settings.cameraRole
        captureQueueUploader = uploader
        healthMonitor.start()
        transportCoordinator.connect()
        updateUIForState()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        applyUpdatedSettings()
        updateUIForState()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        // Capture stays parked until the dashboard asks for preview.
        healthMonitor.start()
        transportCoordinator.connect()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        healthMonitor.stop()
        transportCoordinator.disconnect()
        if state == .timeSyncWaiting {
            syncCoordinator.cancelTimeSync()
        }
        if state == .mutualSyncing {
            syncCoordinator.abortMutualSync(reason: "view dismissed")
        }
    }

    deinit {
        healthMonitor?.stop()
    }

    // MARK: - Recording controls

    /// Dashboard arm landed — spin up the capture session at 240 fps and
    /// move to `.recording`. ClipRecorder is created from the first delivered
    /// frame so the writer uses the real pixel-buffer dimensions.
    func enterRecordingMode() {
        guard state == .standby else { return }
        recordingWorkflow.enterRecordingMode(
            sessionId: currentSessionId,
            serverTimeSyncConfirmed: serverTimeSyncConfirmed
        )
        armHaptic.impactOccurred()
        startRecHaptic.prepare()
        endRecHaptic.prepare()
    }

    /// Return to `.standby` after a cycle was flushed (or the recording
    /// never produced a frame between arm and disarm). Clears any live
    /// clip writer on the processing queue so we can't race with an
    /// in-flight `append` from captureOutput. The upload queue is NOT
    /// touched here — a pitch just enqueued by `persistCompletedCycle`
    /// needs to keep marching even after we've flipped back to standby,
    /// and the queue lifecycle is now owned by `viewDidLoad` instead of
    /// the sync-mode enter/exit boundaries.
    func exitRecordingToStandby() {
        recordingWorkflow.exitRecordingToStandby(
            currentSessionId: currentSessionId,
            currentState: state
        )
    }

    private func setAppliedCaptureTelemetry(
        widthPx: Int,
        heightPx: Int,
        appliedFps: Double,
        formatFovDeg: Double?,
        formatIndex: Int?,
        isVideoBinned: Bool?,
        appliedMaxExposureS: Double?
    ) {
        captureTelemetryLock.lock()
        appliedCaptureWidthPx = widthPx
        appliedCaptureHeightPx = heightPx
        self.appliedCaptureFps = appliedFps
        appliedFormatFovDeg = formatFovDeg
        appliedFormatIndex = formatIndex
        appliedFormatIsVideoBinned = isVideoBinned
        self.appliedMaxExposureS = appliedMaxExposureS
        captureTelemetryLock.unlock()
    }

    private func currentCaptureTelemetry(targetFps: Double) -> ServerUploader.CaptureTelemetry {
        captureTelemetryLock.lock()
        defer { captureTelemetryLock.unlock() }
        let appliedTelemetry = CameraCaptureRuntime.AppliedTelemetry(
            widthPx: appliedCaptureWidthPx,
            heightPx: appliedCaptureHeightPx,
            appliedFps: appliedCaptureFps,
            formatFovDeg: appliedFormatFovDeg,
            formatIndex: appliedFormatIndex,
            isVideoBinned: appliedFormatIsVideoBinned,
            appliedMaxExposureS: appliedMaxExposureS
        )
        let latestDimensions = frameProcessingState.latestDimensions()
        return captureRuntime.currentCaptureTelemetry(
            latestImageWidth: latestDimensions.width,
            latestImageHeight: latestDimensions.height,
            targetFps: targetFps,
            appliedTelemetry: appliedTelemetry
        )
    }

    private func currentTargetFps() -> Double {
        switch state {
        case .recording:
            return trackingFps
        case .standby, .timeSyncWaiting, .mutualSyncing, .uploading:
            return standbyFps
        }
    }

    private func applyServerCaptureHeight(_ newHeight: Int) {
        captureRuntime.applyServerCaptureHeight(newHeight, bounds: view.bounds)
    }

    /// Encode the given pixel buffer at its NATIVE resolution (no
    /// downsample, no scale) as a high-quality JPEG and POST it to the
    /// server's calibration-frame endpoint. Runs on the capture queue so
    /// as not to block the main thread; hop off-queue for the HTTP call
    /// so the capture queue doesn't stall on network latency.
    nonisolated private func uploadCalibrationFrame(_ pixelBuffer: CVPixelBuffer) {
        let w = CVPixelBufferGetWidth(pixelBuffer)
        let h = CVPixelBufferGetHeight(pixelBuffer)
        let cam = captureQueueCameraRole
        guard let uploader = captureQueueUploader else { return }
        // CIImage is retain-counted + thread-safe; constructing here on
        // the capture queue is fine. The encode itself we hop onto a
        // utility queue so the next sample isn't delayed.
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        DispatchQueue.global(qos: .userInitiated).async {
            let ctx = CIContext(options: [.useSoftwareRenderer: false])
            guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
                  let jpeg = ctx.jpegRepresentation(
                      of: ci, colorSpace: cs,
                      options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.9]
                  )
            else {
                log.warning("calibration frame: native-res JPEG encode failed")
                return
            }
            log.info("calibration frame: encoded \(w)x\(h) bytes=\(jpeg.count)")
            let path = "/camera/\(cam)/calibration_frame"
            uploader.postRawJPEG(path: path, jpeg: jpeg) { result in
                switch result {
                case .success:
                    log.info("calibration frame upload ok cam=\(cam, privacy: .public) bytes=\(jpeg.count)")
                case .failure(let err):
                    log.error("calibration frame upload failed cam=\(cam, privacy: .public) err=\(err.localizedDescription, privacy: .public)")
                }
            }
        }
    }

    private func startCapture(at targetFps: Double) {
        captureRuntime.startCapture(targetFps: targetFps)
    }

    private func reconcileStandbyCaptureState() {
        captureRuntime.reconcileStandbyCaptureState(
            previewRequested: transportCoordinator.isPreviewRequested,
            calibrationFrameCaptureArmed: transportCoordinator.hasPendingCalibrationFrameCaptureRequest
        )
    }

    private func stopCapture() {
        captureRuntime.stopCapture(resetFpsState: {})
    }

    // MARK: - Layout + overlays

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        captureRuntime.updatePreviewFrame(in: view.bounds)
        overlayView.updateBorderPath(for: view.bounds)
    }

    // MARK: - Server health + upload queue wiring

    private func buildSyncCoordinator() -> CameraSyncCoordinator {
        CameraSyncCoordinator(
            dependencies: .init(
                getState: { [weak self] in self?.state ?? .standby },
                getCameraRole: { [weak self] in self?.settings.cameraRole ?? "?" },
                standbyFps: standbyFps,
                uploader: { [weak self] in self!.uploader },
                healthMonitor: { [weak self] in self?.healthMonitor },
                chirpDetector: { [weak self] in self?.captureRuntime.chirpDetector },
                setupAudioCapture: { [weak self] in
                    guard let self else { return }
                    self.captureRuntime.requestAudioCaptureAccess(cameraRole: self.settings.cameraRole)
                },
                startCapture: { [weak self] fps in self?.startCapture(at: fps) },
                reconcileStandbyCaptureState: { [weak self] in self?.reconcileStandbyCaptureState() },
                transitionState: { [weak self] newState in self?.transitionSyncState(to: newState) },
                setStatusText: { [weak self] text in self?.lastUploadStatusText = text },
                showErrorBanner: { [weak self] text in self?.showErrorBanner(text) },
                hideBanner: { [weak self] in self?.hideBanner() },
                flashErrorBanner: { [weak self] text, duration in
                    self?.flashErrorBanner(text, duration: duration)
                },
                refreshUI: { [weak self] in self?.updateUIForState() },
                makeMutualSyncAudio: { emitAtS, recordDurationS in
                    MutualSyncAudio(emitAtS: emitAtS, recordingDurationS: recordDurationS)
                }
            )
        )
    }

    private func buildTransportCoordinator() -> CameraTransportCoordinator {
        CameraTransportCoordinator(
            healthMonitor: healthMonitor,
            uploader: uploader,
            serverConfig: serverConfig,
            cameraRole: settings.cameraRole,
            dependencies: .init(
                getState: { [weak self] in self?.state ?? .standby },
                getCurrentSessionId: { [weak self] in self?.currentSessionId },
                getCurrentSessionPaths: { [weak self] in self?.currentSessionPaths ?? [] },
                setCurrentSessionId: { [weak self] in self?.currentSessionId = $0 },
                setCurrentSessionPaths: { [weak self] in self?.currentSessionPaths = $0 },
                getCurrentTargetFps: { [weak self] in self?.currentTargetFps() ?? 60 },
                getCurrentCaptureHeight: { [weak self] in self?.captureRuntime.currentCaptureHeight ?? AppSettings.captureHeightFixed },
                getSyncId: { [weak self] in self?.syncCoordinator.lastSyncId },
                getSyncAnchorTimestampS: { [weak self] in self?.syncCoordinator.lastSyncAnchorTimestampS },
                getChirpSnapshot: { [weak self] in self?.captureRuntime.chirpSnapshot() },
                startTimeSync: { [weak self] syncId in self?.syncCoordinator.startTimeSync(syncId: syncId) },
                applyMutualSync: { [weak self] syncId, emitAtS, recordDurationS in
                    self?.syncCoordinator.applyMutualSync(syncId: syncId, emitAtS: emitAtS, recordDurationS: recordDurationS)
                },
                applyRemoteArm: { [weak self] in self?.applyRemoteArm() },
                applyRemoteDisarm: { [weak self] in self?.applyRemoteDisarm() },
                updateTimeSyncServerState: { [weak self] confirmed, syncId in
                    self?.updateServerTimeSyncState(confirmed: confirmed, syncId: syncId)
                },
                applyChirpThreshold: { [weak self] threshold in
                    self?.captureRuntime.setChirpThreshold(threshold)
                },
                applyMutualSyncThreshold: { [weak self] threshold in
                    self?.applyPushedMutualSyncThreshold(threshold)
                },
                applyHeartbeatInterval: { [weak self] interval in
                    self?.healthMonitor.updateBaseInterval(interval)
                },
                applyHSVRange: { [weak self] hsvRange in
                    self?.applyPushedHSVRange(hsvRange)
                },
                applyTrackingExposureCap: { [weak self] cap, fps in
                    self?.captureRuntime.applyTrackingExposureCap(cap, targetFps: fps)
                },
                applyServerCaptureHeight: { [weak self] height in self?.applyServerCaptureHeight(height) },
                startStandbyCapture: { [weak self] in self?.startCapture(at: self?.standbyFps ?? 60) },
                stopCapture: { [weak self] in self?.stopCapture() },
                refreshModeLabel: { [weak self] in self?.refreshModeLabel() }
            )
        )
    }

    private func refreshModeLabel() {
        updateUIForState()
    }

    private func updateServerTimeSyncState(confirmed: Bool, syncId: String?) {
        if serverTimeSyncConfirmed != confirmed || serverTimeSyncId != syncId {
            serverTimeSyncConfirmed = confirmed
            serverTimeSyncId = syncId
            DispatchQueue.main.async { self.updateUIForState() }
        }
    }

    private func applyPushedMutualSyncThreshold(_ threshold: Double) {
        _ = threshold
    }

    private func applyPushedHSVRange(_ hsvRange: ServerUploader.HSVRangePayload) {
        currentHSVRange = hsvRange
        detectionPool.updateHSVRange(hsvRange)
        recordingWorkflow.analysisUploadQueue.updateHSVRange(hsvRange)
    }

    private func wireHealthMonitorStatusCallbacks() {
        healthMonitor.onStatusChanged = { [weak self] _, _ in
            self?.updateUIForState()
        }
    }

    private func wireUploadQueueCallbacks() {
        recordingWorkflow.payloadUploadQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        recordingWorkflow.payloadUploadQueue.onLastResultChanged = { [weak self] _ in
            self?.updateUIForState()
        }
        recordingWorkflow.payloadUploadQueue.onUploadingChanged = { [weak self] _ in
            self?.updateUIForState()
        }
        recordingWorkflow.payloadUploadQueue.onPayloadDropped = { [weak self] fileURL, error in
            guard let self else { return }
            let basename = fileURL.deletingPathExtension().lastPathComponent
            let detail = Self.describeUploadError(error)
            log.error("camera payload dropped file=\(basename, privacy: .public) reason=\(detail, privacy: .public)")
            self.updateUIForState()
        }
    }

    private func wireAnalysisQueueCallbacks() {
        recordingWorkflow.analysisUploadQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        recordingWorkflow.analysisUploadQueue.onLastResultChanged = { [weak self] _ in
            self?.updateUIForState()
        }
    }

    /// Short human-readable detail for `UploadError`. `PayloadUploadQueue`
    /// has its own one-word categoriser for the small "Upload:" line; this
    /// one is for the more prominent "Last:" alert and includes status
    /// codes so the operator can grep server logs.
    private static func describeUploadError(_ error: ServerUploader.UploadError) -> String {
        switch error {
        case .network(let urlError):
            return "network (\(urlError.code.rawValue))"
        case .client(let code, _):
            return "HTTP \(code) (client)"
        case .server(let code, _):
            return "HTTP \(code) (server)"
        case .decoding:
            return "decode error"
        case .invalidResponse:
            return "no response"
        }
    }

    private func transitionSyncState(to newState: AppState) {
        transitionState(to: newState)
    }

    private func transitionState(
        to newState: AppState,
        pendingBootstrap: Bool = false,
        sessionId: String? = nil,
        refreshUI: Bool = true
    ) {
        stateController.transition(
            to: newState,
            pendingBootstrap: pendingBootstrap,
            sessionId: sessionId,
            refreshUI: refreshUI
        )
    }

    private func showErrorBanner(_ text: String) {
        statusPresenter.showErrorBanner(text)
    }

    private func hideBanner() {
        statusPresenter.hideBanner()
    }

    private func flashErrorBanner(_ text: String, duration: TimeInterval) {
        showErrorBanner(text)
        DispatchQueue.main.asyncAfter(deadline: .now() + duration) { [weak self] in
            self?.hideBanner()
        }
    }



    private func applyRemoteArm() {
        log.info("camera received arm command state=\(Self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            enterRecordingMode()
        case .timeSyncWaiting, .mutualSyncing, .recording, .uploading:
            // Active state — arm is a no-op. `.timeSyncWaiting` finishes
            // on its own and returns to standby; next heartbeat re-sends
            // the arm command and this branch flips us into recording.
            // Server's session ↔ sync precondition makes the
            // `.mutualSyncing` path impossible in practice.
            break
        }
    }

    private func applyRemoteDisarm() {
        log.info("camera received disarm command state=\(Self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            break
        case .timeSyncWaiting:
            syncCoordinator.cancelTimeSync(reason: "disarmed")
        case .mutualSyncing:
            syncCoordinator.abortMutualSync(reason: "disarmed")
        case .uploading:
            // Upload flow runs its own course; state transitions back to
            // standby via persistCompletedCycle once the queue accepts.
            break
        case .recording:
            recordingWorkflow.handleRemoteDisarm(
                currentSessionId: currentSessionId,
                currentState: state
            )
        }
    }

    /// Re-read persisted settings and refresh the endpoint / role wiring.
    private func applyUpdatedSettings() {
        let latest = AppSettingsStore.load()
        let serverChanged = latest.serverIP != settings.serverIP
        let cameraRoleChanged = latest.cameraRole != settings.cameraRole

        settings = latest

        if serverChanged {
            serverConfig = ServerUploader.ServerConfig(
                serverIP: latest.serverIP,
                serverPort: AppSettings.serverPortFixed
            )
            uploader = ServerUploader(config: serverConfig)
            captureQueueUploader = uploader
            recordingWorkflow.updateUploader(uploader)
            transportCoordinator.updateConnection(
                serverConfig: serverConfig,
                uploader: uploader,
                cameraRole: latest.cameraRole,
                reconnect: true
            )
            healthMonitor.probeNow()
        }
        if cameraRoleChanged {
            transportCoordinator.updateConnection(
                serverConfig: serverConfig,
                uploader: uploader,
                cameraRole: latest.cameraRole,
                reconnect: true
            )
        }
        captureQueueCameraRole = latest.cameraRole
        recordingWorkflow.updateCameraRole(latest.cameraRole)
        syncInlineControlsFromSettings()
    }

    private func setupUI() {
        overlayView.install(in: view)
        overlayView.onRoleChanged = { [weak self] in
            self?.roleControlChanged()
        }
        overlayView.onIPTapped = { [weak self] in
            self?.showIPEditAlert()
        }
        statusPresenter = CameraStatusPresenter(
            topStatusChip: overlayView.topStatusChip,
            warningLabel: overlayView.warningLabel,
            connectionLabel: overlayView.connectionLabel,
            previewLabel: overlayView.previewLabel,
            stateBorderLayer: overlayView.stateBorderLayer
        )
        syncInlineControlsFromSettings()
    }

    private func syncInlineControlsFromSettings() {
        overlayView.syncRole(cameraRole: settings.cameraRole)
        overlayView.syncIP(settings.serverIP)
    }

    private func showIPEditAlert() {
        let alert = UIAlertController(title: "Server IP", message: nil, preferredStyle: .alert)
        alert.addTextField { [weak self] tf in
            tf.text = self?.settings.serverIP
            tf.keyboardType = .numbersAndPunctuation
            tf.placeholder = "192.168.x.x"
            tf.clearButtonMode = .whileEditing
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Save", style: .default) { [weak self] _ in
            guard let self, let ip = alert.textFields?.first?.text, !ip.isEmpty else { return }
            let updated = AppSettings(serverIP: ip, cameraRole: self.settings.cameraRole)
            AppSettingsStore.save(updated)
            self.applyUpdatedSettings()
        })
        present(alert, animated: true)
    }

    private func roleControlChanged() {
        let updated = AppSettings(
            serverIP: settings.serverIP,
            cameraRole: overlayView.selectedCameraRole
        )
        AppSettingsStore.save(updated)
        hideBanner()
        applyUpdatedSettings()
        updateUIForState()
    }

    private func updateUIForState() {
        let reachable = healthMonitor?.isReachable ?? false
        let connectionText = "LINK · \((healthMonitor?.statusText ?? "offline").uppercased())"
        let previewState = transportCoordinator.isPreviewRequested ? "REMOTE ON" : (captureRuntime.isSessionRunning ? "LOCAL ACTIVE" : "OFF")
        overlayView.syncConnection(reachable: reachable)
        overlayView.syncStatus(connectionText)
        statusPresenter.render(
            state: state,
            connectionText: connectionText,
            previewText: "PREVIEW · \(previewState)"
        ) { [weak self] isRecording in
            self?.overlayView.setRecordingActive(isRecording)
        }
    }

    nonisolated static func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
        case .mutualSyncing: return "MUTUAL_SYNC"
        case .recording: return "RECORDING"
        case .uploading: return "UPLOADING"
        }
    }

    // MARK: - Video frame processing

    nonisolated func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // Only the video data output reaches here; audio samples go through
        // the chirp detector's own delegate on its own queue.
        guard connection.output is AVCaptureVideoDataOutput else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let ts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let timestampS = CMTimeGetSeconds(ts)
        if timestampS.isNaN || timestampS.isInfinite { return }

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let frameSample = frameProcessingState.recordFrame(width: width, height: height)

        // Locked snapshot of state, pending-bootstrap, and the session id
        // frozen at arm time. The rest of this method only touches `snap`.
        let snap = frameStateBox.snapshot()

        // Rate-limited debug heartbeat so Xcode console can confirm frames
        // are actually flowing and which state the capture thread sees.
        // Log the first 3 frames (catches "session started but zero frames"
        // bugs) then once every 240 frames (~1 s at tracking fps, ~4 s at
        // idle) for steady monitoring.
        if frameSample.frameIndex <= 3 || frameSample.frameIndex % 240 == 0 {
            log.info("camera frame idx=\(frameSample.frameIndex) state=\(Self.stateText(snap.state), privacy: .public) pendingBootstrap=\(snap.pendingBootstrap) sid=\(snap.sessionId ?? "nil", privacy: .public)")
        }

        // Push a preview JPEG only when requested and not doing anything
        // time-critical. `.recording` owns every
        // frame for ClipRecorder; `.timeSyncWaiting` is mic-centric and
        // the preview encode would compete for CPU. Outside those two
        // states the capture queue is otherwise idle.
        if captureQueueTransportCoordinator.isPreviewRequested
            && snap.state != .recording
            && snap.state != .timeSyncWaiting {
            captureQueueTransportCoordinator.pushPreviewFrame(pixelBuffer)
        }
        if captureQueueTransportCoordinator.consumeCalibrationFrameCaptureRequest(whileIn: snap.state) {
            uploadCalibrationFrame(pixelBuffer)
        }

        // Only the `.recording` state cares about samples.
        guard snap.state == .recording else { return }

        // PR61: all modes write a local MOV so any on-device result can be
        // generated later from the finalized file instead of the live callback.
        if frameStateBox.consumePendingBootstrap() {
            if !captureQueueRecordingWorkflow.bootstrapClipRecorder(width: frameSample.width, height: frameSample.height, sessionId: snap.sessionId) {
                DispatchQueue.main.async {
                    self.exitRecordingToStandby()
                }
                return
            }
        }
        captureQueueRecordingWorkflow.appendSample(sampleBuffer)

        // Detection fans out over a bounded concurrent pool. Live WS
        // streaming consumes the same FramePayloads as the local fallback.
        dispatchDetection(pixelBuffer: pixelBuffer, timestampS: timestampS)

        // Bootstrap the PitchRecorder on the first sample. The payload's
        // `video_start_pts_s` always comes from this sample's session-clock PTS.
        if let sid = snap.sessionId, !sid.isEmpty {
            captureQueueRecordingWorkflow.startRecorderIfNeeded(sessionId: sid, timestampS: timestampS)
        } else {
            log.error("camera recording started without session_id cam=\(self.captureQueueCameraRole, privacy: .public)")
            return
        }
    }

    nonisolated private func dispatchDetection(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) {
        _ = detectionPool.enqueue(pixelBuffer: pixelBuffer, timestampS: timestampS)
    }

    private func handleDetectedFrame(_ frame: ServerUploader.FramePayload) {
        detectionStateLock.lock()
        detectionFramesBuffer.append(frame)
        detectionStateLock.unlock()

        captureQueueTransportCoordinator.dispatchLiveFrame(frame)
    }

    /// Bump the detection generation, clear the buffer, reset the throttle.
    /// Called at both ends of a recording cycle so no stale detections
    /// bleed across arms. Detector itself is stateless — nothing to rebuild.
    private func resetBallDetectionState() {
        detectionStateLock.lock()
        detectionFramesBuffer.removeAll()
        detectionStateLock.unlock()
        detectionPool.invalidateGeneration()
        detectionPool.reset()
    }

    /// Take ownership of the accumulated per-frame detection results for
    /// this recording cycle.
    func drainDetectedFrames() -> [ServerUploader.FramePayload] {
        detectionStateLock.lock()
        defer { detectionStateLock.unlock() }
        let out = detectionFramesBuffer
        detectionFramesBuffer.removeAll()
        return out
    }

}
