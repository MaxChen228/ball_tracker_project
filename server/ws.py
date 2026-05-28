from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class DeviceSocketSnapshot:
    camera_id: str
    connected: bool
    connected_at: float | None
    last_seen_at: float | None
    last_latency_ms: float | None


class DeviceSocketManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sockets: dict[str, WebSocket] = {}
        self._connected_at: dict[str, float] = {}
        self._last_seen_at: dict[str, float] = {}
        self._last_latency_ms: dict[str, float | None] = {}

    async def connect(self, camera_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.bind(camera_id, websocket)

    def bind(self, camera_id: str, websocket: WebSocket) -> None:
        """Register an already-accepted WebSocket under camera_id.

        Used by the device-uuid handshake (PR3): the WS is accepted at
        the top of the handler so it can send `cam_id_pending` /
        `cam_id_assigned` before the cam_id binding is known. Once the
        assignment resolves, the handler calls `bind()` directly rather
        than going through `connect()` which would attempt a second
        accept().
        """
        now = time.time()
        with self._lock:
            self._sockets[camera_id] = websocket
            self._connected_at[camera_id] = now
            self._last_seen_at[camera_id] = now
            self._last_latency_ms.setdefault(camera_id, None)

    def disconnect(self, camera_id: str, websocket: WebSocket | None = None) -> None:
        with self._lock:
            current = self._sockets.get(camera_id)
            if websocket is not None and current is not websocket:
                return
            self._sockets.pop(camera_id, None)

    def note_seen(self, camera_id: str, *, sent_ts: float | None = None) -> None:
        now = time.time()
        with self._lock:
            self._last_seen_at[camera_id] = now
            if sent_ts is not None:
                self._last_latency_ms[camera_id] = max(0.0, (now - sent_ts) * 1000.0)

    def snapshot_socket(self, camera_id: str) -> WebSocket | None:
        """Return the raw WebSocket bound to `camera_id`, if any. Used by
        the device-uuid reassign flow to close a stale binding so iOS
        reconnects through the handshake."""
        with self._lock:
            return self._sockets.get(camera_id)

    def snapshot(self) -> dict[str, DeviceSocketSnapshot]:
        with self._lock:
            all_ids = set(self._connected_at) | set(self._last_seen_at) | set(self._sockets)
            return {
                cam: DeviceSocketSnapshot(
                    camera_id=cam,
                    connected=cam in self._sockets,
                    connected_at=self._connected_at.get(cam),
                    last_seen_at=self._last_seen_at.get(cam),
                    last_latency_ms=self._last_latency_ms.get(cam),
                )
                for cam in all_ids
            }

    async def send(self, camera_id: str, message: dict[str, Any]) -> bool:
        """Send `message` to the named cam socket.

        Returns True on success, False if the cam is not connected or the
        underlying WS send raised. On failure a warning is logged with the
        cam id + message type so operators don't have to cross-reference a
        silent /events row against a clean server log — important because
        commands (arm, disarm, sync_command, settings) go through here and
        a silent drop means the phone sits in the wrong state.
        """
        mtype = message.get("type") if isinstance(message, dict) else "<non-dict>"
        with self._lock:
            websocket = self._sockets.get(camera_id)
        if websocket is None:
            logger.warning(
                "device_ws.send: cam=%s not connected, dropped type=%s",
                camera_id,
                mtype,
            )
            return False
        try:
            await websocket.send_json(message)
            return True
        except Exception as exc:
            # Don't pop `_sockets[cam]` here. The receive loop in
            # `routes/device_ws.ws_device` is the canonical lifetime owner
            # of the socket — it eventually catches the same I/O failure
            # (the WebSocket transport is full-duplex; if send raised, the
            # receive side will too on its next `receive_json`), enters its
            # `finally` block, and calls `disconnect(cam, ws)` then.
            #
            # Popping the socket from this send-side call while the receive
            # task is still alive creates a split-brain: snapshot().connected
            # flips to False here, but the receive loop is still consuming
            # frames against the same (now-invisible) socket. The next
            # connect from that cam treats it as a fresh connection and
            # broadcasts online=True while the old task is still alive,
            # racing two `device_status` payloads through SSE.
            logger.warning(
                "device_ws.send: cam=%s send failed (type=%s, leaving socket "
                "for receive loop to close): %s",
                camera_id,
                mtype,
                exc,
            )
            return False

    async def broadcast(self, message_by_camera: dict[str, dict[str, Any]]) -> None:
        """Fire-and-forget broadcast. Per-cam failures are already logged
        by `send()`; we additionally log any exception that leaks out of
        gather (shouldn't happen since send catches, but belt + braces)."""
        if not message_by_camera:
            return
        results = await asyncio.gather(
            *(self.send(cam, msg) for cam, msg in message_by_camera.items()),
            return_exceptions=True,
        )
        for (cam, _msg), result in zip(message_by_camera.items(), results):
            if isinstance(result, BaseException):
                logger.warning(
                    "device_ws.broadcast: cam=%s unexpected exception: %s",
                    cam,
                    result,
                )

