import UIKit
import AVFoundation
import CoreMedia
import CoreVideo

/// Main camera view. State machine:
/// - STANDBY: live preview, chirp detector off, no frame buffering
/// - TIME_SYNC_WAITING: listens on the mic for the reference chirp, saves
///   session-clock PTS of the peak as the anchor
/// - SYNC_WAITING: tracking armed, waiting for ball to enter frame
/// - RECORDING: cycle in progress
/// - UPLOADING: cycle persisted, handed to the upload queue
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

    private var serverStatusTextValue: String = "unknown"
    private var isServerReachable: Bool = false
    private var statusPollTimer: Timer?
    private let ballOverlayLayer = CAShapeLayer()

    // Payload persistence + upload queue.
    private let payloadStore = PitchPayloadStore()
    private var pendingPayloadFiles: [URL] = []
    private var isUploadingPayload: Bool = false

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

    // UserDefaults keys for calibration-derived intrinsics.
    private static let keyHorizontalFovRad = "horizontal_fov_rad"
    private static let keyImageWidthPx = "image_width_px"
    private static let keyImageHeightPx = "image_height_px"
    private static let keyIntrinsicCx = "intrinsic_cx"
    private static let keyIntrinsicCy = "intrinsic_cy"
    private static let keyIntrinsicFx = "intrinsic_fx"
    private static let keyIntrinsicFz = "intrinsic_fz"
    private static let keyIntrinsicDistortion = "intrinsic_distortion"

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

        let intrinsics = loadIntrinsicsFromUserDefaults()

        ballDetector = BallDetector(
            hsvRange: BallDetector.HSVRange(
                hMin: settings.hMin,
                hMax: settings.hMax,
                sMin: settings.sMin,
                sMax: settings.sMax,
                vMin: settings.vMin,
                vMax: settings.vMax
            ),
            intrinsics: intrinsics
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
            let enriched = self.enrichedPayload(from: payload)
            self.persistCompletedCycle(enriched)
        }

        do {
            try payloadStore.ensureDirectory()
            pendingPayloadFiles = try payloadStore.listPayloadFiles()
        } catch {
            DispatchQueue.main.async {
                self.lastUploadStatusText = "Store init failed: \(error.localizedDescription)"
            }
        }

        uploader = ServerUploader(config: serverConfig)

        setupUI()
        setupPreviewAndCapture()
        setupAudioCapture()
        setupBallOverlay()
        setupDisplayLink()
        startStatusPolling()
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
        let nav = UINavigationController(rootViewController: vc)
        nav.modalPresentationStyle = .formSheet
        present(nav, animated: true)
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        reloadSettingsFromUserDefaults()
        reloadBallDetectorWithLatestIntrinsics()
        updateUIForState()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if !session.isRunning {
            session.startRunning()
        }
        startStatusPolling()
        displayLink?.isPaused = false
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        statusPollTimer?.invalidate()
        statusPollTimer = nil
        displayLink?.isPaused = true
        if state == .timeSyncWaiting {
            cancelTimeSync()
        }
    }

    // MARK: - Tracking controls

    func enterSyncMode() {
        guard state == .standby else { return }
        recorder.reset()
        reloadBallDetectorWithLatestIntrinsics()
        pendingPayloadFiles.removeAll(keepingCapacity: true)
        if let files = try? payloadStore.listPayloadFiles() {
            pendingPayloadFiles.append(contentsOf: files)
        }
        isUploadingPayload = false
        state = .syncWaiting
        warningLabel.isHidden = true
        updateUIForState()
        processNextPayloadIfNeeded()
    }

    func exitSyncMode() {
        state = .standby
        recorder.reset()
        pendingPayloadFiles.removeAll(keepingCapacity: true)
        isUploadingPayload = false
        warningLabel.isHidden = true
        updateUIForState()
    }

    private func persistCompletedCycle(_ payload: ServerUploader.PitchPayload) {
        do {
            let fileURL = try payloadStore.save(payload)
            DispatchQueue.main.async {
                self.state = .syncWaiting
                self.lastUploadStatusText = "Cached pitch \(payload.cycle_number)"
                self.enqueuePayloadForUpload(fileURL)
                self.updateUIForState()
            }
        } catch {
            DispatchQueue.main.async {
                self.lastUploadStatusText = "Cache failed: \(error.localizedDescription)"
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
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        latestChirpSnapshot = nil
        state = .standby
        warningLabel.isHidden = true
        lastUploadStatusText = "Time sync \(reason)"
        updateUIForState()
    }

    private func completeTimeSync(_ event: AudioChirpDetector.ChirpEvent) {
        guard state == .timeSyncWaiting else { return }
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
                UserDefaults.standard.set(horizontalFovRadians, forKey: Self.keyHorizontalFovRad)

                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
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
        UserDefaults.standard.set(horizontalFovRadians, forKey: Self.keyHorizontalFovRad)
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
            lastUploadStatusText = "Mic input failed: \(error.localizedDescription)"
            return
        }
        session.beginConfiguration()
        guard session.canAddInput(input) else {
            session.commitConfiguration()
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

        let detector = AudioChirpDetector()
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

    private func startStatusPolling() {
        statusPollTimer?.invalidate()
        statusPollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollServerStatus()
        }
        statusPollTimer?.tolerance = 0.5
        pollServerStatus()
    }

    private func pollServerStatus() {
        let latest = SettingsViewController.loadFromUserDefaults()
        let serverChanged = latest.serverIP != settings.serverIP || latest.serverPort != settings.serverPort
        let formatChanged = latest.captureWidth != settings.captureWidth
            || latest.captureHeight != settings.captureHeight
            || latest.captureFps != settings.captureFps
        settings = latest
        if serverChanged {
            serverConfig = ServerUploader.ServerConfig(serverIP: latest.serverIP, serverPort: latest.serverPort)
            uploader = ServerUploader(config: serverConfig)
        }
        if formatChanged {
            reconfigureCapture()
        }
        uploader.fetchStatus { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .success(let dict):
                    self.isServerReachable = true
                    if let state = dict["state"] as? String {
                        self.serverStatusTextValue = state
                    } else {
                        self.serverStatusTextValue = "reachable"
                    }
                case .failure:
                    self.isServerReachable = false
                    self.serverStatusTextValue = "offline"
                }
                self.updateUIForState()
            }
        }
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

        let row2 = UIStackView(arrangedSubviews: [serverStatusDot, serverStatusLabel, fpsLabel])
        row2.axis = .horizontal
        row2.spacing = 8
        row2.alignment = .center

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
            enterSyncMode()
        } else {
            exitSyncMode()
        }
    }

    private func updateUIForState() {
        topStatusLabel.text = "State: \(stateText(state))"
        serverStatusLabel.text = "Server \(serverReachableText())"
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

    private func serverReachableText() -> String {
        return serverStatusTextValue
    }

    private func serverReachableColor() -> UIColor {
        if isUploadingPayload {
            return .systemOrange
        }
        return isServerReachable ? .systemGreen : .systemRed
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
            UserDefaults.standard.set(width, forKey: Self.keyImageWidthPx)
            UserDefaults.standard.set(height, forKey: Self.keyImageHeightPx)
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
                let anchorFrameIndex = lastSyncAnchorFrameIndex ?? currentIndex
                let anchorTimestampS = lastSyncAnchorTimestampS ?? timestampS
                recorder.startRecording(
                    anchorFrameIndex: anchorFrameIndex,
                    anchorTimestampS: anchorTimestampS,
                    startFrameIndex: currentIndex
                )
                state = .recording
                DispatchQueue.main.async {
                    self.updateUIForState()
                }
            }

        case .recording:
            recorder.handleFrame(frame)

        default:
            return
        }
    }

    private func enqueuePayloadForUpload(_ fileURL: URL) {
        pendingPayloadFiles.append(fileURL)
        processNextPayloadIfNeeded()
    }

    private func processNextPayloadIfNeeded() {
        guard !isUploadingPayload else { return }
        guard !pendingPayloadFiles.isEmpty else { return }

        isUploadingPayload = true
        let fileURL = pendingPayloadFiles.removeFirst()

        DispatchQueue.main.async {
            self.lastUploadStatusText = "Uploading cached pitch..."
            self.updateUIForState()
        }

        let payload: ServerUploader.PitchPayload
        do {
            payload = try payloadStore.load(fileURL)
        } catch {
            isUploadingPayload = false
            lastUploadStatusText = "Cache read failed: \(error.localizedDescription)"
            updateUIForState()
            processNextPayloadIfNeeded()
            return
        }

        uploader.uploadPitch(payload) { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                var retryAfterFailure = false
                switch result {
                case .success(let response):
                    self.payloadStore.delete(fileURL)
                    self.lastUploadStatusText = "Uploaded pitch \(payload.cycle_number)"
                    self.lastResultText = self.formatResultSummary(response)
                case .failure(let error):
                    self.lastUploadStatusText = "Upload failed: \(error.localizedDescription)"
                    self.pendingPayloadFiles.insert(fileURL, at: 0)
                    retryAfterFailure = true
                }
                self.isUploadingPayload = false
                self.updateUIForState()
                if retryAfterFailure {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                        self.processNextPayloadIfNeeded()
                    }
                } else {
                    self.processNextPayloadIfNeeded()
                }
            }
        }
    }

    private func formatResultSummary(_ r: ServerUploader.PitchUploadResponse) -> String {
        if let err = r.error, !err.isEmpty {
            return "#\(r.cycle) ✗ \(err)"
        }
        if !r.paired {
            return "#\(r.cycle) 已收 (等待另一相機)"
        }
        if r.triangulated_points == 0 {
            return "#\(r.cycle) ✗ 0 pts (時間窗口未對齊?)"
        }
        let gapMm = (r.mean_residual_m ?? 0) * 1000.0
        let peakZ = r.peak_z_m ?? 0
        let dur = r.duration_s ?? 0
        return String(format: "#%d ✓ %d pts gap=%.0fmm peak=%.2fm dur=%.2fs",
                      r.cycle, r.triangulated_points, gapMm, peakZ, dur)
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
        let d = UserDefaults.standard
        var intrinsics: ServerUploader.IntrinsicsPayload? = nil
        if
            d.object(forKey: Self.keyIntrinsicFx) != nil,
            d.object(forKey: Self.keyIntrinsicFz) != nil,
            d.object(forKey: Self.keyIntrinsicCx) != nil,
            d.object(forKey: Self.keyIntrinsicCy) != nil
        {
            var distortion: [Double]? = nil
            if let arr = d.array(forKey: Self.keyIntrinsicDistortion) as? [Double], arr.count == 5 {
                distortion = arr
            }
            intrinsics = ServerUploader.IntrinsicsPayload(
                fx: d.double(forKey: Self.keyIntrinsicFx),
                fz: d.double(forKey: Self.keyIntrinsicFz),
                cx: d.double(forKey: Self.keyIntrinsicCx),
                cy: d.double(forKey: Self.keyIntrinsicCy),
                distortion: distortion
            )
        }
        let homography = d.array(forKey: "homography_3x3") as? [Double]
        let w = d.integer(forKey: Self.keyImageWidthPx)
        let h = d.integer(forKey: Self.keyImageHeightPx)
        return ServerUploader.PitchPayload(
            camera_id: payload.camera_id,
            sync_anchor_frame_index: payload.sync_anchor_frame_index,
            sync_anchor_timestamp_s: payload.sync_anchor_timestamp_s,
            cycle_number: payload.cycle_number,
            frames: payload.frames,
            intrinsics: intrinsics,
            homography: homography,
            image_width_px: w > 0 ? w : nil,
            image_height_px: h > 0 ? h : nil
        )
    }

    private func loadIntrinsicsFromUserDefaults() -> BallDetector.Intrinsics? {
        let d = UserDefaults.standard
        let hasAll =
            d.object(forKey: Self.keyIntrinsicFx) != nil &&
            d.object(forKey: Self.keyIntrinsicFz) != nil &&
            d.object(forKey: Self.keyIntrinsicCx) != nil &&
            d.object(forKey: Self.keyIntrinsicCy) != nil
        guard hasAll else { return nil }
        let fx = d.double(forKey: Self.keyIntrinsicFx)
        let fz = d.double(forKey: Self.keyIntrinsicFz)
        let cx = d.double(forKey: Self.keyIntrinsicCx)
        let cy = d.double(forKey: Self.keyIntrinsicCy)
        if fx == 0 || fz == 0 { return nil }
        return BallDetector.Intrinsics(cx: cx, cy: cy, fx: fx, fz: fz)
    }

    private func reloadBallDetectorWithLatestIntrinsics() {
        guard settings != nil else { return }
        let intrinsics = loadIntrinsicsFromUserDefaults()
        ballDetector = BallDetector(
            hsvRange: BallDetector.HSVRange(
                hMin: settings.hMin,
                hMax: settings.hMax,
                sMin: settings.sMin,
                sMax: settings.sMax,
                vMin: settings.vMin,
                vMax: settings.vMax
            ),
            intrinsics: intrinsics
        )
    }

    private func reloadSettingsFromUserDefaults() {
        settings = SettingsViewController.loadFromUserDefaults()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: settings.serverPort)
        uploader = ServerUploader(config: serverConfig)
        if recorder == nil { return }
        recorder.setCameraId(settings.cameraRole)
    }
}
