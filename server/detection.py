"""Server-side ball detection.

The iPhone uploads a raw H.264 MOV per cycle; this module runs an HSV
threshold → connected-components → largest-blob pipeline on each decoded
frame to recover `(px, py)` in image space. The result feeds `pipeline.py`
which synthesises `FramePayload`s for the existing `triangulate_cycle` path.

HSV defaults target a yellow tennis ball (the fluorescent yellow-green
ball currently used on the rig). Override via the `BALL_TRACKER_HSV_RANGE`
env var (comma-separated `hMin,hMax,sMin,sMax,vMin,vMax`) if you change
the ball — e.g. a deep-blue baseball uses `100,130,140,255,40,255`.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


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
        # Fluorescent yellow-green tennis ball — the ball currently used
        # on the physical rig. OpenCV H range is 0-179; tennis-ball hue
        # sits ~25-55 (lime-yellow to yellow-green). High S/V filters out
        # pale wood floor and warm wall tones.
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
        return np.array([self.h_min, self.s_min, self.v_min], dtype=np.uint8)

    def hi(self) -> np.ndarray:
        return np.array([self.h_max, self.s_max, self.v_max], dtype=np.uint8)


# Fallback minimum / maximum area (in pixels) a candidate blob must
# have when no per-session radius prior is supplied. 1080p = 2.07 M px
# so 150_000 = 7.2% of frame — unrealistically large for a ball in
# flight, but kept as a loose cap for the fallback path.
_MIN_AREA_PX = 20
_MAX_AREA_PX = 150_000

# Multipliers on `expected_radius_px` when a radius prior is supplied.
# `area = π r²` so bounds of r/2 and 1.8r give area ∈ [π(0.5r)², π(1.8r)²].
# 0.5× lower bound catches far-side frames where the ball is smaller
# than the plate-distance estimate; 1.8× upper bound absorbs motion
# blur smearing + perspective foreshortening near the camera.
_RADIUS_PRIOR_MIN_FACTOR = 0.5
_RADIUS_PRIOR_MAX_FACTOR = 1.8


def area_bounds_from_radius_prior(
    expected_radius_px: float,
) -> tuple[int, int]:
    """Compute (min_area_px, max_area_px) from an expected pixel radius.

    Raises `ValueError` on non-finite / non-positive input — **never**
    falls back to the loose defaults silently, per project's no-silent-
    fallback rule. Callers that want the fallback must pass
    `expected_radius_px=None` explicitly.
    """
    if not math.isfinite(expected_radius_px) or expected_radius_px <= 0:
        raise ValueError(
            f"expected_radius_px must be finite positive; got {expected_radius_px!r}"
        )
    area_min = math.pi * (_RADIUS_PRIOR_MIN_FACTOR * expected_radius_px) ** 2
    area_max = math.pi * (_RADIUS_PRIOR_MAX_FACTOR * expected_radius_px) ** 2
    # Never loosen below the default floor — a sub-pixel blob is noise
    # regardless of any prior; never go tighter than sensible. Round to
    # int for cv2 stats comparability.
    return max(_MIN_AREA_PX, int(area_min)), max(int(area_max), _MIN_AREA_PX + 1)

# Shape gates against yellow-green clutter (cardboard, clothes, pale floor
# tiles) that slip through HSV. A real ball — even one in flight — stays
# very close to a filled circle on our rig; operator confirmed motion blur
# at 240 fps causes only mild ellipsing. Tuned loose enough to keep those
# through, tight enough to drop clothing folds and elongated reflections.
# Runtime-overridable via `ShapeGate` (state.shape_gate()); these constants
# are the defaults used when no override is supplied.
_MIN_ASPECT = 0.70  # min(w,h)/max(w,h); 1.0 = square bbox, 0.70 ≈ 3:2
# Theoretical circle fill = π/4 ≈ 0.785 but empirical `combined = hsv AND
# fg_mask` fill for real balls on our rig sits at 0.63-0.70 (median 0.68
# across s_fcf73afa/s_03d533c4) because ball-side shadows, the seam, and
# HSV edge bleed each carve ~10-15% out of the bbox. 0.55 sits a safety
# margin below the lowest observed ball (0.63) and catches marginal
# frames that 0.60 was just barely rejecting. p50=0.68 empirical gives
# 0.13 of headroom which is ~2σ in our measured distribution.
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


def detect_ball(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    *,
    fg_mask: np.ndarray | None = None,
    expected_radius_px: float | None = None,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    shape_gate: ShapeGate | None = None,
) -> tuple[float, float] | None:
    """Find the largest HSV-masked blob whose area is within the active
    area bounds AND whose bbox aspect ratio and fill ratio clear the
    ball-shape gates. Returns `(px, py)` centroid in pixel coordinates,
    else `None`.

    `fg_mask` (uint8 0/255) is optionally AND-ed with the HSV mask — used
    by the pipeline to restrict detection to moving pixels from a
    background subtractor. Pass `None` for HSV-only behaviour.

    `expected_radius_px` narrows the area gate to
    `[π(0.5r)², π(1.8r)²]` — callers derive `r` from calibration (plate
    distance + focal length + real ball radius). Pass `None` to opt into
    the loose `[_MIN_AREA_PX, _MAX_AREA_PX]` fallback (roughly 30×
    looser at the top end). This is **never** a silent fallback — the
    caller chooses; `pipeline.detect_pitch` logs once per session which
    mode it picked.

    Simple-minded on purpose: no morphological ops, no temporal smoothing.
    Anything more aggressive belongs in a follow-up ML-based detector.
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return None
    if expected_radius_px is None:
        min_area, max_area = _MIN_AREA_PX, _MAX_AREA_PX
    else:
        min_area, max_area = area_bounds_from_radius_prior(expected_radius_px)
    gate = shape_gate if shape_gate is not None else ShapeGate.default()
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, hsv_range.lo(), hsv_range.hi())
    if fg_mask is not None:
        mask = cv2.bitwise_and(mask, fg_mask)

    # Connected components with stats (label 0 is the background).
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return None

    from candidate_selector import Candidate, select_best_candidate

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
        return None
    max_area_batch = max(c.area for c in survivors)
    # Finalize area_score now that we know the batch max.
    scored = [
        Candidate(
            cx=c.cx, cy=c.cy, area=c.area,
            area_score=c.area / max_area_batch if max_area_batch > 0 else 0.0,
        )
        for c in survivors
    ]
    winner = select_best_candidate(
        scored,
        prev_position=prev_position,
        prev_velocity=prev_velocity,
        dt=dt,
        r_px_expected=expected_radius_px,
    )
    if winner is None:
        return None
    return winner.cx, winner.cy
