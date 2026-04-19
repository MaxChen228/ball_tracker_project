"""Ballistic trajectory fitting + RANSAC outlier rejection.

Input: the per-frame triangulated points produced by `pairing.triangulate_cycle`
(already sorted by anchor-relative time). Output: a `TrajectoryFit` carrying
the best-fit 3D quadratic `p(t) = p0 + v0·t + ½·a·t²`, the set of inlier
indices, RMS residual on those inliers, and two derived milestones —
`release` (point at earliest inlier time) and `plate_crossing` (the fit
evaluated at Y = 0, when a real-valued crossing exists).

Each axis is an independent quadratic in `t`, so the fit is three 3-parameter
least-squares problems and RANSAC samples from the minimum set `k=3`. This
is robust to the 240 fps near-noise-free case AND the 10-15% gross-outlier
case we routinely see when one phone briefly confuses a background highlight
for the ball.
"""
from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

from schemas import TrajectoryFit, TriangulatedPoint

logger = logging.getLogger(__name__)

# Below this many triangulated frames a RANSAC quadratic is meaningless —
# just do a plain least-squares fit over whatever we have and call every
# point an inlier. Min for an exact per-axis quadratic is 3.
_MIN_FRAMES_FOR_RANSAC = 6

# RANSAC threshold on 3D residual (metres): max(_RESIDUAL_FLOOR_M,
# _RESIDUAL_SCALE × median triangulation residual). The floor catches the
# case where triangulation is near-perfect (residual ~1 mm) but one
# frame's ball detection is off by a ball-radius or more.
_RESIDUAL_FLOOR_M = 0.05
_RESIDUAL_SCALE = 3.0

_RANSAC_ITERATIONS = 50
_RANSAC_SAMPLE_SIZE = 3  # minimum for a 3-param quadratic per axis


