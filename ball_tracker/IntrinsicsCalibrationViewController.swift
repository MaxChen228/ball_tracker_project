import AVFoundation
import UIKit
import os

private let vcLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "intrinsics.vc")

/// Full-screen modal that drives the ChArUco intrinsics calibration flow.
///
/// Responsibilities:
///  - own the `IntrinsicsCaptureController` (independent AVCaptureSession)
///  - render live preview + 3-bar pose-diversity progress + status text
///  - on ready → call `solver.solve()` → write pending cache → POST
///  - present retry-upload UI if a pending record exists from a prior run
final class IntrinsicsCalibrationViewController: UIViewController {

    /// Called by the host VC after this VC dismisses, regardless of outcome.
    /// `uploaded == true` means a POST returned 200 — host should kick off
    /// the auto-cal chain (delayed). false means the operator cancelled or
    /// the run failed/was stashed to pending cache.
    var onFinished: ((_ uploaded: Bool) -> Void)?

    private let uploader: ServerUploader
    private let cameraRole: String
    private let captureController = IntrinsicsCaptureController()

    private let statusLabel = UILabel()
    private let cancelButton = UIButton(type: .system)
    private let retryButton = UIButton(type: .system)
    private let yawBar = UIProgressView(progressViewStyle: .bar)
    private let pitchBar = UIProgressView(progressViewStyle: .bar)
    private let distanceBar = UIProgressView(progressViewStyle: .bar)
    private let progressContainer = UIView()
    private let dimOverlay = UIView()

    private var didReceiveReadyToSolve = false

    init(uploader: ServerUploader, cameraRole: String) {
        self.uploader = uploader
        self.cameraRole = cameraRole
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .fullScreen
    }

    required init?(coder: NSCoder) { fatalError() }

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        setupCaptureLayer()
        setupUI()
        wireCallbacks()

        if !captureController.configureSession() {
            statusLabel.text = "相機初始化失敗"
            return
        }

