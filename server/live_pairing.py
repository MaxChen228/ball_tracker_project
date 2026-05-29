from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from candidate_selector import Candidate, score_candidates
from pairing import _MAX_DT_S as _SERVER_POST_WINDOW_S
from pairing_tuning import PairingTuning
from schemas import FramePayload, TriangulatedPoint

logger = logging.getLogger("ball_tracker")


# Sentinel for "no sync anchors known yet" — an empty dict, so any
# `anchors.get(cam_id)` returns None explicitly without silently falling
# through `(anchors or {})` style guards. Production callers
# (`state_detection.ingest_live_frame`) build a real dict keyed by the
# rig's camera_ids. Test fixtures / pre-arm callers default to {}.
NO_ANCHORS: dict[str, float | None] = {}


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
    """Incremental cross-camera pairing over a bounded rolling window.

    Pair-based today (one ingest produces 0–N pairs against the single
    peer cam's buffer), but the per-cam dicts are camera_id-keyed so
    a future N-camera live session can grow buffers organically as new
    cameras stream their first frame."""

    session_id: str
    # Cross-cam pairing window. Match the server_post pairing constant
    # (`pairing._MAX_DT_S`, default 1/120 ≈ 8.33 ms) so live and
    # server_post don't silently disagree on which cross-cam frame pairs
    # are eligible — that mismatch was the subtle bug behind a 5 % drop
    # in live-pair yield vs. reprocess for the same MOVs.
    window_s: float = _SERVER_POST_WINDOW_S
    max_frames_per_cam: int = 500
    # Per-cam buffers grow lazily on first ingest. Empty dict is the
    # "no frame seen yet from any camera" baseline; ingest() uses
    # `setdefault` to materialize the deque on first touch per cam_id.
    buffers: dict[str, deque[FramePayload]] = field(default_factory=dict)
    frames_by_cam: dict[str, list[FramePayload]] = field(default_factory=dict)
    frame_counts: dict[str, int] = field(default_factory=dict)
    triangulated: list[TriangulatedPoint] = field(default_factory=list)
    # Dedupe key for already-triangulated candidate pairs, keyed by
    # (cam_lo, cam_hi, a_frame_idx, b_frame_idx, ca_idx, cb_idx) — the
    # candidate-index half is canonicalized so position [4] always
    # references the lexically-lower cam's candidate index and [5] the
    # higher's, regardless of which cam triggered the ingest call (mirrors
    # the lower-cam-first canonicalization the ingest loop applies to the
    # frame-index half). The cam-pair prefix keys the dedup PER PAIR so
    # that, with N≥3 cams, the same A-frame paired against B and against C
    # produces two distinct entries instead of one masking the other.
    paired_frame_ids: set[tuple[str, str, int, int, int, int]] = field(default_factory=set)
    completed_cameras: set[str] = field(default_factory=set)
    abort_reasons: dict[str, str] = field(default_factory=dict)
    # Per-cam cached geometry. Populated on first ingest per camera by
    # state.ingest_live_frame; reused across the 8 ms pair loop so the
    # hot path skips per-frame pitch construction + SVD extrinsics.
    camera_poses: dict[str, CameraPose] = field(default_factory=dict)
    # Operator-tunable gap threshold, refreshed by state.ingest_live_frame
    # on every ingest. `PairingTuning.gap_threshold_m` (default 0.20 m,
    # per-session override via the viewer's Gap slider →
    # /sessions/{sid}/recompute) gates the stamped-tuning filter applied
    # before segmenter consumption. Cost is no longer here — each
    # algorithm owns its own threshold via
    # `algorithms.cost_threshold_for_algorithm`.
    pairing_tuning: PairingTuning = field(default_factory=PairingTuning.default)
    # Detection-config snapshot frozen at arm time (or at first
    # ingest_live_frame when arm pre-stamping was bypassed by a test
    # fixture / direct-construct path). Carries through to
    # `PitchPayload.live_config_used` and onto
    # `SessionResult.live_config_used`. None when no live frame has
    # flowed yet (server_post-only flow / unstamped test fixture).
    live_config_used: "DetectionConfigSnapshotPayload | None" = None

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

        iOS sends `aspect` and `fill` on every candidate — the live
        path runs the same 3-axis shape cost as server_post.
        """
        cands = frame.candidates
        if not cands:
            return frame.model_copy(update={"px": None, "py": None, "ball_detected": False})
        selector_in = [
            Candidate(cx=c.px, cy=c.py, area=c.area, aspect=c.aspect, fill=c.fill)
            for c in cands
        ]
        costs = score_candidates(selector_in)
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
        triangulate_pair: Callable[[str, str, FramePayload, FramePayload], list[TriangulatedPoint]],
        anchors: dict[str, float | None] = NO_ANCHORS,
    ) -> list[TriangulatedPoint]:
        """Buffer one frame and pair it against the most recent peer-cam
        frames within `window_s`, for EVERY peer cam independently (the
        pair-as-atom live path: with N cams the incoming frame is matched
        against each of the N−1 peers, each producing its own pair's
        points). The callback receives `(cam_lo, cam_hi, frame_lo,
        frame_hi)` with cams canonicalized lexically so it can look up the
        right per-camera pose.

        `anchors` — `{cam_id: sync_anchor_timestamp_s_or_None}`. Defaults
        to module-level `NO_ANCHORS` (empty dict — `anchors.get(cam)`
        returns None for any unknown cam, no silent KeyError) so test
        fixtures and pre-arm callers don't accidentally trip the
        `(anchors or {})` fallback pattern. The cross-cam
        window check uses anchor-relative time (`frame.timestamp_s −
        anchors[cam]`) when both entries are non-None. Required for the
        anchor-relative path because each camera reports its own
        device-local clock (each iPhone's `mach_absolute_time` since
        boot, so two phones sit tens of thousands of seconds apart) —
        without anchors we'd be window-matching across that clock skew.
        Stored frames keep raw timestamps — only the dt comparison is
        anchor-shifted, so downstream persistence and triangulation see
        the original values. Per-cam value may be None when that cam
        hasn't received its sync chirp yet (partial-anchor branch)."""
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

            # Peer cams = every other cam_id whose buffer has frames.
            # 0 peers (only this cam has streamed) → no pairs yet. With
            # N≥2 peers we run the window match against EACH peer — the
            # pair-as-atom live path: the incoming frame contributes to
            # every (cam, peer) pair independently.
            peer_cams = [c for c in self.buffers if c != cam and self.buffers[c]]
            if not peer_cams:
                return []
            own_anchor = anchors.get(cam)
            created: list[TriangulatedPoint] = []
            for other in peer_cams:
                peer_anchor = anchors.get(other)
                if (own_anchor is None) != (peer_anchor is None):
                    # Partial anchor (this cam synced, peer not, or vice
                    # versa) — refusing to match on raw timestamps; the two
                    # devices' clocks sit ~10⁴ s apart so any pair would be
                    # garbage. Skip THIS peer (others may be fully synced).
                    logger.info(
                        "live_pairing: partial anchor session=%s cam=%s peer=%s "
                        "(own=%s peer=%s) — skipping this pair",
                        self.session_id, cam, other,
                        own_anchor is not None, peer_anchor is not None,
                    )
                    continue
                adjust = own_anchor is not None and peer_anchor is not None
                own_t = frame.timestamp_s - own_anchor if adjust else frame.timestamp_s
                candidates = self.buffers.setdefault(other, deque())
                # Canonicalize cam ordering lexically so the callback always
                # sees (cam_lo, cam_hi, frame_lo, frame_hi) and can resolve
                # the correct per-camera pose for either direction.
                cam_lo, cam_hi = (cam, other) if cam < other else (other, cam)
                for peer in reversed(candidates):
                    peer_t = peer.timestamp_s - peer_anchor if adjust else peer.timestamp_s
                    dt = peer_t - own_t
                    if dt < -self.window_s:
                        break
                    if abs(dt) > self.window_s or not peer.ball_detected:
                        continue
                    if cam < other:
                        frame_lo, frame_hi = frame, peer
                    else:
                        frame_lo, frame_hi = peer, frame
                    lo_frame_idx, hi_frame_idx = frame_lo.frame_index, frame_hi.frame_index
                    points = triangulate_pair(cam_lo, cam_hi, frame_lo, frame_hi)
                    if not points:
                        continue
                    for pt in points:
                        # Index pair is lo-first / hi-second by construction
                        # — source_a_cand_idx stamped from the callback's
                        # loop over frame_lo.candidates.
                        dedup_key = (
                            cam_lo, cam_hi, lo_frame_idx, hi_frame_idx,
                            pt.source_a_cand_idx if pt.source_a_cand_idx is not None else -1,
                            pt.source_b_cand_idx if pt.source_b_cand_idx is not None else -1,
                        )
                        if dedup_key in self.paired_frame_ids:
                            continue
                        self.paired_frame_ids.add(dedup_key)
                        self.triangulated.append(pt)
                        created.append(pt)
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
