import AVFoundation
import CoreMedia
import Foundation

final class ConcurrentDetectionPool {
    let maxConcurrency: Int

    var onFrame: ((ServerUploader.FramePayload) -> Void)?

    private(set) var droppedFrameCount: Int = 0
    private(set) var stridedSkipCount: Int = 0

    private let detectionQueue: DispatchQueue
    private let detectionSemaphore: DispatchSemaphore
    private let stateLock = NSLock()
    private var hsvRange: ServerUploader.HSVRangePayload = .tennis
    private var shapeGate: ServerUploader.ShapeGatePayload = .default

    private var currentGeneration: Int = 0
    private var callIndex: Int = 0
    /// Monotonic counter for stride decisions, independent of `callIndex`
    /// (`callIndex` only advances for dispatched frames so it can't be used
    /// as the stride cursor — it would skip nothing).
    private var strideCursor: Int = 0
    private var frameStride: Int = 1

    init(maxConcurrency: Int = 3, frameStride: Int = 1) {
        self.maxConcurrency = maxConcurrency
        self.frameStride = max(1, frameStride)
        self.detectionQueue = DispatchQueue(
            label: "com.Max0228.ball-tracker.detection",
            qos: .userInteractive,
            attributes: .concurrent
        )
        self.detectionSemaphore = DispatchSemaphore(value: maxConcurrency)
    }

    /// Configure 1-of-N frame stride. `stride=1` sends every frame;
    /// `stride=4` sends 1 of every 4 (60 Hz out of 240 fps capture).
    /// Values < 1 are clamped to 1. Safe to call from any thread.
    func setFrameStride(_ stride: Int) {
        stateLock.lock()
        frameStride = max(1, stride)
        // Reset cursor so the next frame is always sent under the new stride
        // (avoids "the stride change lands mid-window and skips another 3").
        strideCursor = 0
        stateLock.unlock()
    }

    /// Non-blocking dispatch. Returns false if dropped (pool saturated) or
    /// skipped by the stride throttle.
    func enqueue(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) -> Bool {
        // Stride check BEFORE semaphore: no point reserving a worker slot
        // for a frame we're going to skip.
        stateLock.lock()
        let cursor = strideCursor
        strideCursor &+= 1
        let stride = frameStride
        stateLock.unlock()
        if stride > 1 && (cursor % stride) != 0 {
            stateLock.lock()
            stridedSkipCount += 1
            stateLock.unlock()
            return false
        }

        guard detectionSemaphore.wait(timeout: .now()) == .success else {
            stateLock.lock()
            droppedFrameCount += 1
            stateLock.unlock()
            return false
        }

        stateLock.lock()
        let gen = currentGeneration
        let index = callIndex
        let hsvRange = self.hsvRange
        let shapeGate = self.shapeGate
        callIndex += 1
        stateLock.unlock()

        let retainedPixelBuffer = Unmanaged.passRetained(pixelBuffer)
        detectionQueue.async(execute: { [weak self] in
            let pb = retainedPixelBuffer.takeUnretainedValue()
            guard let self else {
                retainedPixelBuffer.release()
                return
            }
            defer {
                retainedPixelBuffer.release()
                self.detectionSemaphore.signal()
            }

            let detection = BTBallDetector.detect(
                in: pb,
                hMin: Int32(hsvRange.h_min),
                hMax: Int32(hsvRange.h_max),
                sMin: Int32(hsvRange.s_min),
                sMax: Int32(hsvRange.s_max),
                vMin: Int32(hsvRange.v_min),
                vMax: Int32(hsvRange.v_max),
                aspectMin: shapeGate.aspect_min,
                fillMin: shapeGate.fill_min
            )
            let frame = ServerUploader.FramePayload(
                frame_index: index,
                timestamp_s: timestampS,
                px: detection.map { Double($0.px) },
                py: detection.map { Double($0.py) },
                ball_detected: detection != nil
            )

            self.stateLock.lock()
            let stillCurrent = (gen == self.currentGeneration)
            self.stateLock.unlock()

            if stillCurrent {
                self.onFrame?(frame)
            }
        })
        return true
    }

    func invalidateGeneration() {
        stateLock.lock()
        currentGeneration &+= 1
        callIndex = 0
        strideCursor = 0
        stateLock.unlock()
    }

    func reset() {
        stateLock.lock()
        droppedFrameCount = 0
        stridedSkipCount = 0
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
