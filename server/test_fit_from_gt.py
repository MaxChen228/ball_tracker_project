"""Tests for fit_*_from_records functions in scripts/fit_*.py.

These are pure stat-fit functions on lists of SAM3GTRecord — no torch,
no SAM 3 model. Synthetic input is constructed in-test.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make scripts importable as if scripts/ were on sys.path.
_SCRIPTS = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import pytest

from schemas import SAM3GTFrame, SAM3GTRecord
from fit_hsv_from_gt import fit_hsv_from_records  # type: ignore[import-not-found]
from fit_shape_gate_from_gt import fit_shape_gate_from_records  # type: ignore[import-not-found]
from fit_selector_from_gt import fit_selector_from_records  # type: ignore[import-not-found]


def _synthetic_record(
    n_frames: int,
    *,
    hue_mean: float = 110.0,
    hue_std: float = 4.0,
    sat_mean: float = 200.0,
    val_mean: float = 150.0,
    aspect: float = 0.95,
    fill: float = 0.78,
    area: int = 1200,
    confidence: float = 0.92,
) -> SAM3GTRecord:
    frames = []
    for i in range(n_frames):
        frames.append(SAM3GTFrame(
            frame_idx=i,
            t_pts_s=i / 240.0,
            bbox=(100.0, 100.0, 140.0, 140.0),
            centroid_px=(120.0, 120.0),
            mask_area_px=area,
            mask_aspect=aspect,
            mask_fill=fill,
            mask_hue_mean=hue_mean,
            mask_hue_std=hue_std,
            mask_sat_mean=sat_mean,
            mask_val_mean=val_mean,
            confidence=confidence,
        ))
    return SAM3GTRecord(
        session_id="s_deadbeef",
        camera_id="A",
        model_version="fake/sam3 (test)",
        labelled_at="2026-04-28T00:00:00Z",
        prompt_strategy="text:'blue ball'",
        video_fps=240.0,
        video_dims=(1920, 1080),
        frames=frames,
        frames_total=n_frames,
        frames_labelled=n_frames,
        min_confidence=0.5,
    )


# ----- HSV fit -----------------------------------------------------


def test_fit_hsv_centered_blue_distribution():
    records = [_synthetic_record(50, hue_mean=110.0, hue_std=4.0,
                                 sat_mean=200.0, val_mean=150.0)]
    result = fit_hsv_from_records(records, k_sigma=2.0)
    # Hue: 110 ± 2*4 = [102, 118]
    assert 100 <= result.hsv_range.h_min <= 105
    assert 115 <= result.hsv_range.h_max <= 120
    # Sat / val with std=0 within frame and zero between (constant per-frame):
    # proposed = mean exactly.
    assert result.hsv_range.s_min == 200
    assert result.hsv_range.s_max == 200
    assert result.hsv_range.v_min == 150
    assert result.hsv_range.v_max == 150


def test_fit_hsv_widens_on_between_frame_variance():
    """Records with different per-frame means should widen the proposed range."""
    records = [
        _synthetic_record(20, hue_mean=105.0, hue_std=2.0, sat_mean=180.0),
        _synthetic_record(20, hue_mean=115.0, hue_std=2.0, sat_mean=220.0),
    ]
    result = fit_hsv_from_records(records, k_sigma=2.0)
    # weighted_mean(hue) ≈ 110, but pooled variance includes between-frame
    # spread (5 px around mean) → proposed range must be wider than just
    # 110 ± 2*2.
    assert result.hsv_range.h_min < 105
    assert result.hsv_range.h_max > 115
    # Sat: between-frame spread of 20 dominates (within = 0). Pooled
    # std ≈ 20 → proposed range ≈ 200 ± 40.
    assert result.hsv_range.s_min < 180
    assert result.hsv_range.s_max > 220


def test_fit_hsv_clamps_to_channel_range():
    """Hue is 0-179 in OpenCV; sat/val are 0-255. Proposed must clamp."""
    records = [_synthetic_record(10, hue_mean=178.0, hue_std=10.0)]
    result = fit_hsv_from_records(records, k_sigma=3.0)
    assert result.hsv_range.h_max <= 179
    assert result.hsv_range.h_min >= 0


def test_fit_hsv_empty_input_raises():
    with pytest.raises(ValueError):
        fit_hsv_from_records([_synthetic_record(0)])


# ----- shape_gate fit ----------------------------------------------


def test_fit_shape_gate_at_p5_minus_margin():
    """The proposal sits at the (reject_percentile)th percentile minus margin."""
    # 100 frames at fill ~ 0.7-0.8 with some at 0.6 (low tail).
    records = [
        _synthetic_record(80, fill=0.75, aspect=0.95),
        _synthetic_record(20, fill=0.62, aspect=0.92),
    ]
    result = fit_shape_gate_from_records(
        records,
        reject_percentile=5.0,
        safety_margin=0.05,
    )
    # 5th percentile of [80 × 0.75, 20 × 0.62] is in the low-tail cluster.
    assert result.fill_p5 < 0.7
    # Proposed = p5 - 0.05.
    assert result.shape_gate.fill_min == pytest.approx(result.fill_p5 - 0.05, abs=0.001)
    # aspect is uniform 0.92-0.95 — proposed should sit at p5 - margin.
    assert result.shape_gate.aspect_min == pytest.approx(
        result.aspect_p5 - 0.05, abs=0.001
    )


def test_fit_shape_gate_clamps_to_zero():
    """Even if p5 - margin goes negative, propose 0.0."""
    records = [_synthetic_record(10, fill=0.04, aspect=0.03)]
    result = fit_shape_gate_from_records(
        records, reject_percentile=5.0, safety_margin=0.5,
    )
    assert result.shape_gate.fill_min == 0.0
    assert result.shape_gate.aspect_min == 0.0


# ----- selector fit ------------------------------------------------


def test_fit_selector_recovers_radius():
    # mask_area = π·r² → r = sqrt(area/π). For r=12 → area ≈ 452.
    target_r = 12.0
    target_area = int(round(math.pi * target_r * target_r))
    records = [_synthetic_record(50, area=target_area)]
    result = fit_selector_from_records(records)
    assert result.tuning.r_px_expected == pytest.approx(target_r, abs=0.5)
    # w_area / w_dist / dist_cost_sat_radii stay at defaults.
    from candidate_selector import CandidateSelectorTuning
    default = CandidateSelectorTuning.default()
    assert result.tuning.w_area == default.w_area
    assert result.tuning.w_dist == default.w_dist
    assert result.tuning.dist_cost_sat_radii == default.dist_cost_sat_radii


def test_fit_selector_skips_zero_area():
    records = [_synthetic_record(5, area=0)]
    with pytest.raises(ValueError):
        fit_selector_from_records(records)
