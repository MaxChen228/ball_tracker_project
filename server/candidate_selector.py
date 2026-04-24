"""Select the best ball candidate from a set of CC-stat survivors.

`detect_ball` historically picked the largest-area blob that passed the
area + aspect + fill gates. That works when the ball is the most
prominent yellow-green thing in frame; it fails when a coach's shirt,
the other tripod's yellow sticker, or a backwall reflection squeaks
through and is physically larger than the ball.

This module layers a cheap temporal prior on top: if the previous frame
had a detection, the ball's next position is within ~v_prev × dt of
that position (no acceleration model — equal-velocity straight-line).
We combine a normalized `distance_cost` with the area score so the
correct blob wins whenever a simple size-biased pick would be fooled.

Contract:
- Candidates are supplied already-filtered (area/aspect/fill passed).
- If `prev_position is None` or `prev_velocity is None` or `dt is None`,
  we **explicitly** fall back to largest-area — no silent behaviour,
  no hidden state.
- Distance cost is normalized by `r_px_expected`; unit-less score so
  the area and distance terms are commensurable without tuning per
  resolution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Weight on area score vs distance cost in the combined metric. Both
# terms are clipped to [0, 1] so the weights read as a direct ratio.
# 0.3 / 0.7 pulls strongly toward the temporal prior when it's present
# (the main point of this module); area breaks ties when the prediction
# is ambiguous.
_W_AREA = 0.3
_W_DIST = 0.7

# Distance cost saturates at this many expected-ball-radii away from
# the prediction — beyond this, everything looks equally bad and area
# determines the winner. 8× covers one frame of ball motion at 200 km/h
# with a 30 cm ball radius @ 240 fps (≈ 23 cm = 6.9 r) plus headroom.
_DIST_COST_SAT_RADII = 8.0


@dataclass
class Candidate:
    cx: float
    cy: float
    area: int
    # Precomputed 0..1 area score: area / max_area_in_batch. Computed
    # by the caller who has the full candidate list.
    area_score: float


def select_best_candidate(
    candidates: list[Candidate],
    *,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    r_px_expected: float | None = None,
) -> Candidate | None:
    """Pick the best candidate. See module docstring for the scoring.

    Returns `None` iff `candidates` is empty.
    """
    if not candidates:
        return None

    has_temporal = (
        prev_position is not None
        and prev_velocity is not None
        and dt is not None
        and r_px_expected is not None
        and r_px_expected > 0
        and math.isfinite(dt)
        and dt > 0
    )
    if not has_temporal:
        # Explicit fallback — no prior info, pick by area. Documented
        # (see docstring), not silent.
        return max(candidates, key=lambda c: c.area)

    px_pred = prev_position[0] + prev_velocity[0] * dt
    py_pred = prev_position[1] + prev_velocity[1] * dt

    best: Candidate | None = None
    best_score = math.inf
    for c in candidates:
        dx = c.cx - px_pred
        dy = c.cy - py_pred
        dist_radii = math.hypot(dx, dy) / r_px_expected
        dist_cost = min(dist_radii / _DIST_COST_SAT_RADII, 1.0)
        # Lower is better. Area contributes as `1 - area_score` (big =
        # low cost); distance is already a cost.
        score = _W_AREA * (1.0 - c.area_score) + _W_DIST * dist_cost
        if score < best_score:
            best_score = score
            best = c
    return best
