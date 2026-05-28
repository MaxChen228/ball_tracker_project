"""Pending-device registry for the multi-camera handshake (Phase 0 PR3).

A phone connects to `/ws/device/{device_uuid}` before the operator has
formally assigned it a camera_id. The WS handler holds that socket in
`PendingDeviceManager` and awaits the assignment event; meanwhile the
dashboard Device Pool surfaces it as a promotion candidate. When
`/devices/assign` fires, the assign route both writes the persistent
record AND wakes the awaiting handler so iOS can transition out of its
"waiting for dashboard" state.

Race-free without locks: asyncio single-threaded execution means the
WS handler's `state.assignment_for_device → register pending entry`
sequence cannot be interleaved with assign endpoint's
`state.assign_device → notify_assigned` sequence as long as neither
spans an `await`. The first `await` on the WS handler (sending the
`cam_id_pending` message) happens AFTER the pending entry is already
registered, so a concurrent assign always finds the entry and sets the
event — no lost wakeup possible.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("ball_tracker")


@dataclass
class PendingDeviceEntry:
    """One unassigned-but-connected phone waiting on the dashboard.

    `event` is set by `notify_assigned()` once the operator promotes the
    device; `assigned_cam_id` carries the cam_id the WS handler should
    transition to. `websocket` is kept so /devices/pool can surface the
    live socket state and so unassign-during-pending can close it.

    `_loop` is captured at register time so notifications fired from
    another event loop / thread (e.g. TestClient WS sessions run their
    receive loop in a portal thread distinct from the HTTP handler's
    loop) can still safely set the event via `call_soon_threadsafe`.
    In production all handlers share one loop and the capture is a
    no-op, but the cross-loop case is exactly how the tests exercise
    the pending → assigned round-trip.
    """
    device_uuid: str
    websocket: WebSocket
    device_model: str | None
    registered_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    assigned_cam_id: str | None = None
    _loop: asyncio.AbstractEventLoop | None = None


class PendingDeviceManager:
    """In-memory only — nothing pending survives a restart.

    The persistent layer is `DeviceAssignmentStore`. Pending entries are
    by definition transient (the phone is currently connected and
    awaiting promotion); a server restart drops the WS and iOS will
    reconnect and re-enter pending.
    """

    def __init__(self) -> None:
        self._entries: dict[str, PendingDeviceEntry] = {}

    def register(
        self,
        *,
        device_uuid: str,
        websocket: WebSocket,
        device_model: str | None = None,
    ) -> PendingDeviceEntry:
        """Insert a pending entry. Replaces any prior entry for the same
        device_uuid (handles iOS reconnect-during-pending without leaking
        a stale entry).

        MUST be called from an async context — captures the running
        event loop for cross-loop-safe notify (see `_loop` field). MUST
        be called from a synchronous code path with no `await` between
        the caller's "is this device assigned?" check and this
        registration — otherwise assign endpoint can fire in between
        and notify_assigned will find no entry to wake.
        """
        prior = self._entries.get(device_uuid)
        if prior is not None and prior.websocket is not websocket:
            # Stale entry from a prior connect; the newer socket
            # supersedes. Wake the prior so any orphaned waiter
            # unblocks and unregisters itself.
            self._set_event_safely(prior)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # called outside async ctx (e.g. tests) — fall back
        entry = PendingDeviceEntry(
            device_uuid=device_uuid,
            websocket=websocket,
            device_model=device_model,
            registered_at=time.time(),
            _loop=loop,
        )
        self._entries[device_uuid] = entry
        return entry

    @staticmethod
    def _set_event_safely(entry: PendingDeviceEntry) -> None:
        """Set `entry.event` from any thread / loop. In production all
        handlers share one loop and this is just a direct `.set()`. In
        TestClient setups the WS receive loop runs in a portal thread
        with its own loop, so we must thread through `call_soon_threadsafe`
        to actually wake the awaiter."""
        if entry._loop is None or entry._loop is asyncio.get_event_loop_policy().get_event_loop() and not entry._loop.is_running():
            entry.event.set()
            return
        # Caller may or may not be inside the captured loop. Try direct
        # set if we're in the same loop; otherwise schedule cross-loop.
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is entry._loop:
            entry.event.set()
        else:
            try:
                entry._loop.call_soon_threadsafe(entry.event.set)
            except RuntimeError:
                # Loop already closed — best effort fallback.
                entry.event.set()

    def notify_assigned(self, device_uuid: str, camera_id: str) -> bool:
        """Wake the WS handler awaiting this device's promotion.

        Sync call — must not span an `await` from the assign endpoint's
        `state.assign_device` sync write. Returns True iff an awaiting
        entry was found; False is normal (assign happened with no phone
        yet connected). Thread-safe: notifications from a different
        event loop are routed through `call_soon_threadsafe`.
        """
        entry = self._entries.get(device_uuid)
        if entry is None:
            return False
        entry.assigned_cam_id = camera_id
        self._set_event_safely(entry)
        return True

    def notify_unassigned(self, device_uuid: str) -> PendingDeviceEntry | None:
        """Operator unassigned a device that's currently pending — wake
        with no cam_id so the WS handler closes (`assigned_cam_id` stays
        None). Returns the entry if one existed, for the caller to
        inspect (e.g., to close its socket explicitly)."""
        entry = self._entries.get(device_uuid)
        if entry is None:
            return None
        # assigned_cam_id stays None → handler sees no assignment, closes.
        self._set_event_safely(entry)
        return entry

    def unregister(self, device_uuid: str) -> None:
        """Drop the entry once the WS handler has transitioned out (either
        promoted into active flow or disconnected). Idempotent."""
        self._entries.pop(device_uuid, None)

    def get(self, device_uuid: str) -> PendingDeviceEntry | None:
        return self._entries.get(device_uuid)

    def snapshot_for_pool(self) -> list[dict[str, Any]]:
        """Serialisable list for GET /devices/pool. Order stable by
        registered_at so the dashboard shows oldest-pending first."""
        items = sorted(self._entries.values(), key=lambda e: e.registered_at)
        return [
            {
                "device_uuid": e.device_uuid,
                "device_model": e.device_model,
                "registered_at": e.registered_at,
                "ws_connected": True,  # by definition — entry only exists while WS held
            }
            for e in items
        ]
