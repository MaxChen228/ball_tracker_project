import XCTest
@testable import ball_tracker

/// XCTest suite for `PayloadUploadQueue` — the serialised retry-aware
/// uploader for cached pitch payloads. We stub `URLSession.shared`'s
/// transport with `QueueStubURLProtocol` so the real `ServerUploader`
/// makes the HTTP round-trip but the response is hand-crafted, then
/// observe the queue's public callbacks (`onPayloadDropped`,
/// `onUploadingChanged`, `onLastResultChanged`) and on-disk side-effects
/// (file presence under a temp `PitchPayloadStore` directory).
///
/// All tests inject `RetryPolicy.fast` so the production 60 s 4xx cooldown
/// and 4 → 60 s 5xx ladder don't blow the suite's wall clock. The policy
/// preserves semantic shape (one 4xx retry before drop, 5-entry ladder
/// capped at the last) so the fast-path assertions still speak to real
/// behaviour.
final class PayloadUploadQueueTests: XCTestCase {

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        QueueStubURLProtocol.reset()
        URLProtocol.registerClass(QueueStubURLProtocol.self)

        // Each test gets a fresh on-disk payload directory so previous
        // runs / parallel suites can't leak files.
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent("PayloadUploadQueueTests_\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        tempDir = base
    }

    override func tearDown() {
        URLProtocol.unregisterClass(QueueStubURLProtocol.self)
        QueueStubURLProtocol.reset()
        if let tempDir {
            try? FileManager.default.removeItem(at: tempDir)
        }
        super.tearDown()
    }

    // MARK: 1. reloadPending() finds existing JSON files on disk.

    func testReloadPendingPicksUpExistingPayloads() throws {
        let store = makeStore()
        let url1 = try store.save(Self.makePayload(sessionId: "s_aaa1"),
                                   videoURL: try Self.writeFakeVideo(named: "v1.mov"))
        let url2 = try store.save(Self.makePayload(sessionId: "s_bbb2"),
                                   videoURL: try Self.writeFakeVideo(named: "v2.mov"))

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        let files = try store.listPayloadFiles().sorted { $0.path < $1.path }
        let expected = [url1, url2].sorted { $0.path < $1.path }
        XCTAssertEqual(Set(files.map { $0.lastPathComponent }),
                       Set(expected.map { $0.lastPathComponent }))
        XCTAssertFalse(queue.isUploading)
    }

    // MARK: 2. Successful upload deletes JSON + companion video.

    func testSuccessfulUploadDeletesPayloadOnDisk() throws {
        let store = makeStore()
        let videoURL = try Self.writeFakeVideo(named: "happy.mov")
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_ok01"), videoURL: videoURL)
        XCTAssertNotNil(store.videoURL(forPayload: jsonURL),
                        "Companion video must exist before upload")

        QueueStubURLProtocol.handler = .success(Self.uploadResponseJSON(sessionId: "s_ok01",
                                                                       paired: true,
                                                                       points: 12))

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        let exp = expectation(description: "upload settles to idle")
        queue.onUploadingChanged = { uploading in
            if !uploading { exp.fulfill() }
        }
        queue.processNextIfNeeded()
        wait(for: [exp], timeout: 5.0)

        XCTAssertFalse(FileManager.default.fileExists(atPath: jsonURL.path),
                       "JSON should be deleted after success")
        XCTAssertNil(store.videoURL(forPayload: jsonURL),
                     "Companion video should be deleted after success")
    }

    // MARK: 3. Network error → re-queue + retry (queue does NOT drop).

    func testNetworkErrorReinsertsAndRetries() throws {
        let store = makeStore()
        let videoURL = try Self.writeFakeVideo(named: "retry.mov")
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_net01"), videoURL: videoURL)

        // .network then 200: queue re-inserts at head, retries after
        // `.fast.baseRetryDelayS` (20 ms), second attempt deletes.
        QueueStubURLProtocol.handler = .scripted(replies: [
            .failure(URLError(.networkConnectionLost)),
            .successJSON(Self.uploadResponseJSON(sessionId: "s_net01", paired: true, points: 1)),
        ])

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        var droppedCalls = 0
        queue.onPayloadDropped = { _, _ in droppedCalls += 1 }

        let exp = expectation(description: "queue retries then succeeds")
        queue.onLastResultChanged = { _ in
            // onLastResultChanged only fires on success; silent after the
            // first network failure, fires once after the 200.
            exp.fulfill()
        }
        queue.processNextIfNeeded()
        wait(for: [exp], timeout: 5.0)

        XCTAssertEqual(droppedCalls, 0, "Network error must NOT trigger drop")
        XCTAssertFalse(FileManager.default.fileExists(atPath: jsonURL.path),
                       "Final 2xx should delete the JSON")
        XCTAssertEqual(QueueStubURLProtocol.requestCount, 2,
                       "Queue must re-attempt after a network error")
    }

