import AVFoundation
import CoreVideo
import Foundation
import UIKit
import os

private let calLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "intrinsics.capture")

/// One slot in the pose-diversity tracker. Each axis has N bins; a shot
/// is recorded into one bin per axis. The state machine only allows
/// `solve()` when every axis has ≥1 bin hit (all-bins-filled) AND total
/// shotCount ≥ targetShotCount.
private struct PoseDiversityTracker {
    /// 5 yaw bins covering ±60° board-relative-to-camera-axis range.
    private static let yawBinCount = 5
    private static let yawHalfRangeRad: Float = .pi / 3
    /// 5 pitch bins, same range.
    private static let pitchBinCount = 5
    private static let pitchHalfRangeRad: Float = .pi / 3
    /// 3 distance buckets: near (≤0.6m), mid (0.6–1.2m), far (>1.2m).
    /// Calibration board is typically held 0.4–1.5m from a phone.
    private static let distanceBinThresholds: [Float] = [0.6, 1.2]

    private var yawBins: Set<Int> = []
    private var pitchBins: Set<Int> = []
    private var distanceBins: Set<Int> = []

    mutating func record(yaw: Float, pitch: Float, distance: Float) {
        yawBins.insert(Self.binIndex(yaw, halfRange: Self.yawHalfRangeRad, count: Self.yawBinCount))
        pitchBins.insert(Self.binIndex(pitch, halfRange: Self.pitchHalfRangeRad, count: Self.pitchBinCount))
        distanceBins.insert(Self.distanceBin(distance))
    }

    var yawProgress: Float { Float(yawBins.count) / Float(Self.yawBinCount) }
    var pitchProgress: Float { Float(pitchBins.count) / Float(Self.pitchBinCount) }
    var distanceProgress: Float { Float(distanceBins.count) / (Float(Self.distanceBinThresholds.count) + 1) }

    var allAxesCovered: Bool {
        yawBins.count >= 3 && pitchBins.count >= 3 && distanceBins.count >= 2
    }

    private static func binIndex(_ value: Float, halfRange: Float, count: Int) -> Int {
        let clamped = max(-halfRange, min(halfRange, value))
        let t = (clamped + halfRange) / (2 * halfRange)
        return min(count - 1, max(0, Int(t * Float(count))))
    }

    private static func distanceBin(_ d: Float) -> Int {
        for (i, threshold) in distanceBinThresholds.enumerated() {
            if d <= threshold { return i }
        }
        return distanceBinThresholds.count
    }
}

/// Pose fingerprint: quantized rvec (8°) + tvec distance bucket (10cm).
/// Same angle, different distance = new pose (separability for fx vs cx).
private struct PoseFingerprint: Hashable {
    let rx: Int8; let ry: Int8; let rz: Int8
    let distDecimeters: Int8

    init(rvecXY: CGPoint, rvecZ: CGFloat, tvecMagnitudeM: CGFloat) {
        let q: Double = 8.0 * .pi / 180.0
        rx = Int8(clamping: Int((Double(rvecXY.x) / q).rounded()))
        ry = Int8(clamping: Int((Double(rvecXY.y) / q).rounded()))
        rz = Int8(clamping: Int((Double(rvecZ) / q).rounded()))
        distDecimeters = Int8(clamping: Int((Double(tvecMagnitudeM) * 10.0).rounded()))
    }
}

/// Rolling median over the last N samples for adaptive sharpness gating.
/// Linear-time on insert — N is small (32), no need for a balanced heap.
private struct RollingMedian {
    private var samples: [Double] = []
    private let capacity: Int

    init(capacity: Int = 32) { self.capacity = capacity }

    mutating func push(_ v: Double) {
        samples.append(v)
        if samples.count > capacity { samples.removeFirst() }
    }

    var median: Double {
        guard !samples.isEmpty else { return 0 }
        let sorted = samples.sorted()
        return sorted[sorted.count / 2]
    }

    var hasEnoughSamples: Bool { samples.count >= 8 }
}

// MARK: - Public API

/// Operator-facing UI state. The VC translates each into a localized
/// status string — none of these strings escape the controller.
enum IntrinsicsCaptureStatus: Equatable {
    case waitingForBoard       // "把棋盤格移到畫面中央"
    case boardTooFar           // "再靠近一點"
    case duplicatePose         // "換一個角度"
    case unstable              // "穩住相機"
    case capturing(progress: Float)  // overall normalized 0...1
    case solving               // "校正中..."
    case uploading             // "上傳中..."
    case completed             // "完成"
    case failedTooFewShots     // 60s timeout / solve failed
    case failedUpload(String)  // post failed; can retry
}

