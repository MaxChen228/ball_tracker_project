import UIKit
import AVFoundation
import CoreMedia
import os

private let captureLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.capture")

final class CameraCaptureRuntime {
    struct AppliedTelemetry {
        let widthPx: Int
        let heightPx: Int
        let appliedFps: Double
        let formatFovDeg: Double?
        let formatIndex: Int?
        let isVideoBinned: Bool?
        let appliedMaxExposureS: Double?
    }

    private let standbyFps: Double
    private let trackingFps: Double
    private let session = AVCaptureSession()
    private let videoOutput = AVCaptureVideoDataOutput()
    private let sessionQueue = DispatchQueue(label: "camera.session.queue", qos: .userInitiated)

    private var previewLayer: AVCaptureVideoPreviewLayer?
    private var audioInput: AVCaptureDeviceInput?
    private var audioOutput: AVCaptureAudioDataOutput?
    private(set) var chirpDetector: AudioChirpDetector?

    private let onTelemetryUpdated: (AppliedTelemetry) -> Void
    private let onErrorBanner: (String) -> Void
    private let onStatusText: (String) -> Void

    private(set) var trackingExposureCapMode: ServerUploader.TrackingExposureCapMode
    private(set) var currentCaptureHeight: Int
    var currentCaptureWidth: Int { captureWidthForHeight(currentCaptureHeight) }
    var isSessionRunning: Bool { session.isRunning }
    var previewVideoLayer: AVCaptureVideoPreviewLayer? { previewLayer }

    init(
        standbyFps: Double,
        trackingFps: Double,
        initialCaptureHeight: Int,
        trackingExposureCapMode: ServerUploader.TrackingExposureCapMode,
        onTelemetryUpdated: @escaping (AppliedTelemetry) -> Void,
        onErrorBanner: @escaping (String) -> Void,
        onStatusText: @escaping (String) -> Void
    ) {
        self.standbyFps = standbyFps
        self.trackingFps = trackingFps
        self.currentCaptureHeight = initialCaptureHeight
        self.trackingExposureCapMode = trackingExposureCapMode
        self.onTelemetryUpdated = onTelemetryUpdated
        self.onErrorBanner = onErrorBanner
        self.onStatusText = onStatusText
    }

    func configureCaptureGraph(
        in view: UIView,
        bounds: CGRect,
        videoDelegate: AVCaptureVideoDataOutputSampleBufferDelegate,
        processingQueue: DispatchQueue
    ) {
        session.beginConfiguration()

        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            dumpAvailableFormats(for: device)
            do {
                try configureCaptureFormat(
                    device,
                    targetWidth: currentCaptureWidth,
                    targetHeight: currentCaptureHeight,
                    targetFps: standbyFps
                )

                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
                captureLog.error("camera capture format configuration failed error=\(error.localizedDescription, privacy: .public)")
            }
        }

