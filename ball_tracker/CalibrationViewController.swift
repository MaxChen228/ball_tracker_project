import UIKit
import AVFoundation
import CoreMedia
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Home plate calibration screen (spec):
/// - Show live preview
/// - User taps four corners of home plate
/// - Persist homography/intrinsics via UserDefaults
final class CalibrationViewController: UIViewController {
    private struct CornerKey: Hashable {
        let rawValue: Int
        static let frontLeft = CornerKey(rawValue: 0)
        static let frontRight = CornerKey(rawValue: 1)
        static let rightShoulder = CornerKey(rawValue: 2)
        static let backTip = CornerKey(rawValue: 3)
        static let leftShoulder = CornerKey(rawValue: 4)
    }

    private final class DraggablePointView: UIView {
        private let label: UILabel
        private let pan: UIPanGestureRecognizer

        var onMoved: ((CGPoint) -> Void)?

        init(color: UIColor, text: String) {
            self.label = UILabel()
            self.pan = UIPanGestureRecognizer(target: nil, action: nil)
            super.init(frame: CGRect(x: 0, y: 0, width: 34, height: 34))

            backgroundColor = color.withAlphaComponent(0.85)
            layer.cornerRadius = 17
            clipsToBounds = true

            label.text = text
            label.textColor = .white
            label.font = UIFont.systemFont(ofSize: 12, weight: .bold)
            label.textAlignment = .center
            label.frame = bounds
            addSubview(label)

            pan.addTarget(self, action: #selector(onPan(_:)))
            addGestureRecognizer(pan)
            isUserInteractionEnabled = true
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

        @objc private func onPan(_ gesture: UIPanGestureRecognizer) {
            guard let superview else { return }
            let location = gesture.location(in: superview)
            // Keep handle fully inside the superview bounds.
            let half = bounds.width / 2.0
            let allowedFrame = superview.bounds.insetBy(dx: half, dy: half)
            let clampedX = min(max(allowedFrame.minX, location.x), allowedFrame.maxX)
            let clampedY = min(max(allowedFrame.minY, location.y), allowedFrame.maxY)
            center = CGPoint(x: clampedX, y: clampedY)
            onMoved?(center)
        }
    }

    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?

    private var handles: [CornerKey: DraggablePointView] = [:]

    private let residualLabel = UILabel()
    private let reprojectedPolyLayer = CAShapeLayer()

    // ArUco auto-calibration
    private let videoOutput = AVCaptureVideoDataOutput()
    private let videoQueue = DispatchQueue(label: "calibration.aruco.queue")
    private let markerOverlayLayer = CAShapeLayer()
    private let arucoStatusLabel = UILabel()
    private var detectionDisplayLink: CADisplayLink?
    // Written from videoQueue, read from main — protected by arucoLock.
    private let arucoLock = NSLock()
    private var latestMarkers: [(id: Int, corners: [CGPoint])] = []
    private var latestPixelSize: CGSize = .zero

    /// 6 ArUco markers (DICT_4X4_50, IDs 0-5) centered on home-plate
    /// landmarks: FL / FR / RS / LS / BT / MF. Extra points (BT = back tip,
    /// MF = mid-front edge) give better front-back spread and a centreline
    /// anchor so RANSAC in `findHomography` can tolerate 1-2 misreads.
    /// User prints small ~3-5 cm squares and tapes them so each marker's
    /// centre is on the corresponding plate vertex.
    private static let markerWorldPoints: [Int: (Double, Double)] = [
        0: (-plateWidthM / 2.0, 0.0),                 // FL
        1: ( plateWidthM / 2.0, 0.0),                 // FR
        2: ( plateWidthM / 2.0, plateShoulderYM),     // RS
        3: (-plateWidthM / 2.0, plateShoulderYM),     // LS
        4: ( 0.0,                plateTipYM),         // BT (back tip)
        5: ( 0.0,                0.0),                // MF (mid-front edge)
    ]

    // Real home-plate pentagon vertices in meters. Axes: X = left/right,
    // Y = depth from front edge (pitcher side) toward back tip (catcher side).
    private static let plateWidthM = 0.432       // 17" front edge
    private static let plateShoulderYM = 0.216   // 8.5" back to shoulder
    private static let plateTipYM = 0.432        // 17" back to back tip

    private static func plateWorldPoints() -> [(Double, Double)] {
        return [
            (-plateWidthM / 2.0, 0.0),          // FL
            (plateWidthM / 2.0, 0.0),           // FR
            (plateWidthM / 2.0, plateShoulderYM),  // RS
            (0.0, plateTipYM),                  // BT
            (-plateWidthM / 2.0, plateShoulderYM), // LS
        ]
    }

    deinit {
        detectionDisplayLink?.invalidate()
        session.stopRunning()
    }

    // UserDefaults keys shared with other components.
    private static let keyHomography = "homography_3x3" // length-9 Double array (row-major)
    private static let keyHorizontalFovRad = "horizontal_fov_rad"
    private static let keyImageWidthPx = "image_width_px"
    private static let keyImageHeightPx = "image_height_px"

    private static let keyIntrinsicCx = "intrinsic_cx"
    private static let keyIntrinsicCy = "intrinsic_cy"
    private static let keyIntrinsicFx = "intrinsic_fx"
    private static let keyIntrinsicFz = "intrinsic_fz"

    // NOTE: We persist intrinsics as fx/fz/cx/cy (used by BallDetector).

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "Exit",
            style: .plain,
            target: self,
            action: #selector(exitCalibration)
        )

