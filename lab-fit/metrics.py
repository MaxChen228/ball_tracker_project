"""Per-segment quality metrics.

These are diagnostic — they don't gate anything in production. The point
is to put numbers on "how good is this segment" so we can build intuition
about the failure modes before deciding what to add to the segmenter.

All functions take a Segment-like object with attributes
(indices, p0, v0, t_anchor, t_start, t_end, rmse_m) and a pts array
of shape (N, 5) [t, x, y, z, residual]. They return scalars.
"""

from __future__ import annotations

import numpy as np

G = np.array([0.0, 0.0, -9.81])


def duration_s(seg) -> float:
    return float(seg.t_end - seg.t_start)


def n_points(seg) -> int:
    return len(seg.indices)


def chord_speed_mps(seg, pts) -> float:
    """Head-to-tail chord length / Δt. Useful as a sanity check vs |v0|."""
    a = pts[seg.indices[0], 1:4]
    b = pts[seg.indices[-1], 1:4]
    return float(np.linalg.norm(b - a) / max(seg.t_end - seg.t_start, 1e-9))


def point_density(seg, pts, frame_interval_s: float = 1 / 240) -> float:
    """n_points / (duration / frame_interval). 1.0 = every frame contributed.
    Low values mean fit is interpolating across dropouts."""
    expected = duration_s(seg) / frame_interval_s
    if expected <= 0:
        return 0.0
    return n_points(seg) / expected


def max_inner_gap_s(seg, pts) -> float:
    """Largest Δt between consecutive points inside the segment. The
    'cross-hole extrapolation' detector — large gap means fit has no
    observation in the middle."""
    ts = np.sort(pts[seg.indices, 0])
    if len(ts) < 2:
        return 0.0
    return float(np.max(np.diff(ts)))


def rmse_to_path_ratio(seg, pts) -> float:
    """RMSE / chord_length — RMSE normalized by the trajectory's spatial
    extent. <0.05 = clean; >0.20 = junk fit."""
    a = pts[seg.indices[0], 1:4]
    b = pts[seg.indices[-1], 1:4]
    chord = float(np.linalg.norm(b - a))
    if chord < 1e-3:
        return float("inf")
    return seg.rmse_m / chord


def loo_rmse_m(seg, pts) -> float:
    """Leave-one-out cross-validated RMSE. For each point in the segment,
    refit on N-1 points and compute residual at the held-out point. Robust
    to leverage points and 'fit memorized the noise' situations."""
    if len(seg.indices) < 6:
        return float("nan")
    sub = pts[seg.indices]
    held_residuals = []
    for k in range(sub.shape[0]):
        train_mask = np.ones(sub.shape[0], dtype=bool)
        train_mask[k] = False
        train = sub[train_mask]
        t_anchor = float(train[0, 0])
        tau = train[:, 0] - t_anchor
        A = np.column_stack([np.ones_like(tau), tau])
        p0 = np.zeros(3)
        v0 = np.zeros(3)
        for axis in range(3):
            rhs = train[:, 1 + axis] - 0.5 * G[axis] * tau * tau
            coef, *_ = np.linalg.lstsq(A, rhs, rcond=None)
            p0[axis] = coef[0]
            v0[axis] = coef[1]
        # Predict at held-out point
        t_held = sub[k, 0]
        tau_h = t_held - t_anchor
        pred = p0 + v0 * tau_h + 0.5 * G * tau_h * tau_h
        held_residuals.append(np.linalg.norm(sub[k, 1:4] - pred))
    return float(np.sqrt(np.mean(np.array(held_residuals) ** 2)))


def per_axis_rmse(seg, pts) -> tuple[float, float, float]:
    """(x_rmse, y_rmse, z_rmse). z is the gravity-pinned axis — typically
    largest. Disproportionate x or y rmse points to stereo triangulation
    issues, not motion modeling."""
    sub = pts[seg.indices]
    t_anchor = seg.t_anchor
    tau = sub[:, 0] - t_anchor
    out = []
    for axis in range(3):
        pred = seg.p0[axis] + seg.v0[axis] * tau + 0.5 * G[axis] * tau * tau
        out.append(float(np.sqrt(np.mean((sub[:, 1 + axis] - pred) ** 2))))
    return tuple(out)  # type: ignore


def all_metrics(seg, pts, frame_interval_s: float = 1 / 240) -> dict:
    rx, ry, rz = per_axis_rmse(seg, pts)
    return {
        "n_points": n_points(seg),
        "duration_s": duration_s(seg),
        "rmse_m": seg.rmse_m,
        "speed_mps": float(np.linalg.norm(seg.v0)),
        "chord_speed_mps": chord_speed_mps(seg, pts),
        "point_density": point_density(seg, pts, frame_interval_s),
        "max_inner_gap_s": max_inner_gap_s(seg, pts),
        "rmse_to_path_ratio": rmse_to_path_ratio(seg, pts),
        "loo_rmse_m": loo_rmse_m(seg, pts),
        "rmse_x": rx,
        "rmse_y": ry,
        "rmse_z": rz,
    }
