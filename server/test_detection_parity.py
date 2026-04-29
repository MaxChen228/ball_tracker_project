"""Parity fixture tests for `detect_ball`.

Three synthetic scenes exercise the Python detector end-to-end:
    1. clean     — ball on uniform dark background
    2. blur      — ball with Gaussian blur simulating motion smearing
    3. cluttered — ball over noise + a non-ball distracter

The goal is a stable regression harness: any change to HSV /
aspect / fill / area constants should either keep these detections
inside the tolerance band or be an intentional retune (in which case
update the tolerances *and* sync the iOS `BallDetector.mm` constants).

TODO(iOS parity): when any shape / area constant moves here, manually
walk through `ball_tracker/BallDetector.mm` and verify the Obj-C++
side (kMinAspect / kMinFill / kMinArea / kMaxArea, plus any new
temporal-prior integration) is in lock-step. iOS ships in a separate
build cycle so the two ends drift easily.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from detection import HSVRange, detect_ball


def _yg_bgr() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 210, 210]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


@pytest.fixture
def hsv():
    return HSVRange.default()


def test_clean_scene(hsv):
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    # dim grey background (outside HSV range).
    img[:] = (30, 30, 30)
    cv2.circle(img, (640, 360), 22, _yg_bgr(), thickness=-1)

    out = detect_ball(img, hsv)
    assert out is not None
    cx, cy = out
    assert abs(cx - 640) < 2.0
    assert abs(cy - 360) < 2.0


def test_motion_blur_scene(hsv):
    """Heavy Gaussian blur simulates motion smearing at 240 fps. The
    ball's aspect stays ~1 but fill drops. Must still be found within
    a few pixels of centre."""
    img = np.full((720, 1280, 3), 30, dtype=np.uint8)
    cv2.circle(img, (800, 400), 22, _yg_bgr(), thickness=-1)
    img = cv2.GaussianBlur(img, (11, 11), sigmaX=3.0)

    out = detect_ball(img, hsv)
    assert out is not None
    cx, cy = out
    assert abs(cx - 800) < 4.0
    assert abs(cy - 400) < 4.0


def test_cluttered_scene_without_prior(hsv):
    """Ball + a non-ball-colour distracter + uniform noise. HSV mask
    cleanly rejects the distracter (blue), so the only candidate is
    the yellow-green ball. No temporal prior needed."""
    rng = np.random.default_rng(seed=0)
    noise = rng.integers(0, 50, size=(720, 1280, 3), dtype=np.uint8)
    img = noise.astype(np.uint8)
    # Real ball.
    cv2.circle(img, (500, 300), 20, _yg_bgr(), thickness=-1)
    # Blue-ish distracter that HSV will reject.
    cv2.rectangle(img, (900, 500), (1100, 700), (200, 50, 50), thickness=-1)

    out = detect_ball(img, hsv)
    assert out is not None
    cx, cy = out
    assert abs(cx - 500) < 3.0
    assert abs(cy - 300) < 3.0


def test_cluttered_scene_size_prior_picks_expected_radius(hsv):
    """Two yellow-green blobs differing in size; selector picks the one
    closer to expected_area = π·r_px_expected². Locks the shape-prior
    contract as a regression guard."""
    from candidate_selector import CandidateSelectorTuning
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[:] = (30, 30, 30)
    cv2.circle(img, (400, 360), 12, _yg_bgr(), thickness=-1)   # ~452 px², near r=12 expected
    cv2.circle(img, (900, 360), 28, _yg_bgr(), thickness=-1)   # ~2460 px², way too big

    # r_px_expected=12 → small ball wins.
    near = detect_ball(
        img, hsv,
        selector_tuning=CandidateSelectorTuning(
            r_px_expected=12.0, w_size=1.0, w_aspect=0.0, w_fill=0.0,
        ),
    )
    assert near is not None
    assert abs(near[0] - 400) < 3

    # r_px_expected=28 → big ball wins (now it's at expected size).
    far = detect_ball(
        img, hsv,
        selector_tuning=CandidateSelectorTuning(
            r_px_expected=28.0, w_size=1.0, w_aspect=0.0, w_fill=0.0,
        ),
    )
    assert far is not None
    assert abs(far[0] - 900) < 3
