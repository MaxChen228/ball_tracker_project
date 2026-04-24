import Foundation
import os

/// Lock-guarded holder for the `ServerUploader` reference that the
/// capture queue reads and the main queue swaps during
/// `applyUpdatedSettings` (triggered by a server-IP change).
///
/// Previously `captureQueueUploader` was `nonisolated(unsafe)` and
/// swapped without synchronization. The capture queue could observe a
/// torn reference — Swift class reference writes are word-sized on all
/// currently shipped iOS devices so pointer-tearing itself is not the
/// risk; the real race is:
///
///   t0: main mutates `captureQueueUploader = newUploader`
///   t1: capture queue reads the new uploader …
///   t2: … while an already-enqueued async task still holds the OLD
///       uploader and posts with the previous server IP
///
/// Both windows are closed here by guarding read + write with a single
/// `os_unfair_lock`. Readers call `snapshot()` which returns a strong
/// reference under the lock — the entire subsequent work runs off that
/// local snapshot, so a mid-flight swap never splits a single logical
/// operation across two uploaders.
///
/// This is deliberately NOT an actor. Making `ServerUploader` itself an
/// actor would force every `captureOutput` caller into `await`, which
/// isn't acceptable inside the 240 fps sample-buffer path.
final class AtomicUploaderBox {
    private var lock = os_unfair_lock_s()
    private var _value: ServerUploader?

    init(_ initial: ServerUploader? = nil) {
        _value = initial
    }

    /// Atomic read. Returns a strong reference captured under the lock;
    /// callers should store it locally and use that snapshot for the
    /// duration of their work rather than re-reading.
    func snapshot() -> ServerUploader? {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return _value
    }

    /// Atomic swap. Does not touch the outgoing uploader (no cancel) —
    /// in-flight requests on the previous uploader will simply finish on
    /// their own; the queue/monitor layers decide what to do with the
    /// results.
    func set(_ newValue: ServerUploader?) {
        os_unfair_lock_lock(&lock)
        _value = newValue
        os_unfair_lock_unlock(&lock)
    }
}
