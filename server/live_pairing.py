from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from schemas import FramePayload, TriangulatedPoint


_OTHER_CAM = {"A": "B", "B": "A"}


# Rolling-window buffer used by the pairing search. Tight, because we only
# look back one `window_s` (8 ms) and pairing is O(buffer) per frame.
_DEFAULT_MAX_FRAMES_PER_CAM = 500

# Cap on `frames_by_cam` — the per-cam archive copy that gets flushed onto
# the pitch JSON as `frames_live`. 240 fps × 60 s session = 14 400 frames
# per cam. 30 000 is ~2 full-length sessions of slack so a stuck
# `cycle_end` can't grow memory without bound before the session is
# dropped by `State._drop_live_pairing`.
_LIVE_FRAMES_ARCHIVE_CAP = 30_000

# Cap on `paired_frame_ids`. The pair-dedup set only needs to remember
# recent pairings (within one `window_s` of the newest frame). A rolling
# deque + companion set keeps O(1) adds / membership with bounded memory.
_PAIRED_FRAME_IDS_CAP = 10_000

# Cap on `triangulated` — the per-session triangulated-point list. Stereo
# pairing emits at most one point per paired frame, which is bounded by
# `_LIVE_FRAMES_ARCHIVE_CAP` on either cam; but keep a dedicated cap so
# a pathological stream can't drive memory through this lane either.
_TRIANGULATED_CAP = 30_000


@dataclass
class LivePairingSession:
    """Incremental live A/B pairing over a bounded rolling window.

    Every collection the ingest loop writes to is bounded: `buffers` by
    `max_frames_per_cam` (pairing-window working set), `frames_by_cam` by
    `_LIVE_FRAMES_ARCHIVE_CAP` (archive to flush onto the pitch JSON),
    `paired_frame_ids` by `_PAIRED_FRAME_IDS_CAP` (rolling dedup set),
    and `triangulated` by `_TRIANGULATED_CAP`. Oldest entries drop first
    so a long or pathologically slow session cannot grow memory without
    bound before `State._drop_live_pairing` evicts the whole session.
    """

    session_id: str
    window_s: float = 0.008
    max_frames_per_cam: int = _DEFAULT_MAX_FRAMES_PER_CAM
    buffers: dict[str, deque[FramePayload]] = field(
        default_factory=lambda: {"A": deque(), "B": deque()}
    )
    frames_by_cam: dict[str, deque[FramePayload]] = field(
        default_factory=lambda: {
            "A": deque(maxlen=_LIVE_FRAMES_ARCHIVE_CAP),
            "B": deque(maxlen=_LIVE_FRAMES_ARCHIVE_CAP),
        }
    )
    frame_counts: dict[str, int] = field(
        default_factory=lambda: {"A": 0, "B": 0}
    )
    triangulated: deque[TriangulatedPoint] = field(
        default_factory=lambda: deque(maxlen=_TRIANGULATED_CAP)
    )
    # Dedup of pair keys. Backed by a set for O(1) membership plus a
    # parallel deque that records insertion order so the oldest key can
    # be evicted when the set exceeds `_PAIRED_FRAME_IDS_CAP`.
    paired_frame_ids: set[tuple[int, int]] = field(default_factory=set)
    _paired_frame_id_order: deque[tuple[int, int]] = field(
        default_factory=lambda: deque(maxlen=_PAIRED_FRAME_IDS_CAP)
    )
    completed_cameras: set[str] = field(default_factory=set)
    abort_reasons: dict[str, str] = field(default_factory=dict)

    def _remember_pair_key(self, key: tuple[int, int]) -> None:
        """Record `key` in the dedup structure, evicting the oldest key
        once the rolling cap is hit so memory stays bounded."""
        if len(self._paired_frame_id_order) == self._paired_frame_id_order.maxlen:
            evicted = self._paired_frame_id_order[0]
            # `deque.append` will push the oldest out once maxlen is
            # reached; mirror that in the set so membership stays
            # accurate.
            self.paired_frame_ids.discard(evicted)
        self._paired_frame_id_order.append(key)
        self.paired_frame_ids.add(key)

    def ingest(
        self,
        cam: str,
        frame: FramePayload,
        triangulate_pair: Callable[[str, FramePayload, FramePayload], TriangulatedPoint | None],
    ) -> list[TriangulatedPoint]:
        buf = self.buffers.setdefault(cam, deque())
        buf.append(frame)
        archive = self.frames_by_cam.setdefault(
            cam, deque(maxlen=_LIVE_FRAMES_ARCHIVE_CAP)
        )
        archive.append(frame)
        while len(buf) > self.max_frames_per_cam:
            buf.popleft()
        self.frame_counts[cam] = self.frame_counts.get(cam, 0) + 1
        if not frame.ball_detected:
            return []

        other = _OTHER_CAM.get(cam)
        if other is None:
            return []
        candidates = self.buffers.setdefault(other, deque())
        created: list[TriangulatedPoint] = []
        for peer in reversed(candidates):
            dt = peer.timestamp_s - frame.timestamp_s
            if dt < -self.window_s:
                break
            if abs(dt) > self.window_s or not peer.ball_detected:
                continue
            pair_key = (
                frame.frame_index if cam == "A" else peer.frame_index,
                peer.frame_index if cam == "A" else frame.frame_index,
            )
            if pair_key in self.paired_frame_ids:
                continue
            point = triangulate_pair(cam, frame, peer)
            if point is None:
                continue
            self._remember_pair_key(pair_key)
            self.triangulated.append(point)
            created.append(point)
        return created

    def mark_completed(self, cam: str) -> None:
        self.completed_cameras.add(cam)

    def mark_aborted(self, cam: str, reason: str) -> None:
        self.abort_reasons[cam] = reason

    def frames_for_camera(self, cam: str) -> list[FramePayload]:
        return list(self.frames_by_cam.get(cam, ()))
