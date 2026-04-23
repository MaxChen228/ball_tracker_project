import AVFoundation
import CoreMedia
import Foundation

final class ConcurrentDetectionPool {
    let maxConcurrency: Int

    var onFrame: ((ServerUploader.FramePayload) -> Void)?

    private(set) var droppedFrameCount: Int = 0

    private let detectionQueue: DispatchQueue
    private let detectionSemaphore: DispatchSemaphore
    private let stateLock = NSLock()
    private var hsvRange: ServerUploader.HSVRangePayload = .tennis

    private var currentGeneration: Int = 0
    private var callIndex: Int = 0

    init(maxConcurrency: Int = 3) {
        self.maxConcurrency = maxConcurrency
        self.detectionQueue = DispatchQueue(
            label: "com.Max0228.ball-tracker.detection",
            qos: .userInteractive,
            attributes: .concurrent
        )
        self.detectionSemaphore = DispatchSemaphore(value: maxConcurrency)
    }

    /// Non-blocking dispatch. Returns false if dropped (pool saturated).
    func enqueue(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) -> Bool {
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
                vMax: Int32(hsvRange.v_max)
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
}
