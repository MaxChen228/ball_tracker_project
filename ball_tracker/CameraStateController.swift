import Foundation
import os

final class CameraStateController {
    typealias AppState = CameraViewController.AppState

    private let frameStateBox: FrameStateBox
    private let onStateChanged: () -> Void

    private(set) var currentState: AppState = .standby

    init(frameStateBox: FrameStateBox, onStateChanged: @escaping () -> Void) {
        self.frameStateBox = frameStateBox
        self.onStateChanged = onStateChanged
    }

    func transition(
        to newState: AppState,
        pendingBootstrap: Bool = false,
        sessionId: String? = nil,
        refreshUI: Bool = true
    ) {
        currentState = newState
        frameStateBox.update(state: newState, pendingBootstrap: pendingBootstrap, sessionId: sessionId)
        if refreshUI {
            onStateChanged()
        }
    }

    func snapshot() -> FrameStateBox.Snapshot {
        frameStateBox.snapshot()
    }

    func consumePendingBootstrap() -> Bool {
        frameStateBox.consumePendingBootstrap()
    }
}

/// Lock-protected mirror of the fields `captureOutput` reads across
/// queues (`state`, `pendingRecordingBootstrap`, `pendingSessionId`).
final class FrameStateBox {
    struct Snapshot {
        let state: CameraViewController.AppState
        let pendingBootstrap: Bool
        let sessionId: String?
    }

    private var lock = os_unfair_lock_s()
    private var _state: CameraViewController.AppState = .standby
    private var _pendingBootstrap: Bool = false
    private var _sessionId: String?

    func snapshot() -> Snapshot {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return Snapshot(
            state: _state,
            pendingBootstrap: _pendingBootstrap,
            sessionId: _sessionId
        )
    }

    func update(state: CameraViewController.AppState, pendingBootstrap: Bool, sessionId: String?) {
        os_unfair_lock_lock(&lock)
        _state = state
        _pendingBootstrap = pendingBootstrap
        _sessionId = sessionId
        os_unfair_lock_unlock(&lock)
    }

    func consumePendingBootstrap() -> Bool {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        let prev = _pendingBootstrap
        _pendingBootstrap = false
        return prev
    }
}
