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


def _scale_intrinsics(intr: IntrinsicsPayload, sx: float, sy: float) -> IntrinsicsPayload:
    # Pixel-unit quantities scale with resolution; radial/tangential
    # distortion coefficients are dimensionless and stay put.
    return IntrinsicsPayload(
        fx=intr.fx * sx,
        fz=intr.fz * sy,
        cx=intr.cx * sx,
        cy=intr.cy * sy,
        distortion=list(intr.distortion) if intr.distortion is not None else None,
    )


def _scale_homography(h: list[float], sx: float, sy: float) -> list[float]:
    # H maps (X,Y,1) on the plate plane to (u,v,1) pixel coords. Rescaling
    # pixels to (sx·u, sy·v) left-multiplies by diag(sx, sy, 1). Normalise
    # H[2,2] back to 1 so downstream code that assumes the convention keeps
    # working.
    H = np.array(h, dtype=float).reshape(3, 3)
    H_new = np.diag([sx, sy, 1.0]) @ H
    if abs(H_new[2, 2]) > 1e-12:
        H_new = H_new / H_new[2, 2]
    return H_new.flatten().tolist()


def scale_pitch_to_video_dims(
    pitch: PitchPayload,
    calibration_dims: tuple[int, int] | None,
) -> PitchPayload:
    """Return a copy of `pitch` whose intrinsics + homography match the MOV's
    pixel grid.

    The iPhone persists intrinsics at calibration time (typically 1920×1080)
    but may record the pitch MOV at a lower resolution (e.g. 1280×720) once
    the resolution picker lands. Server detection yields pixel coordinates
    in the MOV's grid, so `build_K` + `recover_extrinsics` must use intrinsics
    that live on that same grid or triangulation goes systemically wrong.
    This helper rescales fx/fy/cx/cy and H's first two rows by the ratio
    between MOV dims and calibration dims.

    No-op paths (the input is returned unchanged):
      - pitch has no intrinsics / homography / image dims
      - no calibration snapshot cached for this camera
      - calibration dims already equal MOV dims
    """
    if (
        pitch.intrinsics is None
        or pitch.homography is None
        or pitch.image_width_px is None
        or pitch.image_height_px is None
        or calibration_dims is None
    ):
        return pitch
    ref_w, ref_h = calibration_dims
    if ref_w <= 0 or ref_h <= 0:
        return pitch
    if ref_w == pitch.image_width_px and ref_h == pitch.image_height_px:
        # No scaling needed, but still sanity-check the intrinsics that
        # will actually drive triangulation — catches the dims-match-but-
        # basis-is-wrong case where calibration metadata agrees with the
        # MOV grid yet cx/cy sit far off centre because the intrinsics
        # were baked from a cropped 4:3 source and someone lied about
        # the scale history.
        _log_intrinsics_sanity(
            pitch.intrinsics,
            pitch.image_width_px,
            pitch.image_height_px,
            pitch.camera_id,
            pitch.session_id,
        )
        return pitch
    sx = pitch.image_width_px / ref_w
    sy = pitch.image_height_px / ref_h
    logger.info(
        "scaling intrinsics/homography camera=%s session=%s "
        "calib=%dx%d video=%dx%d sx=%.4f sy=%.4f",
        pitch.camera_id, pitch.session_id,
        ref_w, ref_h, pitch.image_width_px, pitch.image_height_px, sx, sy,
    )
    scaled_intrinsics = _scale_intrinsics(pitch.intrinsics, sx, sy)
    # After rescaling, cx/cy should live near the MOV's image centre. Log
    # whenever they don't — that's almost always a basis-dims mismatch
    # (calibration recorded at grid A, intrinsics actually baked at B)
    # which would otherwise produce silently-wrong 3D positions.
    _log_intrinsics_sanity(
        scaled_intrinsics,
        pitch.image_width_px,
        pitch.image_height_px,
        pitch.camera_id,
        pitch.session_id,
    )
    return pitch.model_copy(
        update={
            "intrinsics": scaled_intrinsics,
            "homography": _scale_homography(pitch.homography, sx, sy),
        }
    )


