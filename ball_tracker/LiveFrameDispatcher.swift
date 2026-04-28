import Foundation

/// Dispatches per-frame detection results and cycle-end signals over the
/// WebSocket live channel.
///
/// **Responsibilities** — pure logic, no queue, no timer:
/// - Each `dispatchFrame` call checks three guards (live path, armed session,
///   WS connected) and silently drops if any is missing, bumping the
///   corresponding counter.
/// - `dispatchCycleEnd` sends the `cycle_end` message if the live path is
///   active; the call is a no-op otherwise.
///
/// The class is thread-safe: `dropCounters` is atomically incremented via
/// `OSAtomicIncrement32` guarantees from `Dispatch.sync`; `dispatch*` may be
/// called from any queue.
final class LiveFrameDispatcher {

    // MARK: Drop counter breakdown

    struct DropCounters {
        var notLive: Int = 0
        var noSession: Int = 0
        var wsDown: Int = 0
    }

    private let lock = NSLock()
    private var _drop = DropCounters()

    var dropCounters: DropCounters {
        lock.lock(); defer { lock.unlock() }
        return _drop
    }

    func resetCounters() {
        lock.lock(); defer { lock.unlock() }
        _drop = DropCounters()
    }

    // MARK: Dependencies (injected as closures to avoid circular refs)

    private let connection: ServerWebSocketConnection
    private let cameraId: String
    private let currentSessionId: () -> String?
    private let currentPaths: () -> Set<ServerUploader.DetectionPath>

    // MARK: Init

    init(
        connection: ServerWebSocketConnection,
        cameraId: String,
        currentSessionId: @escaping () -> String?,
        currentPaths: @escaping () -> Set<ServerUploader.DetectionPath>
    ) {
        self.connection = connection
        self.cameraId = cameraId
        self.currentSessionId = currentSessionId
        self.currentPaths = currentPaths
    }

    // MARK: Public API

    /// Stream one detected frame to the server if the live path is active.
    /// Non-blocking; drops silently with counter increment on any guard fail.
    ///
    /// Encoding note: the `[String: Any]` below is handed to
    /// `ServerWebSocketConnection.send`, which bounces onto its WS queue and
    /// calls `JSONSerialization.data(withJSONObject:)` on that queue. There
    /// is **no** per-frame `JSONEncoder()` alloc; `JSONEncoder` is not used
    /// on this path. Do not swap to `JSONEncoder` without reusing one
    /// instance — `JSONEncoder` allocates a fresh writer per call.
    func dispatchFrame(_ frame: ServerUploader.FramePayload) {
        guard currentPaths().contains(.live) else {
            lock.lock(); _drop.notLive += 1; lock.unlock()
            return
        }
        guard let sid = currentSessionId() else {
            lock.lock(); _drop.noSession += 1; lock.unlock()
            return
        }
        guard connection.state == .connected else {
            lock.lock(); _drop.wsDown += 1; lock.unlock()
            return
        }
        // Hand-encoded into [[String: Any]] so JSONSerialization on the
        // WS queue serialises without a Codable round-trip. Mirrors
        // server/schemas.BlobCandidate field-for-field. Empty array →
        // no detection; server side resolves the winner from candidates.
        let candsPayload = frame.candidates.map { c -> [String: Any] in
            [
                "px": c.px,
                "py": c.py,
                "area": c.area,
                "area_score": c.area_score,
            ]
        }
        connection.send([
            "type": "frame",
            "cam": cameraId,
            "sid": sid,
            "i": frame.frame_index,
            "ts": frame.timestamp_s,
            "engine": frame.engine,
            "candidates": candsPayload,
        ])
    }

    /// Send a `cycle_end` signal. No-op if the live path is not active.
    func dispatchCycleEnd(sessionId: String, reason: String = "disarmed") {
        guard currentPaths().contains(.live) else { return }
        guard connection.state == .connected else { return }
        connection.send([
            "type": "cycle_end",
            "cam": cameraId,
            "sid": sessionId,
            "reason": reason,
        ])
    }
}