/// What the VC needs to render each frame.
struct IntrinsicsCaptureProgress {
    let status: IntrinsicsCaptureStatus
    let yawProgress: Float
    let pitchProgress: Float
    let distanceProgress: Float
    /// Most recent detected ChArUco corner positions in PREVIEW pixel
    /// coords (1280×720 NV12 grid). VC draws the green dots overlay.
    let lastCorners: [CGPoint]
}

/// Self-contained AVCaptureSession + auto-trigger state machine for the
/// ChArUco calibration flow. Owned by `IntrinsicsCalibrationViewController`.
///
/// Lifecycle:
///   init → configureSession() → startSession()
///   ...
///   stopSession() (idempotent; called from VC viewWillDisappear)
final class IntrinsicsCaptureController: NSObject {

    // MARK: Tunables

    private static let targetShotCount = 20
    private static let previewMaxHz: TimeInterval = 0.1   // 10 Hz cap
    private static let focusStableSeconds: TimeInterval = 0.3
    private static let sharpnessRatioFloor: Double = 0.7
    private static let boardFillFloor: Double = 0.10
    private static let minCornerCount = 20
    private static let stableFramesRequired = 3
    private static let inactivityTimeoutSeconds: TimeInterval = 60

    // MARK: Callbacks

    /// Fired on main queue whenever progress / status changes.
    var onProgress: ((IntrinsicsCaptureProgress) -> Void)?
    /// Fired on main queue when shotCount + diversity satisfy targets.
    /// VC then dismisses preview UI, kicks off solve+upload on a background queue.
    var onReadyToSolve: ((NSInteger, NSInteger) -> Void)?  // imageWidth, imageHeight

    // MARK: Public state (read by VC)

    let session = AVCaptureSession()
    /// Preview layer that the VC inserts under its overlay views.
    let previewLayer: AVCaptureVideoPreviewLayer

    private(set) var captureDevice: AVCaptureDevice?
    /// Average AVCaptureDevice.lensPosition across all accepted shots.
    /// Written into `source_label` metadata to give us a way to detect
    /// the runtime-vs-calibration focus mismatch problem if K turns
    /// out to be off in production.
    private(set) var averageLensPosition: Float = 0

    // MARK: Internals

    private let calibrator = BTCharucoCalibrator()
    private var lastPreviewAt: Date = .distantPast
    private var lastShotAcceptedAt: Date = Date()
    private var focusStableSince: Date?
    private var photoInFlight: Bool = false
    private var seenFingerprints: Set<PoseFingerprint> = []
    private var pendingFingerprint: PoseFingerprint?
    private var stableFramesAtCurrentPose: Int = 0
    private var sharpnessMedian = RollingMedian()
    private var diversity = PoseDiversityTracker()
    private var lastDetectedCornersPreview: [CGPoint] = []
    private var lensPositionSum: Float = 0
    private var lensPositionSamples: Int = 0
    private var lastShotImageWidth: NSInteger = 0
    private var lastShotImageHeight: NSInteger = 0

    /// Single serial queue used as the **only** writer for every piece of
    /// shared mutable state on this controller:
    /// - video preview delegate callbacks (processPreviewFrame)
    /// - photo capture issuance (capturePhoto)
    /// - photo result acceptance (didFinishPhotoCapture)
    /// - solve() invocation
    /// Serialising everything onto this queue eliminates the data race that
    /// existed when video frames ran on a separate `videoQueue` while
    /// didFinishPhotoCapture mutated `seenFingerprints` / `diversity` /
    /// `lastShotAcceptedAt` etc. from sessionQueue — Swift Sets and the
    /// underlying std::vector in `BTCharucoCalibrator` are not safe for
    /// concurrent mutation. The 10 Hz preview cap on processPreviewFrame
    /// makes the heavy ChArUco eval (~10-50 ms) on this serial queue
    /// affordable; AVCaptureSession reconfig is only called at lifecycle
    /// boundaries so the queue is not on the critical reconfig path.
    private let sessionQueue = DispatchQueue(label: "com.Max0228.ball-tracker.intrinsics.session")
    private let videoOutput = AVCaptureVideoDataOutput()
    private let photoOutput = AVCapturePhotoOutput()
    /// Strong refs to in-flight photo delegates — AVFoundation only holds
    /// them weakly. Without this they're dealloc'd before the callback.
    private var photoDelegates: [Int64: PhotoDelegate] = [:]

    override init() {
        previewLayer = AVCaptureVideoPreviewLayer(session: session)
        previewLayer.videoGravity = .resizeAspect
        super.init()
    }