def _fit_quadratic(t: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Solve the normal equations for `y = a·t² + b·t + c`. Returns (a, b, c).
    Caller guarantees `len(t) >= 3`."""
    # np.polyfit order is highest-degree-first, i.e. [a, b, c] already.
    return np.polyfit(t, y, 2)


def _eval_quadratic(coeffs: np.ndarray, t: np.ndarray) -> np.ndarray:
    a, b, c = coeffs
    return a * t * t + b * t + c


def _fit_all_axes(
    t: np.ndarray, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return _fit_quadratic(t, xs), _fit_quadratic(t, ys), _fit_quadratic(t, zs)


def _residuals_3d(
    t: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    zs: np.ndarray,
    coeffs_x: np.ndarray,
    coeffs_y: np.ndarray,
    coeffs_z: np.ndarray,
) -> np.ndarray:
    dx = xs - _eval_quadratic(coeffs_x, t)
    dy = ys - _eval_quadratic(coeffs_y, t)
    dz = zs - _eval_quadratic(coeffs_z, t)
    return np.sqrt(dx * dx + dy * dy + dz * dz)


def _plate_crossing(
    coeffs_y: np.ndarray,
    coeffs_x: np.ndarray,
    coeffs_z: np.ndarray,
    t_min: float,
    t_max: float,
) -> tuple[float, float, float, float] | None:
    """Solve y(t)=0 for t and return (t, x, y=0, z). When the quadratic has
    no real roots in [t_min, t_max + 0.5s] the return is None — the ball
    never reaches the plate plane in the observed window. The 0.5 s slack
    lets us extrapolate slightly past the last detected frame, which is
    typical when the catcher's mitt occludes the last few samples."""
    a, b, c = coeffs_y
    slack_s = 0.5
    t_hi = t_max + slack_s
    t_lo = t_min - slack_s
    if abs(a) < 1e-9:
        # Degenerate: y is linear. Solve b·t + c = 0.
        if abs(b) < 1e-9:
            return None
        candidates = [-c / b]
    else:
        disc = b * b - 4.0 * a * c
        if disc < 0:
            return None
        sqrt_disc = float(np.sqrt(disc))
        candidates = [
            (-b - sqrt_disc) / (2.0 * a),
            (-b + sqrt_disc) / (2.0 * a),
        ]
    # Prefer the root inside [t_lo, t_hi]; if both qualify, take the
    # earlier one (ball reaches the plate on its way in, not on any
    # bounce-back after).
    candidates = [r for r in candidates if t_lo <= r <= t_hi]
    if not candidates:
        return None
    t_cross = float(min(candidates))
    x_cross = float(_eval_quadratic(coeffs_x, np.array([t_cross]))[0])
    z_cross = float(_eval_quadratic(coeffs_z, np.array([t_cross]))[0])
    return (float(t_cross), x_cross, 0.0, z_cross)


def fit_trajectory(points: Sequence[TriangulatedPoint]) -> TrajectoryFit | None:
    """Run RANSAC quadratic fit over `points`. Returns None when the fit
    cannot be produced (fewer than 3 points, or the linear system is
    singular). The per-axis independence assumption holds because world-
    frame axes are orthogonal and the dominant dynamic (gravity) acts on
    Z alone — X/Y pick up only small residual acceleration that the full
    9-parameter fit absorbs cleanly."""
    n = len(points)
    if n < 3:
        logger.debug("fit skip reason=too_few_points n=%d", n)
        return None

    t = np.asarray([p.t_rel_s for p in points], dtype=float)
    xs = np.asarray([p.x_m for p in points], dtype=float)
    ys = np.asarray([p.y_m for p in points], dtype=float)
    zs = np.asarray([p.z_m for p in points], dtype=float)
    residuals_triang = np.asarray([p.residual_m for p in points], dtype=float)

    # Adaptive threshold: floor at 5 cm so tiny triangulation residuals
    # don't make the inlier cone absurdly tight.
    threshold = max(
        _RESIDUAL_FLOOR_M,
        _RESIDUAL_SCALE * float(np.median(residuals_triang)),
    )

    if n < _MIN_FRAMES_FOR_RANSAC:
        # Too few to make RANSAC meaningful; single LSQ on everything.
        try:
            cx, cy, cz = _fit_all_axes(t, xs, ys, zs)
        except np.linalg.LinAlgError as e:
            logger.warning("fit direct-LSQ failed: %s", e)
            return None
        errs = _residuals_3d(t, xs, ys, zs, cx, cy, cz)
        inlier_idx = list(range(n))
        outlier_idx: list[int] = []
        rms = float(np.sqrt(np.mean(errs * errs)))
    else:
        rng = np.random.default_rng(seed=_seed_from_points(t, xs))
        best_inliers: list[int] = []
        best_coeffs: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        for _ in range(_RANSAC_ITERATIONS):
            sample = rng.choice(n, size=_RANSAC_SAMPLE_SIZE, replace=False)
            try:
                cx = _fit_quadratic(t[sample], xs[sample])
                cy = _fit_quadratic(t[sample], ys[sample])
                cz = _fit_quadratic(t[sample], zs[sample])
            except np.linalg.LinAlgError:
                continue
            errs = _residuals_3d(t, xs, ys, zs, cx, cy, cz)
            inliers = [i for i in range(n) if errs[i] <= threshold]
            if len(inliers) > len(best_inliers):
                best_inliers = inliers
                best_coeffs = (cx, cy, cz)

        if best_coeffs is None or len(best_inliers) < _RANSAC_SAMPLE_SIZE:
            # RANSAC degenerated; fall back to LSQ on everything.
            logger.warning(
                "fit RANSAC degenerate n=%d threshold=%.4f — falling back to LSQ",
                n, threshold,
            )
            try:
                cx, cy, cz = _fit_all_axes(t, xs, ys, zs)
            except np.linalg.LinAlgError as e:
                logger.warning("fit LSQ fallback failed: %s", e)
                return None
            errs = _residuals_3d(t, xs, ys, zs, cx, cy, cz)
            inlier_idx = list(range(n))
            outlier_idx = []
            rms = float(np.sqrt(np.mean(errs * errs)))
        else:
            # Refit on inlier set — RANSAC found the group, least squares
            # now gives the precise coefficients.
            inlier_idx = sorted(best_inliers)
            outlier_idx = [i for i in range(n) if i not in set(inlier_idx)]
            try:
                cx, cy, cz = _fit_all_axes(
                    t[inlier_idx], xs[inlier_idx], ys[inlier_idx], zs[inlier_idx]
                )
            except np.linalg.LinAlgError as e:
                logger.warning("fit refit failed: %s", e)
                return None
            errs_in = _residuals_3d(
                t[inlier_idx], xs[inlier_idx], ys[inlier_idx], zs[inlier_idx],
                cx, cy, cz,
            )
            rms = float(np.sqrt(np.mean(errs_in * errs_in)))

    t_min = float(t[inlier_idx[0]])
    t_max = float(t[inlier_idx[-1]])

    # Release = earliest inlier point on the fit curve. Using the fit value
    # (not the raw triangulated point) so release is smooth with the rest
    # of the curve — matters when the earliest point is itself noisy.
    release = (
        t_min,
        float(_eval_quadratic(cx, np.array([t_min]))[0]),
        float(_eval_quadratic(cy, np.array([t_min]))[0]),
        float(_eval_quadratic(cz, np.array([t_min]))[0]),
    )

    plate = _plate_crossing(cy, cx, cz, t_min, t_max)

    logger.info(
        "fit done n=%d inliers=%d outliers=%d rms=%.4f threshold=%.4f",
        n, len(inlier_idx), len(outlier_idx), rms, threshold,
    )

    return TrajectoryFit(
        coeffs_x=[float(v) for v in cx],
        coeffs_y=[float(v) for v in cy],
        coeffs_z=[float(v) for v in cz],
        t_min_s=t_min,
        t_max_s=t_max,
        inlier_indices=inlier_idx,
        outlier_indices=outlier_idx,
        rms_m=rms,
        threshold_m=float(threshold),
        release_xyz_m=[release[1], release[2], release[3]],
        release_t_s=release[0],
        plate_xyz_m=[plate[1], plate[2], plate[3]] if plate else None,
        plate_t_s=plate[0] if plate else None,
    )


def _seed_from_points(t: np.ndarray, xs: np.ndarray) -> int:
    """Deterministic RNG seed derived from the point set. RANSAC is
    intrinsically stochastic but making it deterministic per input keeps
    re-triangulation on server restart bit-exact, which the test suite
    and the forensic viewer rely on."""
    # Mix a handful of bytes from the data. Exact algorithm doesn't
    # matter — only that the same input yields the same seed.
    data = np.concatenate([t[:8], xs[:8]]).tobytes()
    return int.from_bytes(data[:8], "little", signed=False) & 0x7FFFFFFF


def evaluate(fit: TrajectoryFit, t_s: float | np.ndarray) -> np.ndarray:
    """Evaluate the fitted quadratic at `t_s`. Accepts a scalar or array,
    returns an (N, 3) array. The viewer uses this to densify the curve for
    rendering."""
    t_arr = np.atleast_1d(np.asarray(t_s, dtype=float))
    cx = np.asarray(fit.coeffs_x)
    cy = np.asarray(fit.coeffs_y)
    cz = np.asarray(fit.coeffs_z)
    xs = _eval_quadratic(cx, t_arr)
    ys = _eval_quadratic(cy, t_arr)
    zs = _eval_quadratic(cz, t_arr)
    return np.stack([xs, ys, zs], axis=-1)
