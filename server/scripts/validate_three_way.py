"""Three-way validation: iOS-live vs server_post vs SAM 3 GT.

For a single (session, cam) — loads:
  - SAM3GTRecord                                (data/gt/sam3/...)
  - pitch JSON's frames_live                    (iOS detection over WS)
  - pitch JSON's frames_server_post             (server detection on H.264)

Computes per-frame:
  - centroid distances live↔gt, server↔gt, live↔server
  - presence agreement (both detected / one missing)

Aggregates per (session, cam):
  - recall (live vs GT, server vs GT, live vs server)
  - precision (same pairs)
  - centroid MAE / p95 (same pairs)
  - per-frame timeseries CSV for the dashboard report page

Three numbers we care about (per the plan):
  1. live_vs_server  median centroid diff < 1 px → algorithms aligned;
                     remaining gap is purely H.264 vs BGRA input.
  2. live_recall_vs_gt > 0.90 → distillation params are good for
                     production iOS use.
  3. server_vs_gt    diagnostic of server_post quality on H.264 input.

Writes:
  - data/gt/validation/<sid>_<cam>.json    aggregate metrics
  - data/gt/validation/<sid>_<cam>.csv     per-frame rows for plotting

Usage:
  uv run python scripts/validate_three_way.py --session s_xxx --cam A
  uv run python scripts/validate_three_way.py --all
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from schemas import FramePayload, PitchPayload, SAM3GTRecord  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("validate_three_way")


@dataclass
class PairwiseMetrics:
    """Stats for one pair (e.g. live vs gt)."""
    n_both_present: int
    n_a_only: int
    n_b_only: int
    n_neither: int
    n_a_total: int                # frames where source A has a detection
    n_b_total: int                # frames where source B has a detection
    n_hits: int                   # both present + within match_radius
    centroid_mae_px: float        # mean centroid distance over both-present
    centroid_p95_px: float
    recall: float                 # n_hits / n_b_total (B is reference; for *_vs_gt, B=gt)
    precision: float              # n_hits / n_a_total


def _pairwise(
    a: dict[int, tuple[float, float]],
    b: dict[int, tuple[float, float]],
    *,
    match_radius_px: float,
) -> PairwiseMetrics:
    all_idxs = set(a) | set(b)
    both = [i for i in all_idxs if i in a and i in b]
    a_only = sum(1 for i in all_idxs if i in a and i not in b)
    b_only = sum(1 for i in all_idxs if i in b and i not in a)
    distances = [math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1]) for i in both]
    hits = sum(1 for d in distances if d <= match_radius_px)
    if distances:
        arr = np.asarray(distances)
        mae = float(arr.mean())
        p95 = float(np.percentile(arr, 95))
    else:
        mae = 0.0
        p95 = 0.0
    n_a = len(a)
    n_b = len(b)
    return PairwiseMetrics(
        n_both_present=len(both),
        n_a_only=a_only,
        n_b_only=b_only,
        n_neither=0,             # we don't enumerate the universe of un-decoded frame_idxs
        n_a_total=n_a,
        n_b_total=n_b,
        n_hits=hits,
        centroid_mae_px=mae,
        centroid_p95_px=p95,
        recall=(hits / n_b) if n_b > 0 else 0.0,
        precision=(hits / n_a) if n_a > 0 else 0.0,
    )


def _frames_to_centroid_map(frames: list[FramePayload] | None) -> dict[int, tuple[float, float]]:
    if not frames:
        return {}
    out: dict[int, tuple[float, float]] = {}
    for f in frames:
        if f.ball_detected and f.px is not None and f.py is not None:
            out[f.frame_index] = (float(f.px), float(f.py))
    return out


def _gt_to_centroid_map(record: SAM3GTRecord) -> dict[int, tuple[float, float]]:
    return {f.frame_idx: (float(f.centroid_px[0]), float(f.centroid_px[1])) for f in record.frames}


@dataclass
class ValidationReport:
    session_id: str
    camera_id: str
    match_radius_px: float
    live_vs_gt: PairwiseMetrics
    server_vs_gt: PairwiseMetrics
    live_vs_server: PairwiseMetrics
    n_gt_frames: int
    n_live_frames: int
    n_server_frames: int


def validate_session_cam(
    *,
    pitch_path: Path,
    gt_path: Path,
    match_radius_px: float = 8.0,
) -> tuple[ValidationReport, list[dict]]:
    """Returns (aggregate report, per-frame rows for CSV)."""
    pitch = PitchPayload.model_validate_json(pitch_path.read_text())
    record = SAM3GTRecord.model_validate_json(gt_path.read_text())

    live_map = _frames_to_centroid_map(pitch.frames_live)
    server_map = _frames_to_centroid_map(pitch.frames_server_post)
    gt_map = _gt_to_centroid_map(record)

    live_vs_gt = _pairwise(live_map, gt_map, match_radius_px=match_radius_px)
    server_vs_gt = _pairwise(server_map, gt_map, match_radius_px=match_radius_px)
    live_vs_server = _pairwise(live_map, server_map, match_radius_px=match_radius_px)

    # Per-frame rows: union of all frame_idxs that any source touched.
    all_idxs = sorted(set(live_map) | set(server_map) | set(gt_map))
    rows = []
    for i in all_idxs:
        row = {"frame_idx": i}
        for tag, m in (("live", live_map), ("server", server_map), ("gt", gt_map)):
            if i in m:
                row[f"{tag}_px"] = m[i][0]
                row[f"{tag}_py"] = m[i][1]
            else:
                row[f"{tag}_px"] = ""
                row[f"{tag}_py"] = ""
        for tag, a, b in (
            ("live_vs_gt", live_map, gt_map),
            ("server_vs_gt", server_map, gt_map),
            ("live_vs_server", live_map, server_map),
        ):
            if i in a and i in b:
                row[f"{tag}_dist_px"] = math.hypot(a[i][0] - b[i][0], a[i][1] - b[i][1])
            else:
                row[f"{tag}_dist_px"] = ""
        rows.append(row)

    report = ValidationReport(
        session_id=record.session_id,
        camera_id=record.camera_id,
        match_radius_px=match_radius_px,
        live_vs_gt=live_vs_gt,
        server_vs_gt=server_vs_gt,
        live_vs_server=live_vs_server,
        n_gt_frames=len(gt_map),
        n_live_frames=len(live_map),
        n_server_frames=len(server_map),
    )
    return report, rows


def _write_outputs(
    report: ValidationReport,
    rows: list[dict],
    *,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{report.session_id}_{report.camera_id}"
    json_path = out_dir / f"{base}.json"
    csv_path = out_dir / f"{base}.csv"

    payload = {
        "session_id": report.session_id,
        "camera_id": report.camera_id,
        "match_radius_px": report.match_radius_px,
        "n_gt_frames": report.n_gt_frames,
        "n_live_frames": report.n_live_frames,
        "n_server_frames": report.n_server_frames,
        "live_vs_gt": asdict(report.live_vs_gt),
        "server_vs_gt": asdict(report.server_vs_gt),
        "live_vs_server": asdict(report.live_vs_server),
    }
    json_path.write_text(json.dumps(payload, indent=2))

    if rows:
        keys = list(rows[0].keys())
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    log.info(
        "%s: gt=%d live=%d srv=%d  L/G recall=%.2f mae=%.2fpx  S/G recall=%.2f mae=%.2fpx  L/S median_diff_p95=%.2fpx",
        base,
        report.n_gt_frames, report.n_live_frames, report.n_server_frames,
        report.live_vs_gt.recall, report.live_vs_gt.centroid_mae_px,
        report.server_vs_gt.recall, report.server_vs_gt.centroid_mae_px,
        report.live_vs_server.centroid_p95_px,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session")
    target.add_argument("--all", action="store_true")
    parser.add_argument("--cam")
    parser.add_argument("--match-radius-px", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.session and not args.cam:
        parser.error("--cam is required with --session")

    pitches_dir = args.data_dir / "pitches"
    gt_dir = args.data_dir / "gt" / "sam3"
    out_dir = args.data_dir / "gt" / "validation"

    if args.session:
        targets = [(args.session, args.cam)]
    else:
        targets = []
        for gt_path in sorted(gt_dir.glob("session_*.json")):
            stem = gt_path.stem
            parts = stem.split("_")
            if len(parts) < 4:
                continue
            session_id = "_".join(parts[1:-1])
            camera_id = parts[-1]
            targets.append((session_id, camera_id))

    successes = 0
    for sid, cam in targets:
        gt_path = gt_dir / f"session_{sid}_{cam}.json"
        pitch_path = pitches_dir / f"session_{sid}_{cam}.json"
        if not gt_path.is_file():
            log.warning("skip %s/%s: GT missing (%s)", sid, cam, gt_path)
            continue
        if not pitch_path.is_file():
            log.warning("skip %s/%s: pitch JSON missing (%s)", sid, cam, pitch_path)
            continue
        try:
            report, rows = validate_session_cam(
                pitch_path=pitch_path,
                gt_path=gt_path,
                match_radius_px=args.match_radius_px,
            )
            _write_outputs(report, rows, out_dir=out_dir)
            successes += 1
        except Exception as e:
            log.exception("FAILED %s/%s: %s", sid, cam, e)

    log.info("done: %d/%d (session, cam) reports", successes, len(targets))
    return 0 if successes else 1


if __name__ == "__main__":
    sys.exit(main())
