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

from schemas import BlobCandidate, IntrinsicsPayload, FramePayload, PitchPayload, TriangulatedPoint
from pairing_tuning import PairingTuning
from triangulate import (
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
        fy=intr.fy * sy,
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
    K = build_K(intr.fx, intr.fy, intr.cx, intr.cy)
    H = np.array(H_list, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, R, t, C


def _ray_for_frame(
    px: float,
    py: float,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
) -> np.ndarray:
    """Undistorted-pixel ray for one frame. Zero-distortion fallback when
    `dist_coeffs` is absent — equivalent to a pinhole projection."""
    coeffs = (
        np.asarray(dist_coeffs, dtype=float)
        if dist_coeffs is not None
        else np.zeros(5, dtype=float)
    )
    return undistorted_ray_cam(px, py, K, coeffs)


def triangulate_live_pair(
    pose_a,  # live_pairing.CameraPose
    pose_b,  # live_pairing.CameraPose
    frame_a: FramePayload,
    frame_b: FramePayload,
    *,
    anchor_a: float,
    anchor_b: float,
    tuning: PairingTuning,
) -> list[TriangulatedPoint]:
    """Hot-path multi-candidate triangulation for the live A/B ray pair.

    Iterates every (frame_a.candidates × frame_b.candidates) combination,
    runs ray-midpoint triangulation per pair, filters by selector cost
    and skew-line gap from `tuning`, returns all survivors. Empty list
    when no candidates pair up (no ball detected, outside time window,
    all near-parallel, or all gap-rejected) — same failure surface as
    `triangulate_cycle`.

    Bypasses `triangulate_pair` / `scale_pitch_to_video_dims` because the
    live path's intrinsics are already the calibration snapshot itself
    (no resolution delta → scale factor 1). Uses pre-cached K/R/C/dist
    on each `CameraPose` so every pair only pays the ray math + 2×2
    solve."""
    if not (_valid_frame(frame_a) and _valid_frame(frame_b)):
        return []

    t_rel = frame_a.timestamp_s - anchor_a
    t_b_rel = frame_b.timestamp_s - anchor_b
    if abs(t_b_rel - t_rel) > _MAX_DT_S:
        return []

    cands_a = _frame_candidates(frame_a)
    cands_b = _frame_candidates(frame_b)
    if not cands_a or not cands_b:
        return []

    out: list[TriangulatedPoint] = []
    for ca_idx, ca in enumerate(cands_a):
        if ca.cost is not None and ca.cost > tuning.cost_threshold:
            continue
        d_a_cam = _ray_for_frame(ca.px, ca.py, pose_a.K, pose_a.dist)
        d_a_world = pose_a.R.T @ d_a_cam
        for cb_idx, cb in enumerate(cands_b):
            if cb.cost is not None and cb.cost > tuning.cost_threshold:
                continue
            d_b_cam = _ray_for_frame(cb.px, cb.py, pose_b.K, pose_b.dist)
            d_b_world = pose_b.R.T @ d_b_cam
            P, gap = triangulate_rays(pose_a.C, d_a_world, pose_b.C, d_b_world)
            if P is None or gap > tuning.gap_threshold_m:
                continue
            out.append(TriangulatedPoint(
                t_rel_s=t_rel,
                x_m=float(P[0]),
                y_m=float(P[1]),
                z_m=float(P[2]),
                residual_m=gap,
                source_a_cand_idx=ca_idx,
                source_b_cand_idx=cb_idx,
                cost_a=ca.cost,
                cost_b=cb.cost,
            ))
    return out


def _frame_candidates(f: FramePayload) -> list[BlobCandidate]:
    """Return the candidates the fan-out loop should iterate.

    Production wire (post the iOS aspect/fill landing) always populates
    `frame.candidates`. Legacy persisted JSONs and minimal test
    fixtures sometimes ship `frame.px / frame.py` only with no
    candidates list — synthesize a single-candidate stand-in from the
    resolved winner so the same code path triangulates them too. The
    synthetic candidate carries area=0 / cost=None, fine because the
    selector cost is no longer read by the cost gate when cost is None.
    Returns empty list when neither candidates nor px/py is usable."""
    if f.candidates:
        return list(f.candidates)
    if f.px is None or f.py is None:
        return []
    return [BlobCandidate(
        px=float(f.px), py=float(f.py),
        area=0, area_score=0.0,
        aspect=None, fill=None, cost=None,
    )]


def _valid_frame(f: FramePayload) -> bool:
    """A frame is pair-able iff it has at least one usable candidate
    (real or synthesized from a resolved winner — see
    `_frame_candidates`)."""
    return bool(_frame_candidates(f))


def _frame_items(p: PitchPayload, *, source: str = "server"):
    """Ball-bearing frames as `(t_rel, frame)`, sorted by anchor-relative
    time. `t_rel = timestamp_s − sync_anchor_timestamp_s`. Caller iterates
    `frame.candidates` for fan-out triangulation.

    `source` is kept as a parameter for API compatibility; only `"server"`
    is supported now — it reads `p.frames_server_post` (which the caller
    populates via `pitch_with_path_frames` so live-path triangulation can
    reuse the same routine)."""
    frames = p.frames_server_post
    anchor = p.sync_anchor_timestamp_s
    out = [
        (f.timestamp_s - anchor, f)
        for f in frames if _valid_frame(f)
    ]
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(
    a: PitchPayload, b: PitchPayload, *, source: str = "server",
    tuning: PairingTuning | None = None,
) -> list[TriangulatedPoint]:
    """Pair A and B frames within an 8 ms window of anchor-relative time
    and run multi-candidate fan-out triangulation. Each matched frame
    pair iterates every (A.candidates × B.candidates) combination,
    filters by `tuning.cost_threshold` + `tuning.gap_threshold_m`, and
    emits all survivors. Requires intrinsics + homography on both
    cameras. Default tuning (`PairingTuning.default()`) emits every
    shape-gate-passed candidate (cost_threshold=1.0) and caps the
    skew-line residual at 0.20m — sole authority for residual culling
    (segmenter and viewer trust this gate, no re-filter downstream)."""
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    if tuning is None:
        tuning = PairingTuning.default()

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a, source=source)
    items_b = _frame_items(b, source=source)

    drop_outside_window = 0
    drop_near_parallel = 0
    drop_gap = 0
    drop_cost = 0
    results: list[TriangulatedPoint] = []

    if items_a and items_b:
        # `_frame_items` already sorts by t_rel, so we can binary-search
        # for each A frame's nearest B in O(log N) instead of the
        # full-array O(N) argmin scan.
        b_times = np.array([x[0] for x in items_b])
        n_b = len(b_times)
        dist_a = a.intrinsics.distortion
        dist_b = b.intrinsics.distortion

        for t_rel, frame_a in items_a:
            ins = int(np.searchsorted(b_times, t_rel))
            cands = [i for i in (ins - 1, ins) if 0 <= i < n_b]
            idx = min(cands, key=lambda i: abs(b_times[i] - t_rel))
            dt = float(b_times[idx] - t_rel)
            if abs(dt) > _MAX_DT_S:
                drop_outside_window += 1
                logger.debug(
                    "pairing drop reason=outside_window t_rel=%.6f dt=%.6f max_dt=%.6f",
                    t_rel, dt, _MAX_DT_S,
                )
                continue
            _, frame_b = items_b[idx]

            cands_a = _frame_candidates(frame_a)
            cands_b = _frame_candidates(frame_b)
            for ca_idx, ca in enumerate(cands_a):
                if ca.cost is not None and ca.cost > tuning.cost_threshold:
                    drop_cost += 1
                    continue
                d_a_cam = _ray_for_frame(ca.px, ca.py, K_a, dist_a)
                d_a_world = R_a.T @ d_a_cam
                for cb_idx, cb in enumerate(cands_b):
                    if cb.cost is not None and cb.cost > tuning.cost_threshold:
                        drop_cost += 1
                        continue
                    d_b_cam = _ray_for_frame(cb.px, cb.py, K_b, dist_b)
                    d_b_world = R_b.T @ d_b_cam
                    P, gap = triangulate_rays(C_a, d_a_world, C_b, d_b_world)
                    if P is None:
                        drop_near_parallel += 1
                        continue
                    if gap > tuning.gap_threshold_m:
                        drop_gap += 1
                        continue
                    results.append(TriangulatedPoint(
                        t_rel_s=t_rel,
                        x_m=float(P[0]),
                        y_m=float(P[1]),
                        z_m=float(P[2]),
                        residual_m=gap,
                        source_a_cand_idx=ca_idx,
                        source_b_cand_idx=cb_idx,
                        cost_a=ca.cost,
                        cost_b=cb.cost,
                    ))

    logger.info(
        "pairing cycle complete session_id=%s source=%s frames_in_a=%d frames_in_b=%d "
        "points_out=%d drop_outside_window=%d drop_near_parallel=%d "
        "drop_gap=%d drop_cost=%d max_dt=%.6f cost_thr=%.3f gap_thr=%.3f",
        a.session_id, source, len(items_a), len(items_b), len(results),
        drop_outside_window, drop_near_parallel, drop_gap, drop_cost,
        _MAX_DT_S, tuning.cost_threshold, tuning.gap_threshold_m,
    )
    return results
