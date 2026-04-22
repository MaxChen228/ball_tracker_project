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
    private var captureRuntime: CameraCaptureRuntime!
    // State + frame index. `state` is read/written on main; `captureOutput`
    // (frame queue, up to 240 Hz) reads it via `frameStateBox.snapshot()` so
    // it never observes a partially-updated AppState while `applyRemoteDisarm`
    // mutates it on main. `frameIndex` is touched only on the frame queue.
    private var state: AppState = .standby
    private var frameIndex: Int = 0

    // Lock-protected mirror of the three fields `captureOutput` reads
    // across queues. Main thread is the sole writer; the frame queue
    // takes a single locked snapshot per delivered sample.
    private let frameStateBox = FrameStateBox()

    // Collaborators.
    private var settings: AppSettings!
    private var recorder: PitchRecorder!
    private var uploader: ServerUploader!
    private var serverConfig: ServerUploader.ServerConfig!
    private var healthMonitor: ServerHealthMonitor!
    private var uploadQueue: PayloadUploadQueue!
    private var commandRouter: CameraCommandRouter!
    private var syncCoordinator: CameraSyncCoordinator!
    private var statusPresenter: CameraStatusPresenter!

    // UI state.
    private var lastUploadStatusText: String = ""
    private var lastFrameTimestampForFps: CFTimeInterval = CACurrentMediaTime()
    private var framesSinceLastFpsTick: Int = 0
    private var fpsEstimate: Double = 0

    // Last observed capture dimensions — used only for FPS debug and the
    // capture-dim change log.
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0

    // The `.recording`-was-just-entered bootstrap flag and the snapshot of
    // `currentSessionId` taken at arm time both live in `frameStateBox`;
    // the camera VC itself never reads them after the push, so duplicating
    // them as instance vars would just be two stores out of sync.

    // Remote-control state (driven by WS heartbeat / settings traffic).
    /// Last (command, sync_id) tuple we acted on. Plain "arm" / "disarm"
    /// use a nil sync_id; `"sync_run"` carries the server-minted run id so
    /// back-to-back runs (same command string, different run) still fire.
    /// Only state *transitions* cause local actions so repeated replies
    /// during an active command don't re-trigger handlers.
    private var returnToStandbyAfterCycle: Bool = false
    /// Server-minted pairing key for the currently armed session. Read
    /// off WS arm/settings traffic; tagged onto every recording that starts
    /// while the session is armed. Nil when the server has no active
    /// session. iPhones never mint this themselves.
    private var currentSessionId: String?

    // Payload persistence.
    private let payloadStore = PitchPayloadStore()
    private let analysisStore = AnalysisJobStore()
    private var analysisQueue: AnalysisUploadQueue!

    // Per-cycle H.264 clip writer, created on entry to .recording and
    // finalised when the cycle ends.
    private var clipRecorder: ClipRecorder?
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
    private let detectionPool = ConcurrentDetectionPool(maxConcurrency: 3)
    /// Streams per-frame detection results over WebSocket when the live path
    /// is active. Created lazily (after settings are available).
    private var frameDispatcher: LiveFrameDispatcher?
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
    private let topStatusChip = StatusChip()
    private let controlPanel = UIView()
    private let roleControl = UISegmentedControl(items: ["A", "B"])
    private let connectionLabel = UILabel()
    private let previewLabel = UILabel()
    /// Last-known capture mode from the server. Defaults to camera-only so a
    /// network-unreachable launch still records and uploads video.
    private var currentCaptureMode: ServerUploader.CaptureMode = .cameraOnly
    private var currentSessionPaths: Set<ServerUploader.DetectionPath> = [.serverPost]
    /// WebSocket transport. Lazily initialized so the base URL (built from
    /// settings) is read after `viewDidLoad` where UserDefaults are stable.
    private var ws: ServerWebSocketConnection?
    /// Cache of the server-pushed runtime tunables so the WS settings
    /// callback can skip hot-apply when the value hasn't changed.
    private var lastServerChirpThreshold: Double?
    private var lastServerMutualThreshold: Double?
    private var lastServerHeartbeatInterval: Double?
    private var lastServerTrackingExposureCapMode: ServerUploader.TrackingExposureCapMode?

    // `previewRequestedByServer` mirrors the WS settings flag for this camera.
    // When it flips true→false we reset the uploader so a stale in-flight POST
    // does not land after toggle-off.
    private var previewRequestedByServer: Bool = false
    private var previewUploader: PreviewUploader?
    /// One-shot latch for the server's `calibration_frame_requested` flag.
    /// When true, the next `captureOutput` sample will be encoded at
    /// native resolution (NO downsample) and POSTed to
    /// `/camera/{id}/calibration_frame`. Cleared after upload regardless
    /// of success — the server-side flag drains the moment ANY request
    /// arrives, so retrying from the same heartbeat would just double-POST.
    private var calibrationFrameCaptureArmed: Bool = false
    private let warningLabel = UILabel()

    // Full-screen colored border that reflects AppState. Stroke width +
    // tint change per state; pulses opacity in WAITING states so the
    // operator can see the mode change from across the field.
    private let stateBorderLayer = CAShapeLayer()

    // Top-right "● REC 2.3s" indicator, shown only during .recording.
    private let recIndicator = UIView()
    private let recDotView = UIView()
    private let recTimerLabel = UILabel()
    private var recTimer: Timer?
    private var recStartTime: CFTimeInterval = 0

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

        detectionPool.onFrame = { [weak self] frame in
            self?.handleDetectedFrame(frame)
        }

        settings = AppSettingsStore.load()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: AppSettings.serverPortFixed)

        recorder = PitchRecorder()
        recorder.setCameraId(settings.cameraRole)
        recorder.onRecordingStarted = { [weak self] idx in
            DispatchQueue.main.async {
                guard let self else { return }
                // Keep the "尚未時間校正" warning up while recording — the
                // upload will still go but the server will skip triangulation,
                // and the operator needs to keep seeing why.
                if self.serverTimeSyncConfirmed {
                    self.hideBanner()
                }
                // Start-of-recording feedback: short tone + a medium
                // haptic so the operator registers the transition even
                // if looking away from the screen.
                AudioServicesPlaySystemSound(self.startRecSoundID)
                self.startRecHaptic.impactOccurred()
            }
        }
        recorder.onCycleComplete = { [weak self] payload in
            guard let self else { return }
            let finishingClip = self.clipRecorder
            self.clipRecorder = nil
            if payload.sync_id != nil {
                self.syncCoordinator.clearRecoveredAnchor()
                DispatchQueue.main.async { self.updateUIForState() }
            }
            log.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
            // End-of-recording feedback — haptic + system sound so the
            // operator knows the cycle finished without looking down.
            DispatchQueue.main.async {
                AudioServicesPlaySystemSound(self.endRecSoundID)
                self.endRecHaptic.notificationOccurred(.success)
            }
            if let finishingClip {
                finishingClip.finish { [weak self] videoURL in
                    self?.handleFinishedClip(enriched: payload, videoURL: videoURL)
                }
            } else {
                // Degenerate path: clip bootstrap failed before any MOV
                // existed. Fall back to the advisory live-detection buffer
                // so the cycle is not silently lost.
                self.handleFinishedClip(enriched: payload, videoURL: nil)
            }
        }

        do {
            try payloadStore.ensureDirectory()
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

        uploader = ServerUploader(config: serverConfig)
        uploadQueue = PayloadUploadQueue(store: payloadStore, uploader: uploader)
        analysisQueue = AnalysisUploadQueue(store: analysisStore, uploader: uploader)
        wireUploadQueueCallbacks()
        wireAnalysisQueueCallbacks()
        // Rehydrate cached payloads on launch so failed uploads resume as
        // soon as the server is reachable.
        try? uploadQueue.reloadPending()
        uploadQueue.processNextIfNeeded()
        try? analysisStore.ensureDirectory()
        try? analysisQueue.reloadPending()
        analysisQueue.processNextIfNeeded()

        healthMonitor = ServerHealthMonitor(
            baseIntervalS: 1.0
        )
        wireHealthMonitorCallbacks()
        syncCoordinator = buildSyncCoordinator()
        commandRouter = buildCommandRouter()

        setupUI()
        captureRuntime.configureCaptureGraph(
            in: view,
            bounds: view.bounds,
            videoDelegate: self,
            processingQueue: processingQueue
        )
        captureRuntime.requestAudioCaptureAccess(cameraRole: settings.cameraRole)
        healthMonitor.start()
        connectWebSocket()
        // frameDispatcher needs the ws connection (set up inside connectWebSocket())
        if let wsConn = ws {
            frameDispatcher = LiveFrameDispatcher(
                connection: wsConn,
                cameraId: settings.cameraRole,
                currentSessionId: { [weak self] in self?.currentSessionId },
                currentPaths: { [weak self] in self?.currentSessionPaths ?? [] }
            )
        }
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
        connectWebSocket()
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        healthMonitor.stop()
        disconnectWebSocket()
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
        // Snapshot the server-minted session id at arm time and freeze it
        // into the frame-state box. `self.currentSessionId` may flip during
        // the ~300-500 ms fps-switch window, so the captureOutput path
        // reads this snapshot rather than the live property.
        let snapshotSessionId = currentSessionId
        log.info("camera entering recording session=\(snapshotSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        if !serverTimeSyncConfirmed {
            showErrorBanner("尚未時間校正，將無法三角化")
            log.warning("arm without server-confirmed time sync — server will skip triangulation")
        } else {
            hideBanner()
        }
        startCapture(at: trackingFps)
        recorder.reset()
        resetBallDetectionState()
        state = .recording
        frameStateBox.update(state: .recording, pendingBootstrap: true, sessionId: snapshotSessionId)
        updateUIForState()
        // Acknowledge arm with a light tap; pre-warm the next two haptics
        // so their first fire isn't lazy-initialised.
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
        log.info("camera exit recording → standby session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public) state=\(Self.stateText(self.state), privacy: .public)")
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        recorder.reset()
        // Bump the detection generation so any closure still running on
        // detectionQueue discards its result; also clears the buffer of
        // anything that landed before this point.
        resetBallDetectionState()
        processingQueue.async { [weak self] in
            self?.clipRecorder?.cancel()
            self?.clipRecorder = nil
        }
        hideBanner()
        // Drop the sensor back to whatever standby currently requires.
        reconcileStandbyCaptureState()
        updateUIForState()
    }

    /// Cycle-complete router. PR61 makes the finalized local MOV the
    /// authority for any on-device result:
    ///   - `cameraOnly` → raw MOV upload only; server remains authoritative.
    ///   - `onDevice` → persist the MOV locally, run post-pass analysis,
    ///     upload frames-only once analysis completes.
    ///   - `dual` → upload the raw MOV immediately AND enqueue a late
    ///     post-pass sidecar upload carrying `frames_on_device`.
    ///
    /// Live detection is drained here only as a degraded fallback when clip
    /// writing failed and no MOV exists to analyze.
    private func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let advisoryFrames = drainDetectedFrames()
        let paths = currentSessionPaths
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
            frameDispatcher?.dispatchCycleEnd(sessionId: payload.session_id, reason: "disarmed")
        }
        if !paths.contains(.serverPost) && !paths.contains(.iosPost) {
            // Live-only fallback still persists metadata so the cycle
            // remains recoverable if the WS stream degraded mid-flight.
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
        } else if paths.contains(.iosPost) {
            persistCompletedCycle(enriched.withFrames(advisoryFrames), videoURL: nil)
        } else {
            persistCompletedCycle(enriched.withFrames(advisoryFrames), videoURL: nil)
        }
    }

    /// Mode-one / dual: ship the full recorded MOV as-is. Server runs
    /// authoritative detection on the received clip.
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
            let fileURL = try payloadStore.save(payload, videoURL: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存完成 · 等待上傳"
                self.uploadQueue.enqueue(fileURL)
                // Every cycle-complete returns to standby. Dashboard re-arm
                // happens via the next heartbeat.
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera cycle persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            // Even if the JSON save failed, drop any orphan tmp video.
            if let videoURL {
                try? FileManager.default.removeItem(at: videoURL)
            }
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存失敗 · \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
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
            let fileURL = try analysisStore.save(job, videoURL: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存完成 · 等待錄後分析"
                self.analysisQueue.enqueue(fileURL)
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera analysis persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            try? FileManager.default.removeItem(at: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "錄後分析暫存失敗 · \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
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
        return captureRuntime.currentCaptureTelemetry(
            latestImageWidth: latestImageWidth,
            latestImageHeight: latestImageHeight,
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
    private func uploadCalibrationFrame(_ pixelBuffer: CVPixelBuffer) {
        let w = CVPixelBufferGetWidth(pixelBuffer)
        let h = CVPixelBufferGetHeight(pixelBuffer)
        let cam = settings.cameraRole
        // CIImage is retain-counted + thread-safe; constructing here on
        // the capture queue is fine. The encode itself we hop onto a
        // utility queue so the next sample isn't delayed.
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
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
            self.uploader.postRawJPEG(path: path, jpeg: jpeg) { result in
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
            previewRequested: previewRequestedByServer,
            calibrationFrameCaptureArmed: calibrationFrameCaptureArmed
        )
    }

    private func stopCapture() {
        captureRuntime.stopCapture { [weak self] in
            guard let self else { return }
            self.fpsEstimate = 0
            self.framesSinceLastFpsTick = 0
            self.lastFrameTimestampForFps = CACurrentMediaTime()
        }
    }

    // MARK: - Layout + overlays

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        captureRuntime.updatePreviewFrame(in: view.bounds)
        // Border path follows the root view bounds; regenerated on every
        // layout pass so rotation / safe-area changes stay in sync.
        stateBorderLayer.frame = view.bounds
        stateBorderLayer.path = UIBezierPath(rect: view.bounds).cgPath
    }

    // MARK: - Server health + upload queue wiring

    private func webSocketURL() -> URL? {
        guard
            let host = serverConfig.serverIP.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed),
            !host.isEmpty
        else { return nil }
        var comps = URLComponents()
        comps.scheme = "ws"
        comps.host = host
        comps.port = Int(serverConfig.serverPort)
        // path is built inside ServerWebSocketConnection; just build the base URL here
        return comps.url
    }

    private func connectWebSocket() {
        guard let baseURL = webSocketURL() else { return }
        if ws == nil {
            let conn = ServerWebSocketConnection(baseURL: baseURL, cameraId: settings.cameraRole)
            conn.delegate = self
            ws = conn
        }
        ws?.connect(initialHello: [
            "type": "hello",
            "cam": settings.cameraRole,
            "session_id": currentSessionId as Any,
            "time_sync_id": syncCoordinator.lastSyncId as Any,
            "sync_anchor_timestamp_s": syncCoordinator.lastSyncAnchorTimestampS as Any,
        ])
    }

    private func disconnectWebSocket() {
        ws?.disconnect()
        ws = nil
    }

    private func sendWebSocketJSON(_ obj: [String: Any]) {
        ws?.send(obj)
    }

    private func handlePushedTrackingExposureCap(_ modeStr: String) {
        let exposureMode = ServerUploader.TrackingExposureCapMode(rawValue: modeStr) ?? .frameDuration
        if self.lastServerTrackingExposureCapMode != exposureMode {
            self.lastServerTrackingExposureCapMode = exposureMode
            captureRuntime.applyTrackingExposureCap(modeStr, targetFps: currentTargetFps())
        }
    }

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
                makeMutualSyncAudio: { MutualSyncAudio() }
            )
        )
    }

    private func buildCommandRouter() -> CameraCommandRouter {
        CameraCommandRouter(
            dependencies: .init(
                getState: { [weak self] in self?.state ?? .standby },
                getCameraRole: { [weak self] in self?.settings.cameraRole ?? "?" },
                healthMonitor: healthMonitor,
                getCurrentSessionPaths: { [weak self] in self?.currentSessionPaths ?? [] },
                setCurrentSessionId: { [weak self] in self?.currentSessionId = $0 },
                setCurrentSessionPaths: { [weak self] in self?.currentSessionPaths = $0 },
                refreshModeLabel: { [weak self] in self?.refreshModeLabel() },
                startTimeSync: { [weak self] syncId in self?.syncCoordinator.startTimeSync(syncId: syncId) },
                applyMutualSync: { [weak self] syncId in self?.syncCoordinator.applyMutualSync(syncId: syncId) },
                applyRemoteArm: { [weak self] in self?.applyRemoteArm() },
                applyRemoteDisarm: { [weak self] in self?.applyRemoteDisarm() },
                updateTimeSyncServerState: { [weak self] confirmed, syncId in
                    self?.updateServerTimeSyncState(confirmed: confirmed, syncId: syncId)
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
                    self?.handlePushedTrackingExposureCap(cap)
                },
                currentCaptureHeight: { [weak self] in self?.captureRuntime.currentCaptureHeight ?? AppSettings.captureHeightFixed },
                applyServerCaptureHeight: { [weak self] height in self?.applyServerCaptureHeight(height) },
                isPreviewRequested: { [weak self] in self?.previewRequestedByServer ?? false },
                setPreviewRequested: { [weak self] in self?.previewRequestedByServer = $0 },
                ensurePreviewUploader: { [weak self] in self?.ensurePreviewUploader() },
                resetPreviewUploader: { [weak self] in self?.previewUploader?.reset() },
                startStandbyCapture: { [weak self] in self?.startCapture(at: self?.standbyFps ?? 60) },
                stopCapture: { [weak self] in self?.stopCapture() },
                isCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed ?? false },
                setCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed = $0 }
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

    private func applyPushedChirpThreshold(_ threshold: Double) {
        guard lastServerChirpThreshold != threshold else { return }
        lastServerChirpThreshold = threshold
        DispatchQueue.main.async {
            self.captureRuntime.setChirpThreshold(threshold)
            log.info("quick-chirp threshold hot-applied from server: \(threshold)")
        }
    }

    private func applyPushedMutualSyncThreshold(_ threshold: Double) {
        if lastServerMutualThreshold != threshold {
            lastServerMutualThreshold = threshold
        }
    }

    private func applyPushedHeartbeatInterval(_ interval: Double) {
        guard lastServerHeartbeatInterval != interval else { return }
        lastServerHeartbeatInterval = interval
        DispatchQueue.main.async {
            self.healthMonitor.updateBaseInterval(interval)
            log.info("heartbeat interval hot-applied: \(interval)s")
        }
    }

    private func ensurePreviewUploader() {
        if previewUploader == nil {
            previewUploader = PreviewUploader(uploader: uploader, cameraId: settings.cameraRole)
        }
    }

    private func wireHealthMonitorCallbacks() {
        healthMonitor.onStatusChanged = { [weak self] _, _ in
            self?.updateUIForState()
        }
        healthMonitor.sendWSHeartbeat = { [weak self] timeSyncId in
            guard let self else { return }
            var payload: [String: Any] = [
                "type": "heartbeat",
                "cam": self.settings.cameraRole,
                "t_session_s": CACurrentMediaTime(),
                "time_sync_id": timeSyncId as Any,
                "sync_anchor_timestamp_s": self.syncCoordinator.lastSyncAnchorTimestampS as Any,
            ]
            // Include quick-chirp telemetry only while the detector is
            // actively listening.
            if self.state == .timeSyncWaiting, let s = self.captureRuntime.chirpSnapshot() {
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
            self.sendWebSocketJSON(payload)
        }
    }

    private func wireUploadQueueCallbacks() {
        uploadQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        uploadQueue.onLastResultChanged = { [weak self] _ in
            self?.updateUIForState()
        }
        uploadQueue.onUploadingChanged = { [weak self] _ in
            self?.updateUIForState()
        }
        uploadQueue.onPayloadDropped = { [weak self] fileURL, error in
            guard let self else { return }
            let basename = fileURL.deletingPathExtension().lastPathComponent
            let detail = Self.describeUploadError(error)
            log.error("camera payload dropped file=\(basename, privacy: .public) reason=\(detail, privacy: .public)")
            self.updateUIForState()
        }
    }

    private func wireAnalysisQueueCallbacks() {
        analysisQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        analysisQueue.onLastResultChanged = { [weak self] _ in
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
        state = newState
        frameStateBox.update(state: newState, pendingBootstrap: false, sessionId: nil)
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
            returnToStandbyAfterCycle = true
            processingQueue.async { [weak self] in
                guard let self else { return }
                let active = self.recorder.isActive
                log.info("camera disarm while recording: recorder_active=\(active) clip_exists=\(self.clipRecorder != nil)")
                if active {
                    // Normal path: flush whatever frames the clip contains;
                    // onCycleComplete drives persist + standby transition.
                    self.recorder.forceFinishIfRecording()
                } else {
                    // Disarm arrived before the first frame reached us
                    // (happens if stop is pressed in the ~300 ms fps
                    // switch window). Tear down cleanly on main.
                    log.warning("camera disarm before first frame: no payload produced — frames never reached captureOutput or clip.append failed")
                    self.clipRecorder?.cancel()
                    self.clipRecorder = nil
                    DispatchQueue.main.async {
                        self.returnToStandbyAfterCycle = false
                        self.exitRecordingToStandby()
                    }
                }
            }
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
            uploadQueue.updateUploader(uploader)
            analysisQueue.updateUploader(uploader)
            previewUploader?.updateUploader(uploader)
            disconnectWebSocket()
            connectWebSocket()
            healthMonitor.probeNow()
        }
        if cameraRoleChanged {
            previewUploader = nil
            disconnectWebSocket()
            connectWebSocket()
        }
        recorder?.setCameraId(latest.cameraRole)
        syncInlineControlsFromSettings()
    }

    private func setupUI() {
        topStatusChip.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(topStatusChip)

        setupControlPanel()

        warningLabel.font = DesignTokens.Fonts.sans(size: 18, weight: .bold)
        warningLabel.textColor = DesignTokens.Colors.ink
        warningLabel.backgroundColor = DesignTokens.Colors.warning.withAlphaComponent(0.85)
        warningLabel.layer.cornerRadius = DesignTokens.CornerRadius.chip
        warningLabel.layer.masksToBounds = true
        warningLabel.textAlignment = .center
        warningLabel.numberOfLines = 0
        warningLabel.translatesAutoresizingMaskIntoConstraints = false
        warningLabel.isHidden = true
        view.addSubview(warningLabel)

        NSLayoutConstraint.activate([
            topStatusChip.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.m),
            topStatusChip.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),

            controlPanel.topAnchor.constraint(equalTo: topStatusChip.bottomAnchor, constant: DesignTokens.Spacing.s),
            controlPanel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),
            controlPanel.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -DesignTokens.Spacing.xl),

            warningLabel.topAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: DesignTokens.Spacing.s),
            warningLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            warningLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
        ])

        setupStateBorder()
        setupRecIndicator()
        statusPresenter = CameraStatusPresenter(
            topStatusChip: topStatusChip,
            warningLabel: warningLabel,
            connectionLabel: connectionLabel,
            previewLabel: previewLabel,
            stateBorderLayer: stateBorderLayer
        )
        syncInlineControlsFromSettings()
    }

    private func setupControlPanel() {
        controlPanel.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.backgroundColor = DesignTokens.Colors.hudSurface
        controlPanel.layer.cornerRadius = DesignTokens.CornerRadius.card
        controlPanel.layer.borderWidth = 1
        controlPanel.layer.borderColor = DesignTokens.Colors.cardBorder.cgColor
        view.addSubview(controlPanel)

        let roleLabel = makePanelLabel("ROLE")

        roleControl.translatesAutoresizingMaskIntoConstraints = false
        roleControl.selectedSegmentTintColor = DesignTokens.Colors.accent
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.ink], for: .normal)
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.cardBackground], for: .selected)
        roleControl.addTarget(self, action: #selector(roleControlChanged), for: .valueChanged)

        connectionLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        connectionLabel.textColor = DesignTokens.Colors.ink

        previewLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        previewLabel.textColor = DesignTokens.Colors.sub

        let roleRow = UIStackView(arrangedSubviews: [roleLabel, roleControl])
        roleRow.axis = .horizontal
        roleRow.alignment = .center
        roleRow.spacing = DesignTokens.Spacing.s

        let statusRow = UIStackView(arrangedSubviews: [connectionLabel, previewLabel])
        statusRow.axis = .vertical
        statusRow.alignment = .leading
        statusRow.spacing = DesignTokens.Spacing.xs

        let root = UIStackView(arrangedSubviews: [roleRow, statusRow])
        root.axis = .vertical
        root.spacing = DesignTokens.Spacing.s
        root.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.addSubview(root)

        NSLayoutConstraint.activate([
            roleLabel.widthAnchor.constraint(equalToConstant: 52),
            roleControl.widthAnchor.constraint(equalToConstant: 120),

            root.topAnchor.constraint(equalTo: controlPanel.topAnchor, constant: DesignTokens.Spacing.m),
            root.leadingAnchor.constraint(equalTo: controlPanel.leadingAnchor, constant: DesignTokens.Spacing.m),
            root.trailingAnchor.constraint(equalTo: controlPanel.trailingAnchor, constant: -DesignTokens.Spacing.m),
            root.bottomAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: -DesignTokens.Spacing.m),
        ])
    }

    private func makePanelLabel(_ text: String) -> UILabel {
        let label = UILabel()
        label.font = DesignTokens.Fonts.mono(size: 12, weight: .bold)
        label.textColor = DesignTokens.Colors.sub
        label.text = text
        return label
    }

    private func syncInlineControlsFromSettings() {
        roleControl.selectedSegmentIndex = settings.cameraRole == "B" ? 1 : 0
    }

    @objc private func roleControlChanged() {
        let updated = AppSettings(
            serverIP: settings.serverIP,
            cameraRole: roleControl.selectedSegmentIndex == 1 ? "B" : "A"
        )
        AppSettingsStore.save(updated)
        hideBanner()
        applyUpdatedSettings()
        updateUIForState()
    }

    private func setupStateBorder() {
        stateBorderLayer.fillColor = UIColor.clear.cgColor
        stateBorderLayer.strokeColor = UIColor.clear.cgColor
        stateBorderLayer.lineWidth = 0
        // Sit above the preview layer (index 0) but below the HUD views.
        // Border is decorative — never intercept touches (CAShapeLayer
        // doesn't by default, belt and braces).
        view.layer.addSublayer(stateBorderLayer)
    }

    private func setupRecIndicator() {
        recIndicator.translatesAutoresizingMaskIntoConstraints = false
        recIndicator.backgroundColor = DesignTokens.Colors.hudSurface
        recIndicator.layer.cornerRadius = 14
        recIndicator.layer.borderColor = DesignTokens.Colors.destructive.cgColor
        recIndicator.layer.borderWidth = 1
        recIndicator.isHidden = true

        recDotView.backgroundColor = DesignTokens.Colors.destructive
        recDotView.layer.cornerRadius = 7
        recDotView.translatesAutoresizingMaskIntoConstraints = false

        recTimerLabel.text = "REC 0.0s"
        recTimerLabel.textColor = DesignTokens.Colors.ink
        recTimerLabel.font = DesignTokens.Fonts.mono(size: 16, weight: .bold)
        recTimerLabel.translatesAutoresizingMaskIntoConstraints = false

        recIndicator.addSubview(recDotView)
        recIndicator.addSubview(recTimerLabel)
        view.addSubview(recIndicator)

        NSLayoutConstraint.activate([
            recIndicator.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            recIndicator.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            recIndicator.heightAnchor.constraint(equalToConstant: 32),

            recDotView.leadingAnchor.constraint(equalTo: recIndicator.leadingAnchor, constant: 10),
            recDotView.centerYAnchor.constraint(equalTo: recIndicator.centerYAnchor),
            recDotView.widthAnchor.constraint(equalToConstant: 14),
            recDotView.heightAnchor.constraint(equalToConstant: 14),

            recTimerLabel.leadingAnchor.constraint(equalTo: recDotView.trailingAnchor, constant: 8),
            recTimerLabel.trailingAnchor.constraint(equalTo: recIndicator.trailingAnchor, constant: -12),
            recTimerLabel.centerYAnchor.constraint(equalTo: recIndicator.centerYAnchor),
        ])
    }

    private func updateUIForState() {
        let connectionText = "LINK · \((healthMonitor?.statusText ?? "offline").uppercased())"
        let previewState = previewRequestedByServer ? "REMOTE ON" : (captureRuntime.isSessionRunning ? "LOCAL ACTIVE" : "OFF")
        statusPresenter.render(
            state: state,
            connectionText: connectionText,
            previewText: "PREVIEW · \(previewState)"
        ) { [weak self] isRecording in
            guard let self else { return }
            if isRecording {
                self.recIndicator.isHidden = false
                self.startRecTimer()
            } else {
                self.recIndicator.isHidden = true
                self.stopRecTimer()
            }
        }
    }

    static func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
        case .mutualSyncing: return "MUTUAL_SYNC"
        case .recording: return "RECORDING"
        case .uploading: return "UPLOADING"
        }
    }

    private func startRecTimer() {
        recStartTime = CACurrentMediaTime()
        recTimerLabel.text = "REC 0.0s"
        recDotView.alpha = 1.0
        recTimer?.invalidate()
        recTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let elapsed = CACurrentMediaTime() - self.recStartTime
            self.recTimerLabel.text = String(format: "REC %.1fs", elapsed)
            // 2 Hz blink so the operator can read the dot from distance.
            self.recDotView.alpha = (Int(elapsed * 2) % 2 == 0) ? 1.0 : 0.25
        }
    }

    private func stopRecTimer() {
        recTimer?.invalidate()
        recTimer = nil
        recDotView.alpha = 1.0
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

        updateFpsEstimate()

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        latestImageWidth = width
        latestImageHeight = height

        frameIndex += 1

        // Locked snapshot of state, pending-bootstrap, and the session id
        // frozen at arm time. The rest of this method only touches `snap`.
        let snap = frameStateBox.snapshot()

        // Rate-limited debug heartbeat so Xcode console can confirm frames
        // are actually flowing and which state the capture thread sees.
        // Log the first 3 frames (catches "session started but zero frames"
        // bugs) then once every 240 frames (~1 s at tracking fps, ~4 s at
        // idle) for steady monitoring.
        if frameIndex <= 3 || frameIndex % 240 == 0 {
            log.info("camera frame idx=\(self.frameIndex) state=\(Self.stateText(snap.state), privacy: .public) pendingBootstrap=\(snap.pendingBootstrap) sid=\(snap.sessionId ?? "nil", privacy: .public)")
        }

        // Push a preview JPEG only when requested and not doing anything
        // time-critical. `.recording` owns every
        // frame for ClipRecorder; `.timeSyncWaiting` is mic-centric and
        // the preview encode would compete for CPU. Outside those two
        // states the capture queue is otherwise idle.
        if self.previewRequestedByServer
            && snap.state != .recording
            && snap.state != .timeSyncWaiting {
            self.previewUploader?.pushFrame(pixelBuffer)
        }
        // One-shot native-resolution calibration frame. Drain the latch
        // synchronously so a slow POST cannot double-fire.
        if self.calibrationFrameCaptureArmed
            && snap.state != .recording
            && snap.state != .timeSyncWaiting {
            self.calibrationFrameCaptureArmed = false
            self.uploadCalibrationFrame(pixelBuffer)
        }

        // Only the `.recording` state cares about samples.
        guard snap.state == .recording else { return }

        // PR61: all modes write a local MOV so any on-device result can be
        // generated later from the finalized file instead of the live callback.
        if frameStateBox.consumePendingBootstrap() {
            log.info("camera clip bootstrap start width=\(width) height=\(height) sid=\(snap.sessionId ?? "nil", privacy: .public)")
            startClipRecorder(width: width, height: height)
            if clipRecorder == nil {
                log.error("camera clip bootstrap failed session=\(snap.sessionId ?? "nil", privacy: .public)")
                DispatchQueue.main.async {
                    self.returnToStandbyAfterCycle = false
                    self.exitRecordingToStandby()
                }
                return
            }
            log.info("camera clip bootstrap ok session=\(snap.sessionId ?? "nil", privacy: .public)")
        }
        clipRecorder?.append(sampleBuffer: sampleBuffer)

        // Detection fans out over a bounded concurrent pool. Live WS
        // streaming consumes the same FramePayloads as the local fallback.
        dispatchDetection(pixelBuffer: pixelBuffer, timestampS: timestampS)

        // Bootstrap the PitchRecorder on the first sample. The payload's
        // `video_start_pts_s` always comes from this sample's session-clock PTS.
        if !recorder.isActive {
            guard let sid = snap.sessionId, !sid.isEmpty else {
                log.error("camera recording started without session_id cam=\(self.settings.cameraRole, privacy: .public)")
                return
            }
            log.info("camera first frame, starting recorder session=\(sid, privacy: .public) mode=\(self.currentCaptureMode.rawValue, privacy: .public) video_start_pts=\(timestampS) anchor=\(self.syncCoordinator.lastSyncAnchorTimestampS ?? .nan)")
            recorder.startRecording(
                sessionId: sid,
                syncId: syncCoordinator.lastSyncId,
                anchorTimestampS: syncCoordinator.lastSyncAnchorTimestampS,
                videoStartPtsS: timestampS,
                captureTelemetry: currentCaptureTelemetry(targetFps: trackingFps)
            )
        }
    }

    private func dispatchDetection(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) {
        _ = detectionPool.enqueue(pixelBuffer: pixelBuffer, timestampS: timestampS)
    }

    private func handleDetectedFrame(_ frame: ServerUploader.FramePayload) {
        detectionStateLock.lock()
        detectionFramesBuffer.append(frame)
        detectionStateLock.unlock()

        frameDispatcher?.dispatchFrame(frame)
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

    private func startClipRecorder(width: Int, height: Int) {
        let tmpURL = payloadStore.makeTempVideoURL()
        let cr = ClipRecorder(outputURL: tmpURL)
        do {
            try cr.prepare(width: width, height: height)
            clipRecorder = cr
        } catch {
            // If AVAssetWriter rejects the configuration we degrade to
            // JSON-only and keep recording.
            log.error("camera clip recorder prepare failed error=\(error.localizedDescription, privacy: .public)")
            clipRecorder = nil
        }
    }

    private func updateFpsEstimate() {
        framesSinceLastFpsTick += 1
        let now = CACurrentMediaTime()
        let elapsed = now - lastFrameTimestampForFps
        guard elapsed >= 1.0 else { return }
        fpsEstimate = Double(framesSinceLastFpsTick) / elapsed
        framesSinceLastFpsTick = 0
        lastFrameTimestampForFps = now
    }

}