        navigationItem.rightBarButtonItems = [
            UIBarButtonItem(title: "Save", style: .done, target: self, action: #selector(saveCalibration)),
            UIBarButtonItem(title: "Auto (ArUco)", style: .plain, target: self, action: #selector(saveArucoCalibration)),
        ]

        view.layoutIfNeeded()
        setupPreview()
        setupCornerHandles()
        setupResidualOverlay()
        setupArucoOverlay()
        refreshResidualAndPolygon()
        startDetectionDisplayLink()
    }

    private func setupResidualOverlay() {
        reprojectedPolyLayer.strokeColor = UIColor.systemGreen.cgColor
        reprojectedPolyLayer.fillColor = UIColor.clear.cgColor
        reprojectedPolyLayer.lineWidth = 2
        reprojectedPolyLayer.lineDashPattern = [6, 4]
        view.layer.addSublayer(reprojectedPolyLayer)

        residualLabel.font = .systemFont(ofSize: 18, weight: .semibold)
        residualLabel.textColor = .white
        residualLabel.textAlignment = .center
        residualLabel.backgroundColor = UIColor.black.withAlphaComponent(0.55)
        residualLabel.layer.cornerRadius = 10
        residualLabel.clipsToBounds = true
        residualLabel.numberOfLines = 2
        residualLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(residualLabel)

        NSLayoutConstraint.activate([
            residualLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
            residualLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            residualLabel.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -20),
            residualLabel.heightAnchor.constraint(greaterThanOrEqualToConstant: 56),
        ])
    }

    private func setupPreview() {
        session.beginConfiguration()

        // Use back camera.
        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
                log.error("calibration camera input failed error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async { [weak self] in
                    self?.showAlert(title: "相機無法開啟", message: "無法存取後置相機：\(error.localizedDescription)")
                }
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
        // App UI is locked landscape (see Info.plist). Force the preview
        // connection to sensor-native angle 0 so the preview frame aligns
        // with the CVPixelBuffer orientation the ArUco detector sees —
        // otherwise `captureDevicePointConverted` would apply an implicit
        // rotation and the overlay would land in the wrong place.
        if let connection = preview.connection, connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        view.layer.addSublayer(preview)
        previewLayer = preview

        if !session.isRunning {
            session.startRunning()
        }
    }