        videoOutput.setSampleBufferDelegate(videoDelegate, queue: processingQueue)
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        if session.canAddOutput(videoOutput) {
            session.addOutput(videoOutput)
        }

        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        preview.videoGravity = .resizeAspect
        preview.frame = previewFrame(in: bounds)
        if let connection = preview.connection, connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        preview.isHidden = true
        view.layer.insertSublayer(preview, at: 0)
        previewLayer = preview
    }

    func requestAudioCaptureAccess(cameraRole: String) {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self else { return }
            DispatchQueue.main.async {
                if granted {
                    self.configureAudioCapture(cameraRole: cameraRole)
                } else {
                    captureLog.error("camera mic permission denied cam=\(cameraRole, privacy: .public)")
                    self.onStatusText("麥克風未授權 · 無法時間校正")
                }
            }
        }
    }

    func updatePreviewFrame(in bounds: CGRect) {
        previewLayer?.frame = previewFrame(in: bounds)
    }

    func startCapture(targetFps: Double) {
        guard let device = currentCaptureDevice else { return }
        previewLayer?.isHidden = false
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if self.session.isRunning {
                self.reconfigureActiveSession(device: device, targetFps: targetFps)
                return
            }
            do {
                try self.configureCaptureFormat(
                    device,
                    targetWidth: self.currentCaptureWidth,
                    targetHeight: self.currentCaptureHeight,
                    targetFps: targetFps
                )
                self.session.startRunning()
                captureLog.info("camera capture started fps=\(targetFps)")
            } catch {
                captureLog.error("camera capture start failed error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self.onErrorBanner("相機啟動失敗 (\(Int(targetFps))fps)")
                }
            }
        }
    }

    func stopCapture(resetFpsState: @escaping () -> Void) {
        previewLayer?.isHidden = true
        resetFpsState()
        sessionQueue.async { [weak self] in
            guard let self else { return }
            guard self.session.isRunning else { return }
            self.session.stopRunning()
            captureLog.info("camera capture stopped")
        }
    }

    func reconcileStandbyCaptureState(previewRequested: Bool, calibrationFrameCaptureArmed: Bool) {
        if previewRequested || calibrationFrameCaptureArmed {
            startCapture(targetFps: standbyFps)
        } else {
            stopCapture(resetFpsState: {})
        }
    }

    func applyServerCaptureHeight(_ newHeight: Int, bounds: CGRect) {
        guard let device = currentCaptureDevice else { return }
        let width: Int
        switch newHeight {
        case 720: width = 1280
        case 1080: width = 1920
        default:
            captureLog.warning("ignore unsupported capture_height \(newHeight)")
            return
        }
        currentCaptureHeight = newHeight
        previewLayer?.frame = previewFrame(in: bounds)
        previewLayer?.isHidden = false
        sessionQueue.async { [weak self] in
            guard let self else { return }
            let wasRunning = self.session.isRunning
            if wasRunning { self.session.stopRunning() }
            defer { if wasRunning { self.session.startRunning() } }
            do {
                try self.configureCaptureFormat(
                    device,
                    targetWidth: width,
                    targetHeight: newHeight,
                    targetFps: self.standbyFps
                )
                captureLog.info("camera resolution swapped to \(width)x\(newHeight) from server push")
            } catch {
                captureLog.error("camera resolution swap failed target=\(width)x\(newHeight) error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self.onErrorBanner("解析度切換失敗 (\(newHeight)p 不支援)")
                }
            }
        }
    }

    func applyTrackingExposureCap(_ rawMode: String, targetFps: Double) {
        let exposureMode = ServerUploader.TrackingExposureCapMode(rawValue: rawMode) ?? .frameDuration
        guard trackingExposureCapMode != exposureMode else { return }
        trackingExposureCapMode = exposureMode
        guard let device = currentCaptureDevice else { return }
        sessionQueue.async { [weak self] in
            guard let self else { return }
            do {
                try device.lockForConfiguration()
                defer { device.unlockForConfiguration() }
                let appliedMaxExposureS = try self.applyExposureConfiguration(
                    device,
                    format: device.activeFormat,
                    targetFps: targetFps
                )
                let dims = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
                let applied = device.activeVideoMinFrameDuration
                let appliedFps = applied.value > 0
                    ? Double(applied.timescale) / Double(applied.value)
                    : targetFps
                self.onTelemetryUpdated(
                    .init(
                        widthPx: Int(dims.width),
                        heightPx: Int(dims.height),
                        appliedFps: appliedFps,
                        formatFovDeg: Double(device.activeFormat.videoFieldOfView),
                        formatIndex: nil,
                        isVideoBinned: device.activeFormat.isVideoBinned,
                        appliedMaxExposureS: appliedMaxExposureS
                    )
                )
                captureLog.info("tracking exposure cap hot-applied from server: \(exposureMode.rawValue, privacy: .public)")
            } catch {
                captureLog.error("tracking exposure cap apply failed mode=\(exposureMode.rawValue, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            }
        }
    }

    func setChirpThreshold(_ threshold: Double) {
        chirpDetector?.setThreshold(Float(threshold))
    }

    func chirpSnapshot() -> AudioChirpDetector.Snapshot? {
        chirpDetector?.lastSnapshot
    }

    func currentCaptureTelemetry(
        latestImageWidth: Int,
        latestImageHeight: Int,
        targetFps: Double,
        appliedTelemetry: AppliedTelemetry?
    ) -> ServerUploader.CaptureTelemetry {
        ServerUploader.CaptureTelemetry(
            width_px: latestImageWidth > 0 ? latestImageWidth : (appliedTelemetry?.widthPx ?? 0),
            height_px: latestImageHeight > 0 ? latestImageHeight : (appliedTelemetry?.heightPx ?? 0),
            target_fps: targetFps,
            applied_fps: appliedTelemetry?.appliedFps ?? 0,
            format_fov_deg: appliedTelemetry?.formatFovDeg,
            format_index: appliedTelemetry?.formatIndex,
            is_video_binned: appliedTelemetry?.isVideoBinned,
            tracking_exposure_cap: trackingExposureCapMode.rawValue,
            applied_max_exposure_s: appliedTelemetry?.appliedMaxExposureS
        )
    }

    private func previewFrame(in bounds: CGRect) -> CGRect {
        let aspect = CGSize(width: currentCaptureWidth, height: currentCaptureHeight)
        return AVMakeRect(aspectRatio: aspect, insideRect: bounds).integral
    }

    private func captureWidthForHeight(_ h: Int) -> Int {
        switch h {
        case 720: return 1280
        case 1080: return 1920
        default: return AppSettings.captureWidthFixed
        }
    }

    private func dumpAvailableFormats(for device: AVCaptureDevice) {
        captureLog.info("camera format dump begin device=\(device.localizedName, privacy: .public) uniqueID=\(device.uniqueID, privacy: .public)")
        for (index, format) in device.formats.enumerated() {
            let desc = format.formatDescription
            let dims = CMVideoFormatDescriptionGetDimensions(desc)
            let width = Int(dims.width)
            let height = Int(dims.height)
            let aspect = String(format: "%.4f", Double(width) / Double(height))
            let fpsRanges = format.videoSupportedFrameRateRanges.map { range in
                String(format: "%.0f-%.0f", range.minFrameRate, range.maxFrameRate)
            }.joined(separator: ",")
            let supports120 = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= 120 && range.maxFrameRate >= 120
            }
            let supports240 = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= 240 && range.maxFrameRate >= 240
            }
            let mediaSubType = CMFormatDescriptionGetMediaSubType(desc)
            let isBinned = format.isVideoBinned
            let fov = format.videoFieldOfView
            let fovText = String(format: "%.3f", fov)
            let subTypeText = String(format: "%08X", mediaSubType)
            captureLog.info(
                "camera format[\(index)] \(width)x\(height) aspect=\(aspect, privacy: .public) fps_ranges=[\(fpsRanges, privacy: .public)] supports120=\(supports120, privacy: .public) supports240=\(supports240, privacy: .public) fov_deg=\(fovText, privacy: .public) binned=\(isBinned, privacy: .public) subtype=\(subTypeText, privacy: .public)"
            )
        }
        captureLog.info("camera format dump end count=\(device.formats.count)")
    }

    private enum CaptureFormatError: LocalizedError {
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
        var candidates: [(index: Int, format: AVCaptureDevice.Format, maxFrameRate: Double)] = []
        for (index, format) in device.formats.enumerated() {
            let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            let w = Int(dims.width)
            let h = Int(dims.height)
            let matchesRes = (w == targetWidth && h == targetHeight) || (w == targetHeight && h == targetWidth)
            let matchingRanges = format.videoSupportedFrameRateRanges.filter { range in
                range.minFrameRate <= targetFps && range.maxFrameRate >= targetFps
            }
            if matchesRes, let bestRange = matchingRanges.max(by: { $0.maxFrameRate < $1.maxFrameRate }) {
                candidates.append((index: index, format: format, maxFrameRate: bestRange.maxFrameRate))
            }
        }
        let selectedCandidate = candidates.max { lhs, rhs in
            let lhsFov = lhs.format.videoFieldOfView
            let rhsFov = rhs.format.videoFieldOfView
            if lhsFov != rhsFov { return lhsFov < rhsFov }
            if lhs.maxFrameRate != rhs.maxFrameRate { return lhs.maxFrameRate < rhs.maxFrameRate }
            return lhs.index > rhs.index
        }
        guard let selected = selectedCandidate?.format else {
            for (i, format) in device.formats.enumerated() {
                let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                let ranges = format.videoSupportedFrameRateRanges
                    .map { "\($0.minFrameRate)-\($0.maxFrameRate)" }
                    .joined(separator: ",")
                captureLog.error("camera format[\(i)] \(dims.width)x\(dims.height) fps_ranges=[\(ranges, privacy: .public)]")
            }
            captureLog.error("camera no matching format target=\(targetWidth)x\(targetHeight)@\(targetFps)fps device=\(device.localizedName, privacy: .public) uniqueID=\(device.uniqueID, privacy: .public)")
            throw CaptureFormatError.noMatchingFormat(width: targetWidth, height: targetHeight, fps: targetFps)
        }

        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }

        device.activeFormat = selected
        if let selectedCandidate {
            let selectedDims = CMVideoFormatDescriptionGetDimensions(selected.formatDescription)
            captureLog.info(
                "camera format selected idx=\(selectedCandidate.index) \(selectedDims.width)x\(selectedDims.height) target_fps=\(targetFps) fov_deg=\(String(format: "%.3f", selected.videoFieldOfView), privacy: .public) max_fps=\(selectedCandidate.maxFrameRate, privacy: .public) binned=\(selected.isVideoBinned, privacy: .public)"
            )
        }

        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        device.activeVideoMinFrameDuration = frameDuration
        device.activeVideoMaxFrameDuration = frameDuration
        let appliedMaxExposureS = try applyExposureConfiguration(device, format: selected, targetFps: targetFps)

        let applied = device.activeVideoMinFrameDuration
        let appliedFps = applied.value > 0
            ? Double(applied.timescale) / Double(applied.value)
            : targetFps
        let dims = CMVideoFormatDescriptionGetDimensions(selected.formatDescription)
        onTelemetryUpdated(
            .init(
                widthPx: Int(dims.width),
                heightPx: Int(dims.height),
                appliedFps: appliedFps,
                formatFovDeg: Double(device.activeFormat.videoFieldOfView),
                formatIndex: selectedCandidate?.index,
                isVideoBinned: selected.isVideoBinned,
                appliedMaxExposureS: appliedMaxExposureS
            )
        )
    }

    private func applyExposureConfiguration(
        _ device: AVCaptureDevice,
        format: AVCaptureDevice.Format,
        targetFps: Double
    ) throws -> Double? {
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        let lo = format.minExposureDuration
        let hi = format.maxExposureDuration
        let requestedCap = requestedExposureCapDuration(targetFps: targetFps) ?? frameDuration
        let capped: CMTime
        if CMTimeCompare(requestedCap, lo) < 0 {
            capped = lo
        } else if CMTimeCompare(requestedCap, hi) > 0 {
            capped = hi
        } else {
            capped = requestedCap
        }
        device.activeMaxExposureDuration = capped
        device.exposureMode = .continuousAutoExposure
        let cappedExposureS = CMTimeGetSeconds(capped)
        let exposureCapText = cappedExposureS > 0
            ? String(format: "1/%.0f", 1.0 / cappedExposureS)
            : "n/a"
        captureLog.info(
            "camera exposure configured target_fps=\(targetFps) mode=\(self.trackingExposureCapMode.rawValue, privacy: .public) max_exposure=\(exposureCapText, privacy: .public) iso_range=[\(format.minISO, privacy: .public),\(format.maxISO, privacy: .public)] current_iso=\(device.iso, privacy: .public)"
        )
        return cappedExposureS.isFinite ? cappedExposureS : nil
    }

    private func requestedExposureCapDuration(targetFps: Double) -> CMTime? {
        guard abs(targetFps - trackingFps) < 0.5,
              let seconds = trackingExposureCapMode.maxExposureSeconds else {
            return nil
        }
        return CMTime(seconds: seconds, preferredTimescale: 1_000_000)
    }

    private func reconfigureActiveSession(device: AVCaptureDevice, targetFps: Double) {
        let wasRunning = session.isRunning
        if wasRunning { session.stopRunning() }
        defer { if wasRunning { session.startRunning() } }
        do {
            try configureCaptureFormat(
                device,
                targetWidth: currentCaptureWidth,
                targetHeight: currentCaptureHeight,
                targetFps: targetFps
            )
            let applied = device.activeVideoMinFrameDuration
            let appliedFps = applied.value > 0
                ? Double(applied.timescale) / Double(applied.value)
                : 0
            captureLog.info("camera fps switched target=\(targetFps) applied=\(appliedFps)")
        } catch {
            captureLog.error("camera fps switch failed target=\(targetFps) error=\(error.localizedDescription, privacy: .public)")
            DispatchQueue.main.async {
                self.onErrorBanner("FPS 切換失敗 (\(Int(targetFps))fps 不支援)")
            }
        }
    }

    private var currentCaptureDevice: AVCaptureDevice? {
        (session.inputs.compactMap { $0 as? AVCaptureDeviceInput }
            .first(where: { $0.device.hasMediaType(.video) }))?.device
    }

    private func configureAudioCapture(cameraRole: String) {
        guard chirpDetector == nil else { return }
        guard let mic = AVCaptureDevice.default(for: .audio) else { return }
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: mic)
        } catch {
            captureLog.error("camera mic input init failed error=\(error.localizedDescription, privacy: .public)")
            onStatusText("麥克風啟動失敗 · \(error.localizedDescription)")
            return
        }

        session.automaticallyConfiguresApplicationAudioSession = false
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.defaultToSpeaker, .allowBluetoothA2DP]
            )
            try audioSession.setActive(true, options: [])
            captureLog.info("camera AVAudioSession set to .measurement for flat mic response")
        } catch {
            captureLog.error("camera AVAudioSession config failed error=\(error.localizedDescription, privacy: .public)")
        }

        session.beginConfiguration()
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            captureLog.error("camera session rejected audio input cam=\(cameraRole, privacy: .public)")
            onStatusText("擷取階段拒絕麥克風")
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

        let detector = AudioChirpDetector(threshold: 0.18)
        output.setSampleBufferDelegate(detector, queue: detector.deliveryQueue)
        session.commitConfiguration()

        audioInput = input
        audioOutput = output
        chirpDetector = detector
    }
}
