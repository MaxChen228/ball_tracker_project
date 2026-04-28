"""Unit tests for `candidate_selector.select_best_candidate`."""
from __future__ import annotations

import cv2
import numpy as np

from candidate_selector import (
    Candidate,
    CandidateSelectorTuning,
    select_best_candidate,
)
from detection import HSVRange, detect_ball


_DEFAULT_T = CandidateSelectorTuning.default()
_DEFAULTS = dict(
    w_area=_DEFAULT_T.w_area,
    w_dist=_DEFAULT_T.w_dist,
    dist_cost_sat_radii=_DEFAULT_T.dist_cost_sat_radii,
)


def _yg() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def test_empty_returns_none():
    assert select_best_candidate([], **_DEFAULTS) is None


def test_fallback_largest_without_prior():
    small = Candidate(cx=10, cy=10, area=100, area_score=0.5)
    big = Candidate(cx=200, cy=200, area=200, area_score=1.0)
    assert select_best_candidate([small, big], **_DEFAULTS) is big


def test_temporal_prior_beats_largest():
    """Big distracter far from prediction loses to small candidate
    near the predicted trajectory point."""
    prev_pos = (100.0, 100.0)
    prev_vel = (50.0, 0.0)  # 50 px/s in x
    dt = 1.0 / 240  # one frame @ 240 fps
    r = 12.0
    # predicted = (100 + 50/240, 100) ≈ (100.2, 100)
    near_small = Candidate(cx=102.0, cy=100.0, area=400, area_score=0.4)
    far_big = Candidate(cx=500.0, cy=500.0, area=1000, area_score=1.0)
    winner = select_best_candidate(
        [near_small, far_big],
        prev_position=prev_pos,
        prev_velocity=prev_vel,
        dt=dt,
        r_px_expected=r,
        **_DEFAULTS,
    )
    assert winner is near_small


def test_temporal_prior_fallback_on_non_finite_dt():
    """dt=0 or inf → fallback to largest (explicit behavior, not silent)."""
    c_small = Candidate(cx=0, cy=0, area=10, area_score=0.1)
    c_big = Candidate(cx=9999, cy=9999, area=9999, area_score=1.0)
    out = select_best_candidate(
        [c_small, c_big],
        prev_position=(0, 0),
        prev_velocity=(0, 0),
        dt=0.0,
        r_px_expected=10.0,
        **_DEFAULTS,
    )
    assert out is c_big


def test_detect_ball_end_to_end_temporal_prefers_near_prediction():
    """Put the ball near a predicted point and a larger distracter far
    away; the selector must pick the ball despite being smaller."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)  # small, near-pred
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)  # big distracter
    prev_pos = (200.0, 240.0)
    prev_vel = (4800.0, 0.0)  # 4800 px/s => +20 px/frame @ 240 fps
    dt = 1.0 / 240
    out = detect_ball(
        img, HSVRange.default(),
        prev_position=prev_pos,
        prev_velocity=prev_vel,
        dt=dt,
    )
    assert out is not None
    cx, cy = out
    # ball sits at (220, 240), not at distracter (500, 100).
    assert abs(cx - 220) < 3 and abs(cy - 240) < 3


def test_detect_ball_no_prior_picks_largest():
    """Without prev state, detect_ball must still pick the biggest
    candidate — backward-compat with the old behavior."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (220, 240), 10, _yg(), thickness=-1)
    cv2.circle(img, (500, 100), 20, _yg(), thickness=-1)
    out = detect_ball(img, HSVRange.default())
    assert out is not None
    cx, cy = out
    # bigger distracter at (500, 100).
    assert abs(cx - 500) < 3 and abs(cy - 100) < 3
