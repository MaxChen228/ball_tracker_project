import Foundation
import os

enum CameraAppState {
    case standby
    case timeSyncWaiting
    case mutualSyncing
    case recording
    case uploading
}

struct SyncAnchor {
    let syncId: String
    let anchorTimestampS: Double
}

/// Lock-protected mirror of the three fields `captureOutput` reads across
/// queues (`state`, `pendingRecordingBootstrap`, `pendingSessionId`). Main
/// thread is the sole writer — `applyRemoteArm` / `applyRemoteDisarm` /
/// `enterRecordingMode` / `exitRecordingToStandby` push every transition;
/// the frame queue takes one locked snapshot per delivered sample so a
/// 240 Hz read can never observe a partially-mutated state struct.
final class FrameStateBox {
    struct Snapshot {
        let state: CameraAppState
        let pendingBootstrap: Bool
        let sessionId: String?
    }

    private var lock = os_unfair_lock_s()
    private var _state: CameraAppState = .standby
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

    func consumePendingBootstrap() -> Bool {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        let out = _pendingBootstrap
        _pendingBootstrap = false
        return out
    }

    func update(state: CameraAppState, pendingBootstrap: Bool, sessionId: String?) {
        os_unfair_lock_lock(&lock)
        _state = state
        _pendingBootstrap = pendingBootstrap
        _sessionId = sessionId
        os_unfair_lock_unlock(&lock)
    }
}
