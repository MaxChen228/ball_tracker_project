from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from candidate_selector import Candidate, select_best_candidate
from schemas import FramePayload, TriangulatedPoint


# Fallback ball radius (px) used when the inbound frame doesn't carry
# enough info to derive one from blob area. Tennis ball at ~3 m on a
# 1080p main-wide cam ≈ 12-18 px radius; pick the lower end so distance
# cost saturates a touch sooner. The selector saturates at 8r anyway, so
# being off by 2× barely shifts the winner.
_DEFAULT_R_PX_EXPECTED = 12.0


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
    # Per-cam temporal prior state for candidate selection. Mirrors the
    # equal-velocity straight-line model in pipeline.detect_pitch — reset
    # to None on a miss so we don't extrapolate from a stale anchor.
    last_position: dict[str, tuple[float, float]] = field(default_factory=dict)
    last_velocity: dict[str, tuple[float, float]] = field(default_factory=dict)
    last_timestamp_s: dict[str, float] = field(default_factory=dict)

    def _resolve_candidates(self, cam: str, frame: FramePayload) -> FramePayload:
        """Pick the winning candidate using the temporal prior. Empty
        candidate list → no detection; px/py = None, ball_detected=False."""
        cands = frame.candidates
        if not cands:
            return frame.model_copy(update={"px": None, "py": None, "ball_detected": False})
        prev_pos = self.last_position.get(cam)
        prev_vel = self.last_velocity.get(cam)
        prev_t = self.last_timestamp_s.get(cam)
        dt = (
            frame.timestamp_s - prev_t
            if prev_t is not None and math.isfinite(prev_t) else None
        )
        # Re-normalize area_score against this frame's batch — producer
        # may have computed it against a different denominator.
        max_area = max(c.area for c in cands) or 1
        selector_in = [
            Candidate(cx=c.px, cy=c.py, area=c.area, area_score=c.area / max_area)
            for c in cands
        ]
        winner = select_best_candidate(
            selector_in,
            prev_position=prev_pos,
            prev_velocity=prev_vel,
            dt=dt,
            r_px_expected=_DEFAULT_R_PX_EXPECTED,
        )
        # Selector returns None iff input is empty — guarded above.
        assert winner is not None
        return frame.model_copy(update={
            "px": winner.cx,
            "py": winner.cy,
            "ball_detected": True,
        })

    def _update_temporal_prior(self, cam: str, frame: FramePayload) -> None:
        if not frame.ball_detected or frame.px is None or frame.py is None:
            self.last_position.pop(cam, None)
            self.last_velocity.pop(cam, None)
            self.last_timestamp_s.pop(cam, None)
            return
        prev_pos = self.last_position.get(cam)
        prev_t = self.last_timestamp_s.get(cam)
        if prev_pos is not None and prev_t is not None:
            dt_seen = frame.timestamp_s - prev_t
            if dt_seen > 0:
                self.last_velocity[cam] = (
                    (frame.px - prev_pos[0]) / dt_seen,
                    (frame.py - prev_pos[1]) / dt_seen,
                )
        self.last_position[cam] = (frame.px, frame.py)
        self.last_timestamp_s[cam] = frame.timestamp_s

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
        # Apply temporal-prior candidate selection BEFORE buffering, so
        # downstream pairing + persistence see a single resolved (px, py).
        # Frames without a `candidates` field (legacy single-blob path)
        # pass through unchanged.
        frame = self._resolve_candidates(cam, frame)
        self._update_temporal_prior(cam, frame)
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
