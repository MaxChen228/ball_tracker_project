"""Live-preview buffer for the dashboard's "what's the phone framing?" panel.

Single source of truth for whether a camera is streaming preview frames:

  - Dashboard POSTs `/camera/{id}/preview_request {enabled: bool}` — flips
    a per-camera flag. No TTL, no keep-alive, no client heartbeat games.
  - WS settings payloads carry `preview_requested: bool`; iPhones start /
    stop pushing based on it.
  - When the phone's WS drops (sleep, app background, network blip) the
    server flips the flag back to False in the WS-disconnect `finally`,
    so a reconnecting phone doesn't resume pushing from a stale operator
    intent. Operator must explicitly re-enable.

Earlier designs kept a client-side heartbeat that extended a server TTL.
That was brittle: three concurrent loops (tickPreviewRefresh, SSE,
tickStatus) plus sessionStorage-gated ownership meant a toggle-off could
silently be re-armed by a stale refresh from another path. First-principle
fix: the server owns state, the client only mutates + observes.

Thread-safety: single `threading.Lock` covers all mutations + reads. The
buffer is tiny (a handful of cameras × 1 JPEG each) so coarse locking is
fine.
"""
from __future__ import annotations

import time as _time
from threading import Lock
from typing import Callable


# Hard cap per-camera JPEG size. 480p q50 typically weighs 30-60 KB; 2 MB
# is ~30× slack to reject a misbehaving client pushing full-size stills
# without rejecting legitimate mid-quality frames.
_MAX_JPEG_BYTES = 2 * 1024 * 1024

# Preview is expected to refresh at ~10 fps while active. If the newest
# buffered frame is older than this, treat it as stale and hide it rather
# than painting a misleading frozen image for an offline camera.
FRAME_MAX_AGE_S = 3.0


class PreviewBuffer:
    """Per-camera "latest JPEG + request flag" store.

    Not a ring buffer — preview is transient and only the newest frame
    matters. Dropping older frames is desirable (stale preview is worse
    than no preview)."""

    def __init__(self, time_fn: Callable[[], float] = _time.time) -> None:
        self._lock = Lock()
        # camera_id → (jpeg_bytes, timestamp_s). One slot per camera.
        self._frames: dict[str, tuple[bytes, float]] = {}
        # Set of camera_ids the operator has requested preview for. No TTL:
        # entries are flipped on/off only by explicit request() calls or
        # by the WS-disconnect handler in main.py.
        self._requested: set[str] = set()
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

    def latest(
        self,
        camera_id: str,
        max_age_s: float | None = None,
    ) -> tuple[bytes, float] | None:
        """Return (jpeg_bytes, ts) for the most recent frame, or None
        when nothing has been pushed yet. When `max_age_s` is provided,
        stale frames are lazily swept so frozen preview doesn't survive
        a dead device / disconnected control channel."""
        with self._lock:
            got = self._frames.get(camera_id)
            if got is None:
                return None
            if max_age_s is not None:
                _, ts = got
                if self._time_fn() - ts > max_age_s:
                    self._frames.pop(camera_id, None)
                    return None
            return got

    def clear(self, camera_id: str) -> None:
        """Drop the cached frame for one camera. Used when its request
        flag flips off so a stale thumbnail can't leak into a later
        dashboard session."""
        with self._lock:
            self._frames.pop(camera_id, None)

    # ---------- request-flag plane ----------

    def request(self, camera_id: str, enabled: bool) -> None:
        """Flip preview on or off for this camera. No TTL — the flag
        persists until an explicit `enabled=False` call or the WS
        disconnect handler clears it. `enabled=False` also drops the
        cached frame so the dashboard can't paint a stale thumbnail."""
        with self._lock:
            if enabled:
                self._requested.add(camera_id)
            else:
                self._requested.discard(camera_id)
                self._frames.pop(camera_id, None)

    def is_requested(self, camera_id: str) -> bool:
        """True when the operator has preview turned on for this cam."""
        with self._lock:
            return camera_id in self._requested

    def requested_map(self) -> dict[str, bool]:
        """Snapshot of `{camera_id: True}` for every cam with preview on.
        Used by `/status` so the dashboard paints each Devices row with
        the right toggle state."""
        with self._lock:
            return {cam: True for cam in self._requested}
