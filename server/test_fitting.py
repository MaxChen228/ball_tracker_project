"""Tests for the ballistic trajectory fit + RANSAC outlier rejection in
`fitting.py`."""
from __future__ import annotations

import numpy as np
import pytest

from fitting import evaluate, fit_trajectory
from schemas import TriangulatedPoint


def _make_ballistic_points(
    n: int = 20,
    v0: tuple[float, float, float] = (0.5, -35.0, 2.5),
    p0: tuple[float, float, float] = (0.0, 18.44, 1.8),
    g: float = 9.81,
    duration_s: float = 0.45,
    noise_m: float = 0.0,
    triangulation_residual_m: float = 0.003,
    seed: int = 0,
) -> list[TriangulatedPoint]:
    """Synthetic ground-truth ballistic trajectory. Default rig resembles a
    pitch from ~18 m → plate with ~35 m/s closure rate. Noise is iid
    Gaussian on the 3D position."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0.0, duration_s, n)
    x = p0[0] + v0[0] * t
    y = p0[1] + v0[1] * t
    z = p0[2] + v0[2] * t - 0.5 * g * t * t
    if noise_m > 0:
        x += rng.normal(scale=noise_m, size=n)
        y += rng.normal(scale=noise_m, size=n)
        z += rng.normal(scale=noise_m, size=n)
    return [
        TriangulatedPoint(
            t_rel_s=float(t[i]),
            x_m=float(x[i]),
            y_m=float(y[i]),
            z_m=float(z[i]),
            residual_m=triangulation_residual_m,
        )
        for i in range(n)
    ]


def test_fit_clean_ballistic_recovers_gravity_and_flags_no_outliers():
    points = _make_ballistic_points(n=30, noise_m=0.0)
    fit = fit_trajectory(points)
    assert fit is not None
    # z-axis quadratic term = -g/2 ≈ -4.905.
    assert fit.coeffs_z[0] == pytest.approx(-4.905, abs=1e-3)
    assert fit.inlier_indices == list(range(30))
    assert fit.outlier_indices == []
    assert fit.rms_m < 1e-6


def test_fit_rejects_gross_outliers_with_ransac():
    points = _make_ballistic_points(n=30, noise_m=0.002, seed=1)
    # Corrupt three frames with ~40 cm spurious offsets (ball-confused-for-
    # background-highlight scenario).
    points[5] = points[5].model_copy(update={"x_m": points[5].x_m + 0.4})
    points[12] = points[12].model_copy(update={"z_m": points[12].z_m - 0.45})
    points[20] = points[20].model_copy(update={"y_m": points[20].y_m + 0.6})
    fit = fit_trajectory(points)
    assert fit is not None
    # All three should be tagged as outliers; clean points all inliers.
    assert set(fit.outlier_indices).issuperset({5, 12, 20})
    # Quadratic term should still be near the true gravity even with noise
    # + outliers, because the refit runs on the RANSAC inlier set.
    assert fit.coeffs_z[0] == pytest.approx(-4.905, abs=0.3)


def test_fit_plate_crossing_is_near_y_zero():
    points = _make_ballistic_points(n=30, noise_m=0.0, duration_s=0.55)
    fit = fit_trajectory(points)
    assert fit is not None
    assert fit.plate_xyz_m is not None
    # y coordinate of plate is literally 0.
    assert fit.plate_xyz_m[1] == pytest.approx(0.0, abs=1e-9)
    # Evaluating the fit at plate_t_s should match plate_xyz_m.
    pt = evaluate(fit, fit.plate_t_s)[0]
    assert pt[0] == pytest.approx(fit.plate_xyz_m[0], abs=1e-6)
    assert pt[1] == pytest.approx(0.0, abs=1e-6)
    assert pt[2] == pytest.approx(fit.plate_xyz_m[2], abs=1e-6)


def test_fit_without_plate_crossing_returns_none_for_plate_fields():
    # Ball that never reaches Y=0 within the window + slack: start at
    # Y=5, end at Y=2, Y monotonically decreasing but not fast enough.
    n = 20
    t = np.linspace(0.0, 0.4, n)
    points = [
        TriangulatedPoint(
            t_rel_s=float(t[i]),
            x_m=0.0,
            y_m=5.0 - 7.5 * float(t[i]),  # y(0.4+0.5)=5-7.5*0.9=-1.75 → actually crosses
            z_m=1.5,
            residual_m=0.002,
        )
        for i in range(n)
    ]
    # Adjust so it definitely does NOT cross within [t_min-0.5, t_max+0.5]:
    points = [p.model_copy(update={"y_m": 5.0 - 1.0 * p.t_rel_s}) for p in points]
    fit = fit_trajectory(points)
    assert fit is not None
    assert fit.plate_xyz_m is None
    assert fit.plate_t_s is None


def test_fit_too_few_points_returns_none():
    points = _make_ballistic_points(n=2)
    assert fit_trajectory(points) is None


def test_fit_minimum_points_uses_direct_lsq():
    # 3 points: exact quadratic, skip RANSAC, all inliers.
    points = _make_ballistic_points(n=3, noise_m=0.0)
    fit = fit_trajectory(points)
    assert fit is not None
    assert fit.inlier_indices == [0, 1, 2]
    assert fit.outlier_indices == []


def test_fit_is_deterministic_for_same_input():
    points = _make_ballistic_points(n=25, noise_m=0.003, seed=42)
    points[3] = points[3].model_copy(update={"x_m": points[3].x_m + 0.5})
    a = fit_trajectory(points)
    b = fit_trajectory(points)
    assert a is not None and b is not None
    assert a.coeffs_x == b.coeffs_x
    assert a.coeffs_y == b.coeffs_y
    assert a.coeffs_z == b.coeffs_z
    assert a.inlier_indices == b.inlier_indices


def test_fit_evaluate_matches_coeffs_directly():
    points = _make_ballistic_points(n=20, noise_m=0.0)
    fit = fit_trajectory(points)
    assert fit is not None
    pts = evaluate(fit, np.array([0.0, 0.1, 0.2]))
    assert pts.shape == (3, 3)
    # At t=0 the quadratic evaluates to the constant term of each axis.
    assert pts[0, 0] == pytest.approx(fit.coeffs_x[2], abs=1e-9)
    assert pts[0, 1] == pytest.approx(fit.coeffs_y[2], abs=1e-9)
    assert pts[0, 2] == pytest.approx(fit.coeffs_z[2], abs=1e-9)
