"""Server-side ball detection.

The iPhone uploads a raw H.264 MOV per cycle; this module runs an HSV
threshold → connected-components → largest-blob pipeline on each decoded
frame to recover `(px, py)` in image space. The result feeds `pipeline.py`
which synthesises `FramePayload`s for the existing `triangulate_cycle` path.

HSV ranges are operator-controlled via the dashboard preset library —
`data/presets/<name>.json` is the source of truth. `HSVRange.default()`
exists only for headless tests / first-boot; production code paths thread
explicit operator-chosen HSV through. The env-var fallback
(`BALL_TRACKER_HSV_RANGE`) was removed 2026-05 — it silently masked
preset misconfig (operator thinks they're testing blue-ball range but
detection actually ran the env-var range), violating CLAUDE.md
no-silent-fallback.
"""
from __future__ import annotations

import functools
import logging
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


def _run_hsv_emit_pipeline(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    shape_gate: ShapeGate,
    *,
    close_kernel: int | None,
    area_min: int,
) -> "list[BlobCandidate]":
    """Shared HSV → (optional morph CLOSE) → CC → shape gate → cost-stamp
    pipeline. Caller decides ranking / winner-select. `close_kernel=None`
    skips morphology (PROD / v11_hsv_cc); a small odd int runs
    `cv2.MORPH_CLOSE` with that elliptical kernel size (hybrid_28d V11
    fallback). `area_min` is per-pool: v11_hsv_cc and hybrid_28d's PROD
    pool use 20 (ball-sized only), hybrid_28d's V11 pool uses 3 (rescue
    micro-blobs whose persistence rerank can still distinguish them from
    clutter — see `algorithms/hybrid_28d.py:Hybrid28dParams`)."""
    from candidate_selector import Candidate, score_candidates
    from schemas import BlobCandidate

    if frame_bgr is None or frame_bgr.size == 0:
        return []
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, hsv_range.lo(), hsv_range.hi())
    if close_kernel is not None:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (close_kernel, close_kernel),
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8,
    )
    if num_labels <= 1:
        return []

    # survivors and shape_stats append in lockstep within the same loop
    # iteration — order is locked. The downstream zip relies on that.
    survivors: list[Candidate] = []
    shape_stats: list[tuple[float, float]] = []
    for idx in range(1, num_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < area_min or area > _MAX_AREA_PX:
            continue
        w = int(stats[idx, cv2.CC_STAT_WIDTH])
        h = int(stats[idx, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        aspect = min(w, h) / max(w, h)
        if aspect < shape_gate.aspect_min:
            continue
        fill = area / (w * h)
        if fill < shape_gate.fill_min:
            continue
        cx, cy = centroids[idx]
        survivors.append(
            Candidate(cx=float(cx), cy=float(cy), area=area,
                      aspect=aspect, fill=fill)
        )
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


def detect_ball_with_candidates(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    *,
    shape_gate: ShapeGate | None = None,
) -> tuple["BlobCandidate | None", "list[BlobCandidate]"]:
    """v11_hsv_cc per-frame entry. Returns `(winner_or_None,
    scored_blobs)` — `scored_blobs` carries `aspect`, `fill`,
    `area_score`, and selector `cost` stamped, same shape
    `live_pairing._resolve_candidates` produces so server_post can feed
    `FramePayload.candidates` directly into the viewer's BLOBS overlay.

    Winner is the lowest-cost survivor. Empty / no-survivors → `(None,
    [])`. Selector is track-independent — no `prev_position` plumbing."""
    gate = shape_gate if shape_gate is not None else ShapeGate.default()
    blobs = _run_hsv_emit_pipeline(
        frame_bgr, hsv_range, gate,
        close_kernel=None, area_min=_MIN_AREA_PX,
    )
    if not blobs:
        return None, []
    winner_idx = min(range(len(blobs)), key=lambda i: blobs[i].cost)
    return blobs[winner_idx], blobs


def detect_ball(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
    *,
    shape_gate: ShapeGate | None = None,
) -> tuple[float, float] | None:
    """Thin wrapper around `detect_ball_with_candidates` for callers that
    only want the winner centroid. Returns `(px, py)` or `None`."""
    winner, _ = detect_ball_with_candidates(
        frame_bgr,
        hsv_range,
        shape_gate=shape_gate,
    )
    if winner is None:
        return None
    return winner.px, winner.py
