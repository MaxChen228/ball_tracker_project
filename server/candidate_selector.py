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

Tuning knobs are dashboard-owned (`state.candidate_selector_tuning()`).
The four parameters — `r_px_expected` (fallback when caller omits one),
`w_area`, `w_dist`, `dist_cost_sat_radii` — are passed in explicitly by
the caller; this module ships **no** module-level fallback constants.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateSelectorTuning:
    """Hot-tunable parameters for `select_best_candidate`.

    Owned by `state.candidate_selector_tuning()`; persisted to
    `data/candidate_selector_tuning.json`. The four fields:

    - `r_px_expected` — expected ball radius in pixels, used to
      normalize the distance cost. Falls back to this value when the
      caller's `r_px_expected` arg is None.
    - `w_area` — weight on `(1 - area_score)` in the combined cost
      (lower area_score → higher cost).
    - `w_dist` — weight on `dist_cost` in the combined cost. Dashboard
      enforces `w_area = 1 - w_dist` so only one slider is exposed.
    - `dist_cost_sat_radii` — distance cost saturates at this many
      expected-ball-radii; beyond it everything looks equally bad and
      area determines the winner.
    """

    r_px_expected: float
    w_area: float
    w_dist: float
    dist_cost_sat_radii: float

    @classmethod
    def default(cls) -> "CandidateSelectorTuning":
        # 12 px @ 1080p ≈ tennis ball at ~3 m on iPhone main cam (rig
        # baseline). 0.3/0.7 pulls strongly toward temporal prior; 8×
        # radii saturation covers one frame of 200 km/h ball motion at
        # 240 fps with headroom.
        return cls(
            r_px_expected=12.0,
            w_area=0.3,
            w_dist=0.7,
            dist_cost_sat_radii=8.0,
        )


@dataclass
class Candidate:
    cx: float
    cy: float
    area: int
    # Precomputed 0..1 area score: area / max_area_in_batch. Computed
    # by the caller who has the full candidate list.
    area_score: float


def score_candidates(
    candidates: list[Candidate],
    *,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    r_px_expected: float | None = None,
    w_area: float,
    w_dist: float,
    dist_cost_sat_radii: float,
) -> list[float]:
    """Compute the selector cost for every candidate, in input order.

    With temporal prior: `cost = w_area·(1-area_score) + w_dist·dist_cost`.
    Without (first frame, post-miss, dt invalid): pure area fallback,
    `cost = 1 - area_score` — equivalent ranking to "largest area wins"
    because area_score = area / max_area.

    Empty input → empty output. The three scoring weights are required
    keyword-only args — no module defaults so callers cannot silently
    inherit a stale magic number.
    """
    if not candidates:
        return []

    has_temporal = (
        prev_position is not None
        and prev_velocity is not None
        and dt is not None
        and r_px_expected is not None
        and r_px_expected > 0
        and math.isfinite(dt)
        and dt > 0
        and math.isfinite(dist_cost_sat_radii)
        and dist_cost_sat_radii > 0
    )
    if not has_temporal:
        return [1.0 - c.area_score for c in candidates]

    px_pred = prev_position[0] + prev_velocity[0] * dt
    py_pred = prev_position[1] + prev_velocity[1] * dt
    out: list[float] = []
    for c in candidates:
        dx = c.cx - px_pred
        dy = c.cy - py_pred
        dist_radii = math.hypot(dx, dy) / r_px_expected
        dist_cost = min(dist_radii / dist_cost_sat_radii, 1.0)
        out.append(w_area * (1.0 - c.area_score) + w_dist * dist_cost)
    return out


def select_best_candidate(
    candidates: list[Candidate],
    *,
    prev_position: tuple[float, float] | None = None,
    prev_velocity: tuple[float, float] | None = None,
    dt: float | None = None,
    r_px_expected: float | None = None,
    w_area: float,
    w_dist: float,
    dist_cost_sat_radii: float,
) -> Candidate | None:
    """Pick the best candidate (lowest cost from `score_candidates`).

    Returns `None` iff `candidates` is empty.

    Tie-breaking: returns the **first** candidate at the minimum cost
    (Python `min` is stable on `range`). Matches the original loop's
    `if score < best_score` semantics.
    """
    if not candidates:
        return None
    costs = score_candidates(
        candidates,
        prev_position=prev_position,
        prev_velocity=prev_velocity,
        dt=dt,
        r_px_expected=r_px_expected,
        w_area=w_area,
        w_dist=w_dist,
        dist_cost_sat_radii=dist_cost_sat_radii,
    )
    return candidates[min(range(len(costs)), key=lambda i: costs[i])]
