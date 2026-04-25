"""Tests for the dashboard-tunable candidate-selector knobs.

Covers:
  - `select_best_candidate` honours the explicit `w_area` / `w_dist` /
    `dist_cost_sat_radii` kwargs (no module-level fallback).
  - `detect_ball` threads `selector_tuning` through to the selector so
    extreme weights flip the winner end-to-end.
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


def test_zero_w_dist_falls_back_to_area_winner():
    """w_dist=0 → distance term has no influence; the larger blob wins
    even when it's far from the predicted point."""
    near_small = Candidate(cx=102.0, cy=100.0, area=400, area_score=0.4)
    far_big = Candidate(cx=500.0, cy=500.0, area=1000, area_score=1.0)
    winner = select_best_candidate(
        [near_small, far_big],
        prev_position=(100.0, 100.0),
        prev_velocity=(50.0, 0.0),
        dt=1.0 / 240,
        r_px_expected=12.0,
        w_area=1.0,
        w_dist=0.0,
        dist_cost_sat_radii=8.0,
    )
    assert winner is far_big


def test_full_w_dist_picks_nearest_to_prediction():
    """w_dist=1.0 → ignore area entirely, pick the candidate closest to
    the predicted point even if it's the smallest blob in the batch."""
    near_small = Candidate(cx=101.0, cy=100.0, area=10, area_score=0.01)
    far_big = Candidate(cx=900.0, cy=900.0, area=10_000, area_score=1.0)
    winner = select_best_candidate(
        [near_small, far_big],
        prev_position=(100.0, 100.0),
        prev_velocity=(0.0, 0.0),
        dt=1.0 / 240,
        r_px_expected=12.0,
        w_area=0.0,
        w_dist=1.0,
        dist_cost_sat_radii=8.0,
    )
    assert winner is near_small


def test_tight_saturation_collapses_distance_cost():
    """A very small `dist_cost_sat_radii` saturates the distance cost
    even for nearby candidates — every candidate ends up with cost ≈ 1
    on the distance term, so area decides."""
    near_small = Candidate(cx=110.0, cy=100.0, area=10, area_score=0.01)
    far_big = Candidate(cx=200.0, cy=100.0, area=10_000, area_score=1.0)
    winner = select_best_candidate(
        [near_small, far_big],
        prev_position=(100.0, 100.0),
        prev_velocity=(0.0, 0.0),
        dt=1.0 / 240,
        r_px_expected=12.0,
        w_area=0.5,
        w_dist=0.5,
        dist_cost_sat_radii=1.0,  # both are >>1 radius away → saturated
    )
    assert winner is far_big


def test_default_tuning_classmethod_round_trip():
    t = CandidateSelectorTuning.default()
    assert t.r_px_expected == 12.0
    assert abs(t.w_area + t.w_dist - 1.0) < 1e-9
    assert t.dist_cost_sat_radii == 8.0


def test_detect_ball_respects_selector_tuning():
    """End-to-end: same image + prior, two extreme tunings → two
    different winners. Confirms the kwarg actually reaches the
    selector through the pipeline."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)  # small, near-pred
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)  # big distracter
    prev_pos = (200.0, 240.0)
    prev_vel = (4800.0, 0.0)  # +20 px / frame @ 240 fps
    dt = 1.0 / 240

    # Distance-dominated → near small ball wins.
    dist_tuning = CandidateSelectorTuning(
        r_px_expected=12.0, w_area=0.0, w_dist=1.0, dist_cost_sat_radii=8.0,
    )
    near = detect_ball(
        img, HSVRange.default(),
        expected_radius_px=12.0,
        prev_position=prev_pos, prev_velocity=prev_vel, dt=dt,
        selector_tuning=dist_tuning,
    )
    assert near is not None and abs(near[0] - 220) < 3

    # Area-dominated → big distracter wins despite being far from the
    # predicted point.
    area_tuning = CandidateSelectorTuning(
        r_px_expected=12.0, w_area=1.0, w_dist=0.0, dist_cost_sat_radii=8.0,
    )
    far = detect_ball(
        img, HSVRange.default(),
        expected_radius_px=12.0,
        prev_position=prev_pos, prev_velocity=prev_vel, dt=dt,
        selector_tuning=area_tuning,
    )
    assert far is not None and abs(far[0] - 500) < 3
