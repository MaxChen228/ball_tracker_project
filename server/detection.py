"""Server-side ball detection.

The iPhone uploads a raw H.264 MOV per cycle; this module runs an HSV
threshold → connected-components → largest-blob pipeline on each decoded
frame to recover `(px, py)` in image space. The result feeds `pipeline.py`
which synthesises `FramePayload`s for the existing `triangulate_cycle` path.

HSV defaults target a yellow tennis ball (the fluorescent yellow-green
ball currently used on the rig). Override via the `BALL_TRACKER_HSV_RANGE`
env var (comma-separated `hMin,hMax,sMin,sMax,vMin,vMax`) if you change
the ball — e.g. the deep-blue ball on the rig uses
`105,112,140,255,40,255` (dashboard's "blue ball" preset, narrowed
through 2026-04 from the original h 100-130 after measuring the
actual ball's hue band; v_min stays at 40 to retain the ball's
shadowed underside, otherwise the mask collapses to a thin highlight
arc when the ball is close to the camera).
"""
from __future__ import annotations

import functools
import logging
import math
import os
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# Cached np.uint8 bounds for a given (h_min, s_min, v_min) / (h_max, s_max, v_max)
# tuple. Production hits at most a few distinct ranges (one preset at a time);
# the cache stops cv2.inRange from re-allocating two 3-byte uint8 arrays on
# every frame inside the offline batch reprocess loop.
@functools.lru_cache(maxsize=32)
def _hsv_bounds(
    h_min: int, h_max: int, s_min: int, s_max: int, v_min: int, v_max: int,
) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.array([h_min, s_min, v_min], dtype=np.uint8),
        np.array([h_max, s_max, v_max], dtype=np.uint8),
    )


@dataclass(frozen=True)
class HSVRange:
    h_min: int
    h_max: int
    s_min: int
    s_max: int
    v_min: int
    v_max: int

    @classmethod
    def default(cls) -> "HSVRange":
        # Default fallback HSV (fluorescent yellow-green tennis ball).
        # The rig actually runs the `blue_ball` preset; this default is
        # for headless tests and first-boot. OpenCV H range is 0-179;
        # tennis-ball hue sits ~25-55 (lime-yellow to yellow-green). High
        # S/V filters out pale wood floor and warm wall tones.
        return cls(h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255)

    @classmethod
    def from_env(cls) -> "HSVRange":
        raw = os.environ.get("BALL_TRACKER_HSV_RANGE", "").strip()
        if not raw:
            return cls.default()
        try:
            parts = [int(x) for x in raw.split(",")]
            if len(parts) != 6:
                raise ValueError(f"expected 6 ints, got {len(parts)}")
            return cls(*parts)
        except Exception as e:
            logger.warning(
                "BALL_TRACKER_HSV_RANGE=%r parse failed (%s) — using default yellow-green",
                raw, e,
            )
            return cls.default()

    def lo(self) -> np.ndarray:
        return _hsv_bounds(self.h_min, self.h_max, self.s_min, self.s_max, self.v_min, self.v_max)[0]

    def hi(self) -> np.ndarray:
        return _hsv_bounds(self.h_min, self.h_max, self.s_min, self.s_max, self.v_min, self.v_max)[1]


# Minimum / maximum area (in pixels) a candidate blob must have. 1080p
# = 2.07 M px so 150_000 = 7.2% of frame — unrealistically large for a
# ball in flight, but kept as a loose cap. The geometry-derived radius
# prior was removed 2026-04: it silently rejected real balls when the
# ball was closer to the camera than the plate (e.g. rolling on the
# floor between batter and 1B/3B-line cam) and when the hardcoded
# tennis-ball radius (3.3 cm) didn't match the actual blue hardball
# (~3.9 cm). shape_gate + temporal selector handle clutter without it.
_MIN_AREA_PX = 20
_MAX_AREA_PX = 150_000

# Shape gates against yellow-green clutter (cardboard, clothes, pale floor
# tiles) that slip through HSV. A real ball — even one in flight — stays
# very close to a filled circle on our rig; operator confirmed motion blur
# at 240 fps causes only mild ellipsing. Tuned loose enough to keep those
# through, tight enough to drop clothing folds and elongated reflections.
# Runtime-overridable via `ShapeGate` (state.shape_gate()); these constants
# are the defaults used when no override is supplied.
_MIN_ASPECT = 0.70  # min(w,h)/max(w,h); 1.0 = square bbox, 0.70 ≈ 3:2
# Theoretical circle fill = π/4 ≈ 0.785 but empirical mask fill for
# real balls on our rig sits at 0.63-0.70 (median 0.68 across
# s_fcf73afa/s_03d533c4) because ball-side shadows, the seam, and HSV
# edge bleed each carve ~10-15% out of the bbox. 0.55 sits a safety
# margin below the lowest observed ball (0.63) and catches marginal
# frames that 0.60 was just barely rejecting. p50=0.68 empirical gives
# 0.13 of headroom which is ~2σ in our measured distribution. The
# legacy MOG2-AND-HSV combined-mask measurement is gone post-Phase-A;
# the values quoted here were re-checked against pure-HSV masks and
# match (MOG2 was removing motion not bbox interior, so it didn't
# meaningfully shift fill).
_MIN_FILL = 0.55


