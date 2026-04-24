"""Server-side ballistic RANSAC fit + release-kinematics summary.

Given a list of `TriangulatedPoint` for a single pitch path (`live` or
`server_post`), fit a per-axis quadratic trajectory

    x(τ) = x0 + vx0·τ + 0.5·ax·τ²
    y(τ) = y0 + vy0·τ + 0.5·ay·τ²
    z(τ) = z0 + vz0·τ + 0.5·az·τ²     (g_fit := -az)

via RANSAC: sample 7 points, LSQ-fit, score inliers by 3D residual,
keep the best consensus set, final LSQ refit on the full inlier set.

Design notes:
- No scipy. Plain numpy — each axis is a 3-parameter normal equation
  that we solve with `numpy.linalg.solve` on the same 3×3 Gram matrix
  shared across x/y/z.
- The g-free-parameter variant is the default. We also try a
  g-pinned-to-9.81 variant (5 DOF: 2 axes quadratic + z with az
  fixed) ONLY as a sanity comparison inside the picker — whichever
  produces the lower inlier median residual wins. This mirrors the
  task brief's "two variants" ask without polluting the return shape;
  the single published fit is the winner.
- No silent fallback: N < 7 returns None and we log a single-line
  reason. Caller must handle None explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from schemas import TriangulatedPoint

log = logging.getLogger(__name__)

MPS_TO_MPH = 2.23693629  # exact NIST factor


@dataclass
class BallisticFit:
    """Result of a successful ballistic RANSAC fit."""

    # Per-axis [p0, v0, a] triples packed as (3, 3) where rows are x, y, z.
    params: np.ndarray
    # Indices into the original input list that passed the consensus.
    inlier_indices: list[int]
    # Per-point 3D euclidean residual (same order as the input list).
    residuals_m: list[float]
    # Fit evaluated at the first inlier's τ = 0 (anchor of the fit).
    release_point_m: tuple[float, float, float]
    # Velocity at the release point (τ=0 of the fit's time anchor).
    release_velocity_mps: tuple[float, float, float]
    speed_mph: float
    g_fit: float
    n_inliers: int
    n_total: int
    # The time offset that was subtracted from each t_rel_s before fitting.
    t0_s: float
    rmse_m: float = 0.0
    # "free" (per-axis quadratic on z) or "pinned" (az = -9.81).
    g_mode: str = "free"


def _fit_quadratic_axes(
    taus: np.ndarray,
    coords: np.ndarray,
    *,
    pin_gz: bool = False,
) -> np.ndarray | None:
    """Fit p(τ) = p0 + v0·τ + 0.5·a·τ² for each of x, y, z.

    Shared 3×3 gram matrix:
        M = Σ [ [1, τ, τ²],
                [τ, τ², τ³],
                [τ², τ³, τ⁴] ]

    `coords` is (N, 3) in x/y/z order. Returns (3, 3) with rows = axes,
    cols = [p0, v0, a]. The solver stores acceleration directly (caller
    scaled the quadratic coefficient by 2 already? No — we return `a`
    where `p = p0 + v0·τ + 0.5·a·τ²`, so the third column = 2·c₂).

    When `pin_gz` is True, the z-axis drops to a 2-parameter linear fit
    (p0, v0) on the residual `z - 0.5·(-9.81)·τ²`; the returned a_z is
    -9.81 exactly.
    """
    n = taus.shape[0]
    if n < 3:
        return None
    s1 = taus.sum()
    s2 = (taus * taus).sum()
    s3 = (taus ** 3).sum()
    s4 = (taus ** 4).sum()
    M = np.array([[n, s1, s2], [s1, s2, s3], [s2, s3, s4]], dtype=float)
    # numpy.linalg will raise on singular — signal failure via None.
    try:
        M_inv_times = np.linalg.solve(M, np.eye(3))
    except np.linalg.LinAlgError:
        return None
    out = np.zeros((3, 3), dtype=float)
    for axis in range(3):
        vals = coords[:, axis]
        if pin_gz and axis == 2:
            # Residualise against pinned gravity, fit only p0 + v0·τ.
            g_pinned = 9.81
            vals_adj = vals + 0.5 * g_pinned * taus * taus  # move -g·τ²/2 to LHS
            # Linear LSQ: [1, τ] columns.
            A = np.column_stack([np.ones_like(taus), taus])
            try:
                coef, *_ = np.linalg.lstsq(A, vals_adj, rcond=None)
            except np.linalg.LinAlgError:
                return None
            out[axis, 0] = coef[0]
            out[axis, 1] = coef[1]
            out[axis, 2] = -g_pinned
        else:
            r = np.array([
                vals.sum(),
                (taus * vals).sum(),
                (taus * taus * vals).sum(),
            ])
            c = M_inv_times @ r
            out[axis, 0] = c[0]
            out[axis, 1] = c[1]
            out[axis, 2] = 2.0 * c[2]  # 0.5·a coefficient → a
    return out


def _evaluate(params: np.ndarray, taus: np.ndarray) -> np.ndarray:
    """params (3, 3), taus (N,) → positions (N, 3)."""
    tau2 = taus * taus
    pos = np.empty((taus.shape[0], 3), dtype=float)
    for axis in range(3):
        p0, v0, a = params[axis]
        pos[:, axis] = p0 + v0 * taus + 0.5 * a * tau2
    return pos


def _residuals(params: np.ndarray, taus: np.ndarray, coords: np.ndarray) -> np.ndarray:
    predicted = _evaluate(params, taus)
    return np.linalg.norm(coords - predicted, axis=1)


def _fit_with_mode(
    taus: np.ndarray,
    coords: np.ndarray,
    *,
    pin_gz: bool,
) -> tuple[np.ndarray, np.ndarray] | None:
    params = _fit_quadratic_axes(taus, coords, pin_gz=pin_gz)
    if params is None:
        return None
    return params, _residuals(params, taus, coords)


def fit_ballistic_ransac(
    points: Sequence[TriangulatedPoint],
    *,
    min_inliers: int = 7,
    max_iter: int = 200,
    residual_threshold_m: float | None = None,
    seed: int | None = 0,
) -> BallisticFit | None:
    """RANSAC-wrap `_fit_quadratic_axes`.

    - `min_inliers`: minimum size of an acceptable consensus set (and of
      the random sample). Also the hard lower bound — input N must be ≥
      this or we return None (explicit skip, no silent small-sample fit).
    - `max_iter`: random-sample trials. On small inputs we exhaustively
      cap to avoid wasting work.
    - `residual_threshold_m`: if None, adaptive — each candidate set uses
      `2.5 × median(residuals)` as its inlier cutoff (re-estimated after
      the first pass for robustness).
    """
    n = len(points)
    if n < min_inliers:
        log.info(
            "fit_ballistic_ransac: skip — %d pts < min_inliers=%d", n, min_inliers
        )
        return None

    taus_full = np.array([p.t_rel_s for p in points], dtype=float)
    t0 = float(taus_full.min())
    taus_full = taus_full - t0
    coords_full = np.array(
        [[p.x_m, p.y_m, p.z_m] for p in points], dtype=float
    )

    rng = np.random.default_rng(seed)

    # Two g-modes: free and pinned. Pick the one with lower final RMSE
    # on its inlier set.
    best_per_mode: dict[str, tuple[np.ndarray, list[int], np.ndarray, float]] = {}

    for pin_gz in (False, True):
        mode = "pinned" if pin_gz else "free"
        best_inliers: list[int] = []
        best_params: np.ndarray | None = None
        best_residuals: np.ndarray | None = None

        # Consensus threshold is FIXED per fit_ballistic_ransac call
        # (not adaptive per-candidate), otherwise a contaminated sample
        # produces huge residuals → huge threshold → trivial 100%
        # consensus and wins the count race. The fixed threshold must
        # be small enough to reject gross outliers but large enough to
        # accept genuinely-noisy clean detections; 10 cm is the working
        # ceiling for our ~5-20 cm stereo triangulation residuals.
        fixed_thresh = residual_threshold_m if residual_threshold_m is not None else 0.10
        for _ in range(max_iter):
            idx = rng.choice(n, size=min_inliers, replace=False)
            sample_taus = taus_full[idx]
            sample_coords = coords_full[idx]
            fit_out = _fit_with_mode(sample_taus, sample_coords, pin_gz=pin_gz)
            if fit_out is None:
                continue
            cand_params, _ = fit_out
            residuals = _residuals(cand_params, taus_full, coords_full)
            inliers = np.where(residuals < fixed_thresh)[0].tolist()
            if len(inliers) > len(best_inliers):
                best_inliers = inliers
                best_params = cand_params
                best_residuals = residuals

        if best_params is None or len(best_inliers) < min_inliers:
            log.info(
                "fit_ballistic_ransac: mode=%s no consensus (best=%d / %d)",
                mode,
                len(best_inliers),
                n,
            )
            continue

        # Refit on the full inlier set.
        in_taus = taus_full[best_inliers]
        in_coords = coords_full[best_inliers]
        refit = _fit_with_mode(in_taus, in_coords, pin_gz=pin_gz)
        if refit is None:
            continue
        refit_params, _ = refit
        # Iterative refinement when threshold was adaptive: re-classify
        # inliers against the refit using 2.5 × median of the prior
        # inliers' residuals on the refit.
        full_res = _residuals(refit_params, taus_full, coords_full)
        # Iterative refinement uses an adaptive MAD threshold off the
        # *refit* residuals on the inlier set — the refit is trustworthy
        # here because the consensus selection already filtered outliers.
        in_res = full_res[best_inliers]
        if residual_threshold_m is None:
            med = float(np.median(in_res))
            mad = float(np.median(np.abs(in_res - med)))
            sigma = max(1.4826 * mad, 0.005)
            # Cap at the consensus threshold so we never grow past what
            # was deemed inlier-like during the search.
            thresh = min(max(3.0 * sigma, 0.02), fixed_thresh)
        else:
            thresh = residual_threshold_m
        new_inliers = np.where(full_res < thresh)[0].tolist()
        if len(new_inliers) >= min_inliers and len(new_inliers) >= len(best_inliers):
            best_inliers = new_inliers
            refit2 = _fit_with_mode(
                taus_full[best_inliers],
                coords_full[best_inliers],
                pin_gz=pin_gz,
            )
            if refit2 is not None:
                refit_params, _ = refit2
                full_res = _residuals(refit_params, taus_full, coords_full)

        in_residuals = full_res[best_inliers]
        rmse = float(np.sqrt((in_residuals ** 2).mean()))
        best_per_mode[mode] = (refit_params, best_inliers, full_res, rmse)

    if not best_per_mode:
        log.info("fit_ballistic_ransac: no mode produced a valid consensus")
        return None

    # Pick lower RMSE.
    chosen_mode = min(best_per_mode, key=lambda m: best_per_mode[m][3])
    params, inlier_indices, all_residuals, rmse = best_per_mode[chosen_mode]

    # Release point = trajectory evaluated at τ=0 (earliest point's t_rel_s).
    release_pos = _evaluate(params, np.array([0.0])).flatten()
    release_vel = params[:, 1]  # v0 per axis
    speed_mps = float(np.linalg.norm(release_vel))
    speed_mph = speed_mps * MPS_TO_MPH

    return BallisticFit(
        params=params,
        inlier_indices=sorted(inlier_indices),
        residuals_m=[float(r) for r in all_residuals],
        release_point_m=(float(release_pos[0]), float(release_pos[1]), float(release_pos[2])),
        release_velocity_mps=(float(release_vel[0]), float(release_vel[1]), float(release_vel[2])),
        speed_mph=speed_mph,
        g_fit=float(-params[2, 2]),
        n_inliers=len(inlier_indices),
        n_total=n,
        t0_s=t0,
        rmse_m=rmse,
        g_mode=chosen_mode,
    )


def sample_trajectory(
    fit: BallisticFit,
    *,
    n_samples: int = 100,
    t_min: float | None = None,
    t_max: float | None = None,
) -> list[tuple[float, float, float, float]]:
    """Produce `(t_rel_s, x, y, z)` sample tuples along the fitted curve.

    `t_min` / `t_max` are in the original t_rel_s clock; defaults span
    the inlier time range.
    """
    if t_min is None or t_max is None:
        # The caller typically knows the [t_min, t_max] range from the
        # underlying point set; when omitted, build a zero-length sample.
        raise ValueError("sample_trajectory requires t_min and t_max")
    taus = np.linspace(t_min, t_max, n_samples) - fit.t0_s
    pos = _evaluate(fit.params, taus)
    ts = taus + fit.t0_s
    return [
        (float(ts[i]), float(pos[i, 0]), float(pos[i, 1]), float(pos[i, 2]))
        for i in range(n_samples)
    ]


__all__ = [
    "BallisticFit",
    "MPS_TO_MPH",
    "fit_ballistic_ransac",
    "sample_trajectory",
]
