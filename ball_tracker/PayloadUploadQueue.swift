import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "network")

/// Retry / cooldown knobs for `PayloadUploadQueue`. Factored out so unit
/// tests can inject `.fast` and drive the full retry → cooldown → drop
/// path without burning 60 s of wall-clock time per case.
struct RetryPolicy {
    /// Flat delay for transient `.network`, `.decoding`,
    /// `.invalidResponse` categories — no escalation.
    let baseRetryDelayS: TimeInterval
    /// Exponential 5xx ladder in seconds. `serverBackoffTier` walks
    /// left→right on each consecutive 5xx; the last entry caps any
    /// higher tier. A 2xx reply resets the tier to 0.
    let serverErrorLadder: [TimeInterval]
    /// Gap between a 4xx reply and its cooldown-retry attempt.
    let clientErrorCooldownS: TimeInterval
    /// 4xx retries allowed per payload before the queue drops it
    /// (deletes JSON + clip, fires `onPayloadDropped`).
    let clientErrorRetryBudget: Int

    /// Production defaults — tuned for real-world flaky LAN + occasional
    /// 4xx validation drift. Behaviour identical to the pre-refactor
    /// hard-coded constants.
    static let production = RetryPolicy(
        baseRetryDelayS: 2.0,
        serverErrorLadder: [4, 8, 16, 32, 60],
        clientErrorCooldownS: 60.0,
        clientErrorRetryBudget: 1
    )

    /// Millisecond-scale preset for XCTest. Same policy shape as
    /// `.production` (5-entry ladder, cap at the last, one 4xx retry)
    /// but compressed so the whole drop path fits inside the default
    /// `wait(for:timeout:)` budget.
    static let fast = RetryPolicy(
        baseRetryDelayS: 0.02,
        serverErrorLadder: [0.02, 0.04, 0.08, 0.16, 0.32],
        clientErrorCooldownS: 0.05,
        clientErrorRetryBudget: 1
    )
}

/// Serialises cached pitch payloads up to the server, one at a time. Owns
/// the in-memory queue of `PitchPayloadStore` JSON URLs; on success the
/// store is told to delete the JSON + companion video, on failure the
/// file is re-inserted at the head for a retry after a delay derived from
/// the failure category (see `delay(for:)`).
///
/// Retry policy (see `RetryPolicy` + `delay(for:)` + `shouldDrop(for:fileURL:)`):
/// - `.network` / `.decoding` / `.invalidResponse` — flat `baseRetryDelayS`.
/// - `.server` (5xx) — exponential backoff along `serverErrorLadder`,
///   counted by `serverBackoffTier`. A successful upload resets the tier.
/// - `.client` (4xx) — `clientErrorRetryBudget` cooldown retries, then
///   the payload is dropped (JSON + clip deleted, `onPayloadDropped`
///   fires). Retrying the same bytes can't recover from a
///   malformed/rejected request.
///
/// All callbacks fire on the main queue so callers (the camera VC) can
/// hit UIKit without an extra hop.
final class PayloadUploadQueue {
    private let store: PitchPayloadStore
    private var uploader: ServerUploader
    private let policy: RetryPolicy
    private var pendingFiles: [URL] = []
    private(set) var isUploading: Bool = false

    /// Consecutive-server-error counter. 0 = first entry in the ladder.
    /// Each 5xx response bumps the tier by 1 up to
    /// `policy.serverErrorLadder.count - 1`; a successful upload resets
    /// to 0.
    private var serverBackoffTier: Int = 0

    /// Per-payload-file count of 4xx retries already spent. A payload is
    /// dropped when its count reaches `policy.clientErrorRetryBudget`.
    /// In-memory only — a fresh app launch gets a fresh budget per file.
    private var clientErrorRetryCount: [URL: Int] = [:]

    /// Called with a short human-readable string destined for the
    /// "Upload: …" HUD label.
    var onStatusTextChanged: ((String) -> Void)?
    /// Called with the one-line "Last: …" summary after a successful
    /// triangulation reply.
    var onLastResultChanged: ((String) -> Void)?
    /// Called when the uploading flag toggles — drives the status dot
    /// colour (orange while uploading).
    var onUploadingChanged: ((Bool) -> Void)?
    /// Called exactly once per payload that exceeds the 4xx retry budget,
    /// right after the JSON + companion clip have been deleted from disk.
    var onPayloadDropped: ((URL, ServerUploader.UploadError) -> Void)?

    init(
        store: PitchPayloadStore,
        uploader: ServerUploader,
        policy: RetryPolicy = .production
    ) {
        self.store = store
        self.uploader = uploader
        self.policy = policy
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
        log.info("queue reloaded pending=\(self.pendingFiles.count)")
    }

    /// Drop the in-memory queue only — on-disk files are preserved so a
    /// later re-entry can pick them back up. Also clears per-file 4xx
    /// retry counters; across long sessions the dict would otherwise grow
    /// unbounded (entries are only removed on success or drop).
    func clearPending() {
        pendingFiles.removeAll(keepingCapacity: true)
        clientErrorRetryCount.removeAll(keepingCapacity: false)
        serverBackoffTier = 0
        setUploading(false)
    }