    /// Synchronous (caller is expected to be on main queue from VC viewDidLoad).
    /// Returns false if the session graph couldn't be built — VC then bails
    /// out with the "光線不足或板看不清楚" UI as the catch-all message.
    func configureSession() -> Bool {
        session.beginConfiguration()
        session.sessionPreset = .photo  // gives us 4032×3024 photo + reasonable video

        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                    for: .video, position: .back) else {
            session.commitConfiguration()
            calLog.error("no back wide-angle camera available")
            return false
        }
        captureDevice = device

        do {
            let input = try AVCaptureDeviceInput(device: device)
            guard session.canAddInput(input) else {
                session.commitConfiguration()
                return false
            }
            session.addInput(input)
        } catch {
            session.commitConfiguration()
            calLog.error("AVCaptureDeviceInput init failed: \(error.localizedDescription, privacy: .public)")
            return false
        }

        // Video data output for live preview eval. NV12 because the
        // CharucoCalibrator preview path reads the Y plane directly
        // (mapPixelBufferToGray in CharucoCalibrator.mm) — no BGRA
        // conversion needed for the gray-only ChArUco detector.
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String:
                Int(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange),
        ]
        videoOutput.setSampleBufferDelegate(self, queue: sessionQueue)
        guard session.canAddOutput(videoOutput) else {
            session.commitConfiguration()
            return false
        }
        session.addOutput(videoOutput)

        // Photo output — 4032×3024 max-resolution JPEG.
        guard session.canAddOutput(photoOutput) else {
            session.commitConfiguration()
            return false
        }
        session.addOutput(photoOutput)
        photoOutput.maxPhotoQualityPrioritization = .quality

        // Continuous AF — see CLAUDE plan: runtime is implicit continuous
        // AF, so calibration matches. Snap gate (focus-stable ≥0.3s) is
        // the consistency mechanism, not focus lock.
        do {
            try device.lockForConfiguration()
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            device.unlockForConfiguration()
        } catch {
            calLog.error("device.lockForConfiguration failed: \(error.localizedDescription, privacy: .public)")
        }

        // Orient preview + outputs to portrait. The board is held portrait
        // by the operator; landscape would flip the corner overlay.
        if let conn = previewLayer.connection, conn.isVideoRotationAngleSupported(90) {
            conn.videoRotationAngle = 90
        }
        if let conn = videoOutput.connection(with: .video), conn.isVideoRotationAngleSupported(90) {
            conn.videoRotationAngle = 90
        }
        if let conn = photoOutput.connection(with: .video), conn.isVideoRotationAngleSupported(90) {
            conn.videoRotationAngle = 90
        }

        session.commitConfiguration()
        return true
    }

    func startSession() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if !self.session.isRunning {
                self.session.startRunning()
                calLog.info("intrinsics session started")
            }
        }
    }

    func stopSession() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if self.session.isRunning {
                self.session.stopRunning()
                calLog.info("intrinsics session stopped")
            }
        }
    }

    func reset() {
        calibrator.reset()
        seenFingerprints.removeAll()
        pendingFingerprint = nil
        stableFramesAtCurrentPose = 0
        diversity = PoseDiversityTracker()
        sharpnessMedian = RollingMedian()
        lensPositionSum = 0
        lensPositionSamples = 0
        lastShotImageWidth = 0
        lastShotImageHeight = 0
        lastShotAcceptedAt = Date()
        photoInFlight = false
        focusStableSince = nil
        lastDetectedCornersPreview = []
    }

    // MARK: - State machine

    fileprivate func processPreviewFrame(_ pixelBuffer: CVPixelBuffer) {
        let now = Date()
        if now.timeIntervalSince(lastPreviewAt) < Self.previewMaxHz { return }
        lastPreviewAt = now

        // Inactivity timeout — VC handles by surfacing the failure status.
        if calibrator.shotCount > 0 || !seenFingerprints.isEmpty {
            if now.timeIntervalSince(lastShotAcceptedAt) > Self.inactivityTimeoutSeconds {
                emitProgress(.failedTooFewShots)
                return
            }
        }

        if photoInFlight { return }

        guard let device = captureDevice else { return }
        if device.isAdjustingFocus {
            focusStableSince = nil
            emitProgress(.unstable)
            return
        }
        if focusStableSince == nil { focusStableSince = now }
        if now.timeIntervalSince(focusStableSince!) < Self.focusStableSeconds {
            emitProgress(.unstable)
            return
        }

        guard let eval = calibrator.evaluatePreviewFrame(pixelBuffer) else {
            lastDetectedCornersPreview = []
            emitProgress(.waitingForBoard)
            return
        }

        // Capture corners for overlay rendering. evaluatePreviewFrame
        // doesn't expose raw corners through the ObjC bridge — re-run
        // is too costly; instead use the bbox center + tvecMagnitude as
        // a coarse indicator until the actual corner array is plumbed.
        // For now leave lastDetectedCornersPreview empty so the overlay
        // shows nothing (cleaner than fake dots). Add corner export to
        // the bridge in a follow-up if the no-overlay UX is unclear.
        lastDetectedCornersPreview = []

        if eval.cornerCount < Self.minCornerCount {
            emitProgress(.waitingForBoard)
            return
        }
        if eval.boardFillRatio < Self.boardFillFloor {
            emitProgress(.boardTooFar)
            return
        }

        sharpnessMedian.push(Double(eval.sharpnessVar))
        if sharpnessMedian.hasEnoughSamples {
            let floor = sharpnessMedian.median * Self.sharpnessRatioFloor
            if Double(eval.sharpnessVar) < floor {
                emitProgress(.unstable)
                return
            }
        }

        let fp = PoseFingerprint(rvecXY: eval.rvecXY,
                                   rvecZ: eval.rvecZ,
                                   tvecMagnitudeM: eval.tvecMagnitudeM)
        if seenFingerprints.contains(fp) {
            stableFramesAtCurrentPose = 0
            pendingFingerprint = nil
            emitProgress(.duplicatePose)
            return
        }

        if pendingFingerprint == fp {
            stableFramesAtCurrentPose += 1
        } else {
            pendingFingerprint = fp
            stableFramesAtCurrentPose = 1
        }
        if stableFramesAtCurrentPose < Self.stableFramesRequired {
            emitProgress(.capturing(progress: overallProgress))
            return
        }

        // Stable + novel pose → trigger photo capture.
        photoInFlight = true
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        settings.photoQualityPrioritization = .quality
        let delegate = PhotoDelegate(owner: self, fingerprint: fp,
                                      eval: eval,
                                      lensPosition: device.lensPosition)
        photoDelegates[settings.uniqueID] = delegate
        // Already on sessionQueue (video delegate dispatch and photo issuance
        // now share the same serial queue) — capturePhoto runs in-line.
        photoOutput.capturePhoto(with: settings, delegate: delegate)
        emitProgress(.capturing(progress: overallProgress))
    }

    /// Called by the photo delegate after a JPEG arrives and
    /// `acceptShotFromJPEG` returns. Runs on session queue.
    fileprivate func didFinishPhotoCapture(jpeg: Data?,
                                            fingerprint: PoseFingerprint,
                                            eval: BTCharucoFrameEval,
                                            lensPosition: Float,
                                            settingsID: Int64) {
        photoDelegates.removeValue(forKey: settingsID)
        defer { photoInFlight = false }

        guard let jpeg, !jpeg.isEmpty else {
            calLog.warning("photo capture returned empty JPEG")
            return
        }

        var w: NSInteger = 0
        var h: NSInteger = 0
        do {
            try ObjC.tryAccept(calibrator: calibrator, jpeg: jpeg,
                                widthOut: &w, heightOut: &h)
        } catch {
            calLog.warning("acceptShotFromJPEG rejected: \(error.localizedDescription, privacy: .public)")
            return
        }

        seenFingerprints.insert(fingerprint)
        diversity.record(yaw: Float(eval.tvecYaw),
                          pitch: Float(eval.tvecPitch),
                          distance: Float(eval.tvecMagnitudeM))
        lastShotAcceptedAt = Date()
        lensPositionSum += lensPosition
        lensPositionSamples += 1
        lastShotImageWidth = w
        lastShotImageHeight = h
        averageLensPosition = lensPositionSamples > 0
            ? lensPositionSum / Float(lensPositionSamples) : 0

        DispatchQueue.main.async {
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
        }

        if calibrator.shotCount >= Self.targetShotCount && diversity.allAxesCovered {
            emitProgress(.solving)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.onReadyToSolve?(self.lastShotImageWidth, self.lastShotImageHeight)
            }
        } else {
            emitProgress(.capturing(progress: overallProgress))
        }
    }

    /// Run cv::calibrateCamera on `sessionQueue` — the same queue every
    /// `didFinishPhotoCapture` runs on. Serialising solve onto the writer
    /// queue closes the race where an in-flight photo callback could
    /// still be `push_back`-ing into the calibrator's `_objPts/_imgPts`
    /// std::vector while solve started reading them on a global queue.
    /// `completion` always fires on main queue.
    func solveAsync(completion: @escaping (Result<BTCharucoSolveResult, Error>) -> Void) {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            let result: Result<BTCharucoSolveResult, Error>
            do {
                let r = try ObjC.solve(calibrator: self.calibrator,
                                         imageWidth: self.lastShotImageWidth,
                                         imageHeight: self.lastShotImageHeight)
                result = .success(r)
            } catch {
                result = .failure(error)
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    var shotCount: NSInteger { calibrator.shotCount }
    var imageWidth: NSInteger { lastShotImageWidth }
    var imageHeight: NSInteger { lastShotImageHeight }

    // MARK: - Helpers

    private var overallProgress: Float {
        let shotFrac = min(1, Float(calibrator.shotCount) / Float(Self.targetShotCount))
        let diversityFrac = (diversity.yawProgress
                                + diversity.pitchProgress
                                + diversity.distanceProgress) / 3
        return min(shotFrac, diversityFrac)
    }

    private func emitProgress(_ status: IntrinsicsCaptureStatus) {
        let snap = IntrinsicsCaptureProgress(
            status: status,
            yawProgress: diversity.yawProgress,
            pitchProgress: diversity.pitchProgress,
            distanceProgress: diversity.distanceProgress,
            lastCorners: lastDetectedCornersPreview
        )
        DispatchQueue.main.async { [weak self] in
            self?.onProgress?(snap)
        }
    }
}

// MARK: - AVCaptureVideoDataOutputSampleBufferDelegate

extension IntrinsicsCaptureController: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        processPreviewFrame(pb)
    }
}

