import UIKit
import AVFoundation
import CoreMedia
import CoreVideo
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Main camera view. State machine:
/// - STANDBY: live preview, chirp detector off, no frame buffering
/// - TIME_SYNC_WAITING: listens on the mic for the reference chirp, saves
///   session-clock PTS of the peak as the anchor
/// - SYNC_WAITING: tracking armed, waiting for ball to enter frame
/// - RECORDING: cycle in progress
/// - UPLOADING: cycle persisted, handed to the upload queue
///
/// Heavy side concerns live in dedicated helpers:
/// - `ServerHealthMonitor` owns the 1 Hz heartbeat, backoff, and
///   "last contact" tick timer.
/// - `PayloadUploadQueue` owns the cached-pitch upload worker.
/// - `IntrinsicsStore` owns UserDefaults keys for calibration artefacts.
final class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    enum AppState {
        case standby
        case timeSyncWaiting
        case syncWaiting
        case recording
        case uploading
    }

    // Session + outputs.
    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private let videoOutput = AVCaptureVideoDataOutput()
    private let processingQueue = DispatchQueue(label: "camera.frame.queue")

    // Audio chirp sync. Mic input is always installed once granted so the
    // chirp detector can run as soon as the user taps 時間校正.
    private var audioInput: AVCaptureDeviceInput?
    private var audioOutput: AVCaptureAudioDataOutput?
    private var chirpDetector: AudioChirpDetector?

    // State + frame index.
    private var state: AppState = .standby
    private var frameIndex: Int = 0
    private var horizontalFovRadians: Double = 1.0

    // Collaborators.
    private var settings: SettingsViewController.Settings!
    private var ballDetector: BallDetector!
    private var recorder: PitchRecorder!
    private var uploader: ServerUploader!
    private var serverConfig: ServerUploader.ServerConfig!
    private var healthMonitor: ServerHealthMonitor!
    private var uploadQueue: PayloadUploadQueue!

    // UI state.
    private var lastUploadStatusText: String = "Idle"
    private var lastResultText: String = "(尚無結果)"
    private var lastFrameTimestampForFps: CFTimeInterval = CACurrentMediaTime()
    private var framesSinceLastFpsTick: Int = 0
    private var fpsEstimate: Double = 0
    private var displayLink: CADisplayLink?

    // Detection snapshot for the CADisplayLink pump — written on capture
    // queue, read on main. Per-frame main-thread dispatch is never used.
    private var latestCentroidX: Double?
    private var latestCentroidY: Double?
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0

    // Chirp detector snapshot for the HUD — written on audio queue.
    private var latestChirpSnapshot: AudioChirpDetector.Snapshot?

    private let ballOverlayLayer = CAShapeLayer()

    // Remote-control state (driven by the heartbeat response).
    /// Last command key we acted on for this device (`"arm"` / `"disarm"` /
    /// nil). Only state *transitions* cause local actions so repeated arm
    /// replies during an armed session don't re-trigger enterSyncMode.
    private var lastAppliedCommand: String?
    /// Set by a dashboard `disarm` arriving mid-recording so the cycle-
    /// complete path routes us back to `.standby` instead of the usual
    /// `.syncWaiting` hand-off.
    private var returnToStandbyAfterCycle: Bool = false
    /// Server-minted pairing key for the currently armed session. Read
    /// off each heartbeat reply; tagged onto every recording that starts
    /// while the session is armed. Nil when the server has no active
    /// session. iPhones never mint this themselves.
    private var currentSessionId: String?

    // Payload persistence.
    private let payloadStore = PitchPayloadStore()

    // Per-cycle H.264 clip writer, created on entry to .recording and
    // finalised when the cycle ends. Phase-1 raw-video experiment: the clip
    // travels alongside the JSON payload so server-side detection can be
    // iterated against a canonical source of truth.
    private var clipRecorder: ClipRecorder?

    // Most recently recovered chirp anchor. Used as the `sync_anchor_*`
    // fields on outgoing pitch payloads. Nil until the user completes a
    // 時間校正.
    private var lastSyncAnchorFrameIndex: Int?
    private var lastSyncAnchorTimestampS: Double?

    // Time-sync timeout task so we can cancel if the user aborts early.
    private var timeSyncTimeoutWork: DispatchWorkItem?

    // UI containers.
    private let statusContainer = UIStackView()
    private let topStatusLabel = UILabel()
    private let serverStatusDot = UIView()
    private let serverStatusLabel = UILabel()
    private let fpsLabel = UILabel()
    private let uploadStatusLabel = UILabel()
    private let lastResultLabel = UILabel()
    private let warningLabel = UILabel()
    private let chirpDebugLabel = UILabel()
    private let trackingButton = UIButton(type: .system)
    private let lastContactLabel = UILabel()
    private let testConnectionButton = UIButton(type: .system)

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "Settings",
            style: .plain,
            target: self,
            action: #selector(openSettings)
        )
        navigationItem.rightBarButtonItems = [
            UIBarButtonItem(title: "時間校正", style: .plain, target: self, action: #selector(onTapTimeCalibration)),
            UIBarButtonItem(title: "位置校正", style: .plain, target: self, action: #selector(openCalibration)),
        ]

        settings = SettingsViewController.loadFromUserDefaults()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: settings.serverPort)

        ballDetector = BallDetector(
            hsvRange: BallDetector.HSVRange(
                hMin: settings.hMin,
                hMax: settings.hMax,
                sMin: settings.sMin,
                sMax: settings.sMax,
                vMin: settings.vMin,
                vMax: settings.vMax
            ),
            intrinsics: IntrinsicsStore.loadBallDetectorIntrinsics()
        )

        recorder = PitchRecorder()
        recorder.setCameraId(settings.cameraRole)
        recorder.onRecordingStarted = { [weak self] _ in
            DispatchQueue.main.async {
                self?.warningLabel.isHidden = true
            }
        }
        recorder.onCycleComplete = { [weak self] payload in
            guard let self else { return }
            let finishingClip = self.clipRecorder
            self.clipRecorder = nil
            log.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) frames=\(payload.frames.count) has_clip=\(finishingClip != nil)")
            let enriched = self.enrichedPayload(from: payload)
            if let finishingClip {
                finishingClip.finish { [weak self] videoURL in
                    self?.persistCompletedCycle(enriched, videoURL: videoURL)
                }
            } else {
                self.persistCompletedCycle(enriched, videoURL: nil)
            }
        }

        do {
            try payloadStore.ensureDirectory()
        } catch {
            lastUploadStatusText = "Store init failed: \(error.localizedDescription)"
        }

        uploader = ServerUploader(config: serverConfig)
        uploadQueue = PayloadUploadQueue(store: payloadStore, uploader: uploader)
        wireUploadQueueCallbacks()

        healthMonitor = ServerHealthMonitor(
            uploader: uploader,
            cameraId: settings.cameraRole,
            baseIntervalS: settings.pollInterval
        )
        wireHealthMonitorCallbacks()

        setupUI()
        setupPreviewAndCapture()
        setupAudioCapture()
        setupBallOverlay()
        setupDisplayLink()
        healthMonitor.start()
        updateUIForState()
    }

    @objc private func openCalibration() {
        let vc = CalibrationViewController()
        let nav = UINavigationController(rootViewController: vc)
        nav.modalPresentationStyle = .fullScreen
        present(nav, animated: true)
    }

    @objc private func openSettings() {
        let vc = SettingsViewController()
        vc.onDismiss = { [weak self] in
            self?.applyUpdatedSettings()
        }
        let nav = UINavigationController(rootViewController: vc)
        nav.modalPresentationStyle = .formSheet
        present(nav, animated: true)
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        applyUpdatedSettings()
        updateUIForState()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if !session.isRunning {
            session.startRunning()
        }
        healthMonitor.start()
        displayLink?.isPaused = false
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        healthMonitor.stop()
        displayLink?.isPaused = true
        if state == .timeSyncWaiting {
            cancelTimeSync()
        }
    }

    deinit {
        // CADisplayLink holds a strong reference to its target — without an
        // explicit invalidate the controller can't deallocate, and the
        // selector keeps firing on main. Pausing in viewWillDisappear is
        // not enough; the link must be invalidated for the retain cycle to
        // break. ServerHealthMonitor.stop() is belt-and-braces —
        // viewWillDisappear already calls it, but cover the "deinit without
        // viewWillDisappear" edge case too.
        displayLink?.invalidate()
        displayLink = nil
        healthMonitor?.stop()
    }

    // MARK: - Tracking controls

    func enterSyncMode() {
        guard state == .standby else { return }
        log.info("camera entering sync mode session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        recorder.reset()
        reloadBallDetectorWithLatestIntrinsics()
        try? uploadQueue.reloadPending()
        state = .syncWaiting
        warningLabel.isHidden = true
        updateUIForState()
        uploadQueue.processNextIfNeeded()
    }

    func exitSyncMode() {
        log.info("camera exiting sync mode session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public) state=\(self.stateText(self.state), privacy: .public)")
        state = .standby
        recorder.reset()
        // Tear down the clip writer on the capture queue so we can't race
        // with an in-flight `append` from captureOutput.
        processingQueue.async { [weak self] in
            self?.clipRecorder?.cancel()
            self?.clipRecorder = nil
        }
        uploadQueue.clearPending()
        warningLabel.isHidden = true
        updateUIForState()
    }

    private func persistCompletedCycle(
        _ payload: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        do {
            let fileURL = try payloadStore.save(payload, videoURL: videoURL)
            DispatchQueue.main.async {
                let suffix = videoURL != nil ? " (+video)" : ""
                self.lastUploadStatusText = "Cached \(payload.session_id)\(suffix)"
                self.uploadQueue.enqueue(fileURL)
                if self.returnToStandbyAfterCycle {
                    // A dashboard disarm arrived mid-recording; force-finish
                    // just flushed the cycle. Clean up instead of re-arming.
                    self.returnToStandbyAfterCycle = false
                    self.exitSyncMode()
                } else {
                    self.state = .syncWaiting
                    self.updateUIForState()
                }
            }
        } catch {
            log.error("camera cycle persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            // Even if the JSON save failed, drop any orphan tmp video.
            if let videoURL {
                try? FileManager.default.removeItem(at: videoURL)
            }
            DispatchQueue.main.async {
                self.lastUploadStatusText = "Cache failed: \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
                self.updateUIForState()
            }
        }
    }

    // MARK: - Time calibration (chirp anchor)

    @objc private func onTapTimeCalibration() {
        if state == .timeSyncWaiting {
            cancelTimeSync()
        } else if state == .standby {
            startTimeSync()
        }
        updateUIForState()
    }

    private func startTimeSync() {
        guard let detector = chirpDetector else {
            // Mic permission still pending or denied — try once more.
            setupAudioCapture()
            warningLabel.text = "正在啟動麥克風…"
            warningLabel.isHidden = false
            return
        }
        detector.reset()
        detector.onChirpDetected = { [weak self] event in
            guard let self else { return }
            DispatchQueue.main.async {
                self.completeTimeSync(event)
            }
        }
        state = .timeSyncWaiting
        warningLabel.text = "等待同步音頻觸發中… (把兩機並排，第三裝置播 chirp)"
        warningLabel.isHidden = false
        lastUploadStatusText = "Time sync: waiting for chirp"

        let work = DispatchWorkItem { [weak self] in
            guard let self, self.state == .timeSyncWaiting else { return }
            self.cancelTimeSync(reason: "timeout")
        }
        timeSyncTimeoutWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: work)
    }

    private func cancelTimeSync(reason: String = "cancelled") {
        log.info("camera cancel time-sync reason=\(reason, privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        latestChirpSnapshot = nil
        state = .standby
        lastUploadStatusText = "Time sync \(reason)"

        if reason == "timeout" {
            log.warning("camera time-sync timeout cam=\(self.settings.cameraRole, privacy: .public)")
            // Flash a red banner for 3 s so the operator notices the miss;
            // the HUD's warning label is otherwise yellow for the "waiting"
            // state and hiding it immediately on timeout made it easy to
            // miss that the chirp never arrived.
            let originalBg = warningLabel.backgroundColor
            let originalFg = warningLabel.textColor
            warningLabel.backgroundColor = .systemRed
            warningLabel.textColor = .white
            warningLabel.text = "時間校正逾時：確認 chirp 音訊與麥克風"
            warningLabel.isHidden = false
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let self else { return }
                self.warningLabel.isHidden = true
                self.warningLabel.backgroundColor = originalBg
                self.warningLabel.textColor = originalFg
            }
        } else {
            warningLabel.isHidden = true
        }
        updateUIForState()
    }

    private func completeTimeSync(_ event: AudioChirpDetector.ChirpEvent) {
        guard state == .timeSyncWaiting else { return }
        log.info("camera complete time-sync anchor_frame=\(event.anchorFrameIndex) anchor_ts=\(event.anchorTimestampS) cam=\(self.settings.cameraRole, privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        lastSyncAnchorFrameIndex = event.anchorFrameIndex
        lastSyncAnchorTimestampS = event.anchorTimestampS
        latestChirpSnapshot = nil
        state = .standby
        warningLabel.isHidden = true
        lastUploadStatusText = String(format: "Time sync OK @ %.3fs", event.anchorTimestampS)
        updateUIForState()
    }

    // MARK: - Capture setup

    private func setupPreviewAndCapture() {
        session.beginConfiguration()

        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                try configureCaptureFormat(
                    device,
                    targetWidth: settings.captureWidth,
                    targetHeight: settings.captureHeight,
                    targetFps: Double(settings.captureFps)
                )
                IntrinsicsStore.setHorizontalFov(horizontalFovRadians)

                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
                log.error("camera capture format configuration failed error=\(error.localizedDescription, privacy: .public)")
                // TODO: surface error to UI
            }
        }

        videoOutput.setSampleBufferDelegate(self, queue: processingQueue)
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        if session.canAddOutput(videoOutput) {
            session.addOutput(videoOutput)
        }

        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
        view.layer.insertSublayer(preview, at: 0)
        previewLayer = preview
    }

    private func configureCaptureFormat(
        _ device: AVCaptureDevice,
        targetWidth: Int,
        targetHeight: Int,
        targetFps: Double
    ) throws {
        var selected: AVCaptureDevice.Format?
        for format in device.formats {
            let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            let w = Int(dims.width)
            let h = Int(dims.height)
            let matchesRes = (w == targetWidth && h == targetHeight) || (w == targetHeight && h == targetWidth)
            let supportsFps = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= targetFps && range.maxFrameRate >= targetFps
            }
            if matchesRes && supportsFps {
                selected = format
                break
            }
        }
        guard let selected else { return }

        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }

        device.activeFormat = selected
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        device.activeVideoMinFrameDuration = frameDuration
        device.activeVideoMaxFrameDuration = frameDuration

        horizontalFovRadians = Double(device.activeFormat.videoFieldOfView) * Double.pi / 180.0
    }

    private func reconfigureCapture() {
        guard let device = currentCaptureDevice else { return }
        let wasRunning = session.isRunning
        if wasRunning { session.stopRunning() }
        try? configureCaptureFormat(
            device,
            targetWidth: settings.captureWidth,
            targetHeight: settings.captureHeight,
            targetFps: Double(settings.captureFps)
        )
        IntrinsicsStore.setHorizontalFov(horizontalFovRadians)
        if wasRunning { session.startRunning() }
    }

    private var currentCaptureDevice: AVCaptureDevice? {
        (session.inputs.compactMap { $0 as? AVCaptureDeviceInput }
            .first(where: { $0.device.hasMediaType(.video) }))?.device
    }

    // MARK: - Audio (chirp) capture

    private func setupAudioCapture() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self else { return }
            DispatchQueue.main.async {
                if granted {
                    self.configureAudioCapture()
                } else {
                    log.error("camera mic permission denied cam=\(self.settings.cameraRole, privacy: .public)")
                    self.lastUploadStatusText = "Microphone denied — time sync unavailable"
                    self.updateUIForState()
                }
            }
        }
    }

    private func configureAudioCapture() {
        guard chirpDetector == nil else { return }
        guard let mic = AVCaptureDevice.default(for: .audio) else { return }
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: mic)
        } catch {
            log.error("camera mic input init failed error=\(error.localizedDescription, privacy: .public)")
            lastUploadStatusText = "Mic input failed: \(error.localizedDescription)"
            return
        }
        session.beginConfiguration()
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            log.error("camera session rejected audio input cam=\(self.settings.cameraRole, privacy: .public)")
            lastUploadStatusText = "Session rejected audio input"
            return
        }
        session.addInput(input)

        let output = AVCaptureAudioDataOutput()
        guard session.canAddOutput(output) else {
            session.removeInput(input)
            session.commitConfiguration()
            return
        }
        session.addOutput(output)

        let detector = AudioChirpDetector(threshold: Float(settings.chirpThreshold))
        output.setSampleBufferDelegate(detector, queue: detector.deliveryQueue)
        session.commitConfiguration()

        audioInput = input
        audioOutput = output
        chirpDetector = detector
    }

    // MARK: - Layout + overlays

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    private func setupBallOverlay() {
        ballOverlayLayer.strokeColor = UIColor.systemGreen.cgColor
        ballOverlayLayer.fillColor = UIColor.clear.cgColor
        ballOverlayLayer.lineWidth = 3
        ballOverlayLayer.isHidden = true
        view.layer.addSublayer(ballOverlayLayer)
    }

    private func setupDisplayLink() {
        let link = CADisplayLink(target: self, selector: #selector(handleDisplayTick))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    @objc private func handleDisplayTick() {
        updateBallOverlay(
            centroidX: latestCentroidX,
            centroidY: latestCentroidY,
            imageWidth: latestImageWidth,
            imageHeight: latestImageHeight
        )
        updateChirpDebugOverlay()
    }

    private func updateChirpDebugOverlay() {
        guard state == .timeSyncWaiting, let detector = chirpDetector else {
            return
        }
        // Read-only access to the last snapshot; audio-queue writes may race
        // but the HUD only needs a near-instant view.
        latestChirpSnapshot = detector.lastSnapshot
        guard let snap = latestChirpSnapshot else { return }
        let statusText: String
        let color: UIColor
        if !snap.armed {
            statusText = "warming up"
            color = .systemGray
        } else if snap.triggered {
            statusText = "TRIGGER"
            color = .systemGreen
        } else if snap.lastPeak >= snap.threshold * 0.8 {
            statusText = "close"
            color = .systemOrange
        } else {
            statusText = "listening"
            color = .systemYellow
        }
        chirpDebugLabel.text = String(
            format: "peak %.2f / %.2f  buf %d  %@",
            snap.lastPeak, snap.threshold, snap.bufferFillSamples, statusText
        )
        chirpDebugLabel.textColor = color
    }

    // MARK: - Server health + upload queue wiring

    private func wireHealthMonitorCallbacks() {
        healthMonitor.onStatusChanged = { [weak self] _, _ in
            self?.updateUIForState()
        }
        healthMonitor.onHeartbeatSuccess = { [weak self] response in
            guard let self else { return }
            // Cache the server's session id so `startRecording` can stamp
            // it onto uploads without another round-trip. Nil when idle.
            self.currentSessionId = (response.session?.armed == true)
                ? response.session?.id
                : nil
            let cam = self.settings.cameraRole
            self.handleDashboardCommand(
                response.commands?[cam],
                sessionArmed: response.session?.armed ?? false
            )
        }
        healthMonitor.onLastContactTick = { [weak self] date in
            self?.updateLastContactLabel(from: date)
        }
    }

    private func wireUploadQueueCallbacks() {
        uploadQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        uploadQueue.onLastResultChanged = { [weak self] text in
            self?.lastResultText = text
            self?.updateUIForState()
        }
        uploadQueue.onUploadingChanged = { [weak self] _ in
            self?.updateUIForState()
        }
    }

    /// Dispatch the server's per-device command. Only reacts to state
    /// *transitions* (tracked via `lastAppliedCommand`) so a long-running
    /// armed session doesn't re-enter syncWaiting on every heartbeat.
    private func handleDashboardCommand(_ command: String?, sessionArmed: Bool) {
        defer { lastAppliedCommand = command }
        guard command != lastAppliedCommand else { return }
        switch command {
        case "arm":
            applyRemoteArm()
        case "disarm":
            applyRemoteDisarm()
        default:
            // No pending command. If the server considers us idle but we're
            // still mid-cycle, leave the local recording alone — it will
            // finish naturally via ball-absent or the hard max-duration cap.
            break
        }
        _ = sessionArmed  // reserved for future "did the session id change?" logic
    }

    private func applyRemoteArm() {
        log.info("camera received arm command state=\(self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            enterSyncMode()
        case .timeSyncWaiting, .syncWaiting, .recording, .uploading:
            // Already in an active state — nothing to do. `recording`
            // continues, `syncWaiting` is already waiting for a ball,
            // `uploading` will transition back to syncWaiting on its own.
            break
        }
    }

    private func applyRemoteDisarm() {
        log.info("camera received disarm command state=\(self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            break
        case .timeSyncWaiting:
            cancelTimeSync(reason: "disarmed")
        case .syncWaiting, .uploading:
            exitSyncMode()
        case .recording:
            // Flush whatever we have so the pitch isn't lost. The
            // cycle-complete handler will route us to standby thanks to
            // the flag below instead of bouncing back to syncWaiting.
            returnToStandbyAfterCycle = true
            processingQueue.async { [weak self] in
                self?.recorder.forceFinishIfRecording()
            }
        }
    }

    @objc private func onTapTestConnection() {
        healthMonitor.resetBackoff()
        healthMonitor.probeNow()
    }

    /// Settings-dismiss callback. Re-diffs UserDefaults and reconfigures
    /// anything settings-driven. Replaces the old per-poll diffing.
    private func applyUpdatedSettings() {
        let latest = SettingsViewController.loadFromUserDefaults()
        let serverChanged = latest.serverIP != settings.serverIP
            || latest.serverPort != settings.serverPort
        let formatChanged = latest.captureWidth != settings.captureWidth
            || latest.captureHeight != settings.captureHeight
            || latest.captureFps != settings.captureFps
        let chirpThresholdChanged = latest.chirpThreshold != settings.chirpThreshold
        let pollIntervalChanged = latest.pollInterval != settings.pollInterval
        let cameraRoleChanged = latest.cameraRole != settings.cameraRole

        settings = latest

        if serverChanged {
            serverConfig = ServerUploader.ServerConfig(serverIP: latest.serverIP, serverPort: latest.serverPort)
            uploader = ServerUploader(config: serverConfig)
            healthMonitor.updateUploader(uploader)
            uploadQueue.updateUploader(uploader)
        }
        if formatChanged {
            reconfigureCapture()
        }
        if chirpThresholdChanged {
            chirpDetector?.setThreshold(Float(latest.chirpThreshold))
        }
        if cameraRoleChanged {
            healthMonitor.updateCameraId(latest.cameraRole)
        }
        if pollIntervalChanged {
            healthMonitor.updateBaseInterval(latest.pollInterval)
        }
        recorder?.setCameraId(latest.cameraRole)
        reloadBallDetectorWithLatestIntrinsics()

        if serverChanged || pollIntervalChanged {
            // New endpoint or new cadence — invalidate in-flight probe, reset
            // backoff, and re-probe immediately so the HUD reflects reality.
            healthMonitor.resetBackoff()
            healthMonitor.probeNow()
        }
    }

    private func updateLastContactLabel(from date: Date?) {
        guard let date else {
            lastContactLabel.text = "Last contact: —"
            return
        }
        let s = Int(Date().timeIntervalSince(date))
        let text: String
        if s < 60 {
            text = "\(s)s ago"
        } else if s < 3600 {
            text = "\(s / 60)m \(s % 60)s ago"
        } else {
            text = "\(s / 3600)h ago"
        }
        lastContactLabel.text = "Last contact: \(text)"
    }

    private func setupUI() {
        func styleLabel(_ label: UILabel, size: CGFloat = 18, weight: UIFont.Weight = .semibold) {
            label.font = .systemFont(ofSize: size, weight: weight)
            label.textColor = .white
            label.numberOfLines = 0
            label.lineBreakMode = .byWordWrapping
        }

        styleLabel(topStatusLabel, size: 24, weight: .bold)
        styleLabel(serverStatusLabel, size: 18, weight: .medium)
        styleLabel(fpsLabel, size: 18, weight: .medium)
        styleLabel(uploadStatusLabel, size: 18, weight: .medium)
        styleLabel(lastResultLabel, size: 17, weight: .medium)
        styleLabel(lastContactLabel, size: 15, weight: .regular)
        lastContactLabel.textColor = .lightGray
        lastContactLabel.text = "Last contact: —"
        lastResultLabel.textColor = .systemGreen
        styleLabel(warningLabel, size: 22, weight: .bold)
        warningLabel.textColor = .systemYellow
        warningLabel.textAlignment = .center
        warningLabel.isHidden = true

        chirpDebugLabel.font = .monospacedDigitSystemFont(ofSize: 14, weight: .medium)
        chirpDebugLabel.textColor = .systemGray
        chirpDebugLabel.numberOfLines = 0
        chirpDebugLabel.textAlignment = .center
        chirpDebugLabel.isHidden = true

        serverStatusDot.backgroundColor = .systemRed
        serverStatusDot.layer.cornerRadius = 8
        serverStatusDot.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            serverStatusDot.widthAnchor.constraint(equalToConstant: 16),
            serverStatusDot.heightAnchor.constraint(equalToConstant: 16),
        ])

        let row1 = UIStackView(arrangedSubviews: [topStatusLabel])
        row1.axis = .horizontal
        row1.alignment = .center

        testConnectionButton.setTitle("Test", for: .normal)
        testConnectionButton.setTitleColor(.white, for: .normal)
        testConnectionButton.backgroundColor = UIColor.systemBlue.withAlphaComponent(0.85)
        testConnectionButton.titleLabel?.font = .systemFont(ofSize: 14, weight: .semibold)
        testConnectionButton.layer.cornerRadius = 8
        testConnectionButton.contentEdgeInsets = UIEdgeInsets(top: 4, left: 10, bottom: 4, right: 10)
        testConnectionButton.addTarget(self, action: #selector(onTapTestConnection), for: .touchUpInside)

        let row2 = UIStackView(arrangedSubviews: [serverStatusDot, serverStatusLabel, fpsLabel, testConnectionButton])
        row2.axis = .horizontal
        row2.spacing = 8
        row2.alignment = .center

        let row2b = UIStackView(arrangedSubviews: [lastContactLabel])
        row2b.axis = .horizontal
        row2b.alignment = .center

        let row3 = UIStackView(arrangedSubviews: [uploadStatusLabel])
        row3.axis = .horizontal
        row3.spacing = 12
        row3.alignment = .center

        let row4 = UIStackView(arrangedSubviews: [lastResultLabel])
        row4.axis = .horizontal
        row4.alignment = .center

        statusContainer.axis = .vertical
        statusContainer.spacing = 8
        statusContainer.translatesAutoresizingMaskIntoConstraints = false
        statusContainer.layoutMargins = UIEdgeInsets(top: 16, left: 18, bottom: 16, right: 18)
        statusContainer.isLayoutMarginsRelativeArrangement = true
        statusContainer.backgroundColor = UIColor.black.withAlphaComponent(0.45)
        statusContainer.layer.cornerRadius = 12
        statusContainer.addArrangedSubview(row1)
        statusContainer.addArrangedSubview(row2)
        statusContainer.addArrangedSubview(row2b)
        statusContainer.addArrangedSubview(row3)
        statusContainer.addArrangedSubview(row4)
        statusContainer.addArrangedSubview(warningLabel)
        statusContainer.addArrangedSubview(chirpDebugLabel)
        view.addSubview(statusContainer)

        func styleButton(_ button: UIButton, title: String, background: UIColor) {
            button.setTitle(title, for: .normal)
            button.setTitleColor(.white, for: .normal)
            button.backgroundColor = background.withAlphaComponent(0.92)
            button.titleLabel?.font = .systemFont(ofSize: 16, weight: .bold)
            button.layer.cornerRadius = 12
            button.contentEdgeInsets = UIEdgeInsets(top: 12, left: 16, bottom: 12, right: 16)
            button.translatesAutoresizingMaskIntoConstraints = false
        }

        styleButton(trackingButton, title: "啟動追蹤", background: .systemBlue)
        trackingButton.addTarget(self, action: #selector(onTapTracking), for: .touchUpInside)
        view.addSubview(trackingButton)

        NSLayoutConstraint.activate([
            statusContainer.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            statusContainer.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            statusContainer.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),

            trackingButton.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            trackingButton.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),
            trackingButton.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -16),
        ])
    }

    @objc private func onTapTracking() {
        if state == .standby {
            // Local escape-hatch: make sure a server session exists before
            // we enter syncWaiting, otherwise the first recording would
            // have no session_id to stamp onto its upload.
            if currentSessionId != nil {
                enterSyncMode()
            } else {
                trackingButton.isEnabled = false
                lastUploadStatusText = "Arming session…"
                updateUIForState()
                uploader.armSession { [weak self] result in
                    DispatchQueue.main.async {
                        guard let self else { return }
                        self.trackingButton.isEnabled = true
                        switch result {
                        case .success(let session):
                            self.currentSessionId = session.armed ? session.id : nil
                            if self.currentSessionId != nil {
                                self.enterSyncMode()
                            } else {
                                self.lastUploadStatusText = "Arm returned an ended session (\(session.end_reason ?? "?")). Try again."
                                self.updateUIForState()
                            }
                        case .failure(let error):
                            self.lastUploadStatusText = "Arm failed: \(error.localizedDescription)"
                            self.updateUIForState()
                        }
                    }
                }
            }
        } else {
            exitSyncMode()
        }
    }

    private func updateUIForState() {
        topStatusLabel.text = "State: \(stateText(state))"
        serverStatusLabel.text = "Server \(healthMonitor?.statusText ?? "unknown")"
        serverStatusDot.backgroundColor = serverReachableColor()
        fpsLabel.text = String(format: "FPS %.1f", fpsEstimate)
        uploadStatusLabel.text = "Upload: \(lastUploadStatusText)"
        lastResultLabel.text = "Last: \(lastResultText)"

        chirpDebugLabel.isHidden = (state != .timeSyncWaiting)

        switch state {
        case .standby:
            trackingButton.setTitle("啟動追蹤", for: .normal)
            trackingButton.backgroundColor = UIColor.systemBlue.withAlphaComponent(0.92)
            trackingButton.isEnabled = true
        case .timeSyncWaiting:
            trackingButton.setTitle("時間校正中…", for: .normal)
            trackingButton.backgroundColor = UIColor.systemGray.withAlphaComponent(0.85)
            trackingButton.isEnabled = false
        case .syncWaiting, .recording, .uploading:
            trackingButton.setTitle("停止追蹤", for: .normal)
            trackingButton.backgroundColor = UIColor.systemRed.withAlphaComponent(0.92)
            trackingButton.isEnabled = true
        }
    }

    private func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
        case .syncWaiting: return "SYNC_WAITING"
        case .recording: return "RECORDING"
        case .uploading: return "UPLOADING"
        }
    }

    private func serverReachableColor() -> UIColor {
        if uploadQueue?.isUploading ?? false {
            return .systemOrange
        }
        return (healthMonitor?.isReachable ?? false) ? .systemGreen : .systemRed
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
        if latestImageWidth != width || latestImageHeight != height {
            IntrinsicsStore.setImageDimensions(width: width, height: height)
        }

        let currentIndex = frameIndex
        frameIndex += 1

        // No ball detection during time sync — just advance frameIndex so
        // cycle start indexes line up across modes.
        if state == .timeSyncWaiting || state == .standby || state == .uploading {
            return
        }

        let detection = ballDetector.detect(
            pixelBuffer: pixelBuffer,
            imageWidth: width,
            imageHeight: height,
            horizontalFovRadians: horizontalFovRadians
        )
        let frame = ServerUploader.FramePayload(
            frame_index: currentIndex,
            timestamp_s: timestampS,
            theta_x_rad: detection.thetaXRad,
            theta_z_rad: detection.thetaZRad,
            px: detection.centroidX,
            py: detection.centroidY,
            ball_detected: detection.ballDetected
        )

        latestCentroidX = detection.centroidX
        latestCentroidY = detection.centroidY
        latestImageWidth = width
        latestImageHeight = height

        switch state {
        case .syncWaiting:
            recorder.handleFrame(frame)
            if detection.ballDetected {
                guard let sid = currentSessionId else {
                    // No armed session on the server — drop the ball
                    // sighting. This can happen in the short window between
                    // a dashboard disarm and the state transition to
                    // .standby; the next heartbeat will tidy up.
                    break
                }
                let anchorFrameIndex = lastSyncAnchorFrameIndex ?? currentIndex
                let anchorTimestampS = lastSyncAnchorTimestampS ?? timestampS
                recorder.startRecording(
                    sessionId: sid,
                    anchorFrameIndex: anchorFrameIndex,
                    anchorTimestampS: anchorTimestampS,
                    startFrameIndex: currentIndex,
                    startTimestampS: timestampS
                )
                startClipRecorder(width: width, height: height)
                clipRecorder?.append(sampleBuffer: sampleBuffer)
                state = .recording
                DispatchQueue.main.async {
                    self.updateUIForState()
                }
            }

        case .recording:
            clipRecorder?.append(sampleBuffer: sampleBuffer)
            recorder.handleFrame(frame)

        default:
            return
        }
    }

    private func startClipRecorder(width: Int, height: Int) {
        let tmpURL = payloadStore.makeTempVideoURL()
        let cr = ClipRecorder(outputURL: tmpURL)
        do {
            try cr.prepare(width: width, height: height)
            clipRecorder = cr
        } catch {
            // Clip writing is a Phase-1 experiment — if AVAssetWriter rejects
            // the configuration we degrade to JSON-only and keep recording.
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

    private func updateBallOverlay(centroidX: Double?, centroidY: Double?, imageWidth: Int, imageHeight: Int) {
        guard
            let previewLayer,
            let cx = centroidX,
            let cy = centroidY,
            imageWidth > 0,
            imageHeight > 0
        else {
            ballOverlayLayer.isHidden = true
            return
        }
        let normalized = CGPoint(
            x: CGFloat(cy / Double(imageHeight)),
            y: CGFloat(cx / Double(imageWidth))
        )
        let layerPoint = previewLayer.layerPointConverted(fromCaptureDevicePoint: normalized)
        let radius: CGFloat = 18
        let path = UIBezierPath(ovalIn: CGRect(x: layerPoint.x - radius, y: layerPoint.y - radius, width: radius * 2, height: radius * 2))
        ballOverlayLayer.path = path.cgPath
        ballOverlayLayer.isHidden = false
    }

    // MARK: - Payload enrichment

    private func enrichedPayload(from payload: ServerUploader.PitchPayload) -> ServerUploader.PitchPayload {
        let dims = IntrinsicsStore.loadImageDimensions()
        return ServerUploader.PitchPayload(
            camera_id: payload.camera_id,
            session_id: payload.session_id,
            sync_anchor_frame_index: payload.sync_anchor_frame_index,
            sync_anchor_timestamp_s: payload.sync_anchor_timestamp_s,
            local_recording_index: payload.local_recording_index,
            frames: payload.frames,
            intrinsics: IntrinsicsStore.loadIntrinsicsPayload(),
            homography: IntrinsicsStore.loadHomography(),
            image_width_px: dims?.width,
            image_height_px: dims?.height
        )
    }

    private func reloadBallDetectorWithLatestIntrinsics() {
        guard settings != nil else { return }
        ballDetector = BallDetector(
            hsvRange: BallDetector.HSVRange(
                hMin: settings.hMin,
                hMax: settings.hMax,
                sMin: settings.sMin,
                sMax: settings.sMax,
                vMin: settings.vMin,
                vMax: settings.vMax
            ),
            intrinsics: IntrinsicsStore.loadBallDetectorIntrinsics()
        )
    }

}
