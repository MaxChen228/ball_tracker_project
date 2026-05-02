"""Hybrid PROD-first + V11-loose-fallback detector (28d_hybrid from
lab/research PR #112).

Per-frame:
- If PROD (tight HSV + tight shape gate) emits ≥1 candidate → rank by
  shape cost and take top-1.
- Else → run V11 (loose HSV + morphology CLOSE + loose shape gate) and
  rank surviving candidates by (motion-persistence ASC, shape cost ASC).
  Persistence = how many of the ±neigh_half surrounding frames have a
  V11 candidate within match_px of this one. Static distractors that
  appear in many neighbors get demoted; truly motion-novel candidates
  rise to the top.

Lab eval on 1956 GT frames across 15 sessions (`27c_R_topK.json` /
`28d_hybrid.json`):
- PROD baseline R_top1 = 0.615
- This detector R_top1 = 0.660 (+0.045, 0/15 session regressions)
- 27.9% rescue rate on PROD's 28% emit-miss subset

Two passes are required because the persistence rerank for the V11
fallback path needs to see both directions of the ±neigh_half window.
The first pass emits raw PROD + V11 candidates per frame; the second
pass does the per-frame rerank. Decode cost is the dominant term — the
two HSV passes per frame are sub-millisecond on 1080p.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from algorithms.base import (
    CancelCheck,
    Detector,
    FrameIteratorFactory,
    ProgressCallback,
)
from schemas import BlobCandidate, FramePayload, HSVRangePayload, ShapeGatePayload

if TYPE_CHECKING:
    pass


logger = logging.getLogger(__name__)


# Per-cell area gates. Same defaults as `detection._MIN_AREA_PX` /
# `_MAX_AREA_PX` — the ball-size envelope is algorithm-independent at
# our 1080p / 240 fps rig. Hardcoded here rather than made tunable so
# operator misconfig of one detector can't silently widen the area
# envelope on the others.
_MIN_AREA_PX = 20
_MAX_AREA_PX = 150_000


class Hybrid28dParams(BaseModel):
    """Per-call params for `Hybrid28dDetector`. Two HSV cubes (PROD
    tight + V11 loose), each with its own shape gate. `v11_close_kernel`
    is the morphological CLOSE size the loose fallback applies to the
    HSV mask before connected components — bridges thin gaps in the
    deeper-blue ball's HSV mask that v11_hsv_cc's no-morphology pipeline
    drops. `neigh_half` + `match_px` are the temporal-persistence rerank
    knobs (see module docstring).

    Defaults match `lab/research/scripts/28d_hybrid.py` constants —
    physics-derived, not per-session tuned: NEIGH_HALF=6 ≈ 50ms at
    240fps (long enough that a real ball has moved >> match radius);
    MATCH_PX=5.0 ≈ CC centroid noise scale on this rig."""
    model_config = ConfigDict(extra="forbid")
    prod_hsv: HSVRangePayload
    prod_shape: ShapeGatePayload
    v11_hsv: HSVRangePayload
    v11_shape: ShapeGatePayload
    v11_close_kernel: int = Field(default=3, ge=1, le=9)
    neigh_half: int = Field(default=6, ge=1, le=30)
    match_px: float = Field(default=5.0, ge=0.5, le=50.0)


def _emit_candidates(
    bgr: np.ndarray,
    hsv: HSVRangePayload,
    shape: ShapeGatePayload,
    *,
    close_kernel: int | None,
) -> list[BlobCandidate]:
    """Single-pass HSV + (optional) morphology CLOSE + connected-
    components + shape gate + cost-stamping. Returns every survivor
    with `cost` populated — caller decides which subset / ordering to
    emit. `close_kernel=None` skips morphology (PROD); a small odd int
    runs `cv2.MORPH_CLOSE` with an elliptical kernel of that size (V11
    loose).

    Mirror of `detection.detect_ball_with_candidates` but stripped of
    the winner-select responsibility — Hybrid28dDetector does its own
    cross-pool ranking."""
    from candidate_selector import Candidate, score_candidates

    if bgr is None or bgr.size == 0:
        return []
    hsv_image = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([hsv.h_min, hsv.s_min, hsv.v_min], dtype=np.uint8)
    hi = np.array([hsv.h_max, hsv.s_max, hsv.v_max], dtype=np.uint8)
    mask = cv2.inRange(hsv_image, lo, hi)
    if close_kernel is not None:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (close_kernel, close_kernel),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    n, _, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return []

    survivors: list[Candidate] = []
    shape_stats: list[tuple[float, float]] = []
    for idx in range(1, n):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < _MIN_AREA_PX or area > _MAX_AREA_PX:
            continue
        w = int(stats[idx, cv2.CC_STAT_WIDTH])
        h = int(stats[idx, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        aspect = min(w, h) / max(w, h)
        if aspect < shape.aspect_min:
            continue
        fill = area / (w * h)
        if fill < shape.fill_min:
            continue
        cx, cy = centroids[idx]
        survivors.append(Candidate(
            cx=float(cx), cy=float(cy), area=area,
            aspect=aspect, fill=fill,
        ))
        shape_stats.append((aspect, fill))

    if not survivors:
        return []
    max_area_batch = max(c.area for c in survivors)
    costs = score_candidates(survivors)
    return [
        BlobCandidate(
            px=c.cx, py=c.cy, area=c.area,
            area_score=c.area / max_area_batch if max_area_batch > 0 else 0.0,
            aspect=float(asp), fill=float(fl),
            cost=float(cost),
        )
        for c, (asp, fl), cost in zip(survivors, shape_stats, costs)
    ]


def _persistence(
    cand: BlobCandidate,
    neighbor_cand_lists: list[list[BlobCandidate]],
    match_px: float,
) -> int:
    """Count how many of `neighbor_cand_lists` contain at least one
    candidate within `match_px` of `cand`. Higher = candidate appears
    repeatedly across the temporal window = more likely a static
    distractor (table edge, shadow, paint mark). Sorted ASC so
    motion-novel candidates rise to the top."""
    tol2 = match_px * match_px
    cx, cy = cand.px, cand.py
    n = 0
    for cl in neighbor_cand_lists:
        for nc in cl:
            if (nc.px - cx) ** 2 + (nc.py - cy) ** 2 <= tol2:
                n += 1
                break
    return n


class Hybrid28dDetector(Detector):
    """28d_hybrid from PR #112. PROD-first emit; V11+motion-novelty
    rescue when PROD is empty.

    Two passes: pass 1 collects raw PROD + V11 candidates per frame
    (the temporal persistence rerank needs the whole stream). Pass 2
    decides each frame's winner. `progress` fires per pass-1 frame —
    pass 2 is in-memory and runs in <100ms on 5000-frame sessions.
    `should_cancel` is checked between every pass-1 frame; pass 2 is
    not interrupted (would leave the result half-formed without
    saving anything)."""
    params_schema: type[BaseModel] = Hybrid28dParams

    def detect(
        self,
        video_path: Path,
        video_start_pts_s: float,
        params: Hybrid28dParams,
        *,
        frame_iter: FrameIteratorFactory | None = None,
        should_cancel: CancelCheck | None = None,
        progress: ProgressCallback | None = None,
    ) -> list[FramePayload]:
        from pipeline import ProcessingCanceled
        from video import iter_frames

        iterator = iter_frames if frame_iter is None else frame_iter

        # --- Pass 1: per-frame raw emit ---------------------------------
        # Stored as parallel lists to avoid keeping the BGR frames live
        # past their decoded turn (memory-bounded by the Python list
        # overhead alone, not the pixel buffers).
        timestamps: list[float] = []
        prod_per_frame: list[list[BlobCandidate]] = []
        v11_per_frame: list[list[BlobCandidate]] = []

        for idx, (absolute_pts_s, bgr) in enumerate(
            iterator(video_path, video_start_pts_s)
        ):
            if should_cancel is not None and should_cancel():
                raise ProcessingCanceled(
                    f"hybrid_28d detection canceled for {video_path.name}"
                )
            if progress is not None:
                progress(idx)
            timestamps.append(absolute_pts_s)
            prod_per_frame.append(_emit_candidates(
                bgr, params.prod_hsv, params.prod_shape, close_kernel=None,
            ))
            v11_per_frame.append(_emit_candidates(
                bgr, params.v11_hsv, params.v11_shape,
                close_kernel=params.v11_close_kernel,
            ))

        n_frames = len(timestamps)
        # --- Pass 2: per-frame rerank + winner select ------------------
        out: list[FramePayload] = []
        rescue_attempted = 0
        rescue_emitted = 0
        for idx in range(n_frames):
            prod_blobs = prod_per_frame[idx]
            v11_blobs = v11_per_frame[idx]
            chosen: list[BlobCandidate]
            if prod_blobs:
                # PROD path — already cost-stamped; sort ASC so the
                # cheapest blob is `candidates[0]` for downstream.
                chosen = sorted(prod_blobs, key=lambda b: b.cost or 0.0)
            elif v11_blobs:
                rescue_attempted += 1
                # V11 fallback path. Build neighbor cand lists in
                # ±neigh_half window (skipping idx itself), then sort
                # by (persistence ASC, shape cost ASC).
                lo_idx = max(0, idx - params.neigh_half)
                hi_idx = min(n_frames - 1, idx + params.neigh_half)
                neigh = [
                    v11_per_frame[j]
                    for j in range(lo_idx, hi_idx + 1)
                    if j != idx
                ]
                chosen = sorted(
                    v11_blobs,
                    key=lambda b: (
                        _persistence(b, neigh, params.match_px),
                        b.cost or 0.0,
                    ),
                )
                rescue_emitted += 1
            else:
                chosen = []

            winner = chosen[0] if chosen else None
            out.append(FramePayload(
                frame_index=idx,
                timestamp_s=timestamps[idx],
                px=winner.px if winner is not None else None,
                py=winner.py if winner is not None else None,
                ball_detected=winner is not None,
                # Always emit the full chosen list — empty when neither
                # PROD nor V11 had survivors. Downstream pairing fans
                # out every cand × cand combination; trimming here would
                # silently narrow the cross-cam search space.
                candidates=chosen,
            ))

        ball_frames = sum(1 for f in out if f.ball_detected)
        logger.info(
            "hybrid_28d video=%s frames=%d ball=%d rescue=%d/%d "
            "prod_hsv=h[%d-%d] v11_hsv=h[%d-%d]",
            video_path.name, n_frames, ball_frames,
            rescue_emitted, rescue_attempted,
            params.prod_hsv.h_min, params.prod_hsv.h_max,
            params.v11_hsv.h_min, params.v11_hsv.h_max,
        )
        return out
