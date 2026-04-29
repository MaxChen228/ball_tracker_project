"""Score ball candidates using a track-independent shape prior.

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

**Why no size term?** Earlier revisions had a `size_pen` based on
`expected_area = π · r_px²`. That assumed a fixed apparent radius —
false. A 240 fps pitch sweeps from far (~10 m, r ≈ 4 px) to near
(~2 m, r ≈ 25 px), so absolute area is a function of distance, not
of "is this object a ball." Aspect and fill are scale-invariant
geometric properties of a sphere; they're the only honest shape
signals for a track-independent selector. Area still gates entry via
`_MIN_AREA_PX = 20` in `detection.py` (reject sub-pixel noise) but is
NOT a cost term.

Cost formula:

    cost = _W_ASPECT · aspect_pen + _W_FILL · fill_pen

Each component is normalized into [0, 1]:

- `aspect_pen` — `(1 - aspect)` normalized so a perfectly-square blob
  (aspect=1) costs 0 and a barely-passing one (aspect ≈ 0.5) costs 1.
- `fill_pen` — `|fill - 0.68|` normalized; 0.68 is the empirical
  median for the project ball.

Unknown shape (`aspect=None` or `fill=None`) maps to **zero penalty**
on that axis. Production captures populate both fields — None only
appears on historical pitch JSONs that predate aspect/fill persistence.

Weights `_W_ASPECT` / `_W_FILL` were a runtime tunable
(`CandidateSelectorTuning`) until the selector retirement: pairing's
default `cost_threshold = 1.0` equals max possible cost, so the cost
gate never fired at default; the only downstream effect was the
monocular ground-trace winner. Locked as constants now; change
requires a code edit + restart.
"""
from __future__ import annotations

from dataclasses import dataclass


# Empirical median fill for the project blue ball. Held as a module
# constant rather than a tuning knob because it's a property of the
# ball, not an operator choice.
_FILL_TYPICAL = 0.68

# Aspect-penalty normalization floor. Held constant: tighter than the
# runtime shape gate (typically 0.56) so candidates near the gate
# already score badly.
_ASPECT_PEN_FLOOR = 0.5

_W_ASPECT = 0.6
_W_FILL = 0.4


@dataclass
class Candidate:
    cx: float
    cy: float
    area: int
    # Shape stats from the CC bounding box. Production paths (live +
    # server_post) always populate them; None only appears on historical
    # pitch JSONs that predate aspect/fill persistence. Cost function
    # treats None as neutral on that axis.
    aspect: float | None = None
    fill: float | None = None


def score_candidates(candidates: list[Candidate]) -> list[float]:
    """Return one cost per candidate, in input order. Lower is more
    ball-like. Empty input → empty output.

    Caller invariant: every candidate has `area > 0` (production
    callers enforce this via `_MIN_AREA_PX = 20` in `detection.py`).
    Area is not a cost term — it only gates entry — so this function
    does not read `c.area`."""
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

        out.append(_W_ASPECT * aspect_pen + _W_FILL * fill_pen)
    return out
