import AVFoundation
import CoreMedia
import Foundation

/// Single-worker dispatch pool around the stateless `BTBallDetector`.
///
/// The class is **named** `ConcurrentDetectionPool` for historical
/// continuity but is no longer concurrent: full-frame HSV +
/// connected-components on a 1080p sample is ~15 ms/frame, so 240 fps
/// × 15 ms = 3.6 s/wall-s — well above one P-core's 1.0 s budget. The
/// producer (`CameraViewController.dispatchDetection`) does NOT
/// throttle — every captured sample is enqueued. Effective detection
/// rate is therefore one core's throughput ≈ 1 / 0.015 ≈ 66
/// detections/s, with the remaining ~174 fps dropping at
/// `enqueue` when `inFlightCount >= maxBacklog`. The dashboard surfaces
/// `droppedFrameCount` so the operator can see this. 66 Hz of full-
/// frame distractor-immune detection beats 240 Hz of ROI-locked
/// detection that misses the ball entirely (s_97655fc6 was the wakeup
/// call).
///
/// Pre-PR: this pool wrapped `BTStatefulBallDetector` which kept an ROI
/// around the previous hit, cutting cost to ~3 ms when the ball stayed
/// close. That created two problems we removed:
///   1. Once a static distractor (e.g. a green plant leaf) became the
///      "previous hit", the ROI locked onto it and the detector could
///      go several seconds before the 10-miss reset let it see the
///      real ball. Same scene processed by server_post (no ROI) found
///      the ball within one frame.
///   2. Live and server_post diverged silently: same MOV bytes, same
///      HSV, same shape gate, but different candidate sets per frame
///      — making it impossible to tell if "live missed the ball"
///      meant detector bug, exposure problem, or ROI lock-in.
/// Stateless full-frame on every frame is exactly what server_post
/// runs, so live ↔ server_post is now byte-aligned end-to-end and the
/// operator sees the same evidence the algorithm sees.
///
/// We absorb a short backlog on the serial queue and drop new frames
/// when the queue already holds `maxBacklog` in-flight tasks.
/// AVFoundation's pixel-buffer pool ultimately bounds how much we can
/// buffer; oversize `maxBacklog` would just push drops upstream where
/// they're invisible.
final class ConcurrentDetectionPool {

    /// Public counter — frames dropped at `enqueue` because the in-flight
    /// queue was full. Visible from the dashboard / HUD so the operator
    /// can see when detection is falling behind.
    private(set) var droppedFrameCount: Int = 0

    /// Frames currently queued or in-flight on `detectionQueue`. Surfaced
    /// for the "still draining" UI status during disarm → standby.
    private(set) var inFlightCount: Int = 0

    var onFrame: ((ServerUploader.FramePayload) -> Void)?

    private let maxBacklog: Int
    private let detectionQueue: DispatchQueue
    private let stateLock = NSLock()
    private var hsvRange: ServerUploader.HSVRangePayload = .tennis
    private var shapeGate: ServerUploader.ShapeGatePayload = .default

    private var currentGeneration: Int = 0
    private var callIndex: Int = 0

    /// `maxBacklog` defaults to 8 — a tight bound that prefers visible
    /// `droppedFrameCount` increments over silently pinning AVFoundation's
    /// pixel-buffer pool. ~33 ms slack at 240 fps producer covers full-
    /// frame HSV+CC spikes without letting the queue grow into the
    /// hundreds of MB of retained pixel buffers.
    init(maxBacklog: Int = 8) {
        self.maxBacklog = max(1, maxBacklog)
        self.detectionQueue = DispatchQueue(
            label: "com.Max0228.ball-tracker.detection",
            qos: .userInitiated
        )
    }

