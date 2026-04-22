import Foundation
import os

final class CameraFrameProcessingState {
    private var lock = os_unfair_lock_s()
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0
    private var frameIndex: Int = 0

    struct FrameSample {
        let frameIndex: Int
        let width: Int
        let height: Int
    }

    func recordFrame(width: Int, height: Int) -> FrameSample {
        os_unfair_lock_lock(&lock)
        latestImageWidth = width
        latestImageHeight = height
        frameIndex += 1
        let sample = FrameSample(frameIndex: frameIndex, width: width, height: height)
        os_unfair_lock_unlock(&lock)
        return sample
    }

    func latestDimensions() -> (width: Int, height: Int) {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return (latestImageWidth, latestImageHeight)
    }
}
