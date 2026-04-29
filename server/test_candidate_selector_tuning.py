"""Tests for the dashboard-tunable candidate-selector knobs.

Covers:
  - `CandidateSelectorTuning.default()` produces the expected weights.
  - Extreme weights flip the winner end-to-end through `detect_ball`.
"""
from __future__ import annotations

import cv2
import numpy as np

from candidate_selector import (
    Candidate,
    CandidateSelectorTuning,
    select_best_candidate,
)
from detection import HSVRange, detect_ball


def _yg() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def test_default_tuning_classmethod_values():
    t = CandidateSelectorTuning.default()
    assert t.r_px_expected == 12.0
    assert t.w_size == 0.5
    assert t.w_aspect == 0.3
    assert t.w_fill == 0.2


def test_size_only_tuning_picks_expected_radius():
    """w_size=1, w_aspect=w_fill=0 → only size_pen matters; the blob
    closest to expected_area wins regardless of aspect/fill."""
    expected = Candidate(cx=0, cy=0, area=452, aspect=0.6, fill=0.0)  # bad shape
    too_small = Candidate(cx=0, cy=0, area=20, aspect=1.0, fill=0.68)  # great shape
    tuning = CandidateSelectorTuning(
        r_px_expected=12.0, w_size=1.0, w_aspect=0.0, w_fill=0.0,
    )
    assert select_best_candidate([expected, too_small], tuning) is expected


def test_aspect_only_tuning_picks_round():
    """w_aspect=1, others 0 → only roundness decides."""
    round_off_size = Candidate(cx=0, cy=0, area=20, aspect=1.0, fill=0.0)
    oblong_at_size = Candidate(cx=0, cy=0, area=452, aspect=0.6, fill=0.68)
    tuning = CandidateSelectorTuning(
        r_px_expected=12.0, w_size=0.0, w_aspect=1.0, w_fill=0.0,
    )
    assert select_best_candidate([round_off_size, oblong_at_size], tuning) is round_off_size


def test_fill_only_tuning_picks_typical_fill():
    """w_fill=1, others 0 → only fill closeness to 0.68 decides."""
    fill_typical = Candidate(cx=0, cy=0, area=20, aspect=0.6, fill=0.68)
    fill_off = Candidate(cx=0, cy=0, area=452, aspect=1.0, fill=1.0)
    tuning = CandidateSelectorTuning(
        r_px_expected=12.0, w_size=0.0, w_aspect=0.0, w_fill=1.0,
    )
    assert select_best_candidate([fill_typical, fill_off], tuning) is fill_typical


def test_detect_ball_respects_selector_tuning():
    """End-to-end: same image, two extreme tunings → two different
    winners. Confirms `selector_tuning` reaches through detect_ball."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    # Two filled circles, both perfectly round + opaque → aspect/fill
    # near-equal. Sizes differ: r=12 ≈ 452 px²; r=30 ≈ 2800 px². Size
    # weighting picks the right-sized one.
    cv2.circle(img, (220, 240), 12, _yg(), thickness=-1)  # near expected
    cv2.circle(img, (500, 100), 30, _yg(), thickness=-1)  # too big

    # r_px_expected=12 → small circle wins.
    near = detect_ball(
        img, HSVRange.default(),
        selector_tuning=CandidateSelectorTuning(
            r_px_expected=12.0, w_size=1.0, w_aspect=0.0, w_fill=0.0,
        ),
    )
    assert near is not None and abs(near[0] - 220) < 3

    # r_px_expected=30 → big circle wins (now it's at expected area).
    far = detect_ball(
        img, HSVRange.default(),
        selector_tuning=CandidateSelectorTuning(
            r_px_expected=30.0, w_size=1.0, w_aspect=0.0, w_fill=0.0,
        ),
    )
    assert far is not None and abs(far[0] - 500) < 3
