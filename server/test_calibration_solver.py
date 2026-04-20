"""Unit tests for `calibration_solver`. Uses projected ground-truth
correspondences (skipping the ArUco detector stage, which needs a real
image — covered separately by the Phase 5 integration test once the
calibration endpoint lands)."""
from __future__ import annotations

import numpy as np
import pytest

from calibration_solver import (
    PLATE_MARKER_WORLD,
    DetectedMarker,
    derive_fov_intrinsics,
    solve_homography,
)


def _synthetic_detections(
    H_true: np.ndarray,
    marker_half_size_px: float = 8.0,
) -> list[DetectedMarker]:
    """Project each plate marker's world centroid through `H_true` to get
    the ground-truth image centroid, then sprinkle a synthetic 4-corner
    square around it so `solve_homography`'s centroid-of-corners math
    sees the right point."""
    detections: list[DetectedMarker] = []
    for mid, (wx, wy) in PLATE_MARKER_WORLD.items():
        w_hom = np.array([wx, wy, 1.0])
        proj = H_true @ w_hom
        cx, cy = proj[:2] / proj[2]
        s = marker_half_size_px
        corners = np.array([
            [cx - s, cy - s],
            [cx + s, cy - s],
            [cx + s, cy + s],
            [cx - s, cy + s],
        ])
        detections.append(DetectedMarker(id=mid, corners=corners))
    return detections


def test_solve_homography_recovers_known_matrix():
    # Arbitrary invertible plate→image homography.
    H_true = np.array([
        [1200.0,  50.0, 960.0],
        [-80.0, 900.0, 540.0],
        [ 0.02,   0.01,   1.0],
    ])
    H_true = H_true / H_true[2, 2]
    detections = _synthetic_detections(H_true)
    result = solve_homography(detections, image_size=(1920, 1080))
    assert result is not None
    H_est = np.array(result.homography_row_major).reshape(3, 3)
    # Exact recovery (within float noise) because the synthetic
    # correspondences are noise-free.
    assert np.allclose(H_est, H_true, atol=1e-6)
    assert result.image_width_px == 1920
    assert result.image_height_px == 1080


def test_solve_homography_tolerates_one_missing_marker():
    H_true = np.eye(3)
    detections = _synthetic_detections(H_true)
    detections = [m for m in detections if m.id != 3]
    result = solve_homography(detections, image_size=(1280, 720))
    assert result is not None
    assert 3 in result.missing_ids
    assert 3 not in result.detected_ids


def test_solve_homography_returns_none_when_too_few_markers():
    H_true = np.eye(3)
    detections = _synthetic_detections(H_true)[:4]  # only 4 < min 5
    assert solve_homography(detections, image_size=(1920, 1080)) is None


def test_solve_homography_handles_ransac_outlier():
    """One gross outlier (a marker whose corners land 500 px away from
    the projected world point) must be rejected by RANSAC, so the other
    5 still give the correct H."""
    H_true = np.array([
        [1200.0, 0.0, 960.0],
        [ 0.0, 1200.0, 540.0],
        [ 0.0,   0.0,   1.0],
    ])
    detections = _synthetic_detections(H_true)
    # Corrupt marker 2 — push its corners far off.
    bad = detections[2]
    detections[2] = DetectedMarker(id=bad.id, corners=bad.corners + 500.0)
    result = solve_homography(detections, image_size=(1920, 1080))
    assert result is not None
    H_est = np.array(result.homography_row_major).reshape(3, 3)
    # Should be close to ground truth despite the outlier.
    # Project marker 0 world pos through both and compare.
    w = np.array([*PLATE_MARKER_WORLD[0], 1.0])
    p_true = (H_true @ w)[:2] / (H_true @ w)[2]
    p_est = (H_est @ w)[:2] / (H_est @ w)[2]
    assert np.linalg.norm(p_est - p_true) < 2.0


def test_derive_fov_intrinsics_matches_ios_formula():
    h_fov = np.radians(60.0)
    fx, fy, cx, cy = derive_fov_intrinsics(1920, 1080, h_fov)
    expected_fx = (1920 / 2.0) / np.tan(h_fov / 2.0)
    assert fx == pytest.approx(expected_fx)
    assert cx == pytest.approx(960.0)
    assert cy == pytest.approx(540.0)
    assert 1600 < fy < 1700  # 16:9 @ 60° hFOV → ~36° vFOV → fy ≈ 1664


def test_derive_fov_intrinsics_rejects_invalid_input():
    with pytest.raises(ValueError):
        derive_fov_intrinsics(0, 1080, 1.0)
    with pytest.raises(ValueError):
        derive_fov_intrinsics(1920, 1080, 0.0)
    with pytest.raises(ValueError):
        derive_fov_intrinsics(1920, 1080, np.pi)
