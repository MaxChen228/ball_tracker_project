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

    private var uploader: ServerUploader
    private var cameraId: String
    private var baseIntervalS: TimeInterval
    /// Mirrored from the camera VC whenever a chirp anchor is set or
    /// cleared. Stamped onto every outgoing heartbeat so the dashboard
    /// can show per-device time-sync status without peeking at payloads.
    private var timeSynced: Bool = false

    private var pollTimer: Timer?
    private var tickTimer: Timer?

    /// Current delay (seconds) used to schedule the next probe after a
    /// failure. 0 means "next probe uses the base interval" (the post-
    /// success state). Capped at `maxBackoffS`.
    private var currentBackoffS: TimeInterval = 0

    /// Monotonic token used to ignore stale `/heartbeat` responses when
    /// a manual retry or settings change kicks off a new probe before
    /// the in-flight one returns.
    private var probeGeneration: Int = 0

    /// Last armed/idle state we logged, so the state-change log only
    /// fires on transitions rather than every successful heartbeat.
    /// `nil` = no heartbeat yet observed this process lifetime.
    private var lastArmedState: Bool?

    /// Ticks every successful heartbeat to throttle `.debug` "still ok"
    /// logs to roughly one per 10 replies. Avoids 1 Hz spam while still
    /// leaving a breadcrumb when debug-level collection is on.
    private var successTickCounter: Int = 0

    private(set) var lastContactAt: Date?
    private(set) var isReachable: Bool = false
    private(set) var statusText: String = "unknown"

    /// Fires whenever `statusText` or `isReachable` changes — including
    /// the transient "checking…" string at probe start.
    var onStatusChanged: ((_ text: String, _ reachable: Bool) -> Void)?
    /// Fires once per successful heartbeat, after local state is
    /// updated. Carries the full server payload so the VC can dispatch
    /// arm/disarm commands and cache the session id.
    var onHeartbeatSuccess: ((ServerUploader.HeartbeatResponse) -> Void)?
    /// Fires every 1 s while the monitor is running, plus on every
    /// probe response. Passes `lastContactAt` so the caller can render
    /// "Last contact: Ns ago" without reaching back in.
    var onLastContactTick: ((Date?) -> Void)?

    init(
        uploader: ServerUploader,
        cameraId: String,
        baseIntervalS: TimeInterval
    ) {
        self.uploader = uploader
        self.cameraId = cameraId
        self.baseIntervalS = baseIntervalS
    }

    /// Hot-swap the uploader when the server endpoint changes. Callers
    /// should follow with `probeNow()` to refresh the HUD immediately.
    func updateUploader(_ uploader: ServerUploader) {
        self.uploader = uploader
    }

    func updateCameraId(_ id: String) {
        cameraId = id
    }

    /// Set by the camera VC when a chirp anchor is recorded or cleared.
    /// Cheap: only the next heartbeat picks this up, no immediate probe.
    func updateTimeSynced(_ synced: Bool) {
        timeSynced = synced
    }

    /// Replace the post-success poll cadence. Does not reschedule any
    /// in-flight probe — pair with `probeNow()` if you want it to take
    /// effect immediately.
    func updateBaseInterval(_ s: TimeInterval) {
        baseIntervalS = s
    }

    /// Kick off health-probe cadence: resets backoff and probes now.
    func start() {
        invalidatePollTimer()
        currentBackoffS = 0
        probeNow()
        startTick()
    }

    func stop() {
        invalidatePollTimer()
        stopTick()
    }

    /// Manual retry (or settings-changed re-probe): cancel any in-flight
    /// probe via the generation token and send a fresh request.
    func probeNow() {
        probeGeneration += 1
        let gen = probeGeneration
        updateStatus(text: "checking…", reachable: isReachable)
        let cam = cameraId
        uploader.sendHeartbeat(cameraId: cam, timeSynced: timeSynced) { [weak self] result in
            DispatchQueue.main.async {
                guard let self, gen == self.probeGeneration else { return }
                switch result {
                case .success(let response):
                    self.lastContactAt = Date()
                    if self.currentBackoffS != 0 {
                        log.info("heartbeat recovered interval_s=\(self.baseIntervalS)")
                    }
                    self.currentBackoffS = 0
                    self.updateStatus(
                        text: Self.heartbeatDisplayText(response),
                        reachable: true
                    )
                    let armed = response.session?.armed ?? false
                    if self.lastArmedState != armed {
                        let newState = armed ? "ARMED" : "IDLE"
                        let sid = response.session?.id ?? "-"
                        log.info("heartbeat state=\(newState, privacy: .public) session=\(sid, privacy: .public)")
                        self.lastArmedState = armed
                    } else {
                        self.successTickCounter &+= 1
                        if self.successTickCounter % 10 == 0 {
                            log.debug("heartbeat ok tick=\(self.successTickCounter)")
                        }
                    }
                    self.onHeartbeatSuccess?(response)
                    self.scheduleNext(after: self.baseIntervalS)
                case .failure:
                    self.updateStatus(text: "offline", reachable: false)
                    let base = self.baseIntervalS
                    let next = self.currentBackoffS == 0
                        ? base
                        : min(Self.maxBackoffS, self.currentBackoffS * 2)
                    // First failure stays quiet; only warn once backoff starts escalating.
                    if self.currentBackoffS != 0 {
                        log.warning("heartbeat backoff interval_s=\(next)")
                    }
                    self.currentBackoffS = next
                    self.scheduleNext(after: next)
                }
                self.onLastContactTick?(self.lastContactAt)
            }
        }
    }

    /// Reset backoff so the next probe uses the base interval. Use this
    /// when the user taps "Test" — the very next request shouldn't
    /// inherit a long backoff window from prior failures.
    func resetBackoff() {
        currentBackoffS = 0
    }

    // MARK: - Private

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

    /// Summarise the heartbeat reply for the `Server` HUD label. Prefer
    /// the session state when available — operators care about ARMED vs
    /// IDLE, not the generic "receiving"/"idle" string the old /status
    /// gave.
    private static func heartbeatDisplayText(_ r: ServerUploader.HeartbeatResponse) -> String {
        if let s = r.session {
            return s.armed ? "ARMED (\(s.id))" : "IDLE"
        }
        return r.state ?? "reachable"
    }
}
