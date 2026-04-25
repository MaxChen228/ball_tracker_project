// Live-preview push pipeline (Phase 4a).
//
// Called from `CameraViewController.captureOutput` whenever the server's
// live settings message says `preview_requested == true` for THIS camera AND the
// state machine is in `.standby` (i.e. not recording and not in 時間校正).
// Each call downsamples the incoming CVPixelBuffer to 480p,
// JPEG-encodes at quality 0.5, and POSTs to
// `/camera/{cameraId}/preview_frame` as `image/jpeg`.
//
// Throttling: ~10 fps (min 100 ms between sends). Back-pressure: if a
// previous POST is still in flight, the current frame is dropped — we never
// queue. Preview is transient; stale frames are worthless.
//
// Bandwidth budget on a LAN: 480p q50 JPEG ≈ 30-60 KB/frame × 10 fps ≈
// 300-600 KB/s per camera. Two cameras simultaneously = ~1.2 MB/s. Well
// within what the iPhone Wi-Fi stack handles comfortably, but we still
// only push while the dashboard is watching (flag-gated upstream).

import Foundation
import CoreImage
import CoreVideo
import UIKit
import os

final class PreviewUploader {
    private let log = Logger(subsystem: "com.ball_tracker", category: "preview.upload")

    private let cameraId: String
    private var uploader: ServerUploader
    private let ciContext: CIContext

    // `throttle.queue` serialises state mutation (lastSentAt, inFlight).
    // Encode + POST happen off of this queue via dispatch_async.
    private let throttleQueue = DispatchQueue(label: "preview.upload.throttle")

    // Ignored if the delta since the last send is below this. 100 ms = 10 fps.
    private let minIntervalS: TimeInterval = 0.100

    // When true, drop incoming frames until the outstanding POST completes.
    private var inFlight: Bool = false
    private var lastSentAt: TimeInterval = 0

    init(uploader: ServerUploader, cameraId: String) {
        self.uploader = uploader
        self.cameraId = cameraId
        // `.useSoftwareRenderer=false` keeps the GPU path active so the
        // resize + JPEG-encode doesn't stall on the CPU side.
        self.ciContext = CIContext(options: [.useSoftwareRenderer: false])
    }

    /// Hot-swap the uploader's server config (e.g. Settings changed IP/port
    /// mid-session). Cheap — `ServerUploader` is a value type + closure.
    func updateUploader(_ new: ServerUploader) {
        throttleQueue.async { self.uploader = new }
    }

    /// Clear in-flight/sent state so the next enqueue fires immediately.
    /// Called when the server flag flips from true→false, so a stale POST
    /// doesn't land after the dashboard already toggled off.
    func reset() {
        throttleQueue.async {
            self.inFlight = false
            self.lastSentAt = 0
        }
    }

    /// Entry point from the capture queue. Non-blocking — work is hopped
    /// onto `throttleQueue` and then encode/HTTP happens off it too.
    func pushFrame(_ pixelBuffer: CVPixelBuffer) {
        let now = CFAbsoluteTimeGetCurrent()
        // Retain the buffer for the hop; CIImage(cvPixelBuffer:) will retain
        // internally too but we're conservative here — cross-queue capture
        // could outlive the caller's sample.
        throttleQueue.async { [weak self] in
            guard let self else { return }
            if self.inFlight { return }
            if now - self.lastSentAt < self.minIntervalS { return }
            guard let jpeg = self.encode(pixelBuffer) else {
                self.log.debug("preview encode failed — skipping frame")
                return
            }
            self.inFlight = true
            self.lastSentAt = now
            let endpoint = "/camera/\(self.cameraId)/preview_frame"
            self.uploader.postRawJPEG(path: endpoint, jpeg: jpeg) { [weak self] result in
                self?.throttleQueue.async {
                    self?.inFlight = false
                }
                if case .failure(let err) = result {
                    self?.log.debug("preview POST failed: \(err.localizedDescription, privacy: .public)")
                }
            }
        }
    }

    /// Downsample → JPEG. Target long edge 854 px (480p 16:9) at q=0.5.
    private func encode(_ pixelBuffer: CVPixelBuffer) -> Data? {
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        let w = ci.extent.width
        let h = ci.extent.height
        let targetLong: CGFloat = 854
        let scale = min(1.0, targetLong / max(w, h))
        let scaled = (scale < 1.0)
            ? ci.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            : ci
        // JPEG representation. sRGB colorspace so the browser doesn't have
        // to guess. Quality 0.5 — preview is advisory, not forensic.
        guard let cs = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        return ciContext.jpegRepresentation(
            of: scaled,
            colorSpace: cs,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.5]
        )
    }
}
