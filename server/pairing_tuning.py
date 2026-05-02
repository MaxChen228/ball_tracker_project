"""Operator-tunable per-session filter for pairing fan-out output.

`PairingTuning` holds the one filter value the operator stamps onto a
session via the viewer's `Gap ≤` slider → Apply →
`POST /sessions/{sid}/recompute`. Stamped value lands on
`SessionResult.gap_threshold_m` and gates which points the segmenter
consumes.

**This field does NOT gate pairing emit.** Pairing's emit-time gate is
the absolute ceiling pair (`pairing._EMIT_COST_CEILING` /
`pairing._EMIT_GAP_CEILING_M`) — disk/memory protection only, not
operator-tunable. The persisted point set is the full emitted set; the
viewer slider filters client-side and Apply re-runs the segmenter on
the stamped subset.

- `gap_threshold_m` — skew-line residual (closest distance between the
  two camera rays) cap. Viewer hides points above the cap; segmenter
  ignores them on Apply. Default 0.20 m (20 cm) — the empirical floor
  below which residual filtering starts cutting real flight points
  (see CLAUDE.md). Slider range 0–200 cm; 200 cm is at-or-above the
  emit ceiling so dragging there reveals every emitted point.

The cost gate that used to live alongside this knob now lives on each
`AlgorithmEntry.cost_threshold` — cost is a property of the detector's
feature distribution (aspect/fill medians for HSV+CC, different for
future detectors), not an operator preference. See
`algorithms.cost_threshold_for_algorithm`.

`server/dry_run_shape.py` and `server/dry_run_multi_ray.py` hard-code
`GAP_MAX=0.30` for offline research artefacts — looser than production
on purpose. Don't tidy them into this default; the looseness is the
point of the offline tools.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairingTuning:
    """Cross-camera per-session filter value. Persisted to
    `data/pairing_tuning.json`. The field must be supplied — no
    module-level fallback, so callers cannot silently inherit a stale
    magic number."""

    gap_threshold_m: float

    @classmethod
    def default(cls) -> "PairingTuning":
        return cls(gap_threshold_m=0.20)
