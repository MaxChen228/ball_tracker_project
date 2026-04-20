"""Live-preview buffer for the dashboard's "what's the phone framing?" panel.

Part of Phase 4a (iOS-decoupling). The iPhone pushes JPEG-encoded preview
frames (~10 fps, 480p, quality 0.5) to the server ONLY when the dashboard
has requested preview for that camera. The buffer keeps one latest JPEG per
camera in memory; the dashboard pulls via `/camera/{id}/preview` (one-shot)
or `/camera/{id}/preview.mjpeg` (multipart stream).

Request semantics:
  - Dashboard POSTs `/camera/{id}/preview_request {enabled: true}` every
    ~2 s while its preview panel is open.
  - Server keeps the flag live for `REQUEST_TTL_S` (5 s) after each call.
  - Heartbeat replies carry `preview_requested: bool` for the beating
    camera; iPhones start/stop pushing based on that field.
  - When the dashboard panel closes (or the browser tab dies), the
    dashboard stops calling preview_request; the TTL lapses; the phone's
    next heartbeat reply sets `preview_requested=false` and the uploader
    stops.

Thread-safety: single `threading.Lock` covers all mutations + reads. The
buffer is tiny (2 cameras × 1 JPEG each) so coarse locking is fine.
"""
from __future__ import annotations

import time as _time
from threading import Lock
from typing import Callable


# Hard cap per-camera JPEG size. 480p q50 typically weighs 30-60 KB; 2 MB
# is ~30× slack to reject a misbehaving client pushing full-size stills
# without rejecting legitimate mid-quality frames.
_MAX_JPEG_BYTES = 2 * 1024 * 1024

# How long a `request()` call keeps the per-camera flag live. Dashboard
# JS re-hits the endpoint every ~2 s while its preview panel is open, so
# 5 s absorbs one missed tick without flapping.
REQUEST_TTL_S = 5.0


class PreviewBuffer:
    """Per-camera "latest JPEG + request flag" store.

    Not a ring buffer — preview is transient and only the newest frame
    matters. Dropping older frames is desirable (stale preview is worse
    than no preview)."""

    def __init__(self, time_fn: Callable[[], float] = _time.time) -> None:
        self._lock = Lock()
        # camera_id → (jpeg_bytes, timestamp_s). One slot per camera.
        self._frames: dict[str, tuple[bytes, float]] = {}
        # camera_id → wall-clock expiry. Absent / past-expiry means the
        # dashboard is not currently watching this camera.
        self._requests: dict[str, float] = {}
        self._time_fn = time_fn

    # ---------- frame plane ----------

    def push(self, camera_id: str, jpeg_bytes: bytes, ts: float) -> bool:
        """Store one JPEG for this camera. Returns False (and drops the
        frame) when oversize. Thread-safe. Silently overwrites the prior
        frame — preview is only ever "latest"."""
        if len(jpeg_bytes) > _MAX_JPEG_BYTES:
            return False
        with self._lock:
            self._frames[camera_id] = (jpeg_bytes, ts)
        return True

    def latest(self, camera_id: str) -> tuple[bytes, float] | None:
        """Return (jpeg_bytes, ts) for the most recent frame, or None
        when nothing has been pushed yet."""
        with self._lock:
            return self._frames.get(camera_id)

    def clear(self, camera_id: str) -> None:
        """Drop the cached frame for one camera. Used when its request
        flag lapses so a stale thumbnail can't leak into a later
        dashboard session."""
        with self._lock:
            self._frames.pop(camera_id, None)

    # ---------- request-flag plane ----------

    def request(self, camera_id: str, enabled: bool) -> None:
        """Turn preview on or off for this camera. `enabled=True` refreshes
        the TTL; `enabled=False` both clears the flag and drops any cached
        frame so the dashboard doesn't serve a stale thumbnail after
        toggling off."""
        with self._lock:
            if enabled:
                self._requests[camera_id] = self._time_fn() + REQUEST_TTL_S
            else:
                self._requests.pop(camera_id, None)
                self._frames.pop(camera_id, None)

    def is_requested(self, camera_id: str) -> bool:
        """True if the dashboard's TTL-gated flag for this camera is
        still live. Lazily sweeps past-expiry entries on read so a
        closed dashboard panel doesn't keep phones pushing forever."""
        with self._lock:
            exp = self._requests.get(camera_id)
            if exp is None:
                return False
            if self._time_fn() >= exp:
                # Lazy sweep: the dashboard stopped pinging. Drop the
                # frame too so a later viewer doesn't see stale bytes.
                self._requests.pop(camera_id, None)
                self._frames.pop(camera_id, None)
                return False
            return True

    def requested_map(self) -> dict[str, bool]:
        """Snapshot of `{camera_id: is_requested}` across every known
        camera. Used by `/status` so the dashboard JS can paint each
        Devices row with the right toggle state. Lazily sweeps too."""
        out: dict[str, bool] = {}
        now = self._time_fn()
        with self._lock:
            dead: list[str] = []
            for cam, exp in self._requests.items():
                if now >= exp:
                    dead.append(cam)
                else:
                    out[cam] = True
            for cam in dead:
                self._requests.pop(cam, None)
                self._frames.pop(cam, None)
        return out