// MARK: - Photo capture delegate

private final class PhotoDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    private weak var owner: IntrinsicsCaptureController?
    private let fingerprint: PoseFingerprint
    private let eval: BTCharucoFrameEval
    private let lensPosition: Float

    init(owner: IntrinsicsCaptureController,
         fingerprint: PoseFingerprint,
         eval: BTCharucoFrameEval,
         lensPosition: Float) {
        self.owner = owner
        self.fingerprint = fingerprint
        self.eval = eval
        self.lensPosition = lensPosition
    }

    func photoOutput(_ output: AVCapturePhotoOutput,
                      didFinishProcessingPhoto photo: AVCapturePhoto,
                      error: Error?) {
        if let error {
            calLog.warning("AVCapturePhoto error: \(error.localizedDescription, privacy: .public)")
        }
        let jpeg = photo.fileDataRepresentation()
        let id = Int64(photo.resolvedSettings.uniqueID)
        owner?.sessionQueueAccept(jpeg: jpeg,
                                    fingerprint: fingerprint,
                                    eval: eval,
                                    lensPosition: lensPosition,
                                    settingsID: id)
    }
}

extension IntrinsicsCaptureController {
    /// Hop the photo result onto the session queue (single writer to
    /// calibrator state). Internal; only the PhotoDelegate calls this.
    fileprivate func sessionQueueAccept(jpeg: Data?,
                                          fingerprint: PoseFingerprint,
                                          eval: BTCharucoFrameEval,
                                          lensPosition: Float,
                                          settingsID: Int64) {
        sessionQueue.async { [weak self] in
            self?.didFinishPhotoCapture(jpeg: jpeg,
                                          fingerprint: fingerprint,
                                          eval: eval,
                                          lensPosition: lensPosition,
                                          settingsID: settingsID)
        }
    }
}

