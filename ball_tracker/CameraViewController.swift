import UIKit
import AVFoundation
import CoreMedia
import CoreVideo

/// Main camera view:
/// - STANDBY: live preview, flash detection OFF, no frame buffering
/// - SYNC_WAITING: flash detection ON, circular pre-roll buffering ON
/// - RECORDING: realtime per-frame detection and pitch buffering
/// - UPLOADING: upload cached pitch payloads
final class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    enum AppState {
        case standby
        case timeSyncWaiting   // dedicated mode: flash-only detection for clock alignment
        case syncWaiting       // tracking armed, waiting for ball to enter frame
        case recording         // cycle in progress
        case uploading
    }

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private let videoOutput = AVCaptureVideoDataOutput()
    private let processingQueue = DispatchQueue(label: "camera.frame.queue")

    private var state: AppState = .standby
    private var frameIndex: Int = 0
    private var horizontalFovRadians: Double = 1.0

    private var settings: SettingsViewController.Settings!

    private var ballDetector: BallDetector!
    private var flashDetector: FlashDetector!
    private var recorder: PitchRecorder!
    private var uploader: ServerUploader!

    private var serverConfig: ServerUploader.ServerConfig!
    private var lastUploadStatusText: String = "Idle"
    private var lastResultText: String = "(尚無結果)"
    private var lastFrameTimestampForFps: CFTimeInterval = CACurrentMediaTime()
    private var framesSinceLastFpsTick: Int = 0
    private var fpsEstimate: Double = 0
    private var displayLink: CADisplayLink?
    // Latest detection snapshot — written by capture queue, read by display link on main.
    // Detection still runs at full capture rate; only the visual is drawn at display refresh.
    private var latestCentroidX: Double?
    private var latestCentroidY: Double?
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0
    private var serverStatusTextValue: String = "unknown"
    private var isServerReachable: Bool = false
    private var statusPollTimer: Timer?
    private let ballOverlayLayer = CAShapeLayer()

    private let payloadStore = PitchPayloadStore()
    private var pendingPayloadFiles: [URL] = []
    private var isUploadingPayload: Bool = false
    private var lastSyncFlashFrameIndex: Int?
    private var lastSyncFlashTimestampS: Double?

    // Mac sync — nil when sync_mode != "mac"
    private var macSyncClient: MacSyncClient?

    private let statusContainer = UIStackView()
    private let topStatusLabel = UILabel()
    private let serverStatusDot = UIView()
    private let serverStatusLabel = UILabel()
    private let fpsLabel = UILabel()
    private let uploadStatusLabel = UILabel()
    private let lastResultLabel = UILabel()
    private let warningLabel = UILabel()
    private let flashDebugLabel = UILabel()
    private let macSyncDebugLabel = UILabel()
    private let trackingButton = UIButton(type: .system)

    // Exposure/WB state captured before entering .timeSyncWaiting so we can
    // restore on exit. Without locking, AE neutralizes the torch within 1–2
    // frames and luminance barely rises above baseline.
    private var savedExposureMode: AVCaptureDevice.ExposureMode?
    private var savedExposureDuration: CMTime?
    private var savedISO: Float?
    private var savedWhiteBalanceMode: AVCaptureDevice.WhiteBalanceMode?
    private var isExposureLockedForFlash: Bool = false

    // Latest FlashDetector snapshot — written on capture queue, read by display
    // link on main. Same pattern as latestCentroidX/Y.
    private var latestFlashSnapshot: FlashDetector.Snapshot?

    // Calibration prerequisites (so CalibrationViewController can compute intrinsics).
    private static let keyHorizontalFovRad = "horizontal_fov_rad"
    private static let keyImageWidthPx = "image_width_px"
    private static let keyImageHeightPx = "image_height_px"
    private static let keyIntrinsicCx = "intrinsic_cx"
    private static let keyIntrinsicCy = "intrinsic_cy"
    private static let keyIntrinsicFx = "intrinsic_fx"
    private static let keyIntrinsicFz = "intrinsic_fz"

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
        flashDetector = FlashDetector(thresholdMultiplier: settings.flashThresholdMultiplier)

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
            do {
                let fileURL = try self.payloadStore.save(enriched)
                DispatchQueue.main.async {
                    self.state = .syncWaiting
                    self.lastUploadStatusText = "Cached pitch \(enriched.cycle_number)"
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
        // If calibration was performed, intrinsics may have been updated in UserDefaults.
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
        // If the user backgrounds this VC mid-calibration, don't leave the
        // camera stuck in custom exposure — AE must re-engage on return.
        if isExposureLockedForFlash {
            restoreExposure()
            if state == .timeSyncWaiting {
                state = .standby
                latestFlashSnapshot = nil
            }
        }
        // Stop mac sync on disappear; it will restart in viewDidAppear/enterSyncMode.
        macSyncClient?.stopSyncing()
    }

    // MARK: - Controls (spec has buttons; skeleton provides methods)

    func enterSyncMode() {
        guard state == .standby else { return }
        recorder.reset()
        // Preserve lastSyncFlash{Frame,Timestamp} from a prior 時間校正 so tracking
        // can align A/B frames. If user skipped 時間校正 they remain nil and the
        // recorder falls back to the first-ball-frame as the anchor (degraded mode).
        // Ensure intrinsics are up-to-date when starting a new session.
        reloadBallDetectorWithLatestIntrinsics()
        pendingPayloadFiles.removeAll(keepingCapacity: true)
        if let files = try? payloadStore.listPayloadFiles() {
            pendingPayloadFiles.append(contentsOf: files)
        }
        isUploadingPayload = false

        // Start mac-sync NTP client when sync_mode == "mac".
        if settings.syncMode == "mac", let base = serverConfig.baseURL() {
            let client = MacSyncClient(serverBaseURL: base)
            macSyncClient = client
            client.startSyncing()
        }

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
        macSyncClient?.stopSyncing()
        macSyncClient = nil
        updateUIForState()
    }

    @objc private func onTapTimeCalibration() {
        if state == .timeSyncWaiting {
            restoreExposure()
            latestFlashSnapshot = nil
            state = .standby
            warningLabel.isHidden = true
            lastUploadStatusText = "Time sync cancelled"
        } else if state == .standby {
            flashDetector.reset()
            latestFlashSnapshot = nil
            lockExposureForFlashDetection()
            state = .timeSyncWaiting
            warningLabel.text = "等待閃光觸發中..."
            warningLabel.isHidden = false
            lastUploadStatusText = "Time sync: waiting for flash"
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
                guard let self, self.state == .timeSyncWaiting else { return }
                self.restoreExposure()
                self.latestFlashSnapshot = nil
                self.state = .standby
                self.warningLabel.isHidden = true
                self.lastUploadStatusText = "Time sync timeout"
                self.updateUIForState()
            }
        }
        updateUIForState()
    }

    // MARK: - Capture setup

    private func setupPreviewAndCapture() {
        session.beginConfiguration()

        // Choose back camera.
        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                try configureCaptureFormat(
                    device,
                    targetWidth: settings.captureWidth,
                    targetHeight: settings.captureHeight,
                    targetFps: Double(settings.captureFps)
                )

                // Persist horizontal FOV used by intrinsics approximation.
                UserDefaults.standard.set(horizontalFovRadians, forKey: Self.keyHorizontalFovRad)

                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
                // TODO: handle error UI
            }
        }

        videoOutput.setSampleBufferDelegate(self, queue: processingQueue)
        videoOutput.alwaysDiscardsLateVideoFrames = true
        // Use BGRA for more predictable pixel buffer layout.
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
        (session.inputs.compactMap { $0 as? AVCaptureDeviceInput }.first)?.device
    }

    /// Freeze exposure + white balance at the currently converged AE values
    /// before flash detection. AE otherwise reacts within 1–2 frames and
    /// collapses the luminance step we're trying to detect.
    private func lockExposureForFlashDetection() {
        guard let device = currentCaptureDevice, !isExposureLockedForFlash else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            savedExposureMode = device.exposureMode
            savedExposureDuration = device.exposureDuration
            savedISO = device.iso
            savedWhiteBalanceMode = device.whiteBalanceMode

            // Prefer .custom (pins the exact ISO + duration AE just settled on).
            // Fall back to .locked if the device doesn't support custom.
            if device.isExposureModeSupported(.custom) {
                let duration = device.exposureDuration
                let iso = max(device.activeFormat.minISO,
                              min(device.activeFormat.maxISO, device.iso))
                device.setExposureModeCustom(duration: duration, iso: iso) { _ in }
            } else if device.isExposureModeSupported(.locked) {
                device.exposureMode = .locked
            }

            if device.isWhiteBalanceModeSupported(.locked) {
                device.whiteBalanceMode = .locked
            }
            isExposureLockedForFlash = true
        } catch {
            // Lock failure is non-fatal: detector will still run, just with
            // AE fighting it. Leave UI to surface the degraded state via HUD.
        }
    }

    private func restoreExposure() {
        guard let device = currentCaptureDevice, isExposureLockedForFlash else { return }
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            let targetExposure = savedExposureMode ?? .continuousAutoExposure
            if device.isExposureModeSupported(targetExposure) {
                device.exposureMode = targetExposure
            } else if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }

            let targetWB = savedWhiteBalanceMode ?? .continuousAutoWhiteBalance
            if device.isWhiteBalanceModeSupported(targetWB) {
                device.whiteBalanceMode = targetWB
            } else if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
        } catch {
            // Non-fatal; AE will re-engage on next camera session restart.
        }
        isExposureLockedForFlash = false
        savedExposureMode = nil
        savedExposureDuration = nil
        savedISO = nil
        savedWhiteBalanceMode = nil
    }

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
        updateFlashDebugOverlay()
        updateMacSyncDebugOverlay()
    }

    private func updateFlashDebugOverlay() {
        guard state == .timeSyncWaiting, let snap = latestFlashSnapshot else {
            return
        }
        let statusText: String
        let color: UIColor
        if !snap.armed {
            statusText = "warming up"
            color = .systemGray
        } else if snap.ratio >= snap.requiredRatio && snap.rise >= snap.requiredRise {
            statusText = "TRIGGER"
            color = .systemGreen
        } else if snap.ratio >= snap.requiredRatio * 0.8 {
            statusText = "close"
            color = .systemOrange
        } else {
            statusText = "armed"
            color = .systemYellow
        }
        flashDebugLabel.text = String(
            format: "lum %.0f  med %.0f  ratio %.2f×/%.2f×  Δ %+.0f/%.0f  %@",
            snap.currentLuminance,
            snap.baselineMedian,
            snap.ratio,
            snap.requiredRatio,
            snap.rise,
            snap.requiredRise,
            statusText
        )
        flashDebugLabel.textColor = color
    }

    private func updateMacSyncDebugOverlay() {
        guard state == .syncWaiting || state == .recording,
              let client = macSyncClient else {
            macSyncDebugLabel.isHidden = true
            return
        }
        macSyncDebugLabel.isHidden = false
        let snap = client.debugSnapshot
        let offsetStr: String
        if let off = snap.currentOffset {
            offsetStr = String(format: "%+.3fms", off * 1000.0)
        } else {
            offsetStr = "pending"
        }
        let rttStr: String
        if let rtt = snap.lastRTT {
            rttStr = String(format: "%.1fms", rtt * 1000.0)
        } else {
            rttStr = "—"
        }
        macSyncDebugLabel.text = "mac-sync offset=\(offsetStr) rtt=\(rttStr) samples=\(snap.sampleCount)"
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
        // Hot-reload settings so Settings changes take effect without waiting for viewWillAppear
        // (which doesn't fire after .formSheet modal dismiss).
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

        flashDebugLabel.font = .monospacedDigitSystemFont(ofSize: 14, weight: .medium)
        flashDebugLabel.textColor = .systemGray
        flashDebugLabel.numberOfLines = 0
        flashDebugLabel.textAlignment = .center
        flashDebugLabel.isHidden = true

        macSyncDebugLabel.font = .monospacedDigitSystemFont(ofSize: 14, weight: .medium)
        macSyncDebugLabel.textColor = .systemCyan
        macSyncDebugLabel.numberOfLines = 0
        macSyncDebugLabel.textAlignment = .center
        macSyncDebugLabel.isHidden = true

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
        statusContainer.addArrangedSubview(flashDebugLabel)
        statusContainer.addArrangedSubview(macSyncDebugLabel)
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

        flashDebugLabel.isHidden = (state != .timeSyncWaiting)
        // macSyncDebugLabel visibility is driven by updateMacSyncDebugOverlay via display link.
        if macSyncClient == nil {
            macSyncDebugLabel.isHidden = true
        }

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

    // MARK: - Frame processing (AVCaptureVideoDataOutputSampleBufferDelegate)

    nonisolated func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let ts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let timestampS = CMTimeGetSeconds(ts)
        if timestampS.isNaN || timestampS.isInfinite {
            return
        }

        updateFpsEstimate()

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)

        // Only write UserDefaults when dimensions actually change (rare).
        if latestImageWidth != width || latestImageHeight != height {
            UserDefaults.standard.set(width, forKey: Self.keyImageWidthPx)
            UserDefaults.standard.set(height, forKey: Self.keyImageHeightPx)
        }

        // Advance frameIndex every frame so cross-mode timestamps stay coherent.
        let currentIndex = frameIndex
        frameIndex += 1

        // Flash detection ONLY in 時間校正 mode. Once captured, save the anchor
        // and auto-return to STANDBY.
        if state == .timeSyncWaiting {
            // Tile-max (not full-frame mean) so a localized torch —
            // e.g. another phone occupying <1/16 of the frame — still
            // produces a sharp per-tile step instead of being smeared out.
            let lumStats = FrameProcessingUtils.luminanceStats(pixelBuffer: pixelBuffer)
            let event = flashDetector.process(
                sampleLuminance: lumStats.maxTile,
                frameIndex: currentIndex,
                timestampS: timestampS
            )
            latestFlashSnapshot = flashDetector.lastSnapshot
            if let flashEvent = event {
                lastSyncFlashFrameIndex = flashEvent.flashFrameIndex
                lastSyncFlashTimestampS = flashEvent.flashTimestampS
                state = .standby
                let ts = flashEvent.flashTimestampS
                DispatchQueue.main.async {
                    self.restoreExposure()
                    self.latestFlashSnapshot = nil
                    self.warningLabel.isHidden = true
                    self.lastUploadStatusText = String(format: "Time sync OK @ %.3fs", ts)
                    self.updateUIForState()
                }
            }
            return
        }

        // Ball detection only in tracking modes.
        let needsDetection = state == .syncWaiting || state == .recording
        let detection: BallDetector.DetectionResult
        if needsDetection {
            detection = ballDetector.detect(
                pixelBuffer: pixelBuffer,
                imageWidth: width,
                imageHeight: height,
                horizontalFovRadians: horizontalFovRadians
            )
        } else {
            detection = BallDetector.DetectionResult(
                ballDetected: false,
                thetaXRad: nil,
                thetaZRad: nil,
                centroidX: nil,
                centroidY: nil
            )
        }
        let frame = ServerUploader.FramePayload(
            frame_index: currentIndex,
            timestamp_s: timestampS,
            theta_x_rad: detection.thetaXRad,
            theta_z_rad: detection.thetaZRad,
            ball_detected: detection.ballDetected
        )

        // Stash latest detection for the CADisplayLink UI pump on main.
        // Detection itself stays at full capture rate; the overlay is drawn
        // at display refresh rate (60/120Hz) — the max useful for visual tracking.
        // No per-frame DispatchQueue.main.async, so 240fps never floods main.
        latestCentroidX = detection.centroidX
        latestCentroidY = detection.centroidY
        latestImageWidth = width
        latestImageHeight = height

        switch state {
        case .standby, .timeSyncWaiting:
            return

        case .syncWaiting:
            recorder.handleFrame(frame)
            if detection.ballDetected {
                let anchorFrameIndex = lastSyncFlashFrameIndex ?? currentIndex
                let anchorTimestampS = lastSyncFlashTimestampS ?? timestampS
                recorder.startRecording(
                    anchorFrameIndex: anchorFrameIndex,
                    anchorTimestampS: anchorTimestampS,
                    startFrameIndex: currentIndex
                )
                state = .recording
                // One-shot dispatch on actual state transition; everything else
                // goes through the display link.
                DispatchQueue.main.async {
                    self.updateUIForState()
                }
            }

        case .recording:
            recorder.handleFrame(frame)

        case .uploading:
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

        // Convert image pixel -> normalized capture coordinates.
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

    private func enrichedPayload(from payload: ServerUploader.PitchPayload) -> ServerUploader.PitchPayload {
        let d = UserDefaults.standard
        var intrinsics: ServerUploader.IntrinsicsPayload? = nil
        if
            d.object(forKey: Self.keyIntrinsicFx) != nil,
            d.object(forKey: Self.keyIntrinsicFz) != nil,
            d.object(forKey: Self.keyIntrinsicCx) != nil,
            d.object(forKey: Self.keyIntrinsicCy) != nil
        {
            intrinsics = ServerUploader.IntrinsicsPayload(
                fx: d.double(forKey: Self.keyIntrinsicFx),
                fz: d.double(forKey: Self.keyIntrinsicFz),
                cx: d.double(forKey: Self.keyIntrinsicCx),
                cy: d.double(forKey: Self.keyIntrinsicCy)
            )
        }
        let homography = d.array(forKey: "homography_3x3") as? [Double]
        let w = d.integer(forKey: Self.keyImageWidthPx)
        let h = d.integer(forKey: Self.keyImageHeightPx)
        // Mac-sync: inject offset if available. The offset sign convention is:
        //   mac_clock_offset_s = phone_monotonic_clock - server_monotonic_clock
        // So server applies: aligned_ts = frame.timestamp_s - mac_clock_offset_s
        // to convert phone timestamps into server clock.
        // MacSyncClient computes: offset = server_t1 - (t0+t3)/2
        //   => offset > 0 means server is ahead.
        // We want phone - server, so we negate before shipping.
        let macOffset: Double?
        if let client = macSyncClient, let est = client.currentOffset {
            macOffset = -est  // flip: phone_clock - server_clock
        } else {
            macOffset = nil
        }
        return ServerUploader.PitchPayload(
            camera_id: payload.camera_id,
            flash_frame_index: payload.flash_frame_index,
            flash_timestamp_s: payload.flash_timestamp_s,
            cycle_number: payload.cycle_number,
            frames: payload.frames,
            intrinsics: intrinsics,
            homography: homography,
            image_width_px: w > 0 ? w : nil,
            image_height_px: h > 0 ? h : nil,
            mac_clock_offset_s: macOffset
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

        // Basic sanity check.
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

        if recorder == nil {
            return
        }

        recorder.setCameraId(settings.cameraRole)
        flashDetector = FlashDetector(thresholdMultiplier: settings.flashThresholdMultiplier)
    }
}

