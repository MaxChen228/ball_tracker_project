import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "network")

/// 1 Hz `/heartbeat` poller with exponential backoff on failure. Owns
/// the liveness-probe timer, the stale-response generation token, and
/// the "last contact" tick timer that drives the HUD label.
///
/// Decoupled from UIKit: callers register callbacks for state changes.
/// Every callback fires on the main queue.
final class ServerHealthMonitor {
    static let maxBackoffS: TimeInterval = 60

    private var baseIntervalS: TimeInterval
    /// Mirrored from the camera VC whenever a legacy chirp anchor is set
    /// or cleared. Stamped onto every outgoing heartbeat so the dashboard
    /// can distinguish "has some anchor" from "has the current sync id".
    private var timeSyncId: String?

    private var pollTimer: Timer?
    private var tickTimer: Timer?

    private(set) var lastContactAt: Date?
    private(set) var isReachable: Bool = false
    private(set) var statusText: String = "unknown"

    /// Fires whenever `statusText` or `isReachable` changes — including
    /// the transient "checking…" string at probe start.
    var onStatusChanged: ((_ text: String, _ reachable: Bool) -> Void)?
    /// Fires every 1 s while the monitor is running, plus on every
    /// probe response. Passes `lastContactAt` so the caller can render
    /// "Last contact: Ns ago" without reaching back in.
    var onLastContactTick: ((Date?) -> Void)?

    /// Triggered when the monitor timer ticks. The caller (VC) should
    /// serialize this into `{"type": "heartbeat", ...}` and send via WS.
    var sendWSHeartbeat: ((_ timeSynced: Bool, _ timeSyncId: String?) -> Void)?

    init(baseIntervalS: TimeInterval) {
        self.baseIntervalS = baseIntervalS
    }

    /// Set by the camera VC when a legacy chirp anchor is recorded or
    /// cleared.
    func updateTimeSyncId(_ syncId: String?) {
        timeSyncId = syncId
    }

    /// Replace the ping cadence. Does not reschedule any
    /// in-flight probe — pair with `probeNow()` if you want it to take
    /// effect immediately.
    func updateBaseInterval(_ s: TimeInterval) {
        baseIntervalS = s
    }

    /// Set by the VC whenever it considers the WS fully healthy
    /// (e.g. `hello` sent or `settings` received).
    func recordConnectionSuccess(status: String) {
        lastContactAt = Date()
        updateStatus(text: status, reachable: true)
    }

    /// Set by the VC when the WS disconnects.
    func recordConnectionDrop() {
        updateStatus(text: "offline", reachable: false)
    }

    /// Kick off health-probe cadence: probes now and starts tickers.
    func start() {
        invalidatePollTimer()
        probeNow()
        startTick()
    }

    func stop() {
        invalidatePollTimer()
        stopTick()
    }

    /// Send a tick to the closure and reschedule.
    func probeNow() {
        sendWSHeartbeat?(timeSyncId != nil, timeSyncId)
        scheduleNext(after: baseIntervalS)
    }
    private func invalidatePollTimer() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func scheduleNext(after delay: TimeInterval) {
        invalidatePollTimer()
        let timer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            self?.probeNow()
        }
        timer.tolerance = max(0.5, delay * 0.1)
        pollTimer = timer
    }

    private func startTick() {
        tickTimer?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.onLastContactTick?(self.lastContactAt)
        }
        timer.tolerance = 0.5
        tickTimer = timer
        onLastContactTick?(lastContactAt)
    }

    private func stopTick() {
        tickTimer?.invalidate()
        tickTimer = nil
    }

    private func updateStatus(text: String, reachable: Bool) {
        statusText = text
        isReachable = reachable
        onStatusChanged?(text, reachable)
    }
}
