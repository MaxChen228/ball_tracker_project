import Foundation

// MARK: - Delegate

protocol ServerWebSocketDelegate: AnyObject {
    /// Called on the main queue when the socket transitions to connected.
    func webSocketDidConnect(_ connection: ServerWebSocketConnection)

    /// Called on the main queue when the socket drops.
    func webSocketDidDisconnect(_ connection: ServerWebSocketConnection, reason: String?)

    /// Called on the main queue for every well-formed JSON message received.
    func webSocket(_ connection: ServerWebSocketConnection, didReceive message: [String: Any])
}

// MARK: - Connection

/// Pure transport layer for the server WebSocket channel.
/// Handles connect / disconnect / automatic exponential-backoff reconnect /
/// receive loop / JSON send. All business-logic (arm, disarm, settings, …)
/// belongs in the delegate — this class knows nothing about application state.
final class ServerWebSocketConnection {

    enum State { case disconnected, connecting, connected, reconnecting }

    weak var delegate: ServerWebSocketDelegate?

    private(set) var state: State = .disconnected
    private(set) var reconnectAttempt: Int = 0

    // MARK: Configuration

    private var baseURL: URL
    private var cameraId: String

    private let wsQueue = DispatchQueue(label: "camera.websocket.queue", qos: .utility)
    private var task: URLSessionWebSocketTask?
    private var reconnectWork: DispatchWorkItem?
    private var pingWork: DispatchWorkItem?

    // Backoff: 1 → 2 → 4 → 8 → 16 → 30 s cap
    private static let backoffCap: TimeInterval = 30
    private static let pingInterval: TimeInterval = 25

    // MARK: Init

    init(baseURL: URL, cameraId: String) {
        self.baseURL = baseURL
        self.cameraId = cameraId
    }

    // MARK: Public API

    /// Start (or restart) the connection. Idempotent — safe to call from
    /// a foreground-reenter hook. `initialHello` is sent immediately after
    /// the socket opens; pass nil to skip.
    func connect(initialHello: [String: Any]? = nil) {
        wsQueue.async { [weak self] in
            guard let self else { return }
            guard self.task == nil else { return }
            self._connect(initialHello: initialHello)
        }
    }

    /// Cleanly close. Cancels any pending reconnect and the ping timer.
    func disconnect() {
        wsQueue.async { [weak self] in
            guard let self else { return }
            self._disconnect(scheduleReconnect: false)
        }
    }

    /// Send a JSON-serialisable dict. Dropped silently when not connected.
    func send(_ payload: [String: Any]) {
        wsQueue.async { [weak self] in
            self?._send(payload)
        }
    }

    /// Reconfigure endpoint without tearing down if the URL hasn't changed.
    func reconfigure(baseURL: URL, cameraId: String) {
        wsQueue.async { [weak self] in
            guard let self else { return }
            let urlChanged = baseURL != self.baseURL
            let roleChanged = cameraId != self.cameraId
            self.baseURL = baseURL
            self.cameraId = cameraId
            if urlChanged || roleChanged {
                self._disconnect(scheduleReconnect: false)
                self._connect(initialHello: nil)
            }
        }
    }

    // MARK: Private — must be called on wsQueue

    private func _connect(initialHello: [String: Any]?) {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        let url = resolvedURL()
        let newTask = URLSession.shared.webSocketTask(with: url)
        task = newTask
        state = .connecting
        newTask.resume()
        state = .connected
        reconnectAttempt = 0
        schedulePing()
        if let hello = initialHello { _send(hello) }
        _receiveNext(task: newTask)
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.delegate?.webSocketDidConnect(self)
        }
    }

    private func _disconnect(scheduleReconnect: Bool) {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        reconnectWork?.cancel()
        reconnectWork = nil
        pingWork?.cancel()
        pingWork = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        state = scheduleReconnect ? .reconnecting : .disconnected
        if scheduleReconnect {
            _scheduleReconnect()
        } else {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.delegate?.webSocketDidDisconnect(self, reason: nil)
            }
        }
    }

    private func _scheduleReconnect() {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        let delay = min(pow(2.0, Double(reconnectAttempt)), Self.backoffCap)
        reconnectAttempt += 1
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.wsQueue.async {
                guard self.task == nil else { return }
                self._connect(initialHello: nil)
            }
        }
        reconnectWork = work
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func _receiveNext(task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            guard let self else { return }
            self.wsQueue.async {
                guard task === self.task else { return } // stale task guard
                switch result {
                case .failure(let error):
                    self._handleDropped(reason: error.localizedDescription)
                case .success(let msg):
                    let text: String?
                    switch msg {
                    case .string(let s): text = s
                    case .data(let d): text = String(data: d, encoding: .utf8)
                    @unknown default: text = nil
                    }
                    if let t = text { self._deliver(text: t) }
                    self._receiveNext(task: task)
                }
            }
        }
    }

    private func _handleDropped(reason: String) {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        task = nil
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.delegate?.webSocketDidDisconnect(self, reason: reason)
        }
        state = .reconnecting
        _scheduleReconnect()
    }

    private func _deliver(text: String) {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        guard
            let data = text.data(using: .utf8),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.delegate?.webSocket(self, didReceive: obj)
        }
    }

    private func _send(_ payload: [String: Any]) {
        guard
            state == .connected,
            let task,
            JSONSerialization.isValidJSONObject(payload),
            let data = try? JSONSerialization.data(withJSONObject: payload),
            let text = String(data: data, encoding: .utf8)
        else { return }
        task.send(.string(text)) { [weak self] error in
            guard let error, let self else { return }
            self.wsQueue.async {
                guard self.task != nil else { return }
                self._handleDropped(reason: error.localizedDescription)
            }
        }
    }

    private func schedulePing() {
        dispatchPrecondition(condition: .onQueue(wsQueue))
        pingWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.wsQueue.async {
                guard self.state == .connected, let t = self.task else { return }
                t.sendPing { _ in }
                self.schedulePing()
            }
        }
        pingWork = work
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + Self.pingInterval, execute: work)
    }

    private func resolvedURL() -> URL {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        comps.path = "/ws/device/\(cameraId)"
        return comps.url ?? baseURL
    }

}
