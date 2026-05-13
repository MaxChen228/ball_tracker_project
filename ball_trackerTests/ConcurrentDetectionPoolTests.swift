import XCTest
import CoreVideo
@testable import ball_tracker

/// Tests for the single-worker `ConcurrentDetectionPool`.
/// Despite the name, a serial queue feeds a single stateless
/// `BTBallDetector` call per frame, with `maxBacklog` as the only
/// backpressure knob. Full-frame HSV+CC at 240 Hz doesn't fit on one
/// P-core, so we drop on overflow rather than running multiple workers.
final class ConcurrentDetectionPoolTests: XCTestCase {

    /// Push minimal settings so `enqueue` doesn't refuse-until-settings.
    /// Values are arbitrary — the test exercises the dispatch path, not the
    /// detector. Numbers mirror the now-retired `HSVRangePayload.tennis` /
    /// `ShapeGatePayload.default` statics (yellow-green tennis preset,
    /// aspect ≥ 0.70 / fill ≥ 0.55) so log diffs remain familiar.
    private func armSettings(on pool: ConcurrentDetectionPool) {
        pool.updateHSVRange(ServerUploader.HSVRangePayload(
            h_min: 25, h_max: 55,
            s_min: 90, s_max: 255,
            v_min: 90, v_max: 255
        ))
        pool.updateShapeGate(ServerUploader.ShapeGatePayload(
            aspect_min: 0.70, fill_min: 0.55
        ))
    }

    private func createDummyPixelBuffer() -> CVPixelBuffer {
        var pixelBuffer: CVPixelBuffer?
        let attrs = [
            kCVPixelBufferCGImageCompatibilityKey: kCFBooleanTrue,
            kCVPixelBufferCGBitmapContextCompatibilityKey: kCFBooleanTrue
        ] as CFDictionary
        CVPixelBufferCreate(kCFAllocatorDefault, 64, 64, kCVPixelFormatType_32BGRA, attrs, &pixelBuffer)
        return pixelBuffer!
    }

    func testDispatchFiresAllAcceptedFrames() {
        let pool = ConcurrentDetectionPool(maxBacklog: 32)
        armSettings(on: pool)
        let exp = expectation(description: "Fires all frames")
        exp.expectedFulfillmentCount = 5

        pool.onFrame = { _ in
            exp.fulfill()
        }

        for i in 0..<5 {
            let accepted = pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: Double(i))
            XCTAssertTrue(accepted)
        }

        waitForExpectations(timeout: 2.0)
    }

    func testBacklogOverflowDropsExcess() {
        // Tight backlog + many synchronous enqueues: the serial queue
        // can't keep up while the loop is running, so we should bottom
        // out at `maxBacklog` accepted before drops kick in.
        let pool = ConcurrentDetectionPool(maxBacklog: 1)
        armSettings(on: pool)

        var acceptedCount = 0
        for _ in 0..<100 {
            if pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0) {
                acceptedCount += 1
            }
        }

        XCTAssertLessThan(acceptedCount, 100, "Should have dropped some frames")
        XCTAssertGreaterThan(pool.droppedFrameCount, 0, "Telemetry should record dropped frames")
    }

    func testInvalidateGenerationSilencesInFlightWorkers() {
        let pool = ConcurrentDetectionPool(maxBacklog: 32)
        armSettings(on: pool)
        var receivedCallbackCount = 0
        pool.onFrame = { _ in receivedCallbackCount += 1 }

        _ = pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0.1)
        pool.invalidateGeneration()

        let exp = expectation(description: "Wait to ensure callback does not fire")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            exp.fulfill()
        }
        waitForExpectations(timeout: 1.0)

        XCTAssertEqual(receivedCallbackCount, 0, "Invalidated generation should silence workers")
    }

    func testFrameIndexIsMonotonic() {
        let pool = ConcurrentDetectionPool(maxBacklog: 64)
        armSettings(on: pool)
        var indices: [Int] = []
        let lock = NSLock()

        let exp = expectation(description: "Receive all frames")
        let total = 20
        exp.expectedFulfillmentCount = total

        pool.onFrame = { frame in
            lock.lock()
            indices.append(frame.frame_index)
            lock.unlock()
            exp.fulfill()
        }

        for i in 0..<total {
            while !pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: Double(i)) {
                Thread.sleep(forTimeInterval: 0.01)
            }
        }

        waitForExpectations(timeout: 5.0)

        // Serial worker → frames arrive at onFrame in dispatch order.
        XCTAssertEqual(indices, Array(0..<total),
                       "Serial worker must deliver frames in monotonic order")
    }

    func testWaitForDrainCompletesAfterAllQueuedFramesProcessed() {
        let pool = ConcurrentDetectionPool(maxBacklog: 64)
        armSettings(on: pool)
        let firedCount = NSCountedSet()
        pool.onFrame = { _ in firedCount.add("frame") }

        let total = 8
        for i in 0..<total {
            XCTAssertTrue(pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: Double(i)))
        }

        let exp = expectation(description: "waitForDrain fires after backlog clears")
        let drainQueue = DispatchQueue(label: "test.drain.callback")
        pool.waitForDrain(on: drainQueue) {
            // By contract this lambda runs strictly AFTER every queued
            // worker, so onFrame must have fired `total` times by now.
            XCTAssertEqual(firedCount.count(for: "frame"), total)
            exp.fulfill()
        }
        waitForExpectations(timeout: 5.0)
    }

    func testReset() {
        let pool = ConcurrentDetectionPool(maxBacklog: 1)
        armSettings(on: pool)
        while pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0) {}
        XCTAssertGreaterThan(pool.droppedFrameCount, 0)

        pool.reset()
        XCTAssertEqual(pool.droppedFrameCount, 0)
    }
}
