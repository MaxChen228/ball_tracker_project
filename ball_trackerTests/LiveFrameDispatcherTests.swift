import XCTest
@testable import ball_tracker

/// Unit tests for `LiveFrameDispatcher`.
/// We use a real `ServerWebSocketConnection` pointing at an unreachable URL
/// so `state` is `.disconnected`. This lets us verify the drop-counter logic
/// without needing a live server.
final class LiveFrameDispatcherTests: XCTestCase {

    // A minimal FramePayload helper. Phase B wire shape: `detected` is
    // expressed as one synthetic candidate (or none). `ballDetected` is
    // derived from `candidates.isEmpty` on the FramePayload itself.
    private func makeFrame(index: Int = 0, detected: Bool = true) -> ServerUploader.FramePayload {
        let candidates: [ServerUploader.BlobCandidate] = detected
            ? [ServerUploader.BlobCandidate(px: 320.0, py: 240.0, area: 100, area_score: 1.0)]
            : []
        return ServerUploader.FramePayload(
            frame_index: index,
            timestamp_s: Double(index) * (1.0 / 240.0),
            candidates: candidates,
            engine: BallDetectionEngineID.hsvIOS
        )
    }

    private func makeConnection() -> ServerWebSocketConnection {
        var comps = URLComponents()
        comps.scheme = "ws"
        comps.host = "127.0.0.1"
        comps.port = 65535
        return ServerWebSocketConnection(baseURL: comps.url!, cameraId: "A")
    }

    // MARK: 1. Drops when .live path not active

    func testDropsWhenLivePathNotInSet() {
        let conn = makeConnection()
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { "s_abc" },
            currentPaths: { [.serverPost] } // no .live
        )

        dispatcher.dispatchFrame(makeFrame())

        XCTAssertEqual(dispatcher.dropCounters.notLive, 1)
        XCTAssertEqual(dispatcher.dropCounters.noSession, 0)
        XCTAssertEqual(dispatcher.dropCounters.wsDown, 0)
    }

    // MARK: 2. Drops when sessionId is nil

    func testDropsWhenNoSession() {
        let conn = makeConnection()
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { nil },
            currentPaths: { [.live] }
        )

        dispatcher.dispatchFrame(makeFrame())

        XCTAssertEqual(dispatcher.dropCounters.notLive, 0)
        XCTAssertEqual(dispatcher.dropCounters.noSession, 1)
        XCTAssertEqual(dispatcher.dropCounters.wsDown, 0)
    }

    // MARK: 3. Drops when WS is down

    func testDropsWhenWSDown() {
        let conn = makeConnection() // state == .disconnected
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { "s_abc" },
            currentPaths: { [.live] }
        )

        dispatcher.dispatchFrame(makeFrame())

        XCTAssertEqual(dispatcher.dropCounters.notLive, 0)
        XCTAssertEqual(dispatcher.dropCounters.noSession, 0)
        XCTAssertEqual(dispatcher.dropCounters.wsDown, 1)
    }

    // MARK: 4. resetCounters clears all buckets

    func testResetCounters() {
        let conn = makeConnection()
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { nil },
            currentPaths: { [.live] }
        )

        dispatcher.dispatchFrame(makeFrame())
        XCTAssertEqual(dispatcher.dropCounters.noSession, 1)

        dispatcher.resetCounters()

        let after = dispatcher.dropCounters
        XCTAssertEqual(after.notLive, 0)
        XCTAssertEqual(after.noSession, 0)
        XCTAssertEqual(after.wsDown, 0)
    }

    // MARK: 5. dispatchCycleEnd is a no-op when live path absent

    func testCycleEndNoopWhenNotLive() {
        let conn = makeConnection()
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { "s_abc" },
            currentPaths: { [.serverPost] }
        )

        // Should not crash
        dispatcher.dispatchCycleEnd(sessionId: "s_abc", reason: "disarmed")
        // No assertion needed — just confirming no crash
    }

    // MARK: 6. Multiple frames accumulate independent drop counts

    func testMultipleDropsAccumulate() {
        let conn = makeConnection()
        let dispatcher = LiveFrameDispatcher(
            connection: conn,
            cameraId: "A",
            currentSessionId: { "s_abc" },
            currentPaths: { [.live] } // WS down → all drops go to wsDown
        )

        for i in 0..<5 {
            dispatcher.dispatchFrame(makeFrame(index: i))
        }

        XCTAssertEqual(dispatcher.dropCounters.wsDown, 5)
    }
}