// MARK: - ServerWebSocketDelegate

extension CameraViewController: ServerWebSocketDelegate {

    func webSocketDidConnect(_ connection: ServerWebSocketConnection) {
        // Transport open is not proof the server is actually reachable.
        // URLSession lets `resume()` succeed before the WS handshake (or
        // a first server payload) completes, which caused false-green
        // "server connected" HUD states after the backend had already
        // died. Reachability is promoted only on real inbound traffic in
        // `didReceive`.
    }

    func webSocketDidDisconnect(_ connection: ServerWebSocketConnection, reason: String?) {
        commandRouter.didDisconnect()
    }

    func webSocket(_ connection: ServerWebSocketConnection, didReceive message: [String: Any]) {
        commandRouter.handle(message: message)
    }
}

/// Lock-protected mirror of the fields `captureOutput` reads across
/// queues (`state`, `pendingRecordingBootstrap`, `pendingSessionId`).
final class FrameStateBox {
    struct Snapshot {
        let state: CameraViewController.AppState
        let pendingBootstrap: Bool
        let sessionId: String?
    }

    private var lock = os_unfair_lock_s()
    private var _state: CameraViewController.AppState = .standby
    private var _pendingBootstrap: Bool = false
    private var _sessionId: String?

    func snapshot() -> Snapshot {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return Snapshot(
            state: _state,
            pendingBootstrap: _pendingBootstrap,
            sessionId: _sessionId
        )
    }

    func update(state: CameraViewController.AppState, pendingBootstrap: Bool, sessionId: String?) {
        os_unfair_lock_lock(&lock)
        _state = state
        _pendingBootstrap = pendingBootstrap
        _sessionId = sessionId
        os_unfair_lock_unlock(&lock)
    }

    /// Edge-trigger helper for the frame queue: clears the bootstrap flag
    /// once the writer is up. Returns the previous value so the caller can
    /// branch on whether *this* sample owned the bootstrap.
    func consumePendingBootstrap() -> Bool {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        let prev = _pendingBootstrap
        _pendingBootstrap = false
        return prev
    }
}