        // If a pending record sits unsent from a prior run, offer retry
        // upload before re-shooting.
        if let pending = IntrinsicsPendingCache.read() {
            offerRetryUpload(pending: pending)
        } else {
            startFreshCapture()
        }
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        captureController.stopSession()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        captureController.previewLayer.frame = view.bounds
    }

    // MARK: - Setup

    private func setupCaptureLayer() {
        captureController.previewLayer.frame = view.bounds
        view.layer.addSublayer(captureController.previewLayer)
    }

    private func setupUI() {
        // Dim overlay sits between preview and HUD for legibility.
        dimOverlay.backgroundColor = UIColor.black.withAlphaComponent(0.35)
        dimOverlay.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(dimOverlay)

        cancelButton.setTitle("✕", for: .normal)
        cancelButton.setTitleColor(.white, for: .normal)
        cancelButton.titleLabel?.font = .systemFont(ofSize: 28, weight: .light)
        cancelButton.translatesAutoresizingMaskIntoConstraints = false
        cancelButton.addTarget(self, action: #selector(handleCancel), for: .touchUpInside)
        view.addSubview(cancelButton)

        statusLabel.text = "準備中..."
        statusLabel.textColor = .white
        statusLabel.font = .systemFont(ofSize: 18, weight: .medium)
        statusLabel.textAlignment = .center
        statusLabel.numberOfLines = 0
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(statusLabel)

        // 3 bars: yaw, pitch, distance.
        [yawBar, pitchBar, distanceBar].forEach { bar in
            bar.progressTintColor = DesignTokens.Colors.success
            bar.trackTintColor = UIColor.white.withAlphaComponent(0.25)
            bar.translatesAutoresizingMaskIntoConstraints = false
        }

        progressContainer.translatesAutoresizingMaskIntoConstraints = false
        progressContainer.backgroundColor = UIColor.black.withAlphaComponent(0.5)
        progressContainer.layer.cornerRadius = 12
        view.addSubview(progressContainer)

        let stack = UIStackView(arrangedSubviews: [yawBar, pitchBar, distanceBar])
        stack.axis = .vertical
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        progressContainer.addSubview(stack)

        retryButton.setTitle("重試", for: .normal)
        retryButton.setTitleColor(.white, for: .normal)
        retryButton.titleLabel?.font = .systemFont(ofSize: 17, weight: .medium)
        retryButton.backgroundColor = DesignTokens.Colors.accent
        retryButton.layer.cornerRadius = 8
        retryButton.contentEdgeInsets = .init(top: 8, left: 16, bottom: 8, right: 16)
        retryButton.translatesAutoresizingMaskIntoConstraints = false
        retryButton.addTarget(self, action: #selector(handleRetry), for: .touchUpInside)
        retryButton.isHidden = true
        view.addSubview(retryButton)

        NSLayoutConstraint.activate([
            dimOverlay.topAnchor.constraint(equalTo: view.topAnchor),
            dimOverlay.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            dimOverlay.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            dimOverlay.heightAnchor.constraint(equalToConstant: 110),

            cancelButton.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 4),
            cancelButton.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 16),
            cancelButton.widthAnchor.constraint(equalToConstant: 44),
            cancelButton.heightAnchor.constraint(equalToConstant: 44),

            statusLabel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 14),
            statusLabel.leadingAnchor.constraint(equalTo: cancelButton.trailingAnchor, constant: 8),
            statusLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -16),

            progressContainer.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),
            progressContainer.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 32),
            progressContainer.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -32),

            stack.topAnchor.constraint(equalTo: progressContainer.topAnchor, constant: 14),
            stack.leadingAnchor.constraint(equalTo: progressContainer.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: progressContainer.trailingAnchor, constant: -16),
            stack.bottomAnchor.constraint(equalTo: progressContainer.bottomAnchor, constant: -14),

            retryButton.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            retryButton.bottomAnchor.constraint(equalTo: progressContainer.topAnchor, constant: -20),
        ])
    }

    private func wireCallbacks() {
        captureController.onProgress = { [weak self] snap in
            self?.applyProgress(snap)
        }
        captureController.onReadyToSolve = { [weak self] _, _ in
            self?.handleReadyToSolve()
        }
    }

    // MARK: - Flow

    private func startFreshCapture() {
        captureController.reset()
        statusLabel.text = "把棋盤格移到畫面中央"
        captureController.startSession()
    }

    private func offerRetryUpload(pending: IntrinsicsPendingRecord) {
        statusLabel.text = "上次校正完成但未上傳,要重試上傳嗎?"
        retryButton.isHidden = false
        retryButton.setTitle("重試上傳", for: .normal)
        // Deliberately do NOT call startSession() here. Letting the capture
        // state machine run while the operator decides "retry or reshoot"
        // races: a brand-new 20-shot calibration can complete and overwrite
        // the pending record the user was about to retry, silently shipping
        // a fresh K instead of the stable one they wanted. Preview stays
        // black until handleRetry routes to either uploadPending (retry)
        // or startFreshCapture (re-shoot).
    }

    private func applyProgress(_ snap: IntrinsicsCaptureProgress) {
        switch snap.status {
        case .waitingForBoard:
            statusLabel.text = "把棋盤格移到畫面中央"
        case .boardTooFar:
            statusLabel.text = "再靠近一點"
        case .duplicatePose:
            statusLabel.text = "換一個角度"
        case .unstable:
            statusLabel.text = "穩住相機"
        case .capturing:
            statusLabel.text = "對好了 ✓ 繼續換角度"
        case .solving:
            statusLabel.text = "校正中..."
        case .uploading:
            statusLabel.text = "上傳中..."
        case .completed:
            statusLabel.text = "完成"
        case .failedTooFewShots:
            statusLabel.text = "光線不足或板看不清楚,請調整環境後重試"
            retryButton.isHidden = false
            retryButton.setTitle("重新開始", for: .normal)
        case .failedUpload(let msg):
            statusLabel.text = "無法連線到 server,稍後重試 (\(msg))"
            retryButton.isHidden = false
            retryButton.setTitle("重試上傳", for: .normal)
        }
        yawBar.setProgress(snap.yawProgress, animated: true)
        pitchBar.setProgress(snap.pitchProgress, animated: true)
        distanceBar.setProgress(snap.distanceProgress, animated: true)
    }

    private func handleReadyToSolve() {
        guard !didReceiveReadyToSolve else { return }
        didReceiveReadyToSolve = true
        captureController.stopSession()
        statusLabel.text = "校正中..."

        // `solveAsync` runs on the controller's sessionQueue — the same
        // serial queue every `didFinishPhotoCapture` runs on. This is the
        // only safe place to read the calibrator's accumulated
        // `_objPts/_imgPts` vectors: any in-flight photo callback that
        // arrives after `stopSession` was queued will land on sessionQueue
        // and serialise before solve, eliminating the std::vector race.
        captureController.solveAsync { [weak self] outcome in
            guard let self else { return }
            switch outcome {
            case .success(let result):
                let record = self.buildPendingRecord(from: result,
                                                       lensPosition: self.captureController.averageLensPosition)
                IntrinsicsPendingCache.write(record)
                self.uploadPending(record)
            case .failure:
                self.statusLabel.text = "校正失敗,請重試"
                self.retryButton.setTitle("重新開始", for: .normal)
                self.retryButton.isHidden = false
                self.didReceiveReadyToSolve = false
            }
        }
    }

    private func uploadPending(_ record: IntrinsicsPendingRecord) {
        statusLabel.text = "上傳中..."
        retryButton.isHidden = true
        uploader.postIntrinsics(deviceId: record.deviceId, record: record) { [weak self] result in
            guard let self else { return }
            switch result {
            case .success:
                IntrinsicsPendingCache.clear()
                self.statusLabel.text = "完成"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                    self?.dismissAndFinish(uploaded: true)
                }
            case .failure(let err):
                self.statusLabel.text = "無法連線到 server,稍後重試"
                self.retryButton.setTitle("重試上傳", for: .normal)
                self.retryButton.isHidden = false
                vcLog.error("postIntrinsics failed: \(err.localizedDescription, privacy: .public)")
            }
        }
    }

    private func buildPendingRecord(from result: BTCharucoSolveResult,
                                      lensPosition: Float) -> IntrinsicsPendingRecord {
        let label = String(format: "ios-charuco-v1-lens%.2f", lensPosition)
        return IntrinsicsPendingRecord(
            deviceId: DeviceIdentity.id,
            deviceModel: DeviceIdentity.model,
            sourceWidthPx: Int(result.imageWidth),
            sourceHeightPx: Int(result.imageHeight),
            fx: Double(result.fx),
            fy: Double(result.fy),
            cx: Double(result.cx),
            cy: Double(result.cy),
            distortion: result.distortion.map { $0.doubleValue },
            rmsReprojectionPx: Double(result.rmsReprojectionPx),
            nImages: Int(result.numImagesUsed),
            calibratedAt: Date().timeIntervalSince1970,
            sourceLabel: label
        )
    }

    private func dismissAndFinish(uploaded: Bool) {
        dismiss(animated: true) { [weak self] in
            self?.onFinished?(uploaded)
        }
    }

    // MARK: - Buttons

    @objc private func handleCancel() {
        captureController.stopSession()
        dismissAndFinish(uploaded: false)
    }

    @objc private func handleRetry() {
        retryButton.isHidden = true
        if let pending = IntrinsicsPendingCache.read() {
            uploadPending(pending)
        } else {
            didReceiveReadyToSolve = false
            startFreshCapture()
        }
    }
}
