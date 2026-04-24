from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from schemas import FramePayload, TriangulatedPoint


_OTHER_CAM = {"A": "B", "B": "A"}


@dataclass
class CameraPose:
    """Cached per-camera geometry for the live-triangulation hot path.

    Pre-computed once from the calibration snapshot when the live session
    first sees a frame from this camera, then reused for every pair —
    `_camera_pose` does SVD + normalization that we'd otherwise pay per
    frame (240 Hz × two cams). `dist` is the 5-coefficient OpenCV
    distortion vector, None on a calibration that didn't ship one."""
    K: Any  # np.ndarray (3×3)
    R: Any  # np.ndarray (3×3)
    C: Any  # np.ndarray (3,)
    dist: list[float] | None
    # The calibration snapshot's image dims at cache time, so a later
    # pitch arriving at a different resolution can detect the mismatch.
    # For the live path both devices stream from the same intrinsics
    # cached at armed time, so this is advisory — a mismatch means the
    # caller should re-scale rather than trust the cached K verbatim.
    image_wh: tuple[int, int]


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
    # Per-cam cached geometry. Populated on first ingest per camera by
    # state.ingest_live_frame; reused across the 8 ms pair loop so the
    # hot path skips per-frame pitch construction + SVD extrinsics.
    camera_poses: dict[str, CameraPose] = field(default_factory=dict)

    def ingest(
        self,
        cam: str,
        frame: FramePayload,
        triangulate_pair: Callable[[str, FramePayload, FramePayload], TriangulatedPoint | None],
        anchors: dict[str, float | None] | None = None,
    ) -> list[TriangulatedPoint]:
        """Buffer one frame and pair it against the most recent peer-cam
        frames within `window_s`.

        `anchors` (optional) — `{cam_id: sync_anchor_timestamp_s}`. When
        supplied, the cross-cam window check uses anchor-relative time
        (`frame.timestamp_s − anchors[cam]`) instead of raw `timestamp_s`.
        Required when each camera reports its own device-local clock (each
        iPhone's `mach_absolute_time` since boot, so two phones sit tens of
        thousands of seconds apart). Stored frames keep raw timestamps —
        only the dt comparison is anchor-shifted, so downstream persistence
        and triangulation see the original values."""
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
        own_anchor = (anchors or {}).get(cam)
        peer_anchor = (anchors or {}).get(other)
        # Drop any anchor offset only when both cams have one — partial
        # adjustment would be worse than none.
        adjust = own_anchor is not None and peer_anchor is not None
        own_t = frame.timestamp_s - own_anchor if adjust else frame.timestamp_s
        candidates = self.buffers.setdefault(other, deque())
        created: list[TriangulatedPoint] = []
        for peer in reversed(candidates):
            peer_t = peer.timestamp_s - peer_anchor if adjust else peer.timestamp_s
            dt = peer_t - own_t
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
