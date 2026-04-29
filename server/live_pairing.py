from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from candidate_selector import Candidate, CandidateSelectorTuning, score_candidates
from schemas import FramePayload, TriangulatedPoint

logger = logging.getLogger("ball_tracker")


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
    # Selector tuning, refreshed by state.ingest_live_frame from the
    # dashboard-controlled `state.candidate_selector_tuning()` on every
    # ingest. Defaults so a session built outside that call (tests) still
    # has a usable tuning instead of None.
    tuning: CandidateSelectorTuning = field(default_factory=CandidateSelectorTuning.default)

    def __post_init__(self) -> None:
        # Internal mutex covering buffers / frames_by_cam / frame_counts /
        # triangulated / paired_frame_ids / camera_poses /
        # completed_cameras / abort_reasons. Two ingest threads
        # (one per cam WS) call into this object concurrently; State holds
        # its own lock only across the lookup, so mutation here MUST
        # serialise itself. RLock because `ingest()` holds the lock while
        # invoking the `triangulate_pair` callback, which legitimately reads
        # back through `camera_pose()` etc. — same thread re-entry, not a
        # cross-thread race.
        self._lock = threading.RLock()

    def _resolve_candidates(self, cam: str, frame: FramePayload) -> FramePayload:
        """Pick the winning candidate using the shape-prior selector.
        Empty candidate list → no detection; px/py = None,
        ball_detected=False.

        Stamps `cost` on every BlobCandidate so the viewer can render
        non-winners ranked by selector cost without re-running the
        selector at view time (which would diverge from "cost actually
        used to pick winner" if dashboard tuning changed).

        iOS-sourced candidates currently lack `aspect`/`fill` (legacy
        wire schema). The shape cost treats those Nones as neutral on
        their respective axes — see candidate_selector module docstring.
        """
        cands = frame.candidates
        if not cands:
            return frame.model_copy(update={"px": None, "py": None, "ball_detected": False})
        selector_in = [
            Candidate(cx=c.px, cy=c.py, area=c.area, aspect=c.aspect, fill=c.fill)
            for c in cands
        ]
        costs = score_candidates(selector_in, self.tuning)
        winner_idx = min(range(len(costs)), key=lambda i: costs[i])
        winner = cands[winner_idx]
        cands_with_cost = [
            c.model_copy(update={"cost": float(cost)})
            for c, cost in zip(cands, costs)
        ]
        return frame.model_copy(update={
            "candidates": cands_with_cost,
            "px": winner.px,
            "py": winner.py,
            "ball_detected": True,
        })

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
        with self._lock:
            # Apply shape-prior candidate selection BEFORE buffering, so
            # downstream pairing + persistence see a single resolved (px, py).
            frame = self._resolve_candidates(cam, frame)
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
            if (own_anchor is None) != (peer_anchor is None):
                # Partial anchor (one cam synced, other not) — refusing to
                # match on raw timestamps; the two devices' clocks sit
                # ~10⁴ s apart so any pair would be garbage. Wait for the
                # second chirp to land.
                logger.info(
                    "live_pairing: partial anchor session=%s cam=%s "
                    "(own=%s peer=%s) — skipping window match",
                    self.session_id, cam,
                    own_anchor is not None, peer_anchor is not None,
                )
                return []
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
        with self._lock:
            self.completed_cameras.add(cam)

    def mark_aborted(self, cam: str, reason: str) -> None:
        with self._lock:
            self.abort_reasons[cam] = reason

    def frames_for_camera(self, cam: str) -> list[FramePayload]:
        with self._lock:
            return list(self.frames_by_cam.get(cam, []))

    def cameras_with_frames(self) -> list[str]:
        with self._lock:
            return [c for c, fs in self.frames_by_cam.items() if fs]

    def frame_counts_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.frame_counts)

    def latest_frame_for(self, cam: str) -> FramePayload | None:
        with self._lock:
            buf = self.frames_by_cam.get(cam)
            return buf[-1] if buf else None

    def update_camera_pose(self, cam: str, pose: Any | None) -> None:
        with self._lock:
            if pose is None:
                self.camera_poses.pop(cam, None)
            else:
                self.camera_poses[cam] = pose

    def camera_pose(self, cam: str) -> Any | None:
        with self._lock:
            return self.camera_poses.get(cam)

    def triangulated_count(self) -> int:
        with self._lock:
            return len(self.triangulated)

    def completed_cameras_snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self.completed_cameras)

    def abort_reasons_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self.abort_reasons)
