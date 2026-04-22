from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from calibration_solver import (
    PLATE_MARKER_WORLD,
    derive_fov_intrinsics,
    detect_all_markers_in_dict,
    solve_homography_from_world_map,
)
from control_routes import settings_message_for, wants_html
from schemas import CalibrationSnapshot, IntrinsicsPayload, MarkerRecord
from triangulate import (
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)


def build_auto_calibration_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    get_logger: Callable[[], Any],
    time_sync_max_age_s: float,
) -> APIRouter:
    router = APIRouter()

    @router.post("/calibration/auto/{camera_id}")
    async def calibration_auto(
        camera_id: str,
        request: Request,
        h_fov_deg: float | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
            raise HTTPException(status_code=400, detail="invalid camera_id")
        auto = await run_auto_calibration(
            state=get_state(),
            device_ws=get_device_ws(),
            camera_id=camera_id,
            h_fov_deg=h_fov_deg,
            track_run=False,
            time_sync_max_age_s=time_sync_max_age_s,
        )
        result = auto["result"]
        intrinsics = auto["intrinsics"]
        snapshot = CalibrationSnapshot(
            camera_id=camera_id,
            intrinsics=intrinsics,
            homography=result.homography_row_major,
            image_width_px=result.image_width_px,
            image_height_px=result.image_height_px,
        )
        get_state().set_calibration(snapshot)
        n_extended_used = sum(1 for mid in result.detected_ids if mid not in PLATE_MARKER_WORLD)
        if wants_html(request):
            return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
        return {
            "ok": True,
            "camera_id": camera_id,
            "detected_ids": result.detected_ids,
            "missing_plate_ids": result.missing_ids,
            "homography": result.homography_row_major,
            "image_width_px": result.image_width_px,
            "image_height_px": result.image_height_px,
            "n_extended_used": n_extended_used,
            "used_pose_solver": auto["solver"] == "pnp_pose",
            "n_3d_markers_used": sum(
                1 for mid in auto["pnp_detected_ids"] if mid not in PLATE_MARKER_WORLD
            ),
            "frames_seen": auto["frames_seen"],
            "good_frames": auto["good_frames"],
            "stable_frames": auto["stable_frames"],
            "reprojection_px": auto["reprojection_px"],
            "delta_position_cm": auto["delta_position_cm"],
            "delta_angle_deg": auto["delta_angle_deg"],
        }

    @router.post("/calibration/auto/start/{camera_id}")
    async def calibration_auto_start(
        camera_id: str,
        h_fov_deg: float | None = None,
    ) -> dict[str, Any]:
        state = get_state()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
            raise HTTPException(status_code=400, detail="invalid camera_id")
        try:
            run = state.start_auto_cal_run(camera_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        async def _runner() -> None:
            try:
                auto = await run_auto_calibration(
                    state=state,
                    device_ws=get_device_ws(),
                    camera_id=camera_id,
                    h_fov_deg=h_fov_deg,
                    track_run=True,
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                result = auto["result"]
                snapshot = CalibrationSnapshot(
                    camera_id=camera_id,
                    intrinsics=auto["intrinsics"],
                    homography=result.homography_row_major,
                    image_width_px=result.image_width_px,
                    image_height_px=result.image_height_px,
                )
                state.set_calibration(snapshot)
                state.finish_auto_cal_run(
                    camera_id,
                    status="completed",
                    applied=True,
                    summary="Verified and applied",
                    detail=(
                        f"frames={auto['frames_seen']} stable={auto['stable_frames']} "
                        f"reproj={auto['reprojection_px']:.2f}px"
                        if auto["reprojection_px"] is not None
                        else f"frames={auto['frames_seen']} stable={auto['stable_frames']}"
                    ),
                    result={
                        "frames_seen": auto["frames_seen"],
                        "good_frames": auto["good_frames"],
                        "stable_frames": auto["stable_frames"],
                        "reprojection_px": auto["reprojection_px"],
                        "used_pose_solver": auto["solver"] == "pnp_pose",
                        "delta_position_cm": auto["delta_position_cm"],
                        "delta_angle_deg": auto["delta_angle_deg"],
                        "detected_ids": result.detected_ids,
                    },
                )
            except HTTPException as e:
                state.finish_auto_cal_run(
                    camera_id,
                    status="failed",
                    applied=False,
                    summary="Auto-cal failed",
                    detail=str(e.detail),
                )
            except Exception as e:  # noqa: BLE001
                get_logger().exception("auto calibration background run failed camera=%s", camera_id)
                state.finish_auto_cal_run(
                    camera_id,
                    status="failed",
                    applied=False,
                    summary="Auto-cal failed",
                    detail=f"{type(e).__name__}: {e}",
                )

        asyncio.create_task(_runner())
        return {"ok": True, "camera_id": camera_id, "run_id": run.id}

    @router.post("/markers/scan")
    async def markers_scan(
        camera_a_id: str = "A",
        camera_b_id: str = "B",
    ) -> dict[str, Any]:
        state = get_state()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_a_id):
            raise HTTPException(status_code=400, detail="invalid camera_a_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_b_id):
            raise HTTPException(status_code=400, detail="invalid camera_b_id")
        if camera_a_id == camera_b_id:
            raise HTTPException(status_code=400, detail="camera_a_id and camera_b_id must differ")
        jpeg_a, jpeg_b = await asyncio.gather(
            await_calibration_frame(
                state,
                get_device_ws(),
                camera_a_id,
                time_sync_max_age_s=time_sync_max_age_s,
            ),
            await_calibration_frame(
                state,
                get_device_ws(),
                camera_b_id,
                time_sync_max_age_s=time_sync_max_age_s,
            ),
        )
        bgr_a = decode_calibration_jpeg(jpeg_a)
        bgr_b = decode_calibration_jpeg(jpeg_b)
        scan = triangulate_marker_candidates(state, camera_a_id, camera_b_id, bgr_a, bgr_b)
        existing_ids = {rec.marker_id for rec in state._marker_registry.all_records()}
        return {
            "ok": True,
            "camera_ids": [camera_a_id, camera_b_id],
            "candidates": scan["candidates"],
            "visibility": scan["visibility"],
            "existing_marker_ids": sorted(existing_ids),
        }

    return router


async def await_calibration_frame(state: Any, device_ws: Any, camera_id: str, *, timeout_s: float = 2.0, time_sync_max_age_s: float) -> bytes:
    state.request_calibration_frame(camera_id)
    await device_ws.send(
        camera_id,
        settings_message_for(
            camera_id=camera_id,
            state=state,
            device_ws=device_ws,
            time_sync_max_age_s=time_sync_max_age_s,
        ),
    )
    loops = max(1, int(round(timeout_s / 0.1)))
    for _ in range(loops):
        got = state.consume_calibration_frame(camera_id)
        if got is not None:
            jpeg_bytes, _ = got
            return jpeg_bytes
        await asyncio.sleep(0.1)
    raise HTTPException(
        status_code=408,
        detail=(
            f"camera {camera_id!r} did not deliver a calibration "
            f"frame within {timeout_s:.0f} s — check the phone is online and awake"
        ),
    )


def decode_calibration_jpeg(jpeg_bytes: bytes) -> np.ndarray:
    import cv2  # noqa: WPS433
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=422, detail="calibration frame is not a decodable JPEG")
    return bgr


def marker_camera_pose(snapshot: CalibrationSnapshot) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    K = build_K(snapshot.intrinsics.fx, snapshot.intrinsics.fz, snapshot.intrinsics.cx, snapshot.intrinsics.cy)
    H = np.asarray(snapshot.homography, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H)
    C_world = camera_center_world(R_wc, t_wc)
    return K, R_wc, C_world


def all_marker_world_xyz(state: Any) -> dict[int, tuple[float, float, float]]:
    world_xyz = {mid: (float(xy[0]), float(xy[1]), 0.0) for mid, xy in PLATE_MARKER_WORLD.items()}
    for rec in state._marker_registry.all_records():
        world_xyz[rec.marker_id] = (rec.x_m, rec.y_m, rec.z_m)
    return world_xyz


def residual_bucket(residual_m: float) -> str:
    if residual_m <= 0.01:
        return "excellent"
    if residual_m <= 0.03:
        return "good"
    if residual_m <= 0.06:
        return "warn"
    return "poor"


def triangulate_marker_candidates(state: Any, camera_a_id: str, camera_b_id: str, bgr_a: np.ndarray, bgr_b: np.ndarray) -> dict[str, Any]:
    snap_a = state.calibrations().get(camera_a_id)
    snap_b = state.calibrations().get(camera_b_id)
    if snap_a is None or snap_b is None:
        missing = [cid for cid, snap in ((camera_a_id, snap_a), (camera_b_id, snap_b)) if snap is None]
        raise HTTPException(status_code=422, detail=f"missing calibration for camera(s): {', '.join(missing)}")
    K_a, R_a, C_a = marker_camera_pose(snap_a)
    K_b, R_b, C_b = marker_camera_pose(snap_b)
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
            delta_existing_m = float(np.linalg.norm(np.array([existing_rec.x_m, existing_rec.y_m, existing_rec.z_m], dtype=np.float64) - point))
        candidates.append(
            {
                "marker_id": int(marker_id),
                "x_m": float(point[0]),
                "y_m": float(point[1]),
                "z_m": float(point[2]),
                "residual_m": float(gap),
                "residual_bucket": residual_bucket(float(gap)),
                "source_camera_ids": [camera_a_id, camera_b_id],
                "suggest_on_plate_plane": abs(float(point[2])) <= 0.03,
                "detected_in": [camera_a_id, camera_b_id],
                "existing_marker": existing_rec is not None,
                "existing_label": existing_rec.label if existing_rec is not None else None,
                "existing_on_plate_plane": existing_rec.on_plate_plane if existing_rec is not None else None,
                "delta_existing_m": delta_existing_m,
                "update_action": "keep" if existing_rec is None else ("refresh" if delta_existing_m is not None and delta_existing_m <= 0.03 else "conflict"),
            }
        )
    only_a_ids = sorted(mid for mid in det_a.keys() - det_b.keys() if mid not in PLATE_MARKER_WORLD)
    only_b_ids = sorted(mid for mid in det_b.keys() - det_a.keys() if mid not in PLATE_MARKER_WORLD)
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


def solve_pnp_homography(state: Any, detected: list[Any], *, intrinsics: IntrinsicsPayload, image_size: tuple[int, int]) -> tuple[list[float], list[int]] | None:
    import cv2  # noqa: WPS433
    world_xyz = all_marker_world_xyz(state)
    markers_by_id = {m.id: m for m in detected if m.id in world_xyz}
    detected_ids = sorted(markers_by_id.keys())
    if len(detected_ids) < 4:
        return None
    object_pts = np.array([world_xyz[mid] for mid in detected_ids], dtype=np.float64)
    image_pts = np.array([markers_by_id[mid].corners.mean(axis=0) for mid in detected_ids], dtype=np.float64)
    if np.linalg.matrix_rank(object_pts - object_pts.mean(axis=0, keepdims=True)) < 3:
        return None
    K = build_K(intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy).astype(np.float64)
    dist = np.asarray(intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    ok, rvec, tvec, inliers = cv2.solvePnPRansac(object_pts, image_pts, K, dist, flags=cv2.SOLVEPNP_ITERATIVE, reprojectionError=4.0, iterationsCount=200, confidence=0.995)
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
    inlier_ids = [detected_ids[int(i)] for i in inliers.flatten().tolist()] if inliers is not None else detected_ids
    return H.flatten().tolist(), inlier_ids


def derive_auto_cal_intrinsics(state: Any, camera_id: str, *, w_img: int, h_img: int, h_fov_deg: float | None = None) -> tuple[IntrinsicsPayload, CalibrationSnapshot | None]:
    prior = state.calibrations().get(camera_id)
    if prior is not None and h_fov_deg is None:
        prior_w = prior.image_width_px
        prior_h = prior.image_height_px
        if prior_w > 0 and prior_h > 0:
            sx = w_img / prior_w
            sy = h_img / prior_h
            intrinsics = IntrinsicsPayload(
                fx=prior.intrinsics.fx * sx,
                fz=prior.intrinsics.fz * sy,
                cx=prior.intrinsics.cx * sx,
                cy=prior.intrinsics.cy * sy,
                distortion=prior.intrinsics.distortion,
            )
            return intrinsics, prior
        prior = None
    h_fov_rad = float(np.radians(h_fov_deg)) if h_fov_deg is not None else 1.1345
    fx, fy, cx, cy = derive_fov_intrinsics(w_img, h_img, h_fov_rad)
    return IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy), None


def solve_auto_cal_solution(state: Any, detected: list[Any], *, intrinsics: IntrinsicsPayload, image_size: tuple[int, int]) -> tuple[Any | None, str, list[int]]:
    world_map: dict[int, tuple[float, float]] = dict(PLATE_MARKER_WORLD)
    world_map.update(state._marker_registry.planar_world_map())
    planar = solve_homography_from_world_map(detected, world_map, image_size=image_size)
    pnp_solution = solve_pnp_homography(state, detected, intrinsics=intrinsics, image_size=image_size)
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


def pose_from_homography(intrinsics: IntrinsicsPayload, homography_row_major: list[float]) -> tuple[np.ndarray, np.ndarray]:
    K = build_K(intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy)
    H_mat = np.array(homography_row_major, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H_mat)
    center = camera_center_world(R_wc, t_wc)
    forward = R_wc.T @ np.array([0.0, 0.0, 1.0], dtype=np.float64)
    forward = forward / np.linalg.norm(forward)
    return center, forward


def reprojection_error_px(state: Any, intrinsics: IntrinsicsPayload, homography_row_major: list[float], detected: list[Any]) -> float | None:
    import cv2  # noqa: WPS433
    world_xyz = all_marker_world_xyz(state)
    K = build_K(intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy).astype(np.float64)
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


async def run_auto_calibration(*, state: Any, device_ws: Any, camera_id: str, h_fov_deg: float | None = None, track_run: bool = False, time_sync_max_age_s: float) -> dict[str, Any]:
    import cv2  # noqa: WPS433
    from calibration_solver import DetectedMarker

    max_frames = 10
    burst_deadline = asyncio.get_event_loop().time() + 6.0
    frames_seen = 0
    good_frames = 0
    stable_frames = 0
    first_shape: tuple[int, int] | None = None
    intrinsics: IntrinsicsPayload | None = None
    prior: CalibrationSnapshot | None = None
    aggregated_corners: dict[int, list[np.ndarray]] = {}
    recent_centers: list[np.ndarray] = []
    recent_forwards: list[np.ndarray] = []
    recent_errors: list[float] = []

    if track_run:
        state.update_auto_cal_run(camera_id, status="searching", summary="Searching for known markers")

    while frames_seen < max_frames and asyncio.get_event_loop().time() < burst_deadline:
        state.request_calibration_frame(camera_id)
        await device_ws.send(
            camera_id,
            settings_message_for(
                camera_id=camera_id,
                state=state,
                device_ws=device_ws,
                time_sync_max_age_s=time_sync_max_age_s,
            ),
        )
        got: tuple[bytes, float] | None = None
        for _ in range(20):
            got = state.consume_calibration_frame(camera_id)
            if got is not None:
                break
            await asyncio.sleep(0.1)
        if got is None:
            break
        jpeg_bytes, _ts = got
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        if first_shape is None:
            first_shape = bgr.shape[:2]
            intrinsics, prior = derive_auto_cal_intrinsics(state, camera_id, w_img=first_shape[1], h_img=first_shape[0], h_fov_deg=h_fov_deg)
        assert intrinsics is not None
        h_img, w_img = bgr.shape[:2]
        detected = [m for m in detect_all_markers_in_dict(bgr) if (m.id in PLATE_MARKER_WORLD or state._marker_registry.get(m.id) is not None)]
        frames_seen += 1
        if not detected:
            if track_run:
                state.update_auto_cal_run(camera_id, status="searching", frames_seen=frames_seen, markers_visible=0, summary="Searching for known markers")
            continue
        frame_solution, solver, _pnp_ids = solve_auto_cal_solution(state, detected, intrinsics=intrinsics, image_size=(w_img, h_img))
        markers_visible = len(detected)
        if frame_solution is None:
            if track_run:
                state.update_auto_cal_run(camera_id, status="tracking", frames_seen=frames_seen, markers_visible=markers_visible, summary="Tracking markers; need more stable geometry", detected_ids=sorted(m.id for m in detected))
            continue
        reproj_px = reprojection_error_px(state, intrinsics, frame_solution.homography_row_major, detected)
        center, forward = pose_from_homography(intrinsics, frame_solution.homography_row_major)
        recent_centers.append(center)
        recent_forwards.append(forward)
        if reproj_px is not None:
            recent_errors.append(reproj_px)
        if len(recent_centers) > 5:
            recent_centers.pop(0)
            recent_forwards.pop(0)
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        pos_jitter_cm = None
        ang_jitter_deg = None
        if len(recent_centers) >= 3:
            pos_jitter_cm = float(np.mean(np.std(np.stack(recent_centers), axis=0)) * 100.0)
            dots = [float(np.clip(np.dot(recent_forwards[0], v), -1.0, 1.0)) for v in recent_forwards[1:]]
            if dots:
                ang_jitter_deg = float(np.degrees(np.mean(np.arccos(dots))))
        frame_good = (
            reproj_px is not None and reproj_px <= 3.5 and pos_jitter_cm is not None and pos_jitter_cm <= 1.5 and ang_jitter_deg is not None and ang_jitter_deg <= 0.8
        )
        if frame_good:
            good_frames += 1
            stable_frames += 1
        else:
            stable_frames = 0
        if markers_visible >= 4:
            for m in detected:
                aggregated_corners.setdefault(m.id, []).append(m.corners)
        if track_run:
            state.update_auto_cal_run(
                camera_id,
                status="stabilizing" if good_frames > 0 else "tracking",
                frames_seen=frames_seen,
                good_frames=good_frames,
                stable_frames=stable_frames,
                markers_visible=markers_visible,
                solver=solver,
                reprojection_px=reproj_px,
                position_jitter_cm=pos_jitter_cm,
                angle_jitter_deg=ang_jitter_deg,
                summary=(f"Holding steady · {stable_frames} stable frame(s)" if good_frames > 0 else "Tracking markers; waiting for stability"),
                detected_ids=sorted(m.id for m in detected),
            )
        if stable_frames >= 4 and good_frames >= 4:
            break

    if frames_seen == 0 or first_shape is None or intrinsics is None:
        raise HTTPException(status_code=408, detail=(f"camera {camera_id!r} did not deliver a calibration frame within 6 s — check the phone is online, awake, preview is enabled, and running the current build"))

    aggregated: list[DetectedMarker] = [DetectedMarker(id=mid, corners=np.mean(np.stack(corner_list), axis=0)) for mid, corner_list in aggregated_corners.items()]
    if track_run:
        state.update_auto_cal_run(camera_id, status="solving", summary="Solving camera pose from stabilized observations")
    result, solver, pnp_detected_ids = solve_auto_cal_solution(state, aggregated, intrinsics=intrinsics, image_size=(first_shape[1], first_shape[0]))
    if result is None:
        seen_counts = ", ".join(f"id{mid}×{len(cs)}" for mid, cs in sorted(aggregated_corners.items()))
        raise HTTPException(status_code=422, detail=(f"need known markers for calibration across {frames_seen} frame(s); got: {seen_counts or '(none)'}"))
    reproj_px = reprojection_error_px(state, intrinsics, result.homography_row_major, aggregated)
    center_new, forward_new = pose_from_homography(intrinsics, result.homography_row_major)
    delta_pos_cm = None
    delta_ang_deg = None
    if prior is not None:
        prior_intrinsics, _ = derive_auto_cal_intrinsics(state, camera_id, w_img=first_shape[1], h_img=first_shape[0], h_fov_deg=None)
        center_old, forward_old = pose_from_homography(prior_intrinsics, prior.homography)
        delta_pos_cm = float(np.linalg.norm(center_new - center_old) * 100.0)
        delta_ang_deg = float(np.degrees(np.arccos(np.clip(np.dot(forward_new, forward_old), -1.0, 1.0))))
    return {
        "frames_seen": frames_seen,
        "good_frames": good_frames,
        "stable_frames": stable_frames,
        "intrinsics": intrinsics,
        "result": result,
        "solver": solver,
        "reprojection_px": reproj_px,
        "delta_position_cm": delta_pos_cm,
        "delta_angle_deg": delta_ang_deg,
        "pnp_detected_ids": pnp_detected_ids,
    }