    /// Non-blocking dispatch. Returns false when the in-flight queue is
    /// already at `maxBacklog` (the frame is dropped + counter bumped).
    func enqueue(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) -> Bool {
        stateLock.lock()
        if inFlightCount >= maxBacklog {
            droppedFrameCount += 1
            stateLock.unlock()
            return false
        }
        inFlightCount += 1
        let gen = currentGeneration
        let index = callIndex
        let hsvSnapshot = hsvRange
        let shapeSnapshot = shapeGate
        callIndex += 1
        stateLock.unlock()

        let retainedPixelBuffer = Unmanaged.passRetained(pixelBuffer)
        detectionQueue.async { [weak self] in
            let pb = retainedPixelBuffer.takeUnretainedValue()
            guard let self else {
                retainedPixelBuffer.release()
                return
            }
            defer {
                retainedPixelBuffer.release()
                self.stateLock.lock()
                self.inFlightCount -= 1
                self.stateLock.unlock()
            }

            // Stateless full-frame multi-candidate detection — every
            // frame runs the same HSV→CC→shape-gate pipeline as
            // server_post against the uploaded MOV. No ROI tracking,
            // no across-frame state.
            let cands = BTBallDetector.detectAllCandidates(
                in: pb,
                hMin: Int32(hsvSnapshot.h_min), hMax: Int32(hsvSnapshot.h_max),
                sMin: Int32(hsvSnapshot.s_min), sMax: Int32(hsvSnapshot.s_max),
                vMin: Int32(hsvSnapshot.v_min), vMax: Int32(hsvSnapshot.v_max),
                aspectMin: shapeSnapshot.aspect_min,
                fillMin: shapeSnapshot.fill_min
            )
            let maxArea = max(1, cands.map { Int($0.areaPx) }.max() ?? 1)
            let candidatesPayload = cands.map { d in
                ServerUploader.BlobCandidate(
                    px: Double(d.px),
                    py: Double(d.py),
                    area: Int(d.areaPx),
                    area_score: Double(d.areaPx) / Double(maxArea),
                    aspect: Double(d.aspect),
                    fill: Double(d.fill)
                )
            }
            let frame = ServerUploader.FramePayload(
                frame_index: index,
                timestamp_s: timestampS,
                candidates: candidatesPayload
            )

            self.stateLock.lock()
            let stillCurrent = (gen == self.currentGeneration)
            self.stateLock.unlock()

            if stillCurrent {
                self.onFrame?(frame)
            }
        }
        return true
    }

    /// Run `completion` once every currently-queued frame has finished
    /// processing. Internally just appends a no-op task to the serial
    /// detection queue, so it can't run before any task ahead of it.
    ///
    /// **Caller responsibility — stop producing enqueues before calling
    /// this.** Any `enqueue` call landing after `waitForDrain` lands
    /// *behind* the drain marker on the serial queue, so the completion
    /// keeps slipping by however long the new work takes. In the live
    /// camera flow this is fine because capture has stopped (ClipRecorder
    /// finish callback) before drain is requested; if you call it during
    /// active capture you'll wait for a moving target.
    ///
    /// **Must NOT be called from the detection queue or from main when
    /// main is blocking on detection.** The completion fires on `queue`,
    /// which lets the camera workflow re-enter main for `cycle_end`
    /// dispatch + standby transition without re-blocking the detection
    /// thread.
    func waitForDrain(on queue: DispatchQueue, completion: @escaping () -> Void) {
        detectionQueue.async {
            queue.async { completion() }
        }
    }

    /// Bump the generation so any in-flight worker that finishes after
    /// this call drops its `onFrame` callback. Resets the per-frame index
    /// for the next session. Stateless detector — no ROI cache to flush.
    func invalidateGeneration() {
        stateLock.lock()
        currentGeneration &+= 1
        callIndex = 0
        stateLock.unlock()
    }

    func reset() {
        stateLock.lock()
        droppedFrameCount = 0
        stateLock.unlock()
    }

    func updateHSVRange(_ hsvRange: ServerUploader.HSVRangePayload) {
        stateLock.lock()
        self.hsvRange = hsvRange
        stateLock.unlock()
    }

    func updateShapeGate(_ shapeGate: ServerUploader.ShapeGatePayload) {
        stateLock.lock()
        self.shapeGate = shapeGate
        stateLock.unlock()
    }
}
