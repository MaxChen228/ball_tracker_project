"""Pure auto-calibration business logic — no FastAPI router glue.

This module owns the numpy / OpenCV pipeline that turns one full-res
JPEG frame from an iPhone into a CalibrationSnapshot:

    JPEG bytes
      → decode + 4:3 → 16:9 center-crop
      → ArUco detect (plate + extended markers)
      → derive intrinsics (ChArUco prior / cached snapshot / FOV fallback)
      → planar homography solve + PnP refinement
      → reprojection-error gate
      → rebuild K + H in canonical 1920×1080 video basis
      → snapshot ready for state.set_calibration

`routes/calibration.py` is now a thin FastAPI handler layer around
`_run_auto_calibration` and the helpers below. Tests / other routes
that need the pure helpers should import them from here directly.

`HTTPException` is imported only as the failure-signal class — this
module raises it so callers (FastAPI route handlers) get the right
status codes without needing wrapper try/except. It does not depend
on any FastAPI router / Request machinery.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from fastapi import HTTPException

from calibration_solver import (
    PLATE_MARKER_WORLD,
    derive_fov_intrinsics,
    detect_all_markers_in_dict,
    solve_homography_from_world_map,
)
from schemas import CalibrationSnapshot, IntrinsicsPayload
from state_calibration import REPROJ_FAIL_PX
from triangulate import build_K, camera_center_world, recover_extrinsics, triangulate_rays, undistorted_ray_cam

logger = logging.getLogger("ball_tracker")

# iPhone main (1x wide) rear camera horizontal FOV — empirically measured
# from the device's `activeFormat.videoFieldOfView` at 240 fps (73.828°).
# Used as the fallback when an uploaded ChArUco/AutoCal pose carries no
# explicit `h_fov_deg` so derived `fx`/`fy` match the rig's actual sensor
# rather than the historical 65° guess (which over-estimated fx by ~14%).
# See MEMORY: reference_iphone_camera_formats.md.
_IPHONE_MAIN_CAM_HFOV_RAD = 1.2885  # 73.828° measured

# Canonical snapshot dims. ArUco may detect at higher resolution (e.g.
# 12 MP photo capture cropped to 4032×2268), but the snapshot is always
# stored at standby video grid so live-path CameraPose construction +
# pitch-time pairing.scale_pitch_to_video_dims see a consistent basis.
# 720p path is handled at consume-time in state.ingest_live_frame.
_CANONICAL_SNAPSHOT_W = 1920
_CANONICAL_SNAPSHOT_H = 1080


def _center_crop_to_aspect(
    bgr: np.ndarray, target_ar: float, *, tol: float = 0.01,
) -> tuple[np.ndarray, int]:
    """Center-crop top/bottom (or left/right) so the image matches `target_ar`.

    Used at auto-calibration entry to convert iPhone 4:3 12 MP stills to
    the rig's 16:9 video basis BEFORE running ArUco. Without this the
    snapshot would carry 4:3 dims through pairing's pure-scale path,
    which can't represent the true 4:3↔16:9 sensor crop and silently
    misplaces fy/cy at pitch time.

    Returns (cropped_bgr, dy_offset). `dy_offset` is the pixel offset of
    the cropped region's top edge relative to the source image; 0 when
    no crop was needed (within tolerance).
    """
    h, w = bgr.shape[:2]
    src_ar = w / h
    if abs(src_ar - target_ar) / target_ar <= tol:
        return bgr, 0
    if src_ar < target_ar:
        # Source too tall (e.g. 4:3 → 16:9). Crop top + bottom.
        new_h = int(round(w / target_ar))
        dy = (h - new_h) // 2
        return bgr[dy : dy + new_h, :, :].copy(), dy
    # Source too wide. Crop left + right.
    new_w = int(round(h * target_ar))
    dx = (w - new_w) // 2
    return bgr[:, dx : dx + new_w, :].copy(), 0


def _decode_calibration_jpeg(jpeg_bytes: bytes) -> np.ndarray:
    import cv2  # noqa: WPS433

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=422, detail="calibration frame is not a decodable JPEG")
    return bgr


def _marker_camera_pose(snapshot: CalibrationSnapshot) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    K = build_K(
        snapshot.intrinsics.fx,
        snapshot.intrinsics.fy,
        snapshot.intrinsics.cx,
        snapshot.intrinsics.cy,
    )
    H = np.asarray(snapshot.homography, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H)
    C_world = camera_center_world(R_wc, t_wc)
    return K, R_wc, C_world


def _all_marker_world_xyz() -> dict[int, tuple[float, float, float]]:
    import main as _main
    state = _main.state

    world_xyz = {
        mid: (float(xy[0]), float(xy[1]), 0.0)
        for mid, xy in PLATE_MARKER_WORLD.items()
    }
    for rec in state._marker_registry.all_records():
        world_xyz[rec.marker_id] = (rec.x_m, rec.y_m, rec.z_m)
    return world_xyz


def _residual_bucket(residual_m: float) -> str:
    if residual_m <= 0.01:
        return "excellent"
    if residual_m <= 0.03:
        return "good"
    if residual_m <= 0.06:
        return "warn"
    return "poor"


def _triangulate_marker_candidates(
    *,
    camera_a_id: str,
    camera_b_id: str,
    bgr_a: np.ndarray,
    bgr_b: np.ndarray,
) -> dict[str, Any]:
    import main as _main
    state = _main.state

    snap_a = state.calibrations().get(camera_a_id)
    snap_b = state.calibrations().get(camera_b_id)
    if snap_a is None or snap_b is None:
        missing = [cid for cid, snap in ((camera_a_id, snap_a), (camera_b_id, snap_b)) if snap is None]
        raise HTTPException(
            status_code=422,
            detail=f"missing calibration for camera(s): {', '.join(missing)}",
        )

    K_a, R_a, C_a = _marker_camera_pose(snap_a)
    K_b, R_b, C_b = _marker_camera_pose(snap_b)
    dist_a = np.asarray(snap_a.intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    dist_b = np.asarray(snap_b.intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)

    det_a = {m.id: m for m in detect_all_markers_in_dict(bgr_a)}
    det_b = {m.id: m for m in detect_all_markers_in_dict(bgr_b)}
    existing = {rec.marker_id: rec for rec in state._marker_registry.all_records()}
    common_ids = sorted(set(det_a.keys()) & set(det_b.keys()))
    candidates: list[dict[str, Any]] = []
    for marker_id in common_ids:
        if marker_id in PLATE_MARKER_WORLD:
            continue
        centroid_a = det_a[marker_id].corners.mean(axis=0)
        centroid_b = det_b[marker_id].corners.mean(axis=0)
        ray_a_cam = undistorted_ray_cam(float(centroid_a[0]), float(centroid_a[1]), K_a, dist_a)
        ray_b_cam = undistorted_ray_cam(float(centroid_b[0]), float(centroid_b[1]), K_b, dist_b)
        ray_a_world = R_a.T @ ray_a_cam
        ray_b_world = R_b.T @ ray_b_cam
        point, gap = triangulate_rays(C_a, ray_a_world, C_b, ray_b_world)
        if point is None:
            continue
        existing_rec = existing.get(int(marker_id))
        delta_existing_m = None
        if existing_rec is not None:
            delta_existing_m = float(
                np.linalg.norm(
                    np.array([existing_rec.x_m, existing_rec.y_m, existing_rec.z_m], dtype=np.float64)
                    - point
                )
            )
        candidates.append(
            {
                "marker_id": int(marker_id),
                "x_m": float(point[0]),
                "y_m": float(point[1]),
                "z_m": float(point[2]),
                "residual_m": float(gap),
                "residual_bucket": _residual_bucket(float(gap)),
                "source_camera_ids": [camera_a_id, camera_b_id],
                "suggest_on_plate_plane": abs(float(point[2])) <= 0.03,
                "detected_in": [camera_a_id, camera_b_id],
                "existing_marker": existing_rec is not None,
                "existing_label": existing_rec.label if existing_rec is not None else None,
                "existing_on_plate_plane": existing_rec.on_plate_plane if existing_rec is not None else None,
                "delta_existing_m": delta_existing_m,
                "update_action": (
                    "keep"
                    if existing_rec is None
                    else ("refresh" if delta_existing_m is not None and delta_existing_m <= 0.03 else "conflict")
                ),
            }
        )
    only_a_ids = sorted(
        mid for mid in det_a.keys() - det_b.keys()
        if mid not in PLATE_MARKER_WORLD
    )
    only_b_ids = sorted(
        mid for mid in det_b.keys() - det_a.keys()
        if mid not in PLATE_MARKER_WORLD
    )
    return {
        "candidates": candidates,
        "visibility": {
            "shared_ids": [row["marker_id"] for row in candidates],
            "camera_a_only_ids": only_a_ids,
            "camera_b_only_ids": only_b_ids,
            "camera_a_detected_ids": sorted(det_a.keys()),
            "camera_b_detected_ids": sorted(det_b.keys()),
        },
    }


def _solve_pnp_homography(
    detected: list[Any],
    *,
    intrinsics: IntrinsicsPayload,
    image_size: tuple[int, int],
) -> tuple[list[float], list[int]] | None:
    import cv2  # noqa: WPS433

    world_xyz = _all_marker_world_xyz()
    markers_by_id = {m.id: m for m in detected if m.id in world_xyz}
    detected_ids = sorted(markers_by_id.keys())
    if len(detected_ids) < 4:
        return None
    object_pts = np.array([world_xyz[mid] for mid in detected_ids], dtype=np.float64)
    image_pts = np.array(
        [markers_by_id[mid].corners.mean(axis=0) for mid in detected_ids],
        dtype=np.float64,
    )
    if np.linalg.matrix_rank(object_pts - object_pts.mean(axis=0, keepdims=True)) < 3:
        return None
    K = build_K(intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy).astype(np.float64)
    dist = np.asarray(intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    ok, rvec, tvec, inliers = cv2.solvePnPRansac(
        object_pts,
        image_pts,
        K,
        dist,
        flags=cv2.SOLVEPNP_ITERATIVE,
        reprojectionError=4.0,
        iterationsCount=200,
        confidence=0.995,
    )
    if not ok:
        return None
    try:
        rvec, tvec = cv2.solvePnPRefineLM(object_pts, image_pts, K, dist, rvec, tvec)
    except Exception:
        pass
    R_wc, _ = cv2.Rodrigues(rvec)
    H = K @ np.column_stack([R_wc[:, 0], R_wc[:, 1], tvec.reshape(3)])
    if abs(H[2, 2]) < 1e-12:
        return None
    H = H / H[2, 2]
    inlier_ids = (
        [detected_ids[int(i)] for i in inliers.flatten().tolist()]
        if inliers is not None
        else detected_ids
    )
    return H.flatten().tolist(), inlier_ids


def _derive_auto_cal_intrinsics(
    camera_id: str,
    *,
    w_img: int,
    h_img: int,
    h_fov_deg: float | None = None,
) -> tuple[IntrinsicsPayload, CalibrationSnapshot | None]:
    """Pick the best intrinsics (K + distortion) to use for this auto-cal
    frame burst. Priority:

    1. ChArUco prior for the physical device currently playing this role
       (scaled from its source resolution to the current capture dims).
       Only taken when the AR matches within 2 % — otherwise we'd
       silently mis-scale cy across a 4:3 → 16:9 crop.
    2. Prior CalibrationSnapshot reused with the same AR-scaled fx/fy/cx/cy
       trick. Legacy path, kept so rigs without any ChArUco record still
       benefit from auto-cal's own accumulated intrinsics across runs.
    3. FOV-based pinhole approximation with zero distortion. Fallback of
       last resort — accurate to within a few percent of truth on a
       well-behaved wide cam, but markedly worse at frame edges.

    `h_fov_deg` explicitly supplied by the caller forces path 3 so an
    operator doing a fresh calibration can bypass any cached K when they
    suspect the prior is stale.
    """
    import main as _main
    state = _main.state

    if h_fov_deg is None:
        charuco = state.device_intrinsics_for_camera(camera_id)
        if charuco is not None and charuco.source_width_px > 0 and charuco.source_height_px > 0:
            # scale_intrinsics_to handles both AR-matching (pure scale) and
            # AR-mismatch (center crop + scale) cases. The latter is the
            # normal path when ChArUco was shot on 4:3 stills and auto-cal
            # runs on 16:9 video-format frames.
            from state_calibration import scale_intrinsics_to

            scaled = scale_intrinsics_to(
                charuco.intrinsics,
                source_width_px=charuco.source_width_px,
                source_height_px=charuco.source_height_px,
                target_width_px=w_img,
                target_height_px=h_img,
            )
            return scaled, None

    prior = state.calibrations().get(camera_id)
    if prior is not None and h_fov_deg is None:
        prior_w = prior.image_width_px
        prior_h = prior.image_height_px
        if prior_w > 0 and prior_h > 0:
            prior_ar = prior_w / prior_h
            new_ar = w_img / h_img
            # iPhone stills (4:3) and video (16:9) come from different
            # sensor crops — scaling fx/fy independently on axis-ratio
            # mismatch produces a bogus fx/fy ratio. Only reuse prior
            # intrinsics when the aspect ratio matches within 2%.
            if abs(prior_ar - new_ar) / prior_ar < 0.02:
                sx = w_img / prior_w
                sy = h_img / prior_h
                intrinsics = IntrinsicsPayload(
                    fx=prior.intrinsics.fx * sx,
                    fy=prior.intrinsics.fy * sy,
                    cx=prior.intrinsics.cx * sx,
                    cy=prior.intrinsics.cy * sy,
                    distortion=prior.intrinsics.distortion,
                )
                return intrinsics, prior
        prior = None
    h_fov_rad = float(np.radians(h_fov_deg)) if h_fov_deg is not None else _IPHONE_MAIN_CAM_HFOV_RAD
    fx, fy, cx, cy = derive_fov_intrinsics(w_img, h_img, h_fov_rad)
    return IntrinsicsPayload(fx=fx, fy=fy, cx=cx, cy=cy), None


def _solve_auto_cal_solution(
    detected: list[Any],
    *,
    intrinsics: IntrinsicsPayload,
    image_size: tuple[int, int],
) -> tuple[Any | None, str, list[int]]:
    import main as _main
    state = _main.state

    world_map: dict[int, tuple[float, float]] = dict(PLATE_MARKER_WORLD)
    world_map.update(state._marker_registry.planar_world_map())
    planar = solve_homography_from_world_map(
        detected, world_map, image_size=image_size
    )
    pnp_solution = _solve_pnp_homography(
        detected, intrinsics=intrinsics, image_size=image_size
    )
    if pnp_solution is None:
        return planar, "planar_homography", []
    H_pnp, pnp_detected_ids = pnp_solution
    if planar is None:
        from calibration_solver import CalibrationSolveResult

        planar = CalibrationSolveResult(
            homography_row_major=H_pnp,
            detected_ids=sorted(pnp_detected_ids),
            missing_ids=[i for i in sorted(PLATE_MARKER_WORLD.keys()) if i not in pnp_detected_ids],
            image_width_px=int(image_size[0]),
            image_height_px=int(image_size[1]),
        )
    else:
        planar = planar.__class__(
            homography_row_major=H_pnp,
            detected_ids=sorted(set(planar.detected_ids) | set(pnp_detected_ids)),
            missing_ids=planar.missing_ids,
            image_width_px=planar.image_width_px,
            image_height_px=planar.image_height_px,
        )
    return planar, "pnp_pose", pnp_detected_ids


def _pose_from_homography(
    intrinsics: IntrinsicsPayload,
    homography_row_major: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    K = build_K(intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy)
    H_mat = np.array(homography_row_major, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H_mat)
    center = camera_center_world(R_wc, t_wc)
    forward = R_wc.T @ np.array([0.0, 0.0, 1.0], dtype=np.float64)
    forward = forward / np.linalg.norm(forward)
    return center, forward


def _reprojection_error_px(
    intrinsics: IntrinsicsPayload,
    homography_row_major: list[float],
    detected: list[Any],
) -> float | None:
    import cv2  # noqa: WPS433

    world_xyz = _all_marker_world_xyz()
    K = build_K(
        intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy
    ).astype(np.float64)
    H_mat = np.array(homography_row_major, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H_mat)
    rvec, _ = cv2.Rodrigues(R_wc)
    dist = np.asarray(intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    errs: list[float] = []
    for m in detected:
        if m.id not in world_xyz:
            continue
        obj = np.array([[world_xyz[m.id]]], dtype=np.float64)
        proj, _ = cv2.projectPoints(obj, rvec, t_wc.reshape(3, 1), K, dist)
        pred = proj.reshape(2)
        obs = m.corners.mean(axis=0)
        errs.append(float(np.linalg.norm(pred - obs)))
    return float(np.mean(errs)) if errs else None


async def _run_auto_calibration(
    camera_id: str,
    *,
    h_fov_deg: float | None = None,
    track_run: bool = False,
) -> dict[str, Any]:
    """Single-shot auto-calibration.

    One call = one full-res JPEG → ArUco detect → solve → store. Every
    failure mode (no frame, no markers, degenerate geometry, reproj > 20
    px) raises 422/408 — there is no buffer to keep. Operator just
    re-aims and presses Recalibrate again.

    Returns the solve_ok payload (`result` / `intrinsics` /
    `reprojection_px` / `delta_*` / `pnp_detected_ids`) that the caller
    uses to write the CalibrationSnapshot.
    """
    import cv2  # noqa: WPS433
    import main as _main
    state = _main.state
    device_ws = _main.device_ws
    _settings_message_for = _main._settings_message_for

    # iOS may briefly stop the AVCaptureSession to swap activeFormat
    # to a 12 MP photo format, capture a still, and swap back to 240 fps.
    # Worst-case budget: ~1.5 s swap-out + ~0.8 s 12 MP capture + ~1.5 s
    # swap-back + ~0.4 s upload of a 3-5 MB JPEG ≈ p95 4-5 s. 10 s gives
    # comfortable headroom; old 5 s ceiling was too tight for the new path.
    frame_timeout_s = 10.0

    if track_run:
        online_ids = sorted(d.camera_id for d in state.online_devices())
        state.update_auto_cal_run(
            camera_id,
            status="searching",
            summary="Requesting full-res frame",
        )
        state.append_auto_cal_event(
            camera_id,
            "single-shot start",
            data={
                "h_fov_deg": h_fov_deg,
                "timeout_s": frame_timeout_s,
                "online_devices": online_ids,
                "target_online": camera_id in online_ids,
            },
        )

    state.request_calibration_frame(camera_id)
    await device_ws.send(camera_id, _settings_message_for(camera_id))
    poll_start = asyncio.get_event_loop().time()
    got = None
    for _ in range(int(round(frame_timeout_s / 0.1))):
        got = state.consume_calibration_frame(camera_id)
        if got is not None:
            break
        await asyncio.sleep(0.1)
    if got is None:
        if track_run:
            state.append_auto_cal_event(
                camera_id,
                f"no frame arrived within {frame_timeout_s:.0f}s",
                level="warn",
                data={"poll_s": round(asyncio.get_event_loop().time() - poll_start, 2)},
            )
        raise HTTPException(
            status_code=408,
            detail=(
                f"camera {camera_id!r} did not deliver a calibration "
                f"frame within {frame_timeout_s:.0f} s — check the phone "
                "is online, awake, and running the current build"
            ),
        )
    jpeg_bytes = got.jpeg_bytes
    photo_fov_deg = got.photo_fov_deg
    video_fov_deg = got.video_fov_deg
    if track_run:
        state.append_auto_cal_event(
            camera_id,
            "frame received",
            data={
                "bytes": len(jpeg_bytes),
                "poll_s": round(asyncio.get_event_loop().time() - poll_start, 2),
                "photo_fov_deg": photo_fov_deg,
                "video_fov_deg": video_fov_deg,
            },
        )
    # Operator-supplied h_fov_deg (?h_fov_deg=...) wins over the
    # iOS-reported photo FOV — explicit override path for debugging.
    # Otherwise prefer the photo format's FOV since this JPEG was
    # captured in the photo basis (12 MP).
    solve_fov_deg = h_fov_deg if h_fov_deg is not None else photo_fov_deg
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        if track_run:
            state.append_auto_cal_event(
                camera_id, "jpeg decode failed", level="warn",
                data={"bytes": len(jpeg_bytes)},
            )
        raise HTTPException(status_code=422, detail="calibration frame is not a decodable JPEG")
    src_h, src_w = bgr.shape[:2]
    # Center-crop 4:3 12 MP stills to 16:9 BEFORE detection so every
    # downstream basis (intrinsics, H, snapshot, live, pitch) lives in
    # the rig's video aspect ratio. pairing._scale_* can't represent
    # the 4:3→16:9 sensor crop as a pure linear scale, so doing the
    # crop here is the only way to keep the snapshot consistent with
    # what the MOV path will see at pitch time.
    bgr, crop_dy = _center_crop_to_aspect(bgr, 16.0 / 9.0, tol=0.01)
    h_img, w_img = bgr.shape[:2]
    if track_run and (w_img, h_img) != (src_w, src_h):
        state.append_auto_cal_event(
            camera_id,
            "ar_crop",
            data={"src": [src_w, src_h], "dst": [w_img, h_img], "dy": crop_dy},
        )

    intrinsics, prior = _derive_auto_cal_intrinsics(
        camera_id, w_img=w_img, h_img=h_img, h_fov_deg=solve_fov_deg,
    )
    if track_run:
        charuco_src = state.device_intrinsics_for_camera(camera_id)
        state.append_auto_cal_event(
            camera_id,
            "intrinsics derived",
            data={
                "w": w_img, "h": h_img,
                "fx": round(intrinsics.fx, 2), "fy": round(intrinsics.fy, 2),
                "cx": round(intrinsics.cx, 2), "cy": round(intrinsics.cy, 2),
                "reused_prior": prior is not None,
                "charuco_prior_device_id": (
                    charuco_src.device_id if charuco_src is not None else None
                ),
                "distortion_available": intrinsics.distortion is not None,
            },
        )

    all_detected = detect_all_markers_in_dict(bgr)
    detected = [
        m for m in all_detected
        if (m.id in PLATE_MARKER_WORLD or state._marker_registry.get(m.id) is not None)
    ]
    if track_run:
        state.append_auto_cal_event(
            camera_id,
            "markers detected",
            data={
                "known_ids": sorted(m.id for m in detected),
                "all_ids": sorted(m.id for m in all_detected),
                "frame_shape": [w_img, h_img],
            },
        )
    if not detected:
        plate_ids_str = ",".join(str(i) for i in sorted(PLATE_MARKER_WORLD.keys()))
        raise HTTPException(
            status_code=422,
            detail=(
                f"no known markers visible on camera {camera_id!r} — "
                f"aim the lens at the plate (IDs {plate_ids_str}) before retrying"
            ),
        )

    if track_run:
        state.update_auto_cal_run(
            camera_id,
            status="solving",
            markers_visible=len(detected),
            detected_ids=sorted(m.id for m in detected),
            summary=f"Solving from {len(detected)} markers",
        )

    result, solver, pnp_detected_ids = _solve_auto_cal_solution(
        detected, intrinsics=intrinsics, image_size=(w_img, h_img),
    )
    if result is None:
        seen_ids = sorted(m.id for m in detected)
        raise HTTPException(
            status_code=422,
            detail=(
                "pose solver failed — markers "
                f"{seen_ids} but geometry is degenerate (colinear or too few)"
            ),
        )

    reproj_px = _reprojection_error_px(
        intrinsics, result.homography_row_major, detected,
    )
    if reproj_px is not None and reproj_px > REPROJ_FAIL_PX:
        if track_run:
            state.append_auto_cal_event(
                camera_id, "reproj above ceiling", level="warn",
                data={"reproj_px": round(reproj_px, 2), "ceiling_px": REPROJ_FAIL_PX},
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"reprojection {reproj_px:.1f} px exceeded {REPROJ_FAIL_PX:.0f} px "
                "limit — re-aim and try again (markers fully visible, less oblique)"
            ),
        )

    # Rebuild K + H in the canonical 1920×1080 live-video basis.
    #
    # Detection runs at the 12 MP photo format's basis (4032×2268 after
    # AR crop); pitch-time triangulation runs at 1920×1080 video format.
    # Those two formats DON'T share a FOV on iPhone main cams — the
    # binned video format crops the sensor differently from the
    # full-frame photo format. A linear K rescale would silently
    # mismatch the FOVs → overlay aligns in the 12 MP capture window
    # then drifts after rollback to 1080p.
    #
    # The solve produced (R_wc, t_wc) consistent with K_solve at the
    # photo basis. Those are basis-independent (physical pose).
    # Distortion coefficients live in normalized image coords (after
    # K^-1) and survive the basis swap. We build K_video from the live
    # video FOV at 1920×1080, then H_video = K_video × [r1 r2 t].
    if video_fov_deg is None:
        # Project rule: experimental phase = lockstep iOS+server, no
        # back-compat. Old iOS clients can't realistically exist here;
        # missing video_fov_deg means the upload is malformed.
        raise HTTPException(
            status_code=422,
            detail=(
                "calibration_frame upload is missing video_fov_deg — "
                "iOS client too old; rebuild and reinstall."
            ),
        )
    if (w_img, h_img) != (_CANONICAL_SNAPSHOT_W, _CANONICAL_SNAPSHOT_H):
        # Recover physical pose from the photo-basis solve.
        K_solve = build_K(
            intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy,
        ).astype(np.float64)
        H_solve = np.array(result.homography_row_major, dtype=np.float64).reshape(3, 3)
        R_wc, t_wc = recover_extrinsics(K_solve, H_solve)

        # Build K_video from the live video format's FOV. cx/cy land at
        # canonical image center — the live video format's principal
        # point is sensor-physical and ~ centered for iPhone main cams.
        video_fov_rad = float(np.radians(video_fov_deg))
        fx_v, fy_v, cx_v, cy_v = derive_fov_intrinsics(
            _CANONICAL_SNAPSHOT_W, _CANONICAL_SNAPSHOT_H, video_fov_rad,
        )
        intrinsics = IntrinsicsPayload(
            fx=fx_v, fy=fy_v, cx=cx_v, cy=cy_v,
            distortion=intrinsics.distortion,  # lens property; basis-invariant
        )

        # H_video = K_video × [r1 | r2 | t] — Zhang's planar homography
        # rebuilt in the live basis.
        K_video = build_K(fx_v, fy_v, cx_v, cy_v).astype(np.float64)
        rt_planar = np.column_stack((R_wc[:, 0], R_wc[:, 1], t_wc.reshape(3)))
        H_video = K_video @ rt_planar
        # Normalize h33 → 1 to match storage convention. h33 = K row 3 ·
        # [r1 r2 t] col 3 = t_z; recover_extrinsics already rejects
        # near-zero t_z (cam on plate plane), so this guard is defensive
        # against numerical drift only.
        if abs(H_video[2, 2]) > 1e-12:
            H_video = H_video / H_video[2, 2]

        from calibration_solver import CalibrationSolveResult
        result = CalibrationSolveResult(
            homography_row_major=H_video.flatten().tolist(),
            detected_ids=result.detected_ids,
            missing_ids=result.missing_ids,
            image_width_px=_CANONICAL_SNAPSHOT_W,
            image_height_px=_CANONICAL_SNAPSHOT_H,
        )
        if track_run:
            state.append_auto_cal_event(
                camera_id,
                "canonical_rebuild",
                data={
                    "src": [w_img, h_img],
                    "dst": [_CANONICAL_SNAPSHOT_W, _CANONICAL_SNAPSHOT_H],
                    "photo_fov_deg": photo_fov_deg,
                    "video_fov_deg": video_fov_deg,
                    "fx_video": round(fx_v, 2),
                    "fy_video": round(fy_v, 2),
                },
            )

    center_new, forward_new = _pose_from_homography(
        intrinsics, result.homography_row_major,
    )
    delta_pos_cm = None
    delta_ang_deg = None
    if prior is not None:
        # Use prior.intrinsics directly — prior.intrinsics + prior.homography
        # are self-consistent within the prior's own basis. Re-deriving via
        # _derive_auto_cal_intrinsics would pick up whatever ChArUco K is
        # currently on file, which may have been swapped between cal runs.
        center_old, forward_old = _pose_from_homography(
            prior.intrinsics, prior.homography,
        )
        delta_pos_cm = float(np.linalg.norm(center_new - center_old) * 100.0)
        delta_ang_deg = float(
            np.degrees(np.arccos(np.clip(np.dot(forward_new, forward_old), -1.0, 1.0)))
        )
    # Persistent last-solve record — dashboard reads this to keep
    # showing "last calibrated N min ago, used markers […]" between
    # recalibrations. Operator's UX continuity.
    from state_calibration import LastSolveRecord
    n_extended = sum(
        1 for mid in result.detected_ids if mid not in PLATE_MARKER_WORLD
    )
    state.record_calibration_last_solve(
        camera_id,
        LastSolveRecord(
            solved_at=state._time_fn(),
            marker_ids=list(result.detected_ids),
            reproj_px=reproj_px,
            n_extended_used=n_extended,
            photo_fov_deg=photo_fov_deg,
            video_fov_deg=video_fov_deg,
            solver=solver,
            fx_video=intrinsics.fx,
            delta_position_cm=delta_pos_cm,
            delta_angle_deg=delta_ang_deg,
        ),
    )
    return {
        "frames_seen": 1,
        "good_frames": 1,
        "stable_frames": 1,
        "intrinsics": intrinsics,
        "result": result,
        "solver": solver,
        "reprojection_px": reproj_px,
        "delta_position_cm": delta_pos_cm,
        "delta_angle_deg": delta_ang_deg,
        "pnp_detected_ids": pnp_detected_ids,
    }