    /// Append a freshly saved JSON URL and kick off the next upload if
    /// the worker slot is idle.
    func enqueue(_ fileURL: URL) {
        pendingFiles.append(fileURL)
        log.info("queue enqueue url=\(fileURL.lastPathComponent, privacy: .public) depth=\(self.pendingFiles.count)")
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
        log.debug("queue dequeue url=\(fileURL.lastPathComponent, privacy: .public) depth=\(self.pendingFiles.count)")

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

        uploader.uploadPitchTyped(payload, videoURL: videoURL) { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                switch result {
                case .success(let response):
                    self.store.delete(fileURL)
                    self.clientErrorRetryCount.removeValue(forKey: fileURL)
                    self.serverBackoffTier = 0
                    log.info("queue delete-on-success url=\(fileURL.lastPathComponent, privacy: .public) session=\(response.session_id, privacy: .public)")
                    self.onStatusTextChanged?("Uploaded \(payload.session_id)")
                    self.onLastResultChanged?(Self.formatResultSummary(response))
                    self.setUploading(false)
                    self.processNextIfNeeded()

                case .failure(let error):
                    // 4xx with budget exhausted → drop the poisoned payload.
                    if self.shouldDrop(for: error, fileURL: fileURL) {
                        self.store.delete(fileURL)
                        self.clientErrorRetryCount.removeValue(forKey: fileURL)
                        log.warning("queue drop url=\(fileURL.lastPathComponent, privacy: .public) reason=\(Self.describe(error), privacy: .public)")
                        self.onStatusTextChanged?(
                            "Dropped \(payload.session_id): \(Self.describe(error))"
                        )
                        self.onPayloadDropped?(fileURL, error)
                        self.setUploading(false)
                        self.processNextIfNeeded()
                        return
                    }

                    // Otherwise re-queue and schedule a category-appropriate retry.
                    self.bookkeepRetry(for: error, fileURL: fileURL)
                    let delay = self.delay(for: error)
                    let delayLog = String(format: "%.2f", delay)
                    log.warning("queue retry url=\(fileURL.lastPathComponent, privacy: .public) reason=\(Self.describe(error), privacy: .public) delay_s=\(delayLog, privacy: .public)")
                    self.onStatusTextChanged?(
                        "Upload failed (\(Self.describe(error))); retry in \(Int(delay.rounded()))s"
                    )
                    self.pendingFiles.insert(fileURL, at: 0)
                    self.setUploading(false)
                    DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                        self?.processNextIfNeeded()
                    }
                }
            }
        }
    }

    /// Retry delay for `error`. Called after `shouldDrop` has already
    /// vetoed terminal 4xx cases, so the 4xx branch here is only ever the
    /// cooldown-retry arm.
    private func delay(for error: ServerUploader.UploadError) -> TimeInterval {
        switch error {
        case .network, .decoding, .invalidResponse:
            return policy.baseRetryDelayS
        case .server:
            let idx = min(serverBackoffTier, policy.serverErrorLadder.count - 1)
            return policy.serverErrorLadder[idx]
        case .client:
            return policy.clientErrorCooldownS
        }
    }

    /// Update bookkeeping before scheduling the retry. 5xx bumps the
    /// backoff tier (capped); 4xx bumps this payload's retry count;
    /// other categories leave counters alone.
    private func bookkeepRetry(for error: ServerUploader.UploadError, fileURL: URL) {
        switch error {
        case .server:
            let maxTier = policy.serverErrorLadder.count - 1
            if serverBackoffTier < maxTier {
                serverBackoffTier += 1
            }
        case .client:
            clientErrorRetryCount[fileURL, default: 0] += 1
        case .network, .decoding, .invalidResponse:
            break
        }
    }

    /// True when `error` is a 4xx and this payload has already spent its
    /// budget — i.e. we've already retried once and the server still
    /// hates it, so further retries are pointless.
    private func shouldDrop(for error: ServerUploader.UploadError, fileURL: URL) -> Bool {
        guard case .client = error else { return false }
        let spent = clientErrorRetryCount[fileURL, default: 0]
        return spent >= policy.clientErrorRetryBudget
    }

    private func setUploading(_ v: Bool) {
        guard v != isUploading else { return }
        isUploading = v
        onUploadingChanged?(v)
    }

    /// One-word category for the status HUD; keeps the string short so the
    /// label doesn't wrap. Full error detail stays available via
    /// `onPayloadDropped`'s typed `UploadError` argument.
    private static func describe(_ error: ServerUploader.UploadError) -> String {
        switch error {
        case .network: return "network"
        case .client(let code, _): return "HTTP \(code)"
        case .server(let code, _): return "HTTP \(code)"
        case .decoding: return "decode"
        case .invalidResponse: return "no response"
        }
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