    // MARK: 4. 5xx ladder retries then succeeds — no drop.

    func testServerErrorRetriesAlongLadder() throws {
        let store = makeStore()
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_5xx1"),
                                      videoURL: try Self.writeFakeVideo(named: "s.mov"))

        // Three 503s (ladder walks 20→40→80 ms under `.fast`) then a 200.
        QueueStubURLProtocol.handler = .scripted(replies: [
            .httpStatus(503, body: nil),
            .httpStatus(503, body: nil),
            .httpStatus(503, body: nil),
            .successJSON(Self.uploadResponseJSON(sessionId: "s_5xx1", paired: true, points: 2)),
        ])

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        var droppedCalls = 0
        queue.onPayloadDropped = { _, _ in droppedCalls += 1 }

        let exp = expectation(description: "5xx ladder settles on success")
        queue.onLastResultChanged = { _ in exp.fulfill() }
        queue.processNextIfNeeded()
        wait(for: [exp], timeout: 5.0)

        XCTAssertEqual(droppedCalls, 0, "5xx retries must NOT drop")
        XCTAssertFalse(FileManager.default.fileExists(atPath: jsonURL.path),
                       "Final 2xx should delete the JSON")
        XCTAssertEqual(QueueStubURLProtocol.requestCount, 4,
                       "Queue must walk the ladder through all three 503s")
    }

    // MARK: 5. 4xx with budget exhausted → drop + delete.

    func testClientErrorBudgetExhaustedDropsAndDeletes() throws {
        let store = makeStore()
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_drop1"),
                                      videoURL: try Self.writeFakeVideo(named: "d.mov"))

        // Budget = 1: first 4xx bumps count to 1 (no drop yet, one
        // cooldown retry scheduled — 50 ms under `.fast`). Second 4xx
        // sees count ≥ budget → drop.
        QueueStubURLProtocol.handler = .scripted(replies: [
            .httpStatus(422, body: Data("bad shape".utf8)),
            .httpStatus(400, body: Data("still bad".utf8)),
        ])

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        let dropExp = expectation(description: "drop fires")
        var dropped: (URL, ServerUploader.UploadError)?
        queue.onPayloadDropped = { url, err in
            dropped = (url, err)
            dropExp.fulfill()
        }

        queue.processNextIfNeeded()
        wait(for: [dropExp], timeout: 5.0)

        XCTAssertNotNil(dropped, "Second 4xx must hit the drop path")
        XCTAssertEqual(dropped?.0.lastPathComponent, jsonURL.lastPathComponent,
                       "Drop callback must carry the dropped file URL")
        if case let .client(code, _)? = dropped?.1 {
            XCTAssertTrue(code == 400 || code == 422,
                          "Drop should carry a 4xx status (got \(code))")
        } else {
            XCTFail("Dropped error should be .client(_); got \(String(describing: dropped?.1))")
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: jsonURL.path),
                       "JSON should be deleted on drop")
        XCTAssertNil(store.videoURL(forPayload: jsonURL),
                     "Companion video should be deleted on drop")
        XCTAssertEqual(QueueStubURLProtocol.requestCount, 2,
                       "Queue must attempt twice before dropping")
    }

    // MARK: 6. First 4xx alone does NOT drop (budget not yet exhausted).

    func testFirstClientErrorWithinBudgetDoesNotDrop() throws {
        let store = makeStore()
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_4xx0"),
                                      videoURL: try Self.writeFakeVideo(named: "c.mov"))

        // Only one 4xx is queued; the scripted stub falls back to a 500
        // for the cooldown-retry attempt, so we observe state AFTER the
        // first settle (before the retry). Using a one-shot handler that
        // never completes would leave the test hanging.
        QueueStubURLProtocol.handler = .httpStatus(422, body: Data("bad shape".utf8))

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()

        var dropped: (URL, ServerUploader.UploadError)?
        queue.onPayloadDropped = { url, err in dropped = (url, err) }

        let firstSettleExp = expectation(description: "first 4xx settles to idle")
        var settleCount = 0
        queue.onUploadingChanged = { uploading in
            if !uploading {
                settleCount += 1
                if settleCount == 1 { firstSettleExp.fulfill() }
            }
        }
        queue.processNextIfNeeded()
        wait(for: [firstSettleExp], timeout: 5.0)

        XCTAssertNil(dropped, "First 4xx is within budget; no drop yet")
        XCTAssertTrue(FileManager.default.fileExists(atPath: jsonURL.path),
                      "JSON must remain on disk while the cooldown retry is pending")
    }

    // MARK: 7. clearPending() empties the in-memory queue but keeps disk files.

    func testClearPendingPreservesDiskFiles() throws {
        let store = makeStore()
        let jsonURL = try store.save(Self.makePayload(sessionId: "s_keep0"),
                                      videoURL: try Self.writeFakeVideo(named: "k.mov"))

        let queue = PayloadUploadQueue(store: store, uploader: makeUploader(), policy: .fast)
        try queue.reloadPending()
        queue.clearPending()

        XCTAssertTrue(FileManager.default.fileExists(atPath: jsonURL.path),
                      "clearPending must NOT touch on-disk files")
        XCTAssertFalse(queue.isUploading)
    }

    // MARK: - Helpers

    private func makeStore() -> PitchPayloadStore {
        let dirName = "store_\(UUID().uuidString)"
        let store = PitchPayloadStore(directoryName: dirName)
        addTeardownBlock {
            let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
            try? FileManager.default.removeItem(at: docs.appendingPathComponent(dirName))
        }
        try? store.ensureDirectory()
        return store
    }

    private func makeUploader() -> ServerUploader {
        // Host doesn't matter — QueueStubURLProtocol intercepts every
        // request before DNS.
        let cfg = ServerUploader.ServerConfig(serverIP: "127.0.0.1", serverPort: 65535)
        return ServerUploader(config: cfg)
    }

    private static func makePayload(sessionId: String) -> ServerUploader.PitchPayload {
        return ServerUploader.PitchPayload(
            camera_id: "A",
            session_id: sessionId,
            sync_anchor_timestamp_s: 1.0,
            video_start_pts_s: 0.5,
            video_fps: 240.0,
            local_recording_index: 0,
            intrinsics: nil,
            homography: nil,
            image_width_px: nil,
            image_height_px: nil,
            frames: [],
            frames_on_device: []
        )
    }

    private static func writeFakeVideo(named name: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString)_\(name)")
        try Data([0x00, 0x01, 0x02, 0x03]).write(to: url)
        return url
    }

    private static func uploadResponseJSON(sessionId: String, paired: Bool, points: Int) -> Data {
        let body: [String: Any] = [
            "ok": true,
            "session_id": sessionId,
            "paired": paired,
            "triangulated_points": points,
            "error": NSNull(),
            "mean_residual_m": 0.001,
            "max_residual_m": 0.002,
            "peak_z_m": 1.5,
            "duration_s": 0.5,
        ]
        return try! JSONSerialization.data(withJSONObject: body)
    }
}

