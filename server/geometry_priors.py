"""Per-session geometry priors for detection.

Given a camera's `IntrinsicsPayload` + `homography`, we can estimate how
large (in pixels) a real tennis ball will appear when it's near home
plate. That prior narrows `detect_ball`'s area gate from the loose
[20, 150_000] fallback to a realistic band — culling distant yellow-
green clutter (the other cam's tripod, a coach's shirt) and spurious
giant blobs without touching shape thresholds.

Physics:
    r_px ≈ fx * R_real / Z_plate
where
    R_real = 0.033 m   (ITF tennis ball radius ≈ 6.6 cm diameter)
    Z_plate = ‖camera_center_world - plate_origin‖ in meters
    fx     = intrinsics.fx (pixels)

Plate origin is world (0, 0, 0) by convention — the homography maps
the plate plane (Z=0) to image pixels, so decomposing H = K[r1 r2 t]
gives camera pose in world coords and `-R^T t` is the camera center.

No silent fallback: callers that cannot compute a prior must explicitly
pass `expected_radius_px=None` to `detect_ball` and accept the loose
bounds; this module raises on degenerate inputs rather than returning
a bogus radius.
"""
from __future__ import annotations

import logging
import math

import numpy as np

from triangulate import build_K, camera_center_world, recover_extrinsics

logger = logging.getLogger(__name__)


# ITF standard tennis ball diameter 6.54–6.86 cm → radius ~0.033 m.
# Constant (not a knob) because this is a physical property of the
# ball, not a tuning parameter. Swap only when the ball type changes.
TENNIS_BALL_RADIUS_M = 0.033


def expected_ball_radius_px(
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    homography_row_major: list[float] | tuple[float, ...],
    *,
    ball_radius_m: float = TENNIS_BALL_RADIUS_M,
) -> float:
    """Project a physical ball radius to pixels at the plate distance
    recovered from `homography_row_major` (9 floats, row-major, h33=1).

    Raises `ValueError` on degenerate K / H. Uses fx only (not fy) for
    the pixel conversion — matches the horizontal pixel pitch the
    detector bbox width is measured in. Returns a float; callers round
    or pass directly to `detect_ball(expected_radius_px=...)`.
    """
    if len(homography_row_major) != 9:
        raise ValueError(
            f"homography must be 9 floats, got {len(homography_row_major)}"
        )
    if not all(math.isfinite(v) for v in homography_row_major):
        raise ValueError("homography contains non-finite values")
    if not (math.isfinite(fx) and fx > 0):
        raise ValueError(f"fx must be finite positive, got {fx!r}")
    if ball_radius_m <= 0:
        raise ValueError(f"ball_radius_m must be positive, got {ball_radius_m!r}")

    K = build_K(fx, fy, cx, cy)
    H = np.asarray(homography_row_major, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)  # raises ValueError on degenerate H
    C = camera_center_world(R, t)    # camera center in world frame

    # Plate origin is (0, 0, 0) — distance from camera to plate center.
    # Using the full Euclidean distance (not just C[2]) because the
    # optical axis may not be vertical; the pythagorean distance is
    # the physically-meaningful "how far is the ball from the lens"
    # when the ball is at plate center.
    z_plate_m = float(np.linalg.norm(C))
    if not math.isfinite(z_plate_m) or z_plate_m <= 0:
        raise ValueError(f"degenerate plate distance: {z_plate_m}")

    return fx * ball_radius_m / z_plate_m
