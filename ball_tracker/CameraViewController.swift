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
/// Dashboard arm goes straight to `.recording`; dashboard cancel (or server
/// session timeout) is the sole exit back to standby. Server ingests the
/// MOV and does HSV detection + triangulation.
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

    // Session + outputs.
    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private let videoOutput = AVCaptureVideoDataOutput()
    // `.userInitiated` QoS is required for 240 fps frame delivery. With default
    // QoS the queue can be throttled by the scheduler which amplifies any
    // detection stalls into dropped-frame cascades (AVCaptureVideoDataOutput
    // has `alwaysDiscardsLateVideoFrames = true`).
    private let processingQueue = DispatchQueue(label: "camera.frame.queue", qos: .userInitiated)

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

    // Last observed capture dimensions; mirrored into IntrinsicsStore so
    // the payload enrichment path and the server-side detection pipeline
    // agree on what resolution the MOV was recorded at.
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0

    // Chirp detector snapshot for the HUD — written on audio queue.
    private var latestChirpSnapshot: AudioChirpDetector.Snapshot?

    // `.recording` was just entered but the ClipRecorder hasn't been
    // prepared yet — the very next captured sample will configure the
    // writer at the observed pixel dimensions.
    private var pendingRecordingBootstrap: Bool = false

    // Remote-control state (driven by the heartbeat response).
    /// Last command key we acted on for this device (`"arm"` / `"disarm"` /
    /// nil). Only state *transitions* cause local actions so repeated arm
    /// replies during an armed session don't re-trigger enterSyncMode.
    private var lastAppliedCommand: String?
    /// Reserved for future branching in the cycle-complete path (e.g. a
    /// re-arm that wants to skip the standby flash). Always true today
    /// because `.recording` always returns to `.standby` now.
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

    // Most recently recovered chirp anchor — session-clock PTS of the
    // chirp peak from the mic matched filter. Stamped onto outgoing
    // payloads as `sync_anchor_timestamp_s`. Nil until the user completes
    // a 時間校正; server rejects unpaired sessions whose anchor is nil.
    private var lastSyncAnchorTimestampS: Double?

    // Time-sync timeout task so we can cancel if the user aborts early.
    private var timeSyncTimeoutWork: DispatchWorkItem?

    // UI containers.
    private let statusContainer = UIStackView()
    private let topStatusLabel = PaddedLabel()
    private let serverStatusDot = UIView()
    private let serverStatusLabel = UILabel()
    private let fpsLabel = UILabel()
    private let uploadStatusLabel = UILabel()
    private let lastResultLabel = UILabel()
    private let warningLabel = UILabel()
    private let chirpDebugLabel = UILabel()
    private let lastContactLabel = UILabel()
    private let testConnectionButton = UIButton(type: .system)

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

        recorder = PitchRecorder()
        recorder.setCameraId(settings.cameraRole)
        recorder.onRecordingStarted = { [weak self] _ in
            DispatchQueue.main.async {
                self?.warningLabel.isHidden = true
                // Start-of-recording feedback: short tone + a medium
                // haptic so the operator registers the transition even
                // if looking away from the screen.
                AudioServicesPlaySystemSound(self?.startRecSoundID ?? 0)
                self?.startRecHaptic.impactOccurred()
            }
        }
        recorder.onCycleComplete = { [weak self] payload in
            guard let self else { return }
            let finishingClip = self.clipRecorder
            self.clipRecorder = nil
            log.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
            // End-of-recording feedback — haptic + system sound so the
            // operator knows the cycle finished without looking down.
            DispatchQueue.main.async {
                AudioServicesPlaySystemSound(self.endRecSoundID)
                self.endRecHaptic.notificationOccurred(.success)
            }
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

    // MARK: - Recording controls

    /// Dashboard arm landed — bump the capture rate to 240 fps and move to
    /// `.recording`. ClipRecorder is *not* created here; we defer that to
    /// the first captureOutput so we can use the real pixel-buffer
    /// dimensions instead of the Settings-declared 1920×1080. The
    /// PitchRecorder is also started from captureOutput, once the first
    /// appended sample's session-clock PTS is known.
    func enterRecordingMode() {
        guard state == .standby else { return }
        log.info("camera entering recording session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        warningLabel.isHidden = true
        switchCaptureFps(trackingFps)
        recorder.reset()
        try? uploadQueue.reloadPending()
        pendingRecordingBootstrap = true
        state = .recording
        updateUIForState()
        uploadQueue.processNextIfNeeded()
        // Acknowledge arm with a light tap; pre-warm the next two haptics
        // so their first fire isn't lazy-initialised.
        armHaptic.impactOccurred()
        startRecHaptic.prepare()
        endRecHaptic.prepare()
    }

    /// Return to `.standby` after a cycle was flushed (or the recording
    /// never produced a frame between arm and disarm). Clears any live
    /// clip writer on the processing queue so we can't race with an
    /// in-flight `append` from captureOutput.
    func exitRecordingToStandby() {
        log.info("camera exit recording → standby session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public) state=\(self.stateText(self.state), privacy: .public)")
        state = .standby
        pendingRecordingBootstrap = false
        recorder.reset()
        processingQueue.async { [weak self] in
            self?.clipRecorder?.cancel()
            self?.clipRecorder = nil
        }
        uploadQueue.clearPending()
        warningLabel.isHidden = true
        switchCaptureFps(standbyFps)
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
                self.lastUploadStatusText = "Cache failed: \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
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
        lastSyncAnchorTimestampS = event.anchorTimestampS
        // Surface the freshly-acquired anchor to the dashboard via the
        // next heartbeat so the sidebar's "time sync" dot flips green.
        healthMonitor?.updateTimeSynced(true)
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
                    targetFps: standbyFps
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
        // App is locked to landscape (Info.plist). Pin the preview connection
        // to sensor-native angle 0 so the on-screen image matches the raw
        // CVPixelBuffer orientation ArUco calibration consumes; a stale
        // 90° rotation here was why "holding phone landscape" still rendered
        // a portrait-oriented preview.
        if let connection = preview.connection, connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        view.layer.insertSublayer(preview, at: 0)
        previewLayer = preview
    }

    enum CaptureFormatError: LocalizedError {
        case noMatchingFormat(width: Int, height: Int, fps: Double)

        var errorDescription: String? {
            switch self {
            case .noMatchingFormat(let w, let h, let fps):
                return "No AVCaptureDevice.Format matches \(w)×\(h) @ \(Int(fps)) fps"
            }
        }
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
        guard let selected else {
            throw CaptureFormatError.noMatchingFormat(width: targetWidth, height: targetHeight, fps: targetFps)
        }

        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }

        device.activeFormat = selected
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        device.activeVideoMinFrameDuration = frameDuration
        device.activeVideoMaxFrameDuration = frameDuration

        // Cap the AE exposure time to the target frame duration. Without this,
        // iOS lengthens individual exposures in low light to brighten the
        // image, which drags the effective capture rate down (a room that
        // wants 70 ms exposures drops a "240 fps" session to ~14 fps). Capped
        // AE keeps the sensor locked to the target rate and compensates with
        // ISO — noisier in dim rooms, but frame rate holds.
        let lo = selected.minExposureDuration
        let hi = selected.maxExposureDuration
        let capped: CMTime
        if CMTimeCompare(frameDuration, lo) < 0 {
            capped = lo
        } else if CMTimeCompare(frameDuration, hi) > 0 {
            capped = hi
        } else {
            capped = frameDuration
        }
        device.activeMaxExposureDuration = capped
        device.exposureMode = .continuousAutoExposure

        horizontalFovRadians = Double(device.activeFormat.videoFieldOfView) * Double.pi / 180.0
    }

    /// Swap the active capture format to a new frame rate at the current
    /// fixed resolution. Used for the idle↔tracking FPS transition
    /// (`standbyFps` ↔ `trackingFps`) triggered by enter/exit of sync mode,
    /// and by the resolution-change path in `applyUpdatedSettings`. The
    /// format swap requires `stopRunning → activeFormat = X → startRunning`,
    /// which blocks the session for ~300-500 ms — deliberately called only
    /// at moments with no time-critical frame work in flight (entering
    /// sync while the user is placing the ball, or exiting back to idle).
    private func switchCaptureFps(_ targetFps: Double) {
        guard let device = currentCaptureDevice else { return }
        let wasRunning = session.isRunning
        if wasRunning { session.stopRunning() }
        defer { if wasRunning { session.startRunning() } }

        do {
            try configureCaptureFormat(
                device,
                targetWidth: settings.captureWidth,
                targetHeight: settings.captureHeight,
                targetFps: targetFps
            )
            IntrinsicsStore.setHorizontalFov(horizontalFovRadians)
            // Read back the actually-applied rate so an operator can tell
            // from logs whether the sensor is honouring our request. 240 fps
            // formats typically crop the sensor ROI, so `videoFieldOfView`
            // may differ between 60 and 240 fps — log it per switch so any
            // FOV-approximation intrinsics drift is visible.
            let applied = device.activeVideoMinFrameDuration
            let appliedFps = applied.value > 0 ? Double(applied.timescale) / Double(applied.value) : 0
            log.info("camera fps switched target=\(targetFps) applied=\(appliedFps) fov_rad=\(self.horizontalFovRadians)")
        } catch {
            log.error("camera fps switch failed target=\(targetFps) error=\(error.localizedDescription, privacy: .public)")
            warningLabel.text = "FPS 切換失敗 (\(Int(targetFps))fps 不支援)"
            warningLabel.isHidden = false
        }
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
        // Border path follows the root view bounds; regenerated on every
        // layout pass so rotation / safe-area changes stay in sync.
        stateBorderLayer.frame = view.bounds
        stateBorderLayer.path = UIBezierPath(rect: view.bounds).cgPath
    }

    private func setupDisplayLink() {
        let link = CADisplayLink(target: self, selector: #selector(handleDisplayTick))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    @objc private func handleDisplayTick() {
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
    /// *transitions* (tracked via `lastAppliedCommand`) so repeated arm
    /// replies during an armed session don't re-trigger enter.
    private func handleDashboardCommand(_ command: String?, sessionArmed: Bool) {
        defer { lastAppliedCommand = command }
        guard command != lastAppliedCommand else { return }
        switch command {
        case "arm":
            applyRemoteArm()
        case "disarm":
            applyRemoteDisarm()
        default:
            // No pending command. Recording only ends via an explicit
            // disarm — never a silent fallthrough.
            break
        }
        _ = sessionArmed  // reserved for future "did the session id change?" logic
    }

    private func applyRemoteArm() {
        log.info("camera received arm command state=\(self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            enterRecordingMode()
        case .timeSyncWaiting, .recording, .uploading:
            // Active state — arm is a no-op. `.timeSyncWaiting` finishes
            // on its own and returns to standby; next heartbeat re-sends
            // the arm command and this branch flips us into recording.
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
        case .uploading:
            // Upload flow runs its own course; state transitions back to
            // standby via persistCompletedCycle once the queue accepts.
            break
        case .recording:
            returnToStandbyAfterCycle = true
            processingQueue.async { [weak self] in
                guard let self else { return }
                if self.recorder.isActive {
                    // Normal path: flush whatever frames the clip contains;
                    // onCycleComplete drives persist + standby transition.
                    self.recorder.forceFinishIfRecording()
                } else {
                    // Disarm arrived before the first frame reached us
                    // (happens if cancel is pressed in the ~300 ms fps
                    // switch window). Tear down cleanly on main.
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
            // Resolution is currently fixed (captureWidthFixed/HeightFixed) so
            // this branch only fires on a stale-prefs migration. Re-pick a
            // format at whichever FPS is appropriate for the current state.
            let fps = (state == .standby || state == .timeSyncWaiting) ? standbyFps : trackingFps
            switchCaptureFps(fps)
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

        topStatusLabel.font = .systemFont(ofSize: 30, weight: .heavy)
        topStatusLabel.textColor = .black
        topStatusLabel.numberOfLines = 1
        topStatusLabel.textAlignment = .center
        topStatusLabel.layer.cornerRadius = 14
        topStatusLabel.layer.masksToBounds = true
        topStatusLabel.adjustsFontSizeToFitWidth = true
        topStatusLabel.minimumScaleFactor = 0.65
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

        let row1 = UIStackView(arrangedSubviews: [topStatusLabel, UIView()])
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

        NSLayoutConstraint.activate([
            statusContainer.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            statusContainer.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 12),
            statusContainer.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
        ])

        setupStateBorder()
        setupRecIndicator()
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
        recIndicator.backgroundColor = UIColor.black.withAlphaComponent(0.55)
        recIndicator.layer.cornerRadius = 14
        recIndicator.layer.borderColor = UIColor.systemRed.cgColor
        recIndicator.layer.borderWidth = 1
        recIndicator.isHidden = true

        recDotView.backgroundColor = .systemRed
        recDotView.layer.cornerRadius = 7
        recDotView.translatesAutoresizingMaskIntoConstraints = false

        recTimerLabel.text = "REC 0.0s"
        recTimerLabel.textColor = .white
        recTimerLabel.font = .monospacedDigitSystemFont(ofSize: 16, weight: .bold)
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
        serverStatusLabel.text = "Server \(healthMonitor?.statusText ?? "unknown")"
        serverStatusDot.backgroundColor = serverReachableColor()
        fpsLabel.text = String(format: "FPS %.1f", fpsEstimate)
        uploadStatusLabel.text = "Upload: \(lastUploadStatusText)"
        lastResultLabel.text = "Last: \(lastResultText)"

        chirpDebugLabel.isHidden = (state != .timeSyncWaiting)
        applyStateVisuals()
    }

    private func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
        case .recording: return "RECORDING"
        case .uploading: return "UPLOADING"
        }
    }

    // MARK: - State visuals

    private func applyStateVisuals() {
        let cfg = stateVisualConfig(for: state)

        // Border
        stateBorderLayer.strokeColor = cfg.borderColor.cgColor
        stateBorderLayer.lineWidth = cfg.borderWidth
        stateBorderLayer.removeAnimation(forKey: "pulse")
        if cfg.pulse {
            let anim = CABasicAnimation(keyPath: "opacity")
            anim.fromValue = 0.35
            anim.toValue = 1.0
            anim.duration = 0.85
            anim.autoreverses = true
            anim.repeatCount = .infinity
            stateBorderLayer.add(anim, forKey: "pulse")
        } else {
            stateBorderLayer.opacity = 1.0
        }

        // Chip
        topStatusLabel.text = cfg.chipText
        topStatusLabel.backgroundColor = cfg.chipBg
        topStatusLabel.textColor = cfg.chipFg

        // REC indicator
        if state == .recording {
            recIndicator.isHidden = false
            startRecTimer()
        } else {
            recIndicator.isHidden = true
            stopRecTimer()
        }
    }

    private struct StateVisualConfig {
        let borderColor: UIColor
        let borderWidth: CGFloat
        let pulse: Bool
        let chipText: String
        let chipBg: UIColor
        let chipFg: UIColor
    }

    private func stateVisualConfig(for s: AppState) -> StateVisualConfig {
        switch s {
        case .standby:
            return .init(
                borderColor: UIColor.white.withAlphaComponent(0.15), borderWidth: 2, pulse: false,
                chipText: "STANDBY",
                chipBg: UIColor(white: 0.25, alpha: 0.9), chipFg: .white
            )
        case .timeSyncWaiting:
            return .init(
                borderColor: .systemBlue, borderWidth: 8, pulse: true,
                chipText: "TIME SYNC",
                chipBg: UIColor.systemBlue.withAlphaComponent(0.95), chipFg: .white
            )
        case .recording:
            return .init(
                borderColor: .systemRed, borderWidth: 14, pulse: false,
                chipText: "● RECORDING",
                chipBg: UIColor.systemRed.withAlphaComponent(0.95), chipFg: .white
            )
        case .uploading:
            return .init(
                borderColor: .systemOrange, borderWidth: 6, pulse: false,
                chipText: "UPLOADING",
                chipBg: UIColor.systemOrange.withAlphaComponent(0.95), chipFg: .black
            )
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
        latestImageWidth = width
        latestImageHeight = height

        frameIndex += 1

        // Only the `.recording` state cares about samples. Phone is a pure
        // capture client — no ball detection, no overlay, no per-frame
        // payload work. Idle / time-sync / uploading all just drop through.
        guard state == .recording else { return }

        // Lazy-bootstrap the ClipRecorder from the first real sample's
        // dimensions (deferred from enterRecordingMode so we can key off
        // whatever the sensor is actually delivering post-fps-switch).
        if pendingRecordingBootstrap {
            pendingRecordingBootstrap = false
            startClipRecorder(width: width, height: height)
            if clipRecorder == nil {
                // Prepare failed. Fall back to standby on main.
                log.error("camera clip bootstrap failed session=\(self.currentSessionId ?? "nil", privacy: .public)")
                DispatchQueue.main.async {
                    self.returnToStandbyAfterCycle = false
                    self.exitRecordingToStandby()
                }
                return
            }
        }

        guard let clip = clipRecorder else { return }
        clip.append(sampleBuffer: sampleBuffer)

        // First successful append kicks off the PitchRecorder so its
        // payload carries the session-clock PTS of the MOV's first frame.
        if !recorder.isActive, let firstPTS = clip.firstSamplePTS {
            let sid = currentSessionId ?? ""
            if sid.isEmpty {
                // Armed locally without a server session id — shouldn't
                // happen (heartbeat sets it before arm fires), but bail
                // rather than uploading a payload the server will 422.
                log.error("camera recording started without session_id cam=\(self.settings.cameraRole, privacy: .public)")
                return
            }
            recorder.startRecording(
                sessionId: sid,
                anchorTimestampS: lastSyncAnchorTimestampS,
                videoStartPtsS: CMTimeGetSeconds(firstPTS),
                videoFps: trackingFps
            )
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

    // MARK: - Payload enrichment

    private func enrichedPayload(from payload: ServerUploader.PitchPayload) -> ServerUploader.PitchPayload {
        let dims = IntrinsicsStore.loadImageDimensions()
        return ServerUploader.PitchPayload(
            camera_id: payload.camera_id,
            session_id: payload.session_id,
            sync_anchor_timestamp_s: payload.sync_anchor_timestamp_s,
            video_start_pts_s: payload.video_start_pts_s,
            video_fps: payload.video_fps,
            local_recording_index: payload.local_recording_index,
            intrinsics: IntrinsicsStore.loadIntrinsicsPayload(),
            homography: IntrinsicsStore.loadHomography(),
            image_width_px: dims?.width,
            image_height_px: dims?.height
        )
    }
}

/// UILabel with per-edge inset — gives a colored background chip real
/// left/right padding without resorting to NSAttributedString hacks.
final class PaddedLabel: UILabel {
    var contentInsets = UIEdgeInsets(top: 6, left: 14, bottom: 6, right: 14)

    override func drawText(in rect: CGRect) {
        super.drawText(in: rect.inset(by: contentInsets))
    }

    override var intrinsicContentSize: CGSize {
        let s = super.intrinsicContentSize
        return CGSize(
            width: s.width + contentInsets.left + contentInsets.right,
            height: s.height + contentInsets.top + contentInsets.bottom
        )
    }
}
