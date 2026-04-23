import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "analysis")

final class AnalysisUploadQueue {
    private let store: AnalysisJobStore
    private var uploader: ServerUploader
    private let analyzer = LocalVideoAnalyzer()
    private let stateLock = NSLock()
    private var hsvRange: ServerUploader.HSVRangePayload = .tennis
    private let workerQueue = DispatchQueue(label: "analysis.upload.queue", qos: .utility)
    private var pendingFiles: [URL] = []
    private(set) var isProcessing: Bool = false

    var onStatusTextChanged: ((String) -> Void)?
    var onLastResultChanged: ((String) -> Void)?

    init(store: AnalysisJobStore, uploader: ServerUploader) {
        self.store = store
        self.uploader = uploader
    }

    func updateUploader(_ uploader: ServerUploader) {
        self.uploader = uploader
    }

    func updateHSVRange(_ hsvRange: ServerUploader.HSVRangePayload) {
        stateLock.lock()
        self.hsvRange = hsvRange
        stateLock.unlock()
    }

    func reloadPending() throws {
        pendingFiles = try store.listJobFiles()
        isProcessing = false
        log.info("analysis queue reloaded pending=\(self.pendingFiles.count)")
    }

    func enqueue(_ fileURL: URL) {
        pendingFiles.append(fileURL)
        processNextIfNeeded()
    }

    func processNextIfNeeded() {
        guard !isProcessing, !pendingFiles.isEmpty else { return }
        let fileURL = pendingFiles.removeFirst()
        isProcessing = true
        onStatusTextChanged?("本機錄後分析中…")
        workerQueue.async { [weak self] in
            self?.runJob(fileURL)
        }
    }

    private func runJob(_ fileURL: URL) {
        let job: AnalysisJobStore.Job
        let videoURL: URL
        do {
            job = try store.load(fileURL)
            guard let foundVideoURL = store.videoURL(forJob: fileURL) else {
                throw URLError(.fileDoesNotExist)
            }
            videoURL = foundVideoURL
        } catch {
            finishFailure(fileURL, message: "analysis cache read failed", retry: false)
            return
        }

        let frames: [ServerUploader.FramePayload]
        do {
            stateLock.lock()
            let hsvRange = self.hsvRange
            stateLock.unlock()
            frames = try analyzer.analyze(
                videoURL: videoURL,
                videoStartPtsS: job.pitch.video_start_pts_s,
                hsvRange: hsvRange
            )
        } catch {
            log.error("analysis decode failed session=\(job.pitch.session_id, privacy: .public) cam=\(job.pitch.camera_id, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            finishFailure(fileURL, message: "本機錄後分析失敗", retry: true)
            return
        }

        switch job.uploadMode {
        case .onDevicePrimary:
            let payload = job.pitch.withFrames(frames)
            uploader.uploadPitchTyped(payload, videoURL: nil) { [weak self] result in
                switch result {
                case .success(let response):
                    self?.store.delete(fileURL)
                    self?.finishSuccess(
                        "錄後分析上傳完成 \(response.session_id)",
                        last: Self.formatPitchResultSummary(response)
                    )
                case .failure(let error):
                    self?.logUploadError(error, sessionId: payload.session_id, cameraId: payload.camera_id, sidecar: false)
                    self?.finishFailure(fileURL, message: "錄後分析上傳失敗", retry: true)
                }
            }
        case .dualSidecar:
            let analysis = ServerUploader.PitchAnalysisPayload(
                camera_id: job.pitch.camera_id,
                session_id: job.pitch.session_id,
                frames_on_device: frames,
                capture_telemetry: job.pitch.capture_telemetry
            )
            uploader.uploadPitchAnalysis(analysis) { [weak self] result in
                switch result {
                case .success(let response):
                    self?.store.delete(fileURL)
                    self?.finishSuccess(
                        "錄後分析補傳完成 \(response.session_id)",
                        last: "iOS analysis \(response.frames_on_device)f / tri \(response.triangulated_on_device)"
                    )
                case .failure(let error):
                    self?.logUploadError(error, sessionId: analysis.session_id, cameraId: analysis.camera_id, sidecar: true)
                    self?.finishFailure(fileURL, message: "錄後 analysis 補傳失敗", retry: true)
                }
            }
        }
    }

    private func logUploadError(
        _ error: ServerUploader.UploadError,
        sessionId: String,
        cameraId: String,
        sidecar: Bool
    ) {
        let lane = sidecar ? "sidecar" : "primary"
        switch error {
        case .network(let urlError):
            log.error("analysis upload failed lane=\(lane, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraId, privacy: .public) network=\(urlError.code.rawValue)")
        case .client(let statusCode, _):
            log.error("analysis upload failed lane=\(lane, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraId, privacy: .public) client=\(statusCode)")
        case .server(let statusCode, _):
            log.error("analysis upload failed lane=\(lane, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraId, privacy: .public) server=\(statusCode)")
        case .decoding(let error):
            log.error("analysis upload failed lane=\(lane, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraId, privacy: .public) decode=\(error.localizedDescription, privacy: .public)")
        case .invalidResponse:
            log.error("analysis upload failed lane=\(lane, privacy: .public) session=\(sessionId, privacy: .public) cam=\(cameraId, privacy: .public) invalid_response")
        }
    }

    private func finishSuccess(_ status: String, last: String) {
        DispatchQueue.main.async { [weak self] in
            self?.onStatusTextChanged?(status)
            self?.onLastResultChanged?(last)
            self?.isProcessing = false
            self?.processNextIfNeeded()
        }
    }

    private func finishFailure(_ fileURL: URL, message: String, retry: Bool) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.onStatusTextChanged?(message)
            self.isProcessing = false
            if retry {
                self.pendingFiles.insert(fileURL, at: 0)
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                    self?.processNextIfNeeded()
                }
            } else {
                self.store.delete(fileURL)
                self.processNextIfNeeded()
            }
        }
    }

    private static func formatPitchResultSummary(_ r: ServerUploader.PitchUploadResponse) -> String {
        if let error = r.error, !error.isEmpty {
            return "Session \(r.session_id) · \(error)"
        }
        return "Session \(r.session_id) · paired=\(r.paired ? "Y" : "N") · 3D=\(r.triangulated_points)"
    }
}