    private func setupCornerHandles() {
        // Initial placement: pentagon in lower-center of frame. User drags each
        // point to its corresponding home-plate vertex.
        //   FL ── FR         ← front (17" edge facing pitcher)
        //   │      │
        //   LS    RS         ← shoulders (8.5" back)
        //    \    /
        //     \  /
        //      BT            ← back tip (17" back, facing catcher)
        let w = view.bounds.width
        let h = view.bounds.height

        func makeHandle(color: UIColor, text: String) -> DraggablePointView {
            let v = DraggablePointView(color: color, text: text)
            v.onMoved = { [weak self] _ in
                self?.refreshResidualAndPolygon()
            }
            view.addSubview(v)
            return v
        }

        let fl = makeHandle(color: .systemBlue, text: "FL")
        let fr = makeHandle(color: .systemGreen, text: "FR")
        let rs = makeHandle(color: .systemOrange, text: "RS")
        let bt = makeHandle(color: .systemRed, text: "BT")
        let ls = makeHandle(color: .systemPurple, text: "LS")

        let leftX = w * 0.35
        let rightX = w * 0.65
        let frontY = h * 0.40
        let shoulderY = h * 0.55
        let tipY = h * 0.70

        fl.center = CGPoint(x: leftX, y: frontY)
        fr.center = CGPoint(x: rightX, y: frontY)
        rs.center = CGPoint(x: rightX, y: shoulderY)
        ls.center = CGPoint(x: leftX, y: shoulderY)
        bt.center = CGPoint(x: w / 2, y: tipY)

        handles[.frontLeft] = fl
        handles[.frontRight] = fr
        handles[.rightShoulder] = rs
        handles[.leftShoulder] = ls
        handles[.backTip] = bt
    }

    @objc private func exitCalibration() {
        session.stopRunning()
        dismiss(animated: true)
    }

    @objc private func saveCalibration() {
        persistCalibrationFromDraggedPoints()
        log.info("calibration saved source=manual")
        postCalibrationToServer(source: "manual")
        dismiss(animated: true)
    }

