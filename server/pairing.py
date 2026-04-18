"""Cross-camera frame pairing + stereo triangulation, extracted from main.py.

Given two `PitchPayload`s (one per camera) that share a server-minted
`session_id`, pair their ball-bearing frames within an 8 ms window of
anchor-relative time and run ray-midpoint triangulation to recover the
3D ball trajectory.
"""
from __future__ import annotations

import logging
import os

import numpy as np

from schemas import IntrinsicsPayload, FramePayload, PitchPayload, TriangulatedPoint
from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)

logger = logging.getLogger(__name__)

# Pairing window between A/B anchor-relative timestamps. 8.33 ms ≈ one frame at
# 240 fps; override via `BALL_TRACKER_MAX_DT_S` env var (in seconds) to widen
# the window for field diagnostics without recompiling.
_DEFAULT_MAX_DT_S = 1.0 / 120.0
_MAX_DT_S = float(os.environ.get("BALL_TRACKER_MAX_DT_S", _DEFAULT_MAX_DT_S))


def _camera_pose(intr: IntrinsicsPayload, H_list: list[float]):
    K = build_K(intr.fx, intr.fz, intr.cx, intr.cy)
    H = np.array(H_list, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, R, t, C


def _ray_for_frame(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
) -> np.ndarray:
    """Per-frame ray choice. Prefer the undistorted-pixel path whenever
    `px`/`py` are present (server detection always produces them); fall
    back to the on-device angle path only when pixels are missing.
    Zero-distortion is the default when `dist_coeffs` is absent — equivalent
    to the pinhole projection the angle path computes, so both yield the
    same ray for zero-distortion input."""
    if px is not None and py is not None:
        coeffs = (
            np.asarray(dist_coeffs, dtype=float)
            if dist_coeffs is not None
            else np.zeros(5, dtype=float)
        )
        return undistorted_ray_cam(px, py, K, coeffs)
    if theta_x is None or theta_z is None:
        raise ValueError("frame has neither usable angles nor pixels")
    return angle_ray_cam(theta_x, theta_z)


def _valid_frame(f: FramePayload) -> bool:
    has_angles = f.theta_x_rad is not None and f.theta_z_rad is not None
    has_pixels = f.px is not None and f.py is not None
    return f.ball_detected and (has_angles or has_pixels)


def _frame_items(p: PitchPayload):
    """Ball-bearing frames as `(t_rel, θx, θz, px, py)`, sorted by
    anchor-relative time. `t_rel = timestamp_s − sync_anchor_timestamp_s`."""
    anchor = p.sync_anchor_timestamp_s
    out = [
        (f.timestamp_s - anchor, f.theta_x_rad, f.theta_z_rad, f.px, f.py)
        for f in p.frames if _valid_frame(f)
    ]
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(a: PitchPayload, b: PitchPayload) -> list[TriangulatedPoint]:
    """Pair A and B frames within an 8 ms window of anchor-relative time and
    run ray-midpoint triangulation. Requires intrinsics + homography on both
    cameras."""
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a)
    items_b = _frame_items(b)

    drop_outside_window = 0
    drop_near_parallel = 0
    results: list[TriangulatedPoint] = []

    if items_a and items_b:
        b_times = np.array([x[0] for x in items_b])
        dist_a = a.intrinsics.distortion
        dist_b = b.intrinsics.distortion

        for t_rel, tx_a, tz_a, px_a, py_a in items_a:
            idx = int(np.argmin(np.abs(b_times - t_rel)))
            dt = float(b_times[idx] - t_rel)
            if abs(dt) > _MAX_DT_S:
                drop_outside_window += 1
                logger.debug(
                    "pairing drop reason=outside_window t_rel=%.6f dt=%.6f max_dt=%.6f",
                    t_rel, dt, _MAX_DT_S,
                )
                continue
            _, tx_b, tz_b, px_b, py_b = items_b[idx]

            d_a_cam = _ray_for_frame(tx_a, tz_a, px_a, py_a, K_a, dist_a)
            d_b_cam = _ray_for_frame(tx_b, tz_b, px_b, py_b, K_b, dist_b)
            d_a_world = R_a.T @ d_a_cam
            d_b_world = R_b.T @ d_b_cam

            P, gap = triangulate_rays(C_a, d_a_world, C_b, d_b_world)
            if P is None:
                # Near-parallel rays for this frame pair — no meaningful 3D point.
                drop_near_parallel += 1
                logger.debug(
                    "pairing drop reason=near_parallel t_rel=%.6f",
                    t_rel,
                )
                continue
            results.append(
                TriangulatedPoint(
                    t_rel_s=t_rel,
                    x_m=float(P[0]),
                    y_m=float(P[1]),
                    z_m=float(P[2]),
                    residual_m=gap,
                )
            )

    logger.info(
        "pairing cycle complete session_id=%s pairs_in_a=%d pairs_in_b=%d "
        "pairs_out=%d drop_outside_window=%d drop_near_parallel=%d max_dt=%.6f",
        a.session_id, len(items_a), len(items_b), len(results),
        drop_outside_window, drop_near_parallel, _MAX_DT_S,
    )
    return results
