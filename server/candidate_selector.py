"""Pick the best ball candidate using a track-independent shape prior.

Replaces the previous temporal-prior cost. The earlier design combined
`area_score` (relative dominance in this frame's batch) with a distance
cost from the predicted-next-position of the previous winner. That
created a positive-feedback loop: a wrongly-picked first winner kept
its lead because every subsequent frame was scored against the
contaminated `prev_position`. Confirmed in two production sessions
(`s_f50fd07f`, `s_962a7db9`) where a stable HSV-passing distractor
locked the selector for the entire pitch.

The new cost is **frame-local and track-independent** — it judges each
candidate purely on shape signals (size, aspect, fill) compared to the
known-ball prior. There is no `prev_position` input; nothing to
contaminate. A wrong pick on one frame does not affect the next.

Cost formula:

    cost = w_size · size_pen + w_aspect · aspect_pen + w_fill · fill_pen

Each component is normalized into [0, 1]:

- `size_pen` — log-octave distance from `expected_area = π · r_px²`.
  An order-of-magnitude off saturates the penalty.
- `aspect_pen` — `(1 - aspect)` normalized so a perfectly-square blob
  (aspect=1) costs 0 and a barely-passing one (aspect ≈ 0.5) costs 1.
- `fill_pen` — `|fill - 0.68|` normalized; 0.68 is the empirical
  median for the project ball (memory: project_ball_empirical_fill).

Unknown shape (`aspect=None` or `fill=None`) maps to **zero penalty**
on that axis. iOS-sourced live candidates predate aspect/fill in the
wire schema and arrive with both as None — this design choice is
explicit (a known unknown is treated as neutral, not as bad). Once
iOS starts shipping shape stats, this clause becomes vestigial.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Empirical median fill for the project blue ball (memory:
# project_ball_empirical_fill). Held as a module constant rather than
# a tuning knob because it's a property of the ball, not an operator
# choice.
_FILL_TYPICAL = 0.68

# Aspect-penalty normalization floor. A perfectly square blob has
# aspect=1 (penalty 0); blobs with aspect ≤ this floor saturate the
# penalty at 1. Held constant: tighter than the runtime shape gate
# (typically 0.56) so candidates near the gate already score badly.
_ASPECT_PEN_FLOOR = 0.5


@dataclass(frozen=True)
class CandidateSelectorTuning:
    """Operator-tunable shape-prior weights for `select_best_candidate`.

    Owned by `state.candidate_selector_tuning()`; persisted to
    `data/candidate_selector_tuning.json`. All four fields must be
    supplied — no module-level fallbacks, so callers cannot silently
    inherit a stale magic number.

    - `r_px_expected` — expected ball radius in pixels. Defines the
      target area `π·r²` against which `size_pen` is computed.
    - `w_size` / `w_aspect` / `w_fill` — penalty weights. Should sum
      to ≤ 1 for the cost to live in [0, 1], but unconstrained here so
      the operator can dial heavier than-unit total if they want a
      sharper preference (the argmin is invariant under positive
      scaling anyway).
    """

    r_px_expected: float
    w_size: float
    w_aspect: float
    w_fill: float

    @classmethod
    def default(cls) -> "CandidateSelectorTuning":
        # Validated on 11 production sessions in dry_run_shape.py: shape
        # mode hits live ground truth on all 10 stable sessions and
        # recovers s_962a7db9 from 0 segs to 84.3 kph (live truth: 84.2).
        return cls(
            r_px_expected=12.0,
            w_size=0.5,
            w_aspect=0.3,
            w_fill=0.2,
        )


@dataclass
class Candidate:
    cx: float
    cy: float
    area: int
    # Optional shape stats. Server-side detection always populates both
    # (it has the bbox). iOS-sourced candidates leave them None until
    # iOS adopts the new wire format — the cost function treats None as
    # "neutral" on that axis (see module docstring).
    aspect: float | None = None
    fill: float | None = None


def score_candidates(
    candidates: list[Candidate],
    tuning: CandidateSelectorTuning,
) -> list[float]:
    """Return one cost per candidate, in input order. Lower is more
    ball-like. Empty input → empty output.

    Caller invariant: every candidate has `area > 0`. Production callers
    enforce this via `MIN_AREA = 15` in `detection.py`; direct unit-test
    callers must pass positive areas (size_pen uses `log2(area / r²)`
    and would raise `ValueError` on `area=0`)."""
    if not candidates:
        return []
    expected_area = math.pi * tuning.r_px_expected * tuning.r_px_expected
    aspect_denom = max(1.0 - _ASPECT_PEN_FLOOR, 1e-6)
    out: list[float] = []
    for c in candidates:
        # log-octave distance, saturates at 4× off (=2 octaves)
        size_pen = min(abs(math.log2(c.area / expected_area)) / 2.0, 1.0)

        if c.aspect is None:
            aspect_pen = 0.0
        else:
            aspect_pen = max(0.0, min((1.0 - c.aspect) / aspect_denom, 1.0))

        if c.fill is None:
            fill_pen = 0.0
        else:
            fill_pen = min(abs(c.fill - _FILL_TYPICAL) / _FILL_TYPICAL, 1.0)

        out.append(
            tuning.w_size * size_pen
            + tuning.w_aspect * aspect_pen
            + tuning.w_fill * fill_pen
        )
    return out


def select_best_candidate(
    candidates: list[Candidate],
    tuning: CandidateSelectorTuning,
) -> Candidate | None:
    """Pick the lowest-cost candidate. Returns None iff `candidates`
    is empty. Tie-break: first candidate at the minimum (Python `min`
    is stable on `range`)."""
    if not candidates:
        return None
    costs = score_candidates(candidates, tuning)
    return candidates[min(range(len(costs)), key=lambda i: costs[i])]
