import UIKit
import AVFoundation
import CoreMedia
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Manual 5-handle home-plate calibration. User drags each handle to a
/// plate landmark, the DLT solver recovers the homography, and tapping
/// Save writes it to UserDefaults + POSTs to the server.
final class ManualCalibrationViewController: UIViewController {
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
            label.font = DesignTokens.Fonts.sans(size: 12, weight: .bold)
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

    deinit {
        session.stopRunning()
    }

    // Handle layout + homography solve assume the view is wider than tall
    // (front-edge L/R on top, back tip at bottom). Portrait flips the
    // aspect ratio and the initial handle placement would overlap.
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        [.landscapeLeft, .landscapeRight]
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        title = "手動校正"

        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "儲存",
            style: .done,
            target: self,
            action: #selector(saveCalibration)
        )

        view.layoutIfNeeded()
        setupPreview()
        setupCornerHandles()
        setupResidualOverlay()
        refreshResidualAndPolygon()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
        reprojectedPolyLayer.frame = view.bounds
    }

    private func setupPreview() {
        session.beginConfiguration()
        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) { session.addInput(input) }
            } catch {
                log.error("manual-calibration camera input failed error=\(error.localizedDescription, privacy: .public)")
                showAlert(title: "相機無法開啟", message: "無法存取後置相機：\(error.localizedDescription)")
            }
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

    private func setupCornerHandles() {
        // Initial placement: pentagon in lower-center of frame.
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

        let fl = makeHandle(color: DesignTokens.Colors.accent, text: "FL")
        let fr = makeHandle(color: DesignTokens.Colors.success, text: "FR")
        let rs = makeHandle(color: DesignTokens.Colors.warning, text: "RS")
        let bt = makeHandle(color: DesignTokens.Colors.destructive, text: "BT")
        let ls = makeHandle(color: DesignTokens.Colors.cameraB, text: "LS")

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

    private func setupResidualOverlay() {
        reprojectedPolyLayer.strokeColor = DesignTokens.Colors.ok.cgColor
        reprojectedPolyLayer.fillColor = UIColor.clear.cgColor
        reprojectedPolyLayer.lineWidth = 2
        reprojectedPolyLayer.lineDashPattern = [6, 4]
        view.layer.addSublayer(reprojectedPolyLayer)

        residualLabel.font = DesignTokens.Fonts.sans(size: 16, weight: .semibold)
        residualLabel.textColor = DesignTokens.Colors.ink
        residualLabel.textAlignment = .center
        residualLabel.backgroundColor = DesignTokens.Colors.hudSurface
        residualLabel.layer.cornerRadius = DesignTokens.CornerRadius.card
        residualLabel.clipsToBounds = true
        residualLabel.numberOfLines = 2
        residualLabel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(residualLabel)

        NSLayoutConstraint.activate([
            residualLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            residualLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
            residualLabel.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -DesignTokens.Spacing.l),
            residualLabel.heightAnchor.constraint(greaterThanOrEqualToConstant: 56),
        ])
    }

    @objc private func saveCalibration() {
        persistCalibrationFromDraggedPoints()
        log.info("calibration saved source=manual")
        CalibrationShared.postCalibrationToServer(source: "manual")
        dismiss(animated: true)
    }

    private func refreshResidualAndPolygon() {
        let d = UserDefaults.standard
        let imageW = d.integer(forKey: CalibrationShared.keyImageWidthPx)
        let imageH = d.integer(forKey: CalibrationShared.keyImageHeightPx)
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
        let world = CalibrationShared.plateWorldPoints()

        guard let H = Self.computeHomography(worldPoints: world, imagePoints: imgPts) else {
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
            color = DesignTokens.Colors.ok
        } else if residualMax < 8 {
            quality = "可接受"
            color = DesignTokens.Colors.pending
        } else {
            quality = "偏差過大，重新拖曳"
            color = DesignTokens.Colors.fail
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
        // (1,1) = bottom-right in landscape-home-right. device.x is the
        // horizontal fraction along the long edge. CVPixelBuffer is in the
        // same landscape frame → image.x / imageWidth → device.x (no axis swap).
        let devX = CGFloat(p.x) / CGFloat(imageWidth)
        let devY = CGFloat(p.y) / CGFloat(imageHeight)
        return previewLayer.layerPointConverted(fromCaptureDevicePoint: CGPoint(x: devX, y: devY))
    }

    private func viewPointToImagePixel(_ p: CGPoint, imageWidth: Int, imageHeight: Int) -> CGPoint {
        guard let previewLayer else { return CGPoint(x: 0, y: 0) }
        let devicePoint = previewLayer.captureDevicePointConverted(fromLayerPoint: p)
        let px = CGFloat(imageWidth) * devicePoint.x
        let py = CGFloat(imageHeight) * devicePoint.y
        let clampedX = min(max(0, px), CGFloat(imageWidth - 1))
        let clampedY = min(max(0, py), CGFloat(imageHeight - 1))
        return CGPoint(x: clampedX, y: clampedY)
    }

    private func persistCalibrationFromDraggedPoints() {
        let d = UserDefaults.standard
        let world = CalibrationShared.plateWorldPoints()
        let imageW = d.integer(forKey: CalibrationShared.keyImageWidthPx)
        let imageH = d.integer(forKey: CalibrationShared.keyImageHeightPx)
        guard imageW > 0, imageH > 0 else {
            CalibrationShared.persistFovIntrinsicsIfPossible()
            return
        }
        let imgPtsViewOrder: [CGPoint] = [
            handles[.frontLeft]?.center ?? .zero,
            handles[.frontRight]?.center ?? .zero,
            handles[.rightShoulder]?.center ?? .zero,
            handles[.backTip]?.center ?? .zero,
            handles[.leftShoulder]?.center ?? .zero,
        ]
        let imgPts: [CGPoint] = imgPtsViewOrder.map {
            viewPointToImagePixel($0, imageWidth: imageW, imageHeight: imageH)
        }
        if let H = Self.computeHomography(worldPoints: world, imagePoints: imgPts) {
            d.set(H, forKey: CalibrationShared.keyHomography)
        }
        CalibrationShared.persistFovIntrinsicsIfPossible()
    }

    /// DLT via 8×8 normal equations with Gaussian elimination. Returns a
    /// row-major length-9 homography mapping world plane (X,Y) to image
    /// pixels (u,v); h33 is normalised to 1. Manual-only — the Auto path
    /// goes through OpenCV RANSAC directly.
    private static func computeHomography(
        worldPoints: [(Double, Double)],
        imagePoints: [CGPoint]
    ) -> [Double]? {
        guard worldPoints.count == imagePoints.count, worldPoints.count >= 4 else { return nil }
        let n = worldPoints.count
        let rows = 2 * n

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

        guard let h = solveLinearSystem8x8(A: AtA, b: Atb) else { return nil }
        return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1.0]
    }

    private static func solveLinearSystem8x8(A: [[Double]], b: [Double]) -> [Double]? {
        var M = A
        var rhs = b
        let n = 8
        for col in 0..<n {
            var pivotRow = col
            var pivotAbs = abs(M[col][col])
            for r in (col + 1)..<n {
                let v = abs(M[r][col])
                if v > pivotAbs { pivotAbs = v; pivotRow = r }
            }
            if pivotAbs < 1e-12 { return nil }
            if pivotRow != col {
                M.swapAt(pivotRow, col)
                rhs.swapAt(pivotRow, col)
            }
            let pivot = M[col][col]
            for c in col..<n { M[col][c] /= pivot }
            rhs[col] /= pivot
            for r in 0..<n where r != col {
                let factor = M[r][col]
                if abs(factor) < 1e-15 { continue }
                for c in col..<n { M[r][c] -= factor * M[col][c] }
                rhs[r] -= factor * rhs[col]
            }
        }
        return rhs
    }

    private func showAlert(title: String, message: String) {
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: true)
    }
}
