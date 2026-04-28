"""Fit candidate_selector tuning (r_px_expected) from SAM 3 GT mask area.

The candidate selector scores blobs by `area_score` (1 - |area - expected_area|/expected_area)
and `dist_cost` (distance from temporal prior, saturated at
`dist_cost_sat_radii * r_px_expected`). Both depend on knowing the
ball's expected pixel radius.

`r_px_expected` is the only param we fit here:
  - From SAM 3 GT mask area, derive r = sqrt(area / π) per frame.
  - Take the median (robust to half-occluded edge frames).

The other params (`w_area`, `w_dist`, `dist_cost_sat_radii`) need
end-to-end recall/precision evaluation to fit — that's
`validate_three_way.py`'s territory. This script defers them to
defaults and ships only the radius proposal.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from candidate_selector import CandidateSelectorTuning  # noqa: E402
from schemas import SAM3GTRecord  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fit_selector")


@dataclass
class SelectorFitResult:
    tuning: CandidateSelectorTuning
    r_p25: float
    r_p50: float
    r_p75: float
    n_frames: int
    n_records: int


def fit_selector_from_records(records: list[SAM3GTRecord]) -> SelectorFitResult:
    radii: list[float] = []
    for record in records:
        for f in record.frames:
            if f.mask_area_px <= 0:
                continue
            radii.append(math.sqrt(f.mask_area_px / math.pi))
    if not radii:
        raise ValueError("no GT frames with non-zero mask area")
    arr = np.asarray(radii, dtype=np.float64)
    p25 = float(np.percentile(arr, 25))
    p50 = float(np.percentile(arr, 50))
    p75 = float(np.percentile(arr, 75))
    default = CandidateSelectorTuning.default()
    proposed = CandidateSelectorTuning(
        r_px_expected=p50,
        w_area=default.w_area,
        w_dist=default.w_dist,
        dist_cost_sat_radii=default.dist_cost_sat_radii,
    )
    return SelectorFitResult(
        tuning=proposed,
        r_p25=p25, r_p50=p50, r_p75=p75,
        n_frames=len(radii),
        n_records=len(records),
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
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    records = _load_records(args.gt_dir)
    if not records:
        log.error("no GT records under %s", args.gt_dir)
        return 1
    result = fit_selector_from_records(records)
    payload = {
        "proposed_selector_tuning": {
            "r_px_expected": result.tuning.r_px_expected,
            "w_area": result.tuning.w_area,
            "w_dist": result.tuning.w_dist,
            "dist_cost_sat_radii": result.tuning.dist_cost_sat_radii,
        },
        "stats": {
            "r_p25": result.r_p25,
            "r_p50": result.r_p50,
            "r_p75": result.r_p75,
        },
        "input": {
            "n_records": result.n_records,
            "n_frames": result.n_frames,
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
