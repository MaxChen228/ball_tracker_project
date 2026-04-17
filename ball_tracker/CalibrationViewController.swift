import UIKit
import AVFoundation
import CoreMedia

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

    deinit {
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

        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "Save",
            style: .done,
            target: self,
            action: #selector(saveCalibration)
        )

        view.layoutIfNeeded()
        setupPreview()
        setupCornerHandles()
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
                // TODO: handle error UI
            }
        }

        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
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
        dismiss(animated: true)
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
    }

    private func persistCalibrationFromDraggedPoints() {
        let d = UserDefaults.standard

        // --- 1) Compute homography (world plane -> image plane) ---
        // Real home-plate pentagon vertices in meters. Axes: X = left/right,
        // Y = depth from front edge (pitcher side) toward back tip (catcher side).
        let widthM = 0.432       // 17" front edge
        let shoulderY = 0.216    // 8.5" back to shoulder
        let tipY = 0.432         // 17" back to back tip

        let world: [(Double, Double)] = [
            (-widthM / 2.0, 0.0),         // FL
            (widthM / 2.0, 0.0),          // FR
            (widthM / 2.0, shoulderY),    // RS (right shoulder)
            (0.0, tipY),                  // BT (back tip, on centerline)
            (-widthM / 2.0, shoulderY),   // LS (left shoulder)
        ]

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

        // AVCapture normalized coordinates are in landscape camera space:
        // x -> vertical fraction, y -> horizontal fraction.
        // For the current portrait-style UI, map into image pixels by image axes.
        let px = CGFloat(imageWidth) * devicePoint.y
        let py = CGFloat(imageHeight) * devicePoint.x

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
}

