import UIKit
import AVFoundation
import CoreMedia
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Auto ArUco home-plate calibration. Detects DICT_4X4_50 markers 0-5
/// in a live preview, overlays cyan polygons on hits, and saves the
/// homography via OpenCV RANSAC on the "拍照校正" tap.
final class AutoCalibrationViewController: UIViewController {
    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?

    private let videoOutput = AVCaptureVideoDataOutput()
    private let videoQueue = DispatchQueue(label: "calibration.aruco.queue")
    private let markerOverlayLayer = CAShapeLayer()
    private let arucoStatusLabel = UILabel()
    private var detectionDisplayLink: CADisplayLink?

    // Written from videoQueue, read from main — protected by arucoLock.
    private let arucoLock = NSLock()
    private var latestMarkers: [(id: Int, corners: [CGPoint])] = []
    private var latestPixelSize: CGSize = .zero

    deinit {
        detectionDisplayLink?.invalidate()
        session.stopRunning()
    }

    // ArUco detector reads the CVPixelBuffer at sensor-native rotation (0°)
    // and the overlay uses landscape view coordinates — portrait would
    // misplace the polygon and break the RANSAC fit on tilted markers.
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        [.landscapeLeft, .landscapeRight]
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        title = "自動校正"

        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "拍照校正",
            style: .done,
            target: self,
            action: #selector(saveArucoCalibration)
        )

        view.layoutIfNeeded()
        setupPreview()
        setupArucoOverlay()
        startDetectionDisplayLink()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    private func setupPreview() {
        session.beginConfiguration()
        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) { session.addInput(input) }
            } catch {
                log.error("auto-calibration camera input failed error=\(error.localizedDescription, privacy: .public)")
                showAlert(title: "相機無法開啟", message: "無法存取後置相機：\(error.localizedDescription)")
            }
        }

        // BGRA output so the Obj-C++ bridge can wrap it as a cv::Mat directly.
        videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        if session.canAddOutput(videoOutput) {
            session.addOutput(videoOutput)
        }
        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
        if let connection = preview.connection, connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        view.layer.addSublayer(preview)
        previewLayer = preview

        if !session.isRunning { session.startRunning() }
    }

    private func setupArucoOverlay() {
        markerOverlayLayer.strokeColor = DesignTokens.Colors.accent.cgColor
        markerOverlayLayer.fillColor = DesignTokens.Colors.accent.withAlphaComponent(0.18).cgColor
        markerOverlayLayer.lineWidth = 2
        view.layer.addSublayer(markerOverlayLayer)

        arucoStatusLabel.font = DesignTokens.Fonts.sans(size: 15, weight: .medium)
        arucoStatusLabel.textColor = DesignTokens.Colors.ink
        arucoStatusLabel.textAlignment = .center
        arucoStatusLabel.backgroundColor = DesignTokens.Colors.surface
        arucoStatusLabel.layer.cornerRadius = DesignTokens.CornerRadius.card
        arucoStatusLabel.clipsToBounds = true
        arucoStatusLabel.numberOfLines = 2
        arucoStatusLabel.text = "ArUco: 等待偵測…"
        arucoStatusLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(arucoStatusLabel)
        NSLayoutConstraint.activate([
            arucoStatusLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            arucoStatusLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
            arucoStatusLabel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.m),
            arucoStatusLabel.heightAnchor.constraint(greaterThanOrEqualToConstant: 40),
        ])
    }

    private func startDetectionDisplayLink() {
        let link = CADisplayLink(target: self, selector: #selector(refreshArucoOverlay))
        link.preferredFramesPerSecond = 15
        link.add(to: .main, forMode: .common)
        detectionDisplayLink = link
    }

    @objc private func refreshArucoOverlay() {
        arucoLock.lock()
        let markers = latestMarkers
        let pixelSize = latestPixelSize
        arucoLock.unlock()

        guard pixelSize.width > 0, pixelSize.height > 0 else {
            arucoStatusLabel.text = "ArUco: 等待相機畫面…"
            markerOverlayLayer.path = nil
            return
        }

        let required = Array(CalibrationShared.markerWorldPoints.keys).sorted()
        let detectedIds = Set(markers.map { $0.id })
        let hits = required.filter { detectedIds.contains($0) }
        arucoStatusLabel.text = "ArUco: 偵測到 \(markers.count) 個 markers，匹配 \(hits.count)/\(required.count) (IDs \(hits.sorted()))"

        let path = UIBezierPath()
        for m in markers {
            let viewPts = m.corners.map {
                imagePixelToViewPoint($0, imageWidth: Int(pixelSize.width), imageHeight: Int(pixelSize.height))
            }
            guard viewPts.count == 4 else { continue }
            path.move(to: viewPts[0])
            for pt in viewPts.dropFirst() { path.addLine(to: pt) }
            path.close()
        }
        markerOverlayLayer.path = path.cgPath
    }

    private func imagePixelToViewPoint(_ p: CGPoint, imageWidth: Int, imageHeight: Int) -> CGPoint {
        guard let previewLayer else { return .zero }
        let devX = CGFloat(p.x) / CGFloat(imageWidth)
        let devY = CGFloat(p.y) / CGFloat(imageHeight)
        return previewLayer.layerPointConverted(fromCaptureDevicePoint: CGPoint(x: devX, y: devY))
    }

    @objc private func saveArucoCalibration() {
        arucoLock.lock()
        let markers = latestMarkers
        let pixelSize = latestPixelSize
        arucoLock.unlock()

        let required = Array(CalibrationShared.markerWorldPoints.keys).sorted()
        let byId = Dictionary(uniqueKeysWithValues: markers.map { ($0.id, $0) })
        let detected = required.filter { byId[$0] != nil }
        let missing = required.filter { byId[$0] == nil }
        // Allow up to 1 missing marker out of 6 — RANSAC in findHomography
        // still has >=5 correspondences to solve with outlier rejection.
        let minRequired = max(4, required.count - 1)
        if detected.count < minRequired {
            log.warning("aruco detection failed reason=insufficient_markers detected=\(detected.count, privacy: .public) required=\(minRequired, privacy: .public) missing=\(missing, privacy: .public)")
            showAlert(
                title: "無法自動校正",
                message: "只偵測到 \(detected.count)/\(required.count) 個 marker（至少需 \(minRequired)）。缺少 IDs: \(missing)。"
            )
            return
        }
        guard pixelSize.width > 0, pixelSize.height > 0 else {
            log.warning("aruco detection failed reason=no_pixel_size")
            showAlert(title: "無法自動校正", message: "尚未取得相機畫面尺寸。")
            return
        }

        let worldValues: [NSValue] = detected.map {
            let (x, y) = CalibrationShared.markerWorldPoints[$0]!
            return NSValue(cgPoint: CGPoint(x: x, y: y))
        }
        let imageValues: [NSValue] = detected.map {
            let m = byId[$0]!
            let cx = (m.corners[0].x + m.corners[1].x + m.corners[2].x + m.corners[3].x) * 0.25
            let cy = (m.corners[0].y + m.corners[1].y + m.corners[2].y + m.corners[3].y) * 0.25
            return NSValue(cgPoint: CGPoint(x: cx, y: cy))
        }

        guard let hNumbers = BTArucoDetector.findHomography(fromWorldPoints: worldValues, imagePoints: imageValues) else {
            log.warning("aruco detection failed reason=ransac_no_solution detected=\(detected.count, privacy: .public)")
            showAlert(title: "無法自動校正", message: "RANSAC 解不出 homography (marker 可能被遮擋)。")
            return
        }
        let H = hNumbers.map { $0.doubleValue }
        UserDefaults.standard.set(H, forKey: CalibrationShared.keyHomography)
        UserDefaults.standard.set(Int(pixelSize.width), forKey: CalibrationShared.keyImageWidthPx)
        UserDefaults.standard.set(Int(pixelSize.height), forKey: CalibrationShared.keyImageHeightPx)

        CalibrationShared.persistFovIntrinsicsIfPossible()
        log.info("calibration saved source=aruco markers=\(detected.count, privacy: .public)")
        CalibrationShared.postCalibrationToServer(source: "aruco")
        dismiss(animated: true)
    }

    private func showAlert(title: String, message: String) {
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }
}

// Delegate conformance must be an unrestricted extension so the frame callback
// can be `nonisolated` under strict concurrency.
extension AutoCalibrationViewController: AVCaptureVideoDataOutputSampleBufferDelegate {
    nonisolated func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let detected = BTArucoDetector.detectMarkers(in: pixelBuffer)
        let markers: [(id: Int, corners: [CGPoint])] = detected.map { m in
            (Int(m.markerId), [m.corner0, m.corner1, m.corner2, m.corner3])
        }
        arucoLock.lock()
        latestMarkers = markers
        latestPixelSize = CGSize(width: width, height: height)
        arucoLock.unlock()
    }
}
