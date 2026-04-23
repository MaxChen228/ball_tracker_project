from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from schemas import FramePayload, TriangulatedPoint


_OTHER_CAM = {"A": "B", "B": "A"}


@dataclass
class LivePairingSession:
    """Incremental live A/B pairing over a bounded rolling window."""

    session_id: str
    window_s: float = 0.008
    max_frames_per_cam: int = 500
    buffers: dict[str, deque[FramePayload]] = field(
        default_factory=lambda: {"A": deque(), "B": deque()}
    )
    frames_by_cam: dict[str, list[FramePayload]] = field(
        default_factory=lambda: {"A": [], "B": []}
    )
    frame_counts: dict[str, int] = field(
        default_factory=lambda: {"A": 0, "B": 0}
    )
    triangulated: list[TriangulatedPoint] = field(default_factory=list)
    paired_frame_ids: set[tuple[int, int]] = field(default_factory=set)
    completed_cameras: set[str] = field(default_factory=set)
    abort_reasons: dict[str, str] = field(default_factory=dict)

    def ingest(
        self,
        cam: str,
        frame: FramePayload,
        triangulate_pair: Callable[[str, FramePayload, FramePayload], TriangulatedPoint | None],
    ) -> list[TriangulatedPoint]:
        buf = self.buffers.setdefault(cam, deque())
        buf.append(frame)
        self.frames_by_cam.setdefault(cam, []).append(frame)
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
            self.paired_frame_ids.add(pair_key)
            self.triangulated.append(point)
            created.append(point)
        return created

    def mark_completed(self, cam: str) -> None:
        self.completed_cameras.add(cam)

    def mark_aborted(self, cam: str, reason: str) -> None:
        self.abort_reasons[cam] = reason

    def frames_for_camera(self, cam: str) -> list[FramePayload]:
        return list(self.frames_by_cam.get(cam, []))
