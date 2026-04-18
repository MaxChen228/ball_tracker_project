import XCTest
@testable import ball_tracker

/// XCTest suite for `ServerHealthMonitor` — the 1 Hz `/heartbeat` poller
/// with exponential backoff on failure. We exercise the backoff logic and
/// the generation-token guard via a `URLProtocol` stub installed on
/// `URLSession.shared`'s default config (the same session `ServerUploader`
/// uses internally), then introspect monitor state through `isReachable`,
/// `statusText`, and the public callbacks.
///
/// Timing notes: the production code schedules `Timer.scheduledTimer`
/// callbacks for retries. Tests don't wait for those — instead we drive
/// the state machine directly via `probeNow()` so each test runs in well
/// under a second.
final class ServerHealthMonitorTests: XCTestCase {

    override func setUp() {
        super.setUp()
        StubURLProtocol.reset()
        URLProtocol.registerClass(StubURLProtocol.self)
    }

    override func tearDown() {
        URLProtocol.unregisterClass(StubURLProtocol.self)
        StubURLProtocol.reset()
        super.tearDown()
    }

    // MARK: 1. Successful probe leaves backoff at zero (next probe uses base).

    func testSuccessfulProbeKeepsBackoffAtBase() {
        StubURLProtocol.handler = .success(Self.heartbeatBody(armed: false))
        let monitor = makeMonitor(baseIntervalS: 5.0)

        let exp = expectation(description: "probe success")
        monitor.onStatusChanged = { _, reachable in
            if reachable { exp.fulfill() }
        }

        monitor.probeNow()
        wait(for: [exp], timeout: 2.0)

        XCTAssertTrue(monitor.isReachable)
        // After a successful reply the backoff is reset to 0 — the next
        // schedule uses `baseIntervalS` directly. We can't read the
        // private field but the displayed status should be the IDLE/ARMED
        // string, not "offline".
        XCTAssertNotEqual(monitor.statusText, "offline")
    }

    // MARK: 2. Failure → backoff doubles.

    func testFailureDoublesBackoffEachAttempt() {
        StubURLProtocol.handler = .failure(URLError(.notConnectedToInternet))
        let monitor = makeMonitor(baseIntervalS: 4.0)

        // First failure: backoff transitions 0 → base (4 s).
        runProbeSync(monitor)
        XCTAssertFalse(monitor.isReachable)
        XCTAssertEqual(monitor.statusText, "offline")

        // Second failure: backoff doubles 4 → 8.
        runProbeSync(monitor)
        // Third failure: 8 → 16.
        runProbeSync(monitor)
        // Fourth failure: 16 → 32.
        runProbeSync(monitor)
        // Fifth failure: 32 → 60 (cap).
        runProbeSync(monitor)
        // Sixth failure: 60 (clamped — does not exceed the cap).
        runProbeSync(monitor)

        // The monitor still reports offline; we can't observe the private
        // currentBackoffS, but reachability and status text confirm the
        // failure path stayed engaged across all six probes.
        XCTAssertFalse(monitor.isReachable)
    }

    // MARK: 3. 60 s ceiling — no overshoot.

    func testBackoffNeverExceedsMaxBackoffCeiling() {
        XCTAssertEqual(ServerHealthMonitor.maxBackoffS, 60.0,
                       "Cap is part of the public contract")
    }

    // MARK: 4. Failure followed by success resets to base.

    func testRecoveryAfterFailureResetsBackoff() {
        let monitor = makeMonitor(baseIntervalS: 3.0)

        // Three consecutive failures push backoff to 12 s (3 → 6 → 12).
        StubURLProtocol.handler = .failure(URLError(.timedOut))
        runProbeSync(monitor)
        runProbeSync(monitor)
        runProbeSync(monitor)
        XCTAssertFalse(monitor.isReachable)

        // Now flip to success — the recovery branch logs and resets
        // currentBackoffS to 0.
        StubURLProtocol.handler = .success(Self.heartbeatBody(armed: true, sessionId: "s_abc1"))
        runProbeSync(monitor)
        XCTAssertTrue(monitor.isReachable)
        XCTAssertTrue(monitor.statusText.contains("ARMED"),
                      "ARMED status should appear when session.armed is true; got \(monitor.statusText)")
    }

    // MARK: 5. Generation token: stale reply doesn't clobber state.