# Principal point (cx, cy) is expected to sit near the image centre for a
# correctly-calibrated main rear camera — deviations of a few percent are
# normal from sensor/lens decentering, but anything beyond this tolerance
# usually means the intrinsics basis doesn't match the declared image
# dimensions (e.g. calibration baked at 4032×3024 but snapshot claims
# 1920×1080, with no rescale applied). We WARN rather than reject so
# forensic inspection still works — a silently wrong triangulation is
# worse than an explicit warning in logs.
_PRINCIPAL_POINT_MAX_OFFSET_FRAC = 0.15


def _log_intrinsics_sanity(
    intr: IntrinsicsPayload,
    image_w: int | None,
    image_h: int | None,
    camera_id: str,
    session_id: str,
) -> None:
    """Sanity-check the intrinsics against the declared image dimensions.

    Emits a single INFO log per pitch summarising (cx/w, cy/h) and a
    WARNING when either fraction deviates from 0.5 by more than
    `_PRINCIPAL_POINT_MAX_OFFSET_FRAC`. The common silent-failure pattern
    in this codebase is: calibration ships intrinsics baked at grid A
    while the pitch metadata claims grid B with no scale applied —
    manifesting as cx/cy being offset nowhere near image centre.
    """
    if image_w is None or image_h is None or image_w <= 0 or image_h <= 0:
        return
    cx_frac = intr.cx / float(image_w)
    cy_frac = intr.cy / float(image_h)
    cx_off = abs(cx_frac - 0.5)
    cy_off = abs(cy_frac - 0.5)
    if cx_off > _PRINCIPAL_POINT_MAX_OFFSET_FRAC or cy_off > _PRINCIPAL_POINT_MAX_OFFSET_FRAC:
        logger.warning(
            "intrinsics principal-point OFF camera=%s session=%s "
            "cx=%.1f cy=%.1f dims=%dx%d cx/w=%.3f cy/h=%.3f "
            "(expected ~0.5, tolerance ±%.2f) — basis/dims mismatch likely",
            camera_id, session_id, intr.cx, intr.cy, image_w, image_h,
            cx_frac, cy_frac, _PRINCIPAL_POINT_MAX_OFFSET_FRAC,
        )
    else:
        logger.info(
            "intrinsics principal-point ok camera=%s session=%s "
            "cx/w=%.3f cy/h=%.3f dims=%dx%d",
            camera_id, session_id, cx_frac, cy_frac, image_w, image_h,
        )


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


def _frame_items(p: PitchPayload, *, source: str = "server"):
    """Ball-bearing frames as `(t_rel, θx, θz, px, py)`, sorted by
    anchor-relative time. `t_rel = timestamp_s − sync_anchor_timestamp_s`.

    `source` is kept as a parameter for API compatibility; only `"server"`
    is supported now — it reads `p.frames_server_post` (the authoritative
    server-side or live detection result projected onto `frames_server_post`
    by the caller via `detection_paths.pitch_with_path_frames`)."""
    frames = p.frames_server_post
    anchor = p.sync_anchor_timestamp_s
    out = [
        (f.timestamp_s - anchor, f.theta_x_rad, f.theta_z_rad, f.px, f.py)
        for f in frames if _valid_frame(f)
    ]
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(
    a: PitchPayload, b: PitchPayload, *, source: str = "server",
) -> list[TriangulatedPoint]:
    """Pair A and B frames within an 8 ms window of anchor-relative time and
    run ray-midpoint triangulation. Requires intrinsics + homography on both
    cameras."""
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a, source=source)
    items_b = _frame_items(b, source=source)

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
        "pairing cycle complete session_id=%s source=%s pairs_in_a=%d pairs_in_b=%d "
        "pairs_out=%d drop_outside_window=%d drop_near_parallel=%d max_dt=%.6f",
        a.session_id, source, len(items_a), len(items_b), len(results),
        drop_outside_window, drop_near_parallel, _MAX_DT_S,
    )
    return results
