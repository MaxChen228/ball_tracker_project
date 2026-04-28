"""Run the full distillation: fit HSV + shape_gate + selector from GT,
evaluate current vs proposed against the same GT on a held-out subset,
and write `data/gt/fit_proposals.json` with metrics.

Held-out evaluation:
  - Random-split GT records into train (default 80%) and holdout (20%).
  - Fit on train.
  - For each frame in holdout records, run the actual server `detect_ball`
    twice — once with current params, once with proposed — against the
    decoded H.264-BGR frame. Compare the result to the GT mask centroid.
  - A "hit" is a detection whose centroid is within `--match-radius-px`
    of the GT centroid (default 8 px ≈ ball radius).
  - Compute recall (frames where detect found ball within match radius
    out of total GT-labelled frames), precision (detections where
    centroid is within match radius out of all detections), and
    centroid MAE.
  - Frames where SAM 3 found nothing get treated as "no GT" and don't
    contribute to either denominator.

Output schema matches the plan's `fit_proposals.json`:

```json
{
  "generated_at": "...",
  "source_sessions": ["s_aaa", ...],
  "holdout_sessions": ["s_ccc", ...],
  "current_params": { hsv_range, shape_gate, selector_tuning },
  "proposed_params": { ... },
  "metrics": {
    "current_on_holdout":  { recall, precision, centroid_mae_px, ... },
    "proposed_on_holdout": { recall, precision, centroid_mae_px, ... }
  }
}
```

Apply step is manual: review the JSON, then `cp` proposed values into
`data/{hsv_range,shape_gate,candidate_selector_tuning}.json`. Future
work: a dashboard modal that renders this JSON + `apply` buttons.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import math
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from candidate_selector import CandidateSelectorTuning  # noqa: E402
from detection import HSVRange, ShapeGate, detect_ball  # noqa: E402
from schemas import SAM3GTRecord, SAM3GTFrame  # noqa: E402
from video import iter_frames, probe_dims  # noqa: E402

from fit_hsv_from_gt import fit_hsv_from_records  # noqa: E402
from fit_selector_from_gt import fit_selector_from_records  # noqa: E402
from fit_shape_gate_from_gt import fit_shape_gate_from_records  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("distill_all")


@dataclass
class EvalMetrics:
    n_gt_frames: int                 # frames where SAM 3 found ball
    n_detections: int                # frames where detect_ball returned a centroid
    n_hits: int                      # detections within match_radius of GT centroid
    recall: float                    # n_hits / n_gt_frames
    precision: float                 # n_hits / n_detections
    centroid_mae_px: float           # mean |detected - GT| over hits
    centroid_p95_px: float           # 95th percentile of centroid distance


def _params_dict(
    hsv: HSVRange,
    gate: ShapeGate,
    sel: CandidateSelectorTuning,
) -> dict:
    return {
        "hsv_range": {
            "h_min": hsv.h_min, "h_max": hsv.h_max,
            "s_min": hsv.s_min, "s_max": hsv.s_max,
            "v_min": hsv.v_min, "v_max": hsv.v_max,
        },
        "shape_gate": {"aspect_min": gate.aspect_min, "fill_min": gate.fill_min},
        "selector_tuning": {
            "r_px_expected": sel.r_px_expected,
            "w_area": sel.w_area, "w_dist": sel.w_dist,
            "dist_cost_sat_radii": sel.dist_cost_sat_radii,
        },
    }


def _split_holdout(
    records: list[SAM3GTRecord],
    holdout_ratio: float,
    seed: int,
) -> tuple[list[SAM3GTRecord], list[SAM3GTRecord]]:
    rng = random.Random(seed)
    shuffled = list(records)
    rng.shuffle(shuffled)
    n_holdout = max(1, int(round(len(shuffled) * holdout_ratio)))
    return shuffled[n_holdout:], shuffled[:n_holdout]


def _eval_on_record(
    record: SAM3GTRecord,
    *,
    videos_dir: Path,
    pitches_dir: Path,
    hsv: HSVRange,
    gate: ShapeGate,
    sel: CandidateSelectorTuning,
    match_radius_px: float,
) -> EvalMetrics:
    """Run `detect_ball` on every decoded frame of the holdout MOV with
    the given params; compare against the per-frame GT centroid.

    Frames that SAM 3 didn't label are not counted in either numerator
    or denominator — we only score frames where ground truth exists."""
    clip = None
    for ext in (".mov", ".mp4", ".m4v"):
        cand = videos_dir / f"session_{record.session_id}_{record.camera_id}{ext}"
        if cand.is_file():
            clip = cand
            break
    if clip is None:
        log.warning("eval skipped %s/%s: no MOV found", record.session_id, record.camera_id)
        return EvalMetrics(0, 0, 0, 0.0, 0.0, 0.0, 0.0)

    # video_start_pts_s for this session — 0.0 is fine for evaluation
    # since GT was labelled with the same offset and we only diff
    # centroids per frame_idx.
    by_frame = {f.frame_idx: f for f in record.frames}
    n_gt = len(record.frames)
    n_det = 0
    n_hit = 0
    distances: list[float] = []

    prev_pos: tuple[float, float] | None = None
    prev_vel: tuple[float, float] | None = None
    prev_t: float | None = None
    for idx, (t, bgr) in enumerate(iter_frames(clip, 0.0)):
        dt = (t - prev_t) if prev_t is not None else None
        centroid = detect_ball(
            bgr, hsv,
            prev_position=prev_pos,
            prev_velocity=prev_vel,
            dt=dt,
            shape_gate=gate,
            selector_tuning=sel,
        )
        if centroid is not None:
            n_det += 1
            gt = by_frame.get(idx)
            if gt is not None:
                gx, gy = gt.centroid_px
                d = math.hypot(centroid[0] - gx, centroid[1] - gy)
                distances.append(d)
                if d <= match_radius_px:
                    n_hit += 1
            if prev_pos is not None and prev_t is not None and dt and dt > 0:
                prev_vel = ((centroid[0] - prev_pos[0]) / dt,
                            (centroid[1] - prev_pos[1]) / dt)
            prev_pos = centroid
            prev_t = t
        else:
            prev_pos = None
            prev_vel = None
            prev_t = None

    recall = (n_hit / n_gt) if n_gt > 0 else 0.0
    precision = (n_hit / n_det) if n_det > 0 else 0.0
    if distances:
        arr = np.asarray(distances)
        mae = float(arr.mean())
        p95 = float(np.percentile(arr, 95))
    else:
        mae = 0.0
        p95 = 0.0
    return EvalMetrics(
        n_gt_frames=n_gt,
        n_detections=n_det,
        n_hits=n_hit,
        recall=recall,
        precision=precision,
        centroid_mae_px=mae,
        centroid_p95_px=p95,
    )


def _aggregate_metrics(per_record: list[EvalMetrics]) -> EvalMetrics:
    """Sum counts across records, weighted-average distances by hit count."""
    total_gt = sum(m.n_gt_frames for m in per_record)
    total_det = sum(m.n_detections for m in per_record)
    total_hit = sum(m.n_hits for m in per_record)
    # weighted MAE / p95
    weights = [m.n_hits for m in per_record]
    total_w = sum(weights)
    if total_w > 0:
        mae = sum(m.centroid_mae_px * m.n_hits for m in per_record) / total_w
        p95 = max(m.centroid_p95_px for m in per_record)
    else:
        mae = 0.0
        p95 = 0.0
    return EvalMetrics(
        n_gt_frames=total_gt,
        n_detections=total_det,
        n_hits=total_hit,
        recall=(total_hit / total_gt) if total_gt > 0 else 0.0,
        precision=(total_hit / total_det) if total_det > 0 else 0.0,
        centroid_mae_px=mae,
        centroid_p95_px=p95,
    )


def _read_current_params(data_dir: Path) -> tuple[HSVRange, ShapeGate, CandidateSelectorTuning]:
    """Load the current data/*.json or default-fall-back per file."""
    hsv_path = data_dir / "hsv_range.json"
    if hsv_path.is_file():
        d = json.loads(hsv_path.read_text())
        hsv = HSVRange(
            h_min=int(d["h_min"]), h_max=int(d["h_max"]),
            s_min=int(d["s_min"]), s_max=int(d["s_max"]),
            v_min=int(d["v_min"]), v_max=int(d["v_max"]),
        )
    else:
        hsv = HSVRange.default()
    gate_path = data_dir / "shape_gate.json"
    if gate_path.is_file():
        d = json.loads(gate_path.read_text())
        gate = ShapeGate(aspect_min=float(d["aspect_min"]), fill_min=float(d["fill_min"]))
    else:
        gate = ShapeGate.default()
    sel_path = data_dir / "candidate_selector_tuning.json"
    if sel_path.is_file():
        d = json.loads(sel_path.read_text())
        sel = CandidateSelectorTuning(
            r_px_expected=float(d["r_px_expected"]),
            w_area=float(d["w_area"]),
            w_dist=float(d["w_dist"]),
            dist_cost_sat_radii=float(d["dist_cost_sat_radii"]),
        )
    else:
        sel = CandidateSelectorTuning.default()
    return hsv, gate, sel


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
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    parser.add_argument("--holdout-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k-sigma", type=float, default=2.0)
    parser.add_argument("--reject-percentile", type=float, default=5.0)
    parser.add_argument("--safety-margin", type=float, default=0.05)
    parser.add_argument(
        "--match-radius-px", type=float, default=8.0,
        help="centroid distance threshold for a hit (default 8 px ≈ ball radius)",
    )
    parser.add_argument(
        "--skip-eval", action="store_true",
        help="only fit; don't run detect_ball on holdout frames (much faster)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output proposals JSON (default: <data-dir>/gt/fit_proposals.json)",
    )
    args = parser.parse_args(argv)

    gt_dir = args.data_dir / "gt" / "sam3"
    out_path = args.out if args.out else args.data_dir / "gt" / "fit_proposals.json"

    records = _load_records(gt_dir)
    if not records:
        log.error("no GT records under %s", gt_dir)
        return 1
    if len(records) < 2 and not args.skip_eval:
        log.warning(
            "only %d GT record(s) — can't split holdout. Falling back to "
            "fitting on all records and evaluating on themselves (will "
            "overstate quality). Add more GT or pass --skip-eval.",
            len(records),
        )
        train, holdout = records, records
    else:
        train, holdout = _split_holdout(records, args.holdout_ratio, args.seed)

    log.info(
        "train=%d records (%d frames), holdout=%d records (%d frames)",
        len(train), sum(len(r.frames) for r in train),
        len(holdout), sum(len(r.frames) for r in holdout),
    )

    # ------- fit -------
    hsv_fit = fit_hsv_from_records(train, k_sigma=args.k_sigma)
    gate_fit = fit_shape_gate_from_records(
        train, reject_percentile=args.reject_percentile,
        safety_margin=args.safety_margin,
    )
    sel_fit = fit_selector_from_records(train)
    proposed_hsv = hsv_fit.hsv_range
    proposed_gate = gate_fit.shape_gate
    proposed_sel = sel_fit.tuning

    current_hsv, current_gate, current_sel = _read_current_params(args.data_dir)

    # ------- eval -------
    if args.skip_eval:
        current_metrics = None
        proposed_metrics = None
    else:
        log.info("evaluating CURRENT params on %d holdout records ...", len(holdout))
        current_per_record = [
            _eval_on_record(
                r,
                videos_dir=args.data_dir / "videos",
                pitches_dir=args.data_dir / "pitches",
                hsv=current_hsv,
                gate=current_gate,
                sel=current_sel,
                match_radius_px=args.match_radius_px,
            ) for r in holdout
        ]
        current_metrics = _aggregate_metrics(current_per_record)
        log.info(
            "  current: recall=%.3f precision=%.3f mae=%.2fpx p95=%.2fpx",
            current_metrics.recall, current_metrics.precision,
            current_metrics.centroid_mae_px, current_metrics.centroid_p95_px,
        )

        log.info("evaluating PROPOSED params on %d holdout records ...", len(holdout))
        proposed_per_record = [
            _eval_on_record(
                r,
                videos_dir=args.data_dir / "videos",
                pitches_dir=args.data_dir / "pitches",
                hsv=proposed_hsv,
                gate=proposed_gate,
                sel=proposed_sel,
                match_radius_px=args.match_radius_px,
            ) for r in holdout
        ]
        proposed_metrics = _aggregate_metrics(proposed_per_record)
        log.info(
            "  proposed: recall=%.3f precision=%.3f mae=%.2fpx p95=%.2fpx",
            proposed_metrics.recall, proposed_metrics.precision,
            proposed_metrics.centroid_mae_px, proposed_metrics.centroid_p95_px,
        )

    # ------- write -------
    payload = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_sessions": sorted({f"{r.session_id}_{r.camera_id}" for r in train}),
        "holdout_sessions": sorted({f"{r.session_id}_{r.camera_id}" for r in holdout}),
        "params": {
            "current": _params_dict(current_hsv, current_gate, current_sel),
            "proposed": _params_dict(proposed_hsv, proposed_gate, proposed_sel),
        },
        "metrics": {
            "current_on_holdout": asdict(current_metrics) if current_metrics else None,
            "proposed_on_holdout": asdict(proposed_metrics) if proposed_metrics else None,
        },
        "fit_inputs": {
            "k_sigma": args.k_sigma,
            "reject_percentile": args.reject_percentile,
            "safety_margin": args.safety_margin,
            "match_radius_px": args.match_radius_px,
            "holdout_ratio": args.holdout_ratio,
            "seed": args.seed,
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    log.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