    /// Fire-and-forget POST of the just-persisted calibration so the
    /// dashboard canvas can draw this camera's pose without waiting for
    /// a first pitch upload. Failures are logged only — the UserDefaults
    /// write already happened, so the phone itself is still calibrated
    /// even if the server copy never lands.
    private func postCalibrationToServer(source: String) {
        guard let intrinsics = IntrinsicsStore.loadIntrinsicsPayload(),
              let homography = IntrinsicsStore.loadHomography(),
              let dims = IntrinsicsStore.loadImageDimensions() else {
            log.info("calibration upload skipped reason=incomplete_local_state source=\(source, privacy: .public)")
            return
        }
        let settings = SettingsViewController.loadFromUserDefaults()
        let uploader = ServerUploader(config: ServerUploader.ServerConfig(
            serverIP: settings.serverIP,
            serverPort: settings.serverPort
        ))
        let payload = ServerUploader.CalibrationPayload(
            camera_id: settings.cameraRole,
            intrinsics: intrinsics,
            homography: homography,
            image_width_px: dims.width,
            image_height_px: dims.height
        )
        uploader.postCalibration(payload) { result in
            switch result {
            case .success:
                log.info("calibration upload ok cam=\(settings.cameraRole, privacy: .public) source=\(source, privacy: .public)")
            case .failure(let error):
                log.error("calibration upload failed cam=\(settings.cameraRole, privacy: .public) source=\(source, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            }
        }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
        reprojectedPolyLayer.frame = view.bounds
    }

    private func refreshResidualAndPolygon() {
        let d = UserDefaults.standard
        let imageW = d.integer(forKey: Self.keyImageWidthPx)
        let imageH = d.integer(forKey: Self.keyImageHeightPx)
        guard imageW > 0, imageH > 0, handles.count == 5 else {
            residualLabel.text = "等待相機尺寸…"
            reprojectedPolyLayer.path = nil
            return
        }

        let viewOrder: [CGPoint] = [
            handles[.frontLeft]?.center ?? .zero,
            handles[.frontRight]?.center ?? .zero,
            handles[.rightShoulder]?.center ?? .zero,
            handles[.backTip]?.center ?? .zero,
            handles[.leftShoulder]?.center ?? .zero,
        ]
        let imgPts = viewOrder.map {
            viewPointToImagePixel($0, imageWidth: imageW, imageHeight: imageH)
        }
        let world = Self.plateWorldPoints()

        guard let H = computeHomography(worldPoints: world, imagePoints: imgPts) else {
            residualLabel.text = "無法解算單應矩陣 (點位退化?)"
            reprojectedPolyLayer.path = nil
            return
        }

        var residualSum = 0.0
        var residualMax = 0.0
        var reprojectedView: [CGPoint] = []
        for i in 0..<5 {
            let (X, Y) = world[i]
            let u = H[0] * X + H[1] * Y + H[2]
            let v = H[3] * X + H[4] * Y + H[5]
            let w = H[6] * X + H[7] * Y + H[8]
            guard abs(w) > 1e-9 else { continue }
            let px = u / w
            let py = v / w
            let dx = px - Double(imgPts[i].x)
            let dy = py - Double(imgPts[i].y)
            let err = (dx * dx + dy * dy).squareRoot()
            residualSum += err
            residualMax = max(residualMax, err)
            reprojectedView.append(imagePixelToViewPoint(
                CGPoint(x: px, y: py), imageWidth: imageW, imageHeight: imageH
            ))
        }
        let residualMean = residualSum / 5.0

        let quality: String
        let color: UIColor
        if residualMax < 3 {
            quality = "極佳"
            color = .systemGreen
        } else if residualMax < 8 {
            quality = "可接受"
            color = .systemYellow
        } else {
            quality = "偏差過大，重新拖曳"
            color = .systemRed
        }
        residualLabel.text = String(format: "殘差 mean=%.1fpx max=%.1fpx  —  %@",
                                    residualMean, residualMax, quality)
        residualLabel.backgroundColor = color.withAlphaComponent(0.55)

        if reprojectedView.count == 5 {
            let path = UIBezierPath()
            path.move(to: reprojectedView[0])
            for pt in reprojectedView.dropFirst() { path.addLine(to: pt) }
            path.close()
            reprojectedPolyLayer.path = path.cgPath
            reprojectedPolyLayer.strokeColor = color.cgColor
        }
    }

    private func imagePixelToViewPoint(_ p: CGPoint, imageWidth: Int, imageHeight: Int) -> CGPoint {
        guard let previewLayer else { return .zero }
        // Apple's normalized capture device coords: (0,0) = top-left,
        // (1,1) = bottom-right in landscape-home-right — i.e. device.x is
        // the horizontal fraction along the long edge, device.y is the
        // vertical fraction along the short edge. The CVPixelBuffer is in
        // the same landscape frame, so image.x / imageWidth → device.x
        // (no axis swap). Earlier revisions swapped these, which visually
        // manifested as the ArUco marker overlay rendering rotated 90° and
        // stretched (cyan boxes never landed on the actual markers).
        let devX = CGFloat(p.x) / CGFloat(imageWidth)
        let devY = CGFloat(p.y) / CGFloat(imageHeight)
        return previewLayer.layerPointConverted(fromCaptureDevicePoint: CGPoint(x: devX, y: devY))
    }

    private func persistCalibrationFromDraggedPoints() {
        let d = UserDefaults.standard

        // --- 1) Compute homography (world plane -> image plane) ---
        let world = Self.plateWorldPoints()

        // Convert taps in view coordinates into approximate image pixel coordinates.
        // (This is approximate until we wire previewLayer->pixel coordinate mapping.)
        let imageW = d.integer(forKey: Self.keyImageWidthPx)
        let imageH = d.integer(forKey: Self.keyImageHeightPx)
        guard imageW > 0, imageH > 0 else {
            // Still persist intrinsics (they don't depend on taps).
            // Homography requires a consistent pixel coordinate frame.
            persistIntrinsicsIfPossible()
            return
        }

        let imgPtsViewOrder: [CGPoint] = [
            handles[.frontLeft]?.center ?? .zero,
            handles[.frontRight]?.center ?? .zero,
            handles[.rightShoulder]?.center ?? .zero,
            handles[.backTip]?.center ?? .zero,
            handles[.leftShoulder]?.center ?? .zero,
        ]

        let imgPts: [CGPoint] = imgPtsViewOrder.map { viewPointToImagePixel($0, imageWidth: imageW, imageHeight: imageH) }

        if let H = computeHomography(worldPoints: world, imagePoints: imgPts) {
            d.set(H, forKey: Self.keyHomography) // length-9
        }

        // --- 2) Compute & persist intrinsics estimates (fx/fz/cx/cy) ---
        persistIntrinsicsIfPossible()
    }

    private func persistIntrinsicsIfPossible() {
        let d = UserDefaults.standard

        // Respect Settings → "Use ChArUco values" override: if the user has
        // pasted precise intrinsics from calibrate_intrinsics.py, do NOT stomp
        // them with the FOV approximation.
        if d.string(forKey: SettingsViewController.keyIntrinsicsSource) == "manual" {
            return
        }

        let imageW = d.integer(forKey: Self.keyImageWidthPx)
        let imageH = d.integer(forKey: Self.keyImageHeightPx)
        let hFovRad = d.double(forKey: Self.keyHorizontalFovRad)

        // If horizontal FOV wasn't captured, we can't estimate focal lengths here.
        guard imageW > 0, imageH > 0, hFovRad > 0 else { return }

        // Spec approximation:
        // fx = (imageWidth / 2) / tan(hFOV/2)
        // verticalFov = 2*atan(tan(hFOV/2) * (imageHeight/imageWidth))
        // fz = (imageHeight / 2) / tan(verticalFov/2)
        let fx = (Double(imageW) / 2.0) / tan(hFovRad / 2.0)
        let verticalFov = 2.0 * atan(tan(hFovRad / 2.0) * (Double(imageH) / Double(imageW)))
        let fz = (Double(imageH) / 2.0) / tan(verticalFov / 2.0)
        let cx = Double(imageW) / 2.0
        let cy = Double(imageH) / 2.0

        d.set(cx, forKey: Self.keyIntrinsicCx)
        d.set(cy, forKey: Self.keyIntrinsicCy)
        d.set(fx, forKey: Self.keyIntrinsicFx)
        d.set(fz, forKey: Self.keyIntrinsicFz)
    }

    private func viewPointToImagePixel(_ p: CGPoint, imageWidth: Int, imageHeight: Int) -> CGPoint {
        guard let previewLayer else { return CGPoint(x: 0, y: 0) }

        // Convert from layer-space point to normalized capture-device coordinates.
        // This respects `videoGravity = .resizeAspectFill`, so cropped preview areas map correctly.
        let devicePoint = previewLayer.captureDevicePointConverted(fromLayerPoint: p)

        // Apple's capture-device normalized coords are in landscape-home-right:
        // device.x = horizontal fraction (along imageWidth = 1920 long edge),
        // device.y = vertical fraction (along imageHeight = 1080 short edge).
        // The CVPixelBuffer the ball detector reads uses the same landscape
        // (x, y) axes, so there's no axis swap between the two. An earlier
        // revision's comment claimed device.x was vertical — that was wrong,
        // and also meant any manual-handle homography it persisted was
        // rotated 90° relative to the ball detector's pixel coords (only the
        // overlay bug was visible, but the stored H was numerically off too).
        let px = CGFloat(imageWidth) * devicePoint.x
        let py = CGFloat(imageHeight) * devicePoint.y

        let clampedX = min(max(0, px), CGFloat(imageWidth - 1))
        let clampedY = min(max(0, py), CGFloat(imageHeight - 1))
        return CGPoint(x: clampedX, y: clampedY)
    }

    /// Compute homography H (3x3) mapping world plane (X,Y) -> image pixels (u,v).
    /// Accepts N ≥ 4 correspondences; N > 4 solves as least-squares via normal equations.
    /// Returns row-major length-9 array if solvable.
    private func computeHomography(worldPoints: [(Double, Double)], imagePoints: [CGPoint]) -> [Double]? {
        guard worldPoints.count == imagePoints.count, worldPoints.count >= 4 else { return nil }
        let n = worldPoints.count
        let rows = 2 * n

        // Build A (rows × 8) and b (rows) for the linearized system.
        var A = Array(repeating: Array(repeating: 0.0, count: 8), count: rows)
        var b = Array(repeating: 0.0, count: rows)

        for i in 0..<n {
            let (X, Y) = worldPoints[i]
            let x = Double(imagePoints[i].x)
            let y = Double(imagePoints[i].y)

            A[2 * i]     = [X, Y, 1, 0, 0, 0, -x * X, -x * Y]
            b[2 * i]     = x
            A[2 * i + 1] = [0, 0, 0, X, Y, 1, -y * X, -y * Y]
            b[2 * i + 1] = y
        }

        // Normal equations: (Aᵀ A) h = Aᵀ b → 8x8 system.
        var AtA = Array(repeating: Array(repeating: 0.0, count: 8), count: 8)
        var Atb = Array(repeating: 0.0, count: 8)
        for i in 0..<8 {
            for j in 0..<8 {
                var s = 0.0
                for k in 0..<rows { s += A[k][i] * A[k][j] }
                AtA[i][j] = s
            }
            var s = 0.0
            for k in 0..<rows { s += A[k][i] * b[k] }
            Atb[i] = s
        }

        if let h = solveLinearSystem8x8(A: AtA, b: Atb) {
            return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1.0]
        }
        return nil
    }

    private func solveLinearSystem8x8(A: [[Double]], b: [Double]) -> [Double]? {
        // Gaussian elimination with partial pivoting.
        var M = A
        var rhs = b

        let n = 8
        for col in 0..<n {
            // Find pivot.
            var pivotRow = col
            var pivotAbs = abs(M[col][col])
            for r in (col + 1)..<n {
                let v = abs(M[r][col])
                if v > pivotAbs {
                    pivotAbs = v
                    pivotRow = r
                }
            }

            if pivotAbs < 1e-12 { return nil }

            if pivotRow != col {
                M.swapAt(pivotRow, col)
                rhs.swapAt(pivotRow, col)
            }

            // Normalize pivot row.
            let pivot = M[col][col]
            for c in col..<n {
                M[col][c] /= pivot
            }
            rhs[col] /= pivot

            // Eliminate other rows.
            for r in 0..<n where r != col {
                let factor = M[r][col]
                if abs(factor) < 1e-15 { continue }
                for c in col..<n {
                    M[r][c] -= factor * M[col][c]
                }
                rhs[r] -= factor * rhs[col]
            }
        }

        return rhs
    }

    // MARK: - ArUco auto-calibration

    private func setupArucoOverlay() {
        markerOverlayLayer.strokeColor = UIColor.systemCyan.cgColor
        markerOverlayLayer.fillColor = UIColor.systemCyan.withAlphaComponent(0.18).cgColor
        markerOverlayLayer.lineWidth = 2
        view.layer.addSublayer(markerOverlayLayer)

        arucoStatusLabel.font = .systemFont(ofSize: 15, weight: .medium)
        arucoStatusLabel.textColor = .white
        arucoStatusLabel.textAlignment = .center
        arucoStatusLabel.backgroundColor = UIColor.black.withAlphaComponent(0.55)
        arucoStatusLabel.layer.cornerRadius = 8
        arucoStatusLabel.clipsToBounds = true
        arucoStatusLabel.numberOfLines = 2
        arucoStatusLabel.text = "ArUco: 等待偵測…"
        arucoStatusLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(arucoStatusLabel)
        NSLayoutConstraint.activate([
            arucoStatusLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
            arucoStatusLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            arucoStatusLabel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
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

        let required = Array(Self.markerWorldPoints.keys).sorted()
        let detectedIds = Set(markers.map { $0.id })
        let hits = required.filter { detectedIds.contains($0) }
        arucoStatusLabel.text = "ArUco: 偵測到 \(markers.count) 個 markers，匹配 \(hits.count)/\(required.count) (IDs \(hits.sorted()))"

        // Draw each marker's 4 corners as a polygon in view space.
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

    @objc private func saveArucoCalibration() {
        arucoLock.lock()
        let markers = latestMarkers
        let pixelSize = latestPixelSize
        arucoLock.unlock()

        let required = Array(Self.markerWorldPoints.keys).sorted()
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

        // Use each detected marker's centre as the correspondence to its plate vertex.
        let worldValues: [NSValue] = detected.map {
            let (x, y) = Self.markerWorldPoints[$0]!
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
        UserDefaults.standard.set(H, forKey: Self.keyHomography)
        UserDefaults.standard.set(Int(pixelSize.width), forKey: Self.keyImageWidthPx)
        UserDefaults.standard.set(Int(pixelSize.height), forKey: Self.keyImageHeightPx)

        // Intrinsics follow the same rule as manual Save: respect the
        // Settings → "Use ChArUco values" override.
        persistIntrinsicsIfPossible()
        log.info("calibration saved source=aruco markers=\(detected.count, privacy: .public)")
        postCalibrationToServer(source: "aruco")
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
extension CalibrationViewController: AVCaptureVideoDataOutputSampleBufferDelegate {
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

