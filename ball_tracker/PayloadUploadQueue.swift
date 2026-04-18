import Foundation

/// Serialises cached pitch payloads up to the server, one at a time. Owns
/// the in-memory queue of `PitchPayloadStore` JSON URLs; on success the
/// store is told to delete the JSON + companion video, on failure the
/// file is re-inserted at the head for a retry after `retryDelayS`.
///
/// All callbacks fire on the main queue so callers (the camera VC) can
/// hit UIKit without an extra hop.
final class PayloadUploadQueue {
    private let store: PitchPayloadStore
    private var uploader: ServerUploader
    private let retryDelayS: TimeInterval
    private var pendingFiles: [URL] = []
    private(set) var isUploading: Bool = false

    /// Called with a short human-readable string destined for the
    /// "Upload: …" HUD label.
    var onStatusTextChanged: ((String) -> Void)?
    /// Called with the one-line "Last: …" summary after a successful
    /// triangulation reply.
    var onLastResultChanged: ((String) -> Void)?
    /// Called when the uploading flag toggles — drives the status dot
    /// colour (orange while uploading).
    var onUploadingChanged: ((Bool) -> Void)?

    init(
        store: PitchPayloadStore,
        uploader: ServerUploader,
        retryDelayS: TimeInterval = 2.0
    ) {
        self.store = store
        self.uploader = uploader
        self.retryDelayS = retryDelayS
    }

    /// Hot-swap the uploader when the server endpoint changes.
    func updateUploader(_ uploader: ServerUploader) {
        self.uploader = uploader
    }

    /// Reload the in-memory queue from disk, replacing any previous
    /// state. Used on entering sync mode so a restart's cached payloads
    /// resume uploading.
    func reloadPending() throws {
        pendingFiles.removeAll(keepingCapacity: true)
        let files = try store.listPayloadFiles()
        pendingFiles.append(contentsOf: files)
        setUploading(false)
    }

    /// Drop the in-memory queue only — on-disk files are preserved so a
    /// later re-entry can pick them back up.
    func clearPending() {
        pendingFiles.removeAll(keepingCapacity: true)
        setUploading(false)
    }

    /// Append a freshly saved JSON URL and kick off the next upload if
    /// the worker slot is idle.
    func enqueue(_ fileURL: URL) {
        pendingFiles.append(fileURL)
        processNextIfNeeded()
    }

    /// Main-queue pump: no-op when already uploading or the queue is
    /// empty; otherwise pops the head and starts one upload round-trip.
    func processNextIfNeeded() {
        guard !isUploading else { return }
        guard !pendingFiles.isEmpty else { return }

        let fileURL = pendingFiles.removeFirst()
        setUploading(true)
        onStatusTextChanged?("Uploading cached pitch...")

        let payload: ServerUploader.PitchPayload
        do {
            payload = try store.load(fileURL)
        } catch {
            setUploading(false)
            onStatusTextChanged?("Cache read failed: \(error.localizedDescription)")
            processNextIfNeeded()
            return
        }

        let videoURL = store.videoURL(forPayload: fileURL)

        uploader.uploadPitch(payload, videoURL: videoURL) { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                var retryAfterFailure = false
                switch result {
                case .success(let response):
                    self.store.delete(fileURL)
                    self.onStatusTextChanged?("Uploaded \(payload.session_id)")
                    self.onLastResultChanged?(Self.formatResultSummary(response))
                case .failure(let error):
                    self.onStatusTextChanged?("Upload failed: \(error.localizedDescription)")
                    self.pendingFiles.insert(fileURL, at: 0)
                    retryAfterFailure = true
                }
                self.setUploading(false)
                if retryAfterFailure {
                    DispatchQueue.main.asyncAfter(deadline: .now() + self.retryDelayS) { [weak self] in
                        self?.processNextIfNeeded()
                    }
                } else {
                    self.processNextIfNeeded()
                }
            }
        }
    }

    private func setUploading(_ v: Bool) {
        guard v != isUploading else { return }
        isUploading = v
        onUploadingChanged?(v)
    }

    private static func formatResultSummary(_ r: ServerUploader.PitchUploadResponse) -> String {
        let sid = r.session_id
        if let err = r.error, !err.isEmpty {
            return "\(sid) ✗ \(err)"
        }
        if !r.paired {
            return "\(sid) 已收 (等待另一相機)"
        }
        if r.triangulated_points == 0 {
            return "\(sid) ✗ 0 pts (時間窗口未對齊?)"
        }
        let gapMm = (r.mean_residual_m ?? 0) * 1000.0
        let peakZ = r.peak_z_m ?? 0
        let dur = r.duration_s ?? 0
        return String(
            format: "%@ ✓ %d pts gap=%.0fmm peak=%.2fm dur=%.2fs",
            sid, r.triangulated_points, gapMm, peakZ, dur
        )
    }
}
