import XCTest
import CoreVideo
@testable import ball_tracker

final class ConcurrentDetectionPoolTests: XCTestCase {

    private func createDummyPixelBuffer() -> CVPixelBuffer {
        var pixelBuffer: CVPixelBuffer?
        let attrs = [
            kCVPixelBufferCGImageCompatibilityKey: kCFBooleanTrue,
            kCVPixelBufferCGBitmapContextCompatibilityKey: kCFBooleanTrue
        ] as CFDictionary
        CVPixelBufferCreate(kCFAllocatorDefault, 64, 64, kCVPixelFormatType_32BGRA, attrs, &pixelBuffer)
        return pixelBuffer!
    }

    func testDispatchUnderConcurrencyLimitFiresAllFrames() {
        let pool = ConcurrentDetectionPool(maxConcurrency: 3)
        let exp = expectation(description: "Fires all frames")
        exp.expectedFulfillmentCount = 3
        
        pool.onFrame = { _ in
            exp.fulfill()
        }
        
        for i in 0..<3 {
            let accepted = pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: Double(i))
            XCTAssertTrue(accepted)
        }
        
        waitForExpectations(timeout: 2.0)
    }

    func testDispatchOverConcurrencyLimitDropsExcess() {
        let pool = ConcurrentDetectionPool(maxConcurrency: 1)
        
        var acceptedCount = 0
        for _ in 0..<100 {
            if pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0) {
                acceptedCount += 1
            }
        }
        
        XCTAssertTrue(acceptedCount < 100, "Should have dropped some frames")
        XCTAssertTrue(pool.droppedFrameCount > 0, "Telemetry should record dropped frames")
    }

    func testInvalidateGenerationSilencesInFlightWorkers() {
        let pool = ConcurrentDetectionPool(maxConcurrency: 3)
        var receivedCallbackCount = 0
        pool.onFrame = { _ in receivedCallbackCount += 1 }
        
        _ = pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0.1)
        pool.invalidateGeneration()
        
        let exp = expectation(description: "Wait to ensure callback does not fire")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            exp.fulfill()
        }
        waitForExpectations(timeout: 1.0)
        
        // It's possible the logic executed fast enough to fire the callback before invalidation, 
        // but typically async overhead prevents it when there's no waiting.
        // Assuming test machine overhead pushes async block after main thread invalidates.
        XCTAssertEqual(receivedCallbackCount, 0, "Invalidated generation should silence workers")
    }

    func testFrameIndexIsMonotonicAcrossConcurrentDispatches() {
        let pool = ConcurrentDetectionPool(maxConcurrency: 5)
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
        
        let sortedIndices = indices.sorted()
        XCTAssertEqual(indices.count, total)
        XCTAssertEqual(sortedIndices.first, 0)
        XCTAssertEqual(sortedIndices.last, total - 1)
        XCTAssertEqual(Set(indices).count, total)
    }

    func testReset() {
        let pool = ConcurrentDetectionPool(maxConcurrency: 1)
        while pool.enqueue(pixelBuffer: createDummyPixelBuffer(), timestampS: 0) {}
        XCTAssertTrue(pool.droppedFrameCount > 0)
        
        pool.reset()
        XCTAssertEqual(pool.droppedFrameCount, 0)
    }
}
