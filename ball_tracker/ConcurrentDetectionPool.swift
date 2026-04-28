import AVFoundation
import CoreMedia
import Foundation

/// Single-worker dispatch pool around a `BallDetectionEngine`.
///
/// The class is **named** `ConcurrentDetectionPool` for historical
/// continuity but is no longer concurrent: HSV + connected-components on
/// a ROI crop runs at ~3 ms/frame, so 240 fps × 3 ms = 720 ms/wall-s of
/// CPU work — well under one P-core. A single serial worker keeps frame
/// order monotonic, which is what stateful engines (e.g. the HSV ROI
/// tracker in `HSVDetectionEngine`) need to keep their per-frame state
/// coherent (each frame's anchor is the previous frame's hit). Multiple
/// workers would either race the non-thread-safe engine or each maintain
/// a divergent history.
///
/// The pool itself is engine-agnostic for detection but HSV-aware for
/// settings application — HSV range + shape gate are runtime-tunable
/// from the dashboard and are inherently engine-specific. Routed via a
/// downcast to `HSVDetectionEngine`; future engines (ML) will expose
/// their own settings paths through analogous concrete-typed methods.
///
/// On ROI miss the per-frame cost spikes to ~15 ms (full-frame fallback),
/// at which point the producer can outpace the consumer. We absorb a
/// short backlog on the serial queue and drop new frames when the queue
/// already holds `maxBacklog` in-flight tasks. AVFoundation's pixel-
/// buffer pool ultimately bounds how much we can buffer; oversize
/// `maxBacklog` would just push drops upstream where they're invisible.
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
    /// The detection engine is constructed here on the caller's thread
    /// (the camera VC's main thread) but every subsequent access —
    /// `applyConfig`, `detect`, `resetTracking` — is dispatched onto
    /// `detectionQueue` (serial), so post-construction it is queue-
    /// confined. Don't reach into this engine from any other thread.
    /// `HSVDetectionEngine` (today's only impl) wraps the non-thread-
    /// safe `BTStatefulBallDetector`; this confinement is what keeps
    /// the inner C++ state coherent.
    private let engine: BallDetectionEngine
    private var hsvRange: ServerUploader.HSVRangePayload = .tennis
    private var shapeGate: ServerUploader.ShapeGatePayload = .default

    private var currentGeneration: Int = 0
    private var callIndex: Int = 0

    /// `maxBacklog` defaults to 8 — a tight bound that prefers visible
    /// `droppedFrameCount` increments over silently pinning AVFoundation's
    /// pixel-buffer pool. ~33 ms slack at 240 fps producer, which covers
    /// transient ROI-miss spikes without letting the queue grow into the
    /// hundreds of MB of retained pixel buffers.
    init(
        engine: BallDetectionEngine = HSVDetectionEngine(),
        maxBacklog: Int = 8
    ) {
        self.engine = engine
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

            // Apply HSV-flavoured runtime knobs inside the queue so the
            // (non-thread-safe) inner detector is only ever touched from
            // this serial worker. Non-HSV engines silently no-op the
            // cast — they expose their own settings paths.
            if let hsvEngine = self.engine as? HSVDetectionEngine {
                hsvEngine.applyConfig(hsv: hsvSnapshot, shape: shapeSnapshot)
            }

            // Engine.detect on a stateful HSV impl uses its ROI hint to
            // short-circuit the ~15 ms full-frame pass to ~3 ms when the
            // ball stays close to its prior position. The pool doesn't
            // know or care — that's the engine's contract.
            let cands = self.engine.detect(in: pb)
            let maxArea = max(1, cands.map { Int($0.areaPx) }.max() ?? 1)
            let candidatesPayload = cands.map { d in
                ServerUploader.BlobCandidate(
                    px: Double(d.px),
                    py: Double(d.py),
                    area: Int(d.areaPx),
                    area_score: Double(d.areaPx) / Double(maxArea)
                )
            }
            let frame = ServerUploader.FramePayload(
                frame_index: index,
                timestamp_s: timestampS,
                candidates: candidatesPayload,
                engine: self.engine.name
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
    /// for the next session and clears the detector's ROI tracking so
    /// the new arm window starts unbiased.
    func invalidateGeneration() {
        stateLock.lock()
        currentGeneration &+= 1
        callIndex = 0
        stateLock.unlock()
        // Engine tracking state is per-arm-window. Reset on the queue so
        // the call lands AFTER any in-flight worker still using the old
        // tracking state, before the first frame of the next generation.
        detectionQueue.async { [engine = self.engine] in
            engine.resetTracking()
        }
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
