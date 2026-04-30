"""Operator-tunable per-session filters for pairing fan-out output.

`PairingTuning` holds two filter values that the operator stamps onto a
session via the viewer's `Cost ≤` / `Gap ≤` sliders → Apply →
`POST /sessions/{sid}/recompute`. Stamped values land on
`SessionResult.cost_threshold` / `gap_threshold_m` and gate which points
the segmenter consumes.

**These fields no longer gate pairing emit.** Pairing's emit-time gate
is the absolute ceiling pair (`pairing._EMIT_COST_CEILING` /
`pairing._EMIT_GAP_CEILING_M`) — disk/memory protection only, not
operator-tunable. The persisted point set is the full emitted set; the
viewer slider filters client-side and Apply re-runs the segmenter on
the stamped subset. Pre-this-PR architecture conflated emit gate with
operator filter; that conflation is now decoupled.

- `cost_threshold` — selector cost cap (semantically `max(cost_a, cost_b)
  ≤ threshold`). The viewer slider hides points client-side; segmenter
  ignores them on Apply. Default 1.0 = "emit everything the shape gate
  let through". Slider range 0–1 covers the production cost band.
- `gap_threshold_m` — skew-line residual (closest distance between the
  two camera rays) cap. Same semantics: viewer hides, segmenter ignores.
  Default 0.20 m. Slider range 0–200 cm; 200 cm is at-or-above the
  emit ceiling so dragging there reveals every emitted point.

`server/dry_run_shape.py` and `server/dry_run_multi_ray.py` hard-code
`GAP_MAX=0.30` for offline research artefacts — looser than production
on purpose. Don't tidy them into this default; the looseness is the
point of the offline tools.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairingTuning:
    """Cross-camera per-session filter values. Persisted to
    `data/pairing_tuning.json`. Both fields must be supplied — no
    module-level fallbacks, so callers cannot silently inherit a stale
    magic number."""

    cost_threshold: float
    gap_threshold_m: float

    @classmethod
    def default(cls) -> "PairingTuning":
        return cls(
            cost_threshold=1.0,
            gap_threshold_m=0.20,
        )
