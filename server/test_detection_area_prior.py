"""Unit tests for the area-prior path in detection.

Covers `area_bounds_from_radius_prior`, `detect_ball(expected_radius_px=...)`,
and `geometry_priors.expected_ball_radius_px` — enough to lock the
physics + no-silent-fallback rules.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from detection import (
    HSVRange,
    area_bounds_from_radius_prior,
    detect_ball,
)
from geometry_priors import (
    TENNIS_BALL_RADIUS_M,
    expected_ball_radius_px,
)


def _yellow_green_bgr() -> tuple[int, int, int]:
    hsv = np.uint8([[[40, 200, 200]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


# --- area_bounds_from_radius_prior -------------------------------------

def test_area_bounds_basic():
    # r=20 → min=π(10)² ≈ 314, max=π(36)² ≈ 4071.
    lo, hi = area_bounds_from_radius_prior(20.0)
    assert 310 <= lo <= 320
    assert 4060 <= hi <= 4080


def test_area_bounds_rejects_non_positive():
    with pytest.raises(ValueError):
        area_bounds_from_radius_prior(0.0)
    with pytest.raises(ValueError):
        area_bounds_from_radius_prior(-5.0)
    with pytest.raises(ValueError):
        area_bounds_from_radius_prior(float("nan"))
    with pytest.raises(ValueError):
        area_bounds_from_radius_prior(float("inf"))


# --- detect_ball with prior --------------------------------------------

def test_detect_ball_prior_rejects_oversized_blob():
    """A huge yellow-green blob (r≈60 px) sits well outside the
    r=20 prior band [π*10², π*36²] ≈ [314, 4071] — it must be rejected,
    whereas the loose fallback [20, 150_000] would accept it."""
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (320, 240), 60, _yellow_green_bgr(), thickness=-1)

    # With prior: reject.
    assert detect_ball(img, HSVRange.default(), expected_radius_px=20.0) is None
    # Without prior: accepted (loose bounds).
    assert detect_ball(img, HSVRange.default(), expected_radius_px=None) is not None


def test_detect_ball_prior_accepts_expected_size():
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(img, (320, 240), 20, _yellow_green_bgr(), thickness=-1)
    out = detect_ball(img, HSVRange.default(), expected_radius_px=20.0)
    assert out is not None


# --- geometry_priors ---------------------------------------------------

def test_expected_ball_radius_matches_physics():
    """Build a minimal pinhole + plate-plane rig and verify the
    returned radius matches r = fx * R / Z.

    Camera sits at world (0, 0, 3) looking along -Z (standard plate-
    facing pose). Homography maps world (X, Y, 0) → pixel via
        u = fx * X/Z + cx
        v = fy * (-Y)/Z + cy
    For a camera-at-origin-of-its-own-frame with R identity and
    t=(0,0,3), the column layout is H = K [r1 r2 t] where world X
    goes to camera X (r1=[1,0,0]), world Y goes to camera -Y so that
    Y-axis points forward from the plate to the catcher: keep it
    simple — we just need a non-degenerate H and check plate distance.
    """
    fx, fy, cx, cy = 1400.0, 1400.0, 960.0, 540.0
    # Construct H from known R, t and compose H = K [r1 r2 t].
    R = np.eye(3)
    t = np.array([0.0, 0.0, 3.0])
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])
    H = K @ np.column_stack([R[:, 0], R[:, 1], t])
    H = H / H[2, 2]  # normalize h33=1

    r_px = expected_ball_radius_px(
        fx, fy, cx, cy, H.flatten().tolist()
    )
    # Z_plate = ‖C - 0‖ where C = -R^T t = (0, 0, -3), so ‖C‖ = 3 m.
    expected = fx * TENNIS_BALL_RADIUS_M / 3.0
    assert math.isclose(r_px, expected, rel_tol=1e-6)


def test_expected_ball_radius_raises_on_degenerate():
    # Wrong length.
    with pytest.raises(ValueError):
        expected_ball_radius_px(1400, 1400, 960, 540, [1, 0, 0])
    # Non-finite.
    with pytest.raises(ValueError):
        expected_ball_radius_px(
            1400, 1400, 960, 540, [1, 0, 0, 0, 1, 0, 0, 0, float("nan")]
        )
    # Non-positive fx.
    with pytest.raises(ValueError):
        expected_ball_radius_px(-1, 1400, 960, 540, [1]*9)
