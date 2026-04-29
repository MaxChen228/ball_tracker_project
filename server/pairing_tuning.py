"""Operator-tunable thresholds for cross-camera triangulation fan-out.

The pairing layer iterates every (frame_a.candidates × frame_b.candidates)
combination per matched frame pair, runs ray-midpoint triangulation, and
emits all survivors. Two thresholds gate which survive:

- `cost_threshold` — selector cost of each candidate (from
  `candidate_selector.score_candidates`). Candidates with `cost > threshold`
  are skipped before triangulation. Default 1.0 = emit every shape-gate-
  passed candidate ("real ball loses competition, dump them all and let
  geometry decide" — operator's stated intent).
- `gap_threshold_m` — skew-line residual (closest distance between the two
  camera rays) of the triangulated point. `gap > threshold` is dropped.
  Default 0.20m, intentionally aligned with `segmenter.find_segments`'s
  `residual_max_m=0.20` so the same physics gate that eats outliers in
  the ballistic fit also gates the triangulation cloud.

`server/dry_run_shape.py` and `server/dry_run_multi_ray.py` use 0.30m for
research artefacts — the looser value is for offline exploration, not the
production gate. Keep them out of sync deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PairingTuning:
    """Cross-camera fan-out thresholds. Persisted to
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