// MARK: - URLProtocol stub for the queue tests

/// Intercepts every HTTP request through `URLSession.shared`'s default
/// config. Supports a scripted reply list so we can simulate "first 503,
/// then 200" without needing a real server.
final class QueueStubURLProtocol: URLProtocol {
    enum Reply {
        case successJSON(Data)
        case httpStatus(Int, body: Data?)
        case failure(URLError)
    }

    enum Handler {
        case success(Data)
        case httpStatus(Int, body: Data?)
        case failure(URLError)
        case scripted(replies: [Reply])
    }

    static var handler: Handler?
    private(set) static var requestCount: Int = 0
    private static var scriptedIndex: Int = 0
    private static let lock = NSLock()

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        handler = nil
        requestCount = 0
        scriptedIndex = 0
    }

    override class func canInit(with request: URLRequest) -> Bool {
        return true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        return request
    }

    override func startLoading() {
        QueueStubURLProtocol.lock.lock()
        QueueStubURLProtocol.requestCount += 1
        let handler = QueueStubURLProtocol.handler
        var resolved: Handler? = handler
        if case .scripted(let replies) = handler {
            let idx = QueueStubURLProtocol.scriptedIndex
            if idx < replies.count {
                let reply = replies[idx]
                QueueStubURLProtocol.scriptedIndex += 1
                switch reply {
                case .successJSON(let data): resolved = .success(data)
                case .httpStatus(let s, let b): resolved = .httpStatus(s, body: b)
                case .failure(let e): resolved = .failure(e)
                }
            } else {
                // Past the script — fall through to a generic 500 so the
                // test fails loudly rather than hanging.
                resolved = .httpStatus(500, body: Data("script exhausted".utf8))
            }
        }
        QueueStubURLProtocol.lock.unlock()

        guard let resolved else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        switch resolved {
        case .success(let data):
            respond(status: 200, body: data)
        case .httpStatus(let code, let body):
            respond(status: code, body: body ?? Data())
        case .failure(let err):
            client?.urlProtocol(self, didFailWithError: err)
        case .scripted:
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
        }
    }

    override func stopLoading() {}

    private func respond(status: Int, body: Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}
