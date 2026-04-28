"""Fit shape_gate (aspect_min, fill_min) from SAM 3 GT mask shape stats.

The shape gate has two thresholds:
  - aspect_min:  blob's bbox `min(w,h) / max(w,h)`. A real ball is round
                 → aspect ≈ 1.0. Clutter (cardboard edges, blue tape)
                 tends to be elongated → aspect closer to 0.
  - fill_min:    `mask_area / bbox_area`. A real ball is solid → fill ≈
                 π/4 ≈ 0.785 in the continuum limit. Clutter that
                 happens to match HSV often has holes / fragmented
                 mask → fill closer to 0.

Approach: take 5th percentile of the GT distribution, then back off a
small safety margin (default 5%). The percentile choice (not min) is
deliberate — SAM 3 occasionally labels partial masks at frame edges
(ball half-occluded) where the shape stats are not representative of
the in-flight ball you want to detect; those outliers shouldn't drag
the threshold all the way down.

Optional `--reject-percentile` (default 5) lets you trade recall vs
precision: 5 keeps 95% of GT frames clearing the gate; 1 keeps 99%.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from detection import ShapeGate  # noqa: E402
from schemas import SAM3GTRecord  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fit_shape_gate")


@dataclass
class ShapeFitResult:
    shape_gate: ShapeGate
    aspect_p5: float
    aspect_p50: float
    aspect_p95: float
    fill_p5: float
    fill_p50: float
    fill_p95: float
    n_frames: int
    n_records: int
    safety_margin: float


def fit_shape_gate_from_records(
    records: list[SAM3GTRecord],
    *,
    reject_percentile: float = 5.0,
    safety_margin: float = 0.05,
) -> ShapeFitResult:
    aspects: list[float] = []
    fills: list[float] = []
    for record in records:
        for f in record.frames:
            aspects.append(f.mask_aspect)
            fills.append(f.mask_fill)
    if not aspects:
        raise ValueError("no GT frames in input")
    aspects_arr = np.asarray(aspects, dtype=np.float64)
    fills_arr = np.asarray(fills, dtype=np.float64)

    aspect_p5 = float(np.percentile(aspects_arr, reject_percentile))
    aspect_p50 = float(np.percentile(aspects_arr, 50))
    aspect_p95 = float(np.percentile(aspects_arr, 100 - reject_percentile))
    fill_p5 = float(np.percentile(fills_arr, reject_percentile))
    fill_p50 = float(np.percentile(fills_arr, 50))
    fill_p95 = float(np.percentile(fills_arr, 100 - reject_percentile))

    # Subtract safety margin then clamp to [0, 1].
    proposed_aspect = max(0.0, aspect_p5 - safety_margin)
    proposed_fill = max(0.0, fill_p5 - safety_margin)
    proposed = ShapeGate(aspect_min=proposed_aspect, fill_min=proposed_fill)

    return ShapeFitResult(
        shape_gate=proposed,
        aspect_p5=aspect_p5, aspect_p50=aspect_p50, aspect_p95=aspect_p95,
        fill_p5=fill_p5, fill_p50=fill_p50, fill_p95=fill_p95,
        n_frames=len(aspects),
        n_records=len(records),
        safety_margin=safety_margin,
    )


def _load_records(gt_dir: Path) -> list[SAM3GTRecord]:
    records: list[SAM3GTRecord] = []
    for path in sorted(gt_dir.glob("session_*.json")):
        try:
            records.append(SAM3GTRecord.model_validate_json(path.read_text()))
        except Exception as e:
            log.warning("skip %s: %s", path.name, e)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gt-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "gt" / "sam3",
    )
    parser.add_argument(
        "--reject-percentile", type=float, default=5.0,
        help="reject this percentile of GT frames at the gate (default 5%%)",
    )
    parser.add_argument(
        "--safety-margin", type=float, default=0.05,
        help="extra slack subtracted from the percentile (default 0.05)",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    records = _load_records(args.gt_dir)
    if not records:
        log.error("no GT records under %s", args.gt_dir)
        return 1

    result = fit_shape_gate_from_records(
        records,
        reject_percentile=args.reject_percentile,
        safety_margin=args.safety_margin,
    )

    payload = {
        "proposed_shape_gate": {
            "aspect_min": result.shape_gate.aspect_min,
            "fill_min": result.shape_gate.fill_min,
        },
        "stats": {
            "aspect": {"p5": result.aspect_p5, "p50": result.aspect_p50, "p95": result.aspect_p95},
            "fill":   {"p5": result.fill_p5,   "p50": result.fill_p50,   "p95": result.fill_p95},
        },
        "input": {
            "n_records": result.n_records,
            "n_frames": result.n_frames,
            "reject_percentile": args.reject_percentile,
            "safety_margin": result.safety_margin,
        },
    }
    out_str = json.dumps(payload, indent=2)
    if args.out:
        args.out.write_text(out_str)
        log.info("wrote %s", args.out)
    else:
        print(out_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