// MARK: - Obj-C bridge tunnel

/// The ObjC++ wrapper expects NSError**; Swift can't take pointers to
/// optional NSError directly when the method also returns BOOL. Wrap the
/// two error-throwing entry points so the call sites read cleanly.
private enum ObjC {
    enum CalibratorError: Error { case failed(String) }

    // ObjC `BOOL accept...error:` and `nullable solve...error:` auto-bridge
    // to Swift throwing methods — the NSError** is consumed by the bridge,
    // not passed by the caller. Wrapping here just gives us in/out for the
    // image-size pointers that aren't part of the throws contract.
    static func tryAccept(calibrator: BTCharucoCalibrator,
                           jpeg: Data,
                           widthOut: inout NSInteger,
                           heightOut: inout NSInteger) throws {
        var width: NSInteger = 0
        var height: NSInteger = 0
        try calibrator.acceptShot(fromJPEG: jpeg,
                                    imageWidth: &width,
                                    imageHeight: &height)
        widthOut = width
        heightOut = height
    }

    static func solve(calibrator: BTCharucoCalibrator,
                       imageWidth: NSInteger,
                       imageHeight: NSInteger) throws -> BTCharucoSolveResult {
        try calibrator.solve(withImageWidth: imageWidth, imageHeight: imageHeight)
    }
}
