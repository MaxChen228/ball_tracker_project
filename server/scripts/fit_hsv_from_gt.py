"""Fit HSV range from SAM 3 GT mask pixel distributions.

The distillation question: given per-frame ball masks from SAM 3,
what's the tightest HSV inRange envelope that still captures (approx)
all of the ball pixels? Tighter = fewer false positives on background;
looser = higher recall at the cost of precision.

Approach (k-sigma on each channel, channel-independent):
  1. Aggregate (h, s, v) values from every GT-labelled frame's mask.
  2. For each channel, compute mean ± k·std where k targets the 95th
     percentile by default. Round to ints, clamp to channel range.
  3. The fit script doesn't do end-to-end recall/precision evaluation
     — that's `validate_three_way.py`'s job. This script only emits
     the proposed numbers + the input distribution stats.

Notes:
  - SAM 3 GT pre-stores `mask_hue_mean / std` per frame so we can
    aggregate without re-reading the MOV pixels. Cheaper but coarser
    (loses within-frame variance). Acceptable for v1 — a refit pass
    that walks the actual mask pixels is a follow-up if the per-frame
    summary turns out to fit too loose.
  - Frames where confidence < SAM3GTRecord.min_confidence have already
    been dropped at label time, so we don't re-filter here.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from detection import HSVRange  # noqa: E402
from schemas import SAM3GTRecord  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fit_hsv_from_gt")


@dataclass
class HSVChannelStats:
    """Aggregated stats for one HSV channel across all GT frames."""
    n: int                    # frames contributing
    weighted_mean: float      # mean of mask_*_mean weighted by mask_area_px
    pooled_std: float         # sqrt of pooled variance across frames
    proposed_min: int         # mean - k·std, clamped + rounded
    proposed_max: int         # mean + k·std, clamped + rounded


def _clamp_round(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def _aggregate_channel_stats(
    means: list[float],
    stds: list[float],
    weights: list[int],
    *,
    k_sigma: float,
    channel_max: int,
) -> HSVChannelStats:
    """Pool weighted mean + variance across frames.

    Pooled variance = E[var_within] + var(means_across_frames). The
    first term comes from per-frame mask_*_std, the second from how
    much per-frame mask_*_mean varies. Both contribute to how wide
    the proposed range needs to be."""
    if not means:
        raise ValueError("no GT frames contributed to fit — empty input")
    total_w = sum(weights)
    if total_w == 0:
        raise ValueError("all GT frames have zero mask area — bad input")

    weighted_mean = sum(m * w for m, w in zip(means, weights)) / total_w
    var_within = sum(s * s * w for s, w in zip(stds, weights)) / total_w
    var_between = sum(((m - weighted_mean) ** 2) * w for m, w in zip(means, weights)) / total_w
    pooled_var = var_within + var_between
    pooled_std = math.sqrt(pooled_var)
    proposed_min = _clamp_round(weighted_mean - k_sigma * pooled_std, 0, channel_max)
    proposed_max = _clamp_round(weighted_mean + k_sigma * pooled_std, 0, channel_max)
    if proposed_min > proposed_max:
        proposed_min, proposed_max = proposed_max, proposed_min
    return HSVChannelStats(
        n=len(means),
        weighted_mean=weighted_mean,
        pooled_std=pooled_std,
        proposed_min=proposed_min,
        proposed_max=proposed_max,
    )


@dataclass
class HSVFitResult:
    hsv_range: HSVRange
    h: HSVChannelStats
    s: HSVChannelStats
    v: HSVChannelStats
    n_frames: int
    n_records: int
    k_sigma: float


def fit_hsv_from_records(
    records: list[SAM3GTRecord],
    *,
    k_sigma: float = 2.0,
) -> HSVFitResult:
    """Fit a single `HSVRange` from a batch of SAM3GTRecord. `k_sigma`
    controls the recall/precision trade — `k=2.0` ≈ 95% of within-mask
    pixels for a roughly Gaussian channel, `k=3.0` ≈ 99.7%. Default 2.0
    biases for precision; raise it if you see edge frames being missed."""
    h_means, h_stds, h_w = [], [], []
    s_means, s_stds, s_w = [], [], []
    v_means, v_stds, v_w = [], [], []
    n_frames = 0
    for record in records:
        for f in record.frames:
            h_means.append(f.mask_hue_mean); h_stds.append(f.mask_hue_std); h_w.append(f.mask_area_px)
            s_means.append(f.mask_sat_mean); s_stds.append(0.0);             s_w.append(f.mask_area_px)
            v_means.append(f.mask_val_mean); v_stds.append(0.0);             v_w.append(f.mask_area_px)
            n_frames += 1
    # Hue uses OpenCV's 0-179 convention; sat / val are 0-255.
    h = _aggregate_channel_stats(h_means, h_stds, h_w, k_sigma=k_sigma, channel_max=179)
    # SAM3GTFrame doesn't store per-channel std for sat/val (only hue
    # has _std in v1 schema — sat/val variance came from `mask_pixels`
    # which we'd need a refit to walk). For now: use zero within-frame
    # std for sat/val — between-frame variance still contributes, but
    # the proposed range will be tighter than reality. Documented
    # caveat; refit pass can widen these.
    s = _aggregate_channel_stats(s_means, s_stds, s_w, k_sigma=k_sigma, channel_max=255)
    v = _aggregate_channel_stats(v_means, v_stds, v_w, k_sigma=k_sigma, channel_max=255)
    proposed = HSVRange(
        h_min=h.proposed_min, h_max=h.proposed_max,
        s_min=s.proposed_min, s_max=s.proposed_max,
        v_min=v.proposed_min, v_max=v.proposed_max,
    )
    return HSVFitResult(
        hsv_range=proposed,
        h=h, s=s, v=v,
        n_frames=n_frames,
        n_records=len(records),
        k_sigma=k_sigma,
    )


def _load_records(gt_dir: Path) -> list[SAM3GTRecord]:
    records: list[SAM3GTRecord] = []
    for path in sorted(gt_dir.glob("session_*.json")):
        # Skip non-record JSONs (e.g. .preview.mp4 won't match anyway).
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
        help="directory of SAM3GTRecord JSONs (default: server/data/gt/sam3/)",
    )
    parser.add_argument(
        "--k-sigma",
        type=float,
        default=2.0,
        help="number of σ around mean to span (default 2.0 ≈ 95%% of mask)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional output JSON path; default prints to stdout",
    )
    args = parser.parse_args(argv)

    records = _load_records(args.gt_dir)
    if not records:
        log.error("no GT records under %s — run label_with_sam3.py first", args.gt_dir)
        return 1
    log.info("loaded %d records, %d total frames", len(records),
             sum(len(r.frames) for r in records))

    result = fit_hsv_from_records(records, k_sigma=args.k_sigma)

    payload = {
        "proposed_hsv_range": {
            "h_min": result.hsv_range.h_min, "h_max": result.hsv_range.h_max,
            "s_min": result.hsv_range.s_min, "s_max": result.hsv_range.s_max,
            "v_min": result.hsv_range.v_min, "v_max": result.hsv_range.v_max,
        },
        "stats": {
            "h": asdict(result.h),
            "s": asdict(result.s),
            "v": asdict(result.v),
        },
        "input": {
            "n_records": result.n_records,
            "n_frames": result.n_frames,
            "k_sigma": result.k_sigma,
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