@dataclass(frozen=True)
class ShapeGate:
    """Operator-tunable aspect/fill thresholds for the HSV blob filter.

    Kept separate from the module-level `_MIN_ASPECT` / `_MIN_FILL`
    fallbacks so callers that don't plumb state (tests, offline scripts)
    still work. `state.shape_gate()` produces a snapshot; the dashboard
    and iOS both receive it so the `live` / `server_post` paths agree.
    """

    aspect_min: float
    fill_min: float

    @classmethod
    def default(cls) -> "ShapeGate":
        return cls(aspect_min=_MIN_ASPECT, fill_min=_MIN_FILL)


def detect_ball_with_candidates(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    *,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    shape_gate: ShapeGate | None = None,
    selector_tuning: "CandidateSelectorTuning | None" = None,
) -> tuple["BlobCandidate | None", "list[BlobCandidate]"]:
    """HSV → CC → shape gate → temporal selector. Returns
    `(winner_or_None, scored_blobs)` where `scored_blobs` is every
    survivor with `area_score` and selector `cost` stamped — same shape
    `live_pairing._resolve_candidates` produces, so server_post can feed
    `FramePayload.candidates` directly into the viewer's BLOBS overlay.

    Empty / no-survivors → `(None, [])`. Both lists ride the same
    decision: if any candidate exists, the lowest-cost one is the winner.
    """
    from candidate_selector import Candidate, CandidateSelectorTuning, score_candidates
    from schemas import BlobCandidate

    if frame_bgr is None or frame_bgr.size == 0:
        return None, []
    min_area, max_area = _MIN_AREA_PX, _MAX_AREA_PX
    gate = shape_gate if shape_gate is not None else ShapeGate.default()
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, hsv_range.lo(), hsv_range.hi())

    # Connected components with stats (label 0 is the background).
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return None, []

    tuning = selector_tuning if selector_tuning is not None else CandidateSelectorTuning.default()

    survivors: list[Candidate] = []
    for idx in range(1, num_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < min_area or area > max_area:
            continue
        w = int(stats[idx, cv2.CC_STAT_WIDTH])
        h = int(stats[idx, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        aspect = min(w, h) / max(w, h)
        if aspect < gate.aspect_min:
            continue
        fill = area / (w * h)
        if fill < gate.fill_min:
            continue
        cx, cy = centroids[idx]
        survivors.append(
            Candidate(cx=float(cx), cy=float(cy), area=area, area_score=0.0)
        )

    if not survivors:
        return None, []
    max_area_batch = max(c.area for c in survivors)
    scored = [
        Candidate(
            cx=c.cx, cy=c.cy, area=c.area,
            area_score=c.area / max_area_batch if max_area_batch > 0 else 0.0,
        )
        for c in survivors
    ]
    costs = score_candidates(
        scored,
        prev_position=prev_position,
        prev_velocity=prev_velocity,
        dt=dt,
        r_px_expected=tuning.r_px_expected,
        w_area=tuning.w_area,
        w_dist=tuning.w_dist,
        dist_cost_sat_radii=tuning.dist_cost_sat_radii,
    )
    blobs = [
        BlobCandidate(
            px=c.cx, py=c.cy, area=c.area,
            area_score=c.area_score, cost=float(cost),
        )
        for c, cost in zip(scored, costs)
    ]
    winner_idx = min(range(len(costs)), key=lambda i: costs[i])
    return blobs[winner_idx], blobs


def detect_ball(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    *,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    shape_gate: ShapeGate | None = None,
    selector_tuning: "CandidateSelectorTuning | None" = None,
) -> tuple[float, float] | None:
    """Thin wrapper around `detect_ball_with_candidates` for callers that
    only want the winner centroid. Returns `(px, py)` or `None`."""
    winner, _ = detect_ball_with_candidates(
        frame_bgr,
        hsv_range,
        prev_position=prev_position,
        prev_velocity=prev_velocity,
        dt=dt,
        shape_gate=shape_gate,
        selector_tuning=selector_tuning,
    )
    if winner is None:
        return None
    return winner.px, winner.py