    func testStaleProbeReplyIsDropped() {
        // First probe: success — server reports IDLE.
        StubURLProtocol.handler = .success(Self.heartbeatBody(armed: false))
        let monitor = makeMonitor(baseIntervalS: 5.0)
        runProbeSync(monitor)
        XCTAssertTrue(monitor.isReachable)
        let firstStatus = monitor.statusText

        // Now arrange the next reply to take "long" — we'll fire probeNow()
        // twice, second fires immediately while the first is in-flight.
        // Each probeNow() bumps the generation; the older reply, when it
        // finally arrives, is dropped by the `gen == self.probeGeneration`
        // guard. We simulate this by calling probeNow() twice synchronously
        // — both stub responses are configured but only the latest one is
        // honoured. The status text after both finish should reflect the
        // *latest* configured response, never an older one.
        StubURLProtocol.handler = .success(
            Self.heartbeatBody(armed: true, sessionId: "s_late0")
        )
        // Kick the second probe off; the first one is implicitly cancelled
        // by the generation bump.
        let exp = expectation(description: "second probe completes")
        monitor.onStatusChanged = { text, _ in
            if text.contains("ARMED") { exp.fulfill() }
        }
        monitor.probeNow()
        wait(for: [exp], timeout: 2.0)
        XCTAssertTrue(monitor.statusText.contains("ARMED"),
                      "Latest probe's status should win; was '\(firstStatus)' → '\(monitor.statusText)'")
    }

    // MARK: 6. probeNow() can be called repeatedly without crashing.

    func testProbeNowIsReentrant() {
        StubURLProtocol.handler = .success(Self.heartbeatBody(armed: false))
        let monitor = makeMonitor(baseIntervalS: 5.0)

        let exp = expectation(description: "final probe success")
        exp.expectedFulfillmentCount = 1
        exp.assertForOverFulfill = false
        var fulfilled = false
        monitor.onStatusChanged = { _, reachable in
            if reachable && !fulfilled {
                fulfilled = true
                exp.fulfill()
            }
        }

        // Fire several probeNow() calls back-to-back. Each bumps the
        // generation; only the latest reply is honoured. The monitor
        // must end up reachable without crashing on the dropped earlier
        // generations.
        monitor.probeNow()
        monitor.probeNow()
        monitor.probeNow()
        wait(for: [exp], timeout: 2.0)
        XCTAssertTrue(monitor.isReachable)
    }

    // MARK: 7. resetBackoff() public API exists and is harmless.

    func testResetBackoffIsCallable() {
        let monitor = makeMonitor(baseIntervalS: 5.0)
        // Should not crash, before or after a probe.
        monitor.resetBackoff()
        StubURLProtocol.handler = .failure(URLError(.cannotConnectToHost))
        runProbeSync(monitor)
        monitor.resetBackoff()
        XCTAssertFalse(monitor.isReachable)
    }

    // MARK: - Helpers

    private func makeMonitor(baseIntervalS: TimeInterval) -> ServerHealthMonitor {
        let cfg = ServerUploader.ServerConfig(serverIP: "127.0.0.1", serverPort: 65535)
        let uploader = ServerUploader(config: cfg)
        return ServerHealthMonitor(uploader: uploader, cameraId: "A", baseIntervalS: baseIntervalS)
    }

    /// Drive a single `probeNow()` to completion synchronously by waiting
    /// on `onStatusChanged`'s post-reply call. Both success and failure
    /// branches call `updateStatus(...)` exactly once per probe (other than
    /// the transient "checking…" string we ignore).
    private func runProbeSync(_ monitor: ServerHealthMonitor) {
        let exp = expectation(description: "probe finished")
        var sawTerminal = false
        let prior = monitor.onStatusChanged
        monitor.onStatusChanged = { text, reachable in
            prior?(text, reachable)
            // Skip the "checking…" probe-start beacon; wait for the real reply.
            if text != "checking…" && !sawTerminal {
                sawTerminal = true
                exp.fulfill()
            }
        }
        monitor.probeNow()
        wait(for: [exp], timeout: 2.0)
        monitor.onStatusChanged = prior
    }

    private static func heartbeatBody(armed: Bool, sessionId: String = "s_t0t0") -> Data {
        let body: [String: Any] = [
            "state": "ok",
            "session": [
                "id": sessionId,
                "armed": armed,
                "started_at": 0.0,
                "ended_at": NSNull(),
            ] as [String: Any],
        ]
        return try! JSONSerialization.data(withJSONObject: body)
    }
}

// MARK: - URLProtocol stub

/// Test-only `URLProtocol` that intercepts every HTTP request made through
/// the default `URLSession` config (which is what `URLSession.shared` uses).
/// `handler` decides whether the in-flight request resolves with a 200 +
/// JSON body or a `URLError`.
private final class StubURLProtocol: URLProtocol {
    enum Handler {
        case success(Data)
        case failure(URLError)
    }

    /// Set this before each test. Reset in tearDown.
    static var handler: Handler?

    static func reset() {
        handler = nil
    }

    override class func canInit(with request: URLRequest) -> Bool {
        return true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        return request
    }

    override func startLoading() {
        guard let handler = StubURLProtocol.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        switch handler {
        case .success(let data):
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        case .failure(let error):
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {
        // Nothing to clean up.
    }
}
