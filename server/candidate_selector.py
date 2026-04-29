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
candidate purely on shape signals (aspect, fill) compared to the
known-ball prior. There is no `prev_position` input; nothing to
contaminate. A wrong pick on one frame does not affect the next.

**Why no size term?** Earlier revisions had a `size_pen` that scored
candidates by log-octave distance from `expected_area = π · r_px²`.
That assumed the ball has a fixed apparent radius — false. A 240 fps
pitch sweeps from far (~10 m, r ≈ 4 px) to near (~2 m, r ≈ 25 px), so
absolute area is a function of distance, not of "is this object a
ball." The old size_pen saturated to 1.0 across most of the flight
and effectively forced argmin into a coin-flip between candidates
with capped cost. Aspect and fill are scale-invariant geometric
properties of a sphere — they hold from r=4 to r=25 — and they're
the only honest shape signals for a track-independent selector.
Area still gates entry via `_MIN_AREA_PX = 20` in `detection.py` (reject
sub-pixel noise), but it is NOT a cost term.

Cost formula:

    cost = w_aspect · aspect_pen + w_fill · fill_pen

Each component is normalized into [0, 1]:

- `aspect_pen` — `(1 - aspect)` normalized so a perfectly-square blob
  (aspect=1) costs 0 and a barely-passing one (aspect ≈ 0.5) costs 1.
- `fill_pen` — `|fill - 0.68|` normalized; 0.68 is the empirical
  median for the project ball (memory: project_ball_empirical_fill).

Unknown shape (`aspect=None` or `fill=None`) maps to **zero penalty**
on that axis. Both server_post and live (iOS) populate both fields
in this build forward — None only appears when historical pitch JSONs
predating aspect/fill persistence get reloaded for offline analysis.
"""
from __future__ import annotations

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
    `data/candidate_selector_tuning.json`. Both fields must be supplied
    — no module-level fallbacks, so callers cannot silently inherit a
    stale magic number.

    - `w_aspect` / `w_fill` — penalty weights. Should sum to ≤ 1 for
      the cost to live in [0, 1], but unconstrained here so the
      operator can dial heavier than-unit total if they want a
      sharper preference (the argmin is invariant under positive
      scaling anyway).
    """

    w_aspect: float
    w_fill: float

    @classmethod
    def default(cls) -> "CandidateSelectorTuning":
        # Carried over from the prior 0.5/0.3/0.2 size/aspect/fill split:
        # normalize the remaining 0.3/0.2 to sum=1 → 0.6/0.4. The argmin
        # is invariant under positive scaling but keeping the range in
        # [0, 1] makes inspecting cost values intuitive.
        return cls(
            w_aspect=0.6,
            w_fill=0.4,
        )


@dataclass
class Candidate:
    cx: float
    cy: float
    area: int
    # Shape stats from the CC bounding box. Both server_post and live
    # (iOS) always populate them; None only appears when historical
    # pitch JSONs predating aspect/fill persistence get reloaded for
    # offline analysis. Cost function treats None as neutral on that
    # axis (see module docstring).
    aspect: float | None = None
    fill: float | None = None


def score_candidates(
    candidates: list[Candidate],
    tuning: CandidateSelectorTuning,
) -> list[float]:
    """Return one cost per candidate, in input order. Lower is more
    ball-like. Empty input → empty output.

    Caller invariant: every candidate has `area > 0`. Production callers
    enforce this via `_MIN_AREA_PX = 20` in `detection.py`. Area is no longer
    a cost term — it only gates entry — so this function does not read
    `c.area`."""
    if not candidates:
        return []
    aspect_denom = max(1.0 - _ASPECT_PEN_FLOOR, 1e-6)
    out: list[float] = []
    for c in candidates:
        if c.aspect is None:
            aspect_pen = 0.0
        else:
            aspect_pen = max(0.0, min((1.0 - c.aspect) / aspect_denom, 1.0))

        if c.fill is None:
            fill_pen = 0.0
        else:
            fill_pen = min(abs(c.fill - _FILL_TYPICAL) / _FILL_TYPICAL, 1.0)

        out.append(
            tuning.w_aspect * aspect_pen
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
