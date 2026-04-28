from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from calibration_solver import (
    PLATE_MARKER_WORLD,
    derive_fov_intrinsics,
    detect_all_markers_in_dict,
    solve_homography_from_world_map,
)
from schemas import CalibrationSnapshot, DeviceIntrinsics, IntrinsicsPayload
from triangulate import build_K, camera_center_world, recover_extrinsics, triangulate_rays, undistorted_ray_cam

router = APIRouter()
logger = logging.getLogger("ball_tracker")

# iPhone main (1x wide) rear camera horizontal FOV — empirically measured
# from the device's `activeFormat.videoFieldOfView` at 240 fps (73.828°).
# Used as the fallback when an uploaded ChArUco/AutoCal pose carries no
# explicit `h_fov_deg` so derived `fx`/`fy` match the rig's actual sensor
# rather than the historical 65° guess (which over-estimated fx by ~14%).
# See MEMORY: reference_iphone_camera_formats.md.
_IPHONE_MAIN_CAM_HFOV_RAD = 1.2885  # 73.828° measured


@router.post("/calibration")
async def post_calibration(snapshot: CalibrationSnapshot) -> dict[str, Any]:
    """iPhone pushes its freshly-solved calibration (intrinsics + homography)
    so the dashboard canvas can show where the camera is positioned in world
    space, even before the first pitch is ever recorded. Idempotent overwrite:
    each camera only keeps its latest snapshot."""
    import main as _main
    state = _main.state
    sse_hub = _main.sse_hub
    device_ws = _main.device_ws

    state.set_calibration(snapshot)
    await sse_hub.broadcast(
        "calibration_changed",
        {
            "cam": snapshot.camera_id,
            "image_width_px": snapshot.image_width_px,
            "image_height_px": snapshot.image_height_px,
        },
    )
    await device_ws.broadcast(
        {
            cam: {"type": "calibration_updated", "cam": snapshot.camera_id}
            for cam in state.known_camera_ids()
            if cam != snapshot.camera_id
        }
    )
    return {
        "ok": True,
        "camera_id": snapshot.camera_id,
        "image_width_px": snapshot.image_width_px,
        "image_height_px": snapshot.image_height_px,
    }


@router.get("/calibration/state")
def calibration_state() -> dict[str, Any]:
    """Dashboard polls this to repaint the canvas whenever a new calibration
    lands. Returns both the raw scene (so callers can rebuild custom views)
    and a ready-to-`Plotly.react` figure spec — the dashboard uses the
    latter so the trace/layout construction stays centralised server-side
    and the browser only speaks figure JSON."""
    import main as _main
    state = _main.state
    from reconstruct import build_calibration_scene
    from render_scene import _build_figure

    cals = state.calibrations()
    scene = build_calibration_scene(cals)
    fig = _build_figure(scene)
    fig.update_layout(
        title=None, margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-6.0, 6.0],
        scene_yaxis_range=[-6.0, 6.0],
        scene_zaxis_range=[-0.2, 3.5],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
        scene_uirevision="dashboard-canvas",
    )
    fig_json = json.loads(fig.to_json())
    # ETag for the plot subtree only. Dashboard compares `plot_etag`
    # across ticks to short-circuit the expensive `JSON.stringify(plot)`
    # digest it previously computed client-side. 16 hex chars = 64 bits
    # of collision resistance — fine given we only diff against the
    # previous tick's etag (no adversarial setting).
    plot_payload = {
        "data": fig_json.get("data", []),
        "layout": fig_json.get("layout", {}),
    }
    plot_etag = hashlib.sha1(
        json.dumps(plot_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    def _cal_mtime(cam_id: str) -> float | None:
        p = state._calibration_path(cam_id)
        try:
            return p.stat().st_mtime
        except OSError:
            return None
    return {
        "calibrations": [
            {
                "camera_id": cam_id,
                "image_width_px": snap.image_width_px,
                "image_height_px": snap.image_height_px,
                "last_ts": _cal_mtime(cam_id),
            }
            for cam_id, snap in sorted(cals.items())
        ],
        "scene": scene.to_dict(),
        "plot": plot_payload,
        "plot_etag": plot_etag,
    }


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@router.get("/calibration/intrinsics")
def list_device_intrinsics() -> dict[str, Any]:
    """Dashboard Intrinsics card reads this to render the per-device status
    table alongside the role→device mapping from `/status`.

    Returns each stored record plus a minimal summary so the UI can show
    "iPhone15,3 · fx=3280 · RMS 0.34 px · 18 shots" without digging into
    the raw JSON.
    """
    import main as _main
    state = _main.state
    records = state.device_intrinsics()
    out: list[dict[str, Any]] = []
    for rec in sorted(records.values(), key=lambda r: r.device_id):
        out.append({
            "device_id": rec.device_id,
            "device_model": rec.device_model,
            "source_width_px": rec.source_width_px,
            "source_height_px": rec.source_height_px,
            "fx": rec.intrinsics.fx,
            "fy": rec.intrinsics.fy,
            "cx": rec.intrinsics.cx,
            "cy": rec.intrinsics.cy,
            "distortion": rec.intrinsics.distortion,
            "rms_reprojection_px": rec.rms_reprojection_px,
            "n_images": rec.n_images,
            "calibrated_at": rec.calibrated_at,
            "source_label": rec.source_label,
        })
    # Include current role→device mapping so the UI can show which A/B
    # slot is currently wired to each device without an extra /status call.
    role_to_device: dict[str, dict[str, Any]] = {}
    for dev in state.online_devices():
        role_to_device[dev.camera_id] = {
            "device_id": dev.device_id,
            "device_model": dev.device_model,
        }
    return {"items": out, "online_roles": role_to_device}


@router.post("/calibration/intrinsics/{device_id}")
async def set_device_intrinsics(device_id: str, request: Request) -> dict[str, Any]:
    """Upload ChArUco-measured intrinsics for one physical sensor. Body is
    the `DeviceIntrinsics` JSON (minus `device_id`, which comes from the
    path — the server overrides any body value to keep the URL authoritative).

    Sanity-checked before store: the same `validate_calibration_snapshot`
    rules applied to the intrinsics half (positive focals, cx/cy inside
    the source frame, fx/fy ratio bounded). A misconfigured upload can't
    silently poison every subsequent auto-cal run.
    """
    import main as _main
    state = _main.state

    if not _DEVICE_ID_RE.fullmatch(device_id):
        raise HTTPException(status_code=400, detail="invalid device_id")
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    body["device_id"] = device_id  # path is authoritative
    try:
        rec = DeviceIntrinsics.model_validate(body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _validate_intrinsics_payload(rec)
    state.set_device_intrinsics(rec)
    logger.info(
        "device intrinsics stored device_id=%s model=%s fx=%.1f fy=%.1f rms=%s",
        rec.device_id, rec.device_model, rec.intrinsics.fx, rec.intrinsics.fy,
        rec.rms_reprojection_px,
    )
    return {
        "ok": True,
        "device_id": rec.device_id,
        "device_model": rec.device_model,
        "source_width_px": rec.source_width_px,
        "source_height_px": rec.source_height_px,
    }


@router.delete("/calibration/intrinsics/{device_id}")
def delete_device_intrinsics(device_id: str) -> dict[str, Any]:
    """Drop a device's ChArUco record. Used when the device is retired or
    the record is known stale — operator must explicitly re-upload before
    the next auto-cal benefits from ChArUco-measured K for that phone."""
    import main as _main
    state = _main.state

    if not _DEVICE_ID_RE.fullmatch(device_id):
        raise HTTPException(status_code=400, detail="invalid device_id")
    existed = state.delete_device_intrinsics(device_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"no intrinsics for device {device_id!r}")
    return {"ok": True, "device_id": device_id, "deleted": True}


def _validate_intrinsics_payload(rec: DeviceIntrinsics) -> None:
    """Mirrors `validate_calibration_snapshot` for the intrinsics-only
    upload path. Catches the class of operator mistakes where a K from
    a different resolution/sensor is pasted — would otherwise produce
    garbage extrinsics downstream with no obvious failure signal."""
    w, h = rec.source_width_px, rec.source_height_px
    k = rec.intrinsics
    if k.fx <= 0 or k.fy <= 0:
        raise HTTPException(
            status_code=422,
            detail=f"non-positive focal length fx={k.fx} fy={k.fy}",
        )
    if max(k.fx, k.fy) / min(k.fx, k.fy) > 2.0:
        raise HTTPException(
            status_code=422,
            detail=f"fx/fy ratio out of bounds: fx={k.fx} fy={k.fy}",
        )
    if not (-0.05 * w <= k.cx <= 1.05 * w):
        raise HTTPException(
            status_code=422,
            detail=(
                f"cx={k.cx} outside image width {w} — K likely from a "
                f"different resolution than source_dims claim"
            ),
        )
    if not (-0.05 * h <= k.cy <= 1.05 * h):
        raise HTTPException(
            status_code=422,
            detail=(
                f"cy={k.cy} outside image height {h} — K likely from a "
                f"different resolution than source_dims claim"
            ),
        )
    if k.distortion is not None and len(k.distortion) != 5:
        raise HTTPException(
            status_code=422,
            detail=(
                f"distortion must have exactly 5 coefficients "
                f"[k1, k2, p1, p2, k3]; got {len(k.distortion)}"
            ),
        )


async def _await_calibration_frame(camera_id: str, *, timeout_s: float = 2.0) -> bytes:
    import main as _main
    state = _main.state
    device_ws = _main.device_ws
    _settings_message_for = _main._settings_message_for

    state.request_calibration_frame(camera_id)
    await device_ws.send(camera_id, _settings_message_for(camera_id))
    loops = max(1, int(round(timeout_s / 0.1)))
    for _ in range(loops):
        got = state.consume_calibration_frame(camera_id)
        if got is not None:
            jpeg_bytes, _ts = got
            return jpeg_bytes
        await asyncio.sleep(0.1)
    raise HTTPException(
        status_code=408,
        detail=(
            f"camera {camera_id!r} did not deliver a calibration "
            f"frame within {timeout_s:.0f} s — check the phone is online and awake"
        ),
    )


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

    Request one full-res JPEG from the phone, detect markers, solve pose,
    done. The previous multi-frame burst with jitter gates was originally
    added to make calibration *easier* but ended up making it harder —
    phone on a tripod has zero jitter by design so the stability check
    just stalled the loop until the fallback aggregator rescued it.

    The accept / reject decision is now binary: either the solver produced
    a pose with reproj error below the sanity ceiling, or it didn't.
    Borderline results (5-15 px reproj) land but the number is surfaced in
    the run detail so the operator can decide whether to retry.
    """
    import cv2  # noqa: WPS433
    import main as _main
    state = _main.state
    device_ws = _main.device_ws
    _settings_message_for = _main._settings_message_for

    frame_timeout_s = 5.0
    # Hard ceiling — anything worse than this means the solver latched
    # onto garbage (wrong marker correspondences, degenerate geometry) and
    # shipping it would poison downstream triangulation silently.
    reproj_reject_px = 20.0

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
    got: tuple[bytes, float] | None = None
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
    jpeg_bytes, _ts = got
    if track_run:
        state.append_auto_cal_event(
            camera_id,
            "frame received",
            data={
                "bytes": len(jpeg_bytes),
                "poll_s": round(asyncio.get_event_loop().time() - poll_start, 2),
            },
        )
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        if track_run:
            state.append_auto_cal_event(
                camera_id, "jpeg decode failed", level="warn",
                data={"bytes": len(jpeg_bytes)},
            )
        raise HTTPException(status_code=422, detail="calibration frame is not a decodable JPEG")
    h_img, w_img = bgr.shape[:2]

    intrinsics, prior = _derive_auto_cal_intrinsics(
        camera_id, w_img=w_img, h_img=h_img, h_fov_deg=h_fov_deg,
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
        raise HTTPException(
            status_code=422,
            detail=(
                f"no known markers visible on camera {camera_id!r} — "
                "aim the lens at the plate (IDs 0-5) before retrying"
            ),
        )

    if track_run:
        state.update_auto_cal_run(
            camera_id,
            status="solving",
            markers_visible=len(detected),
            detected_ids=sorted(m.id for m in detected),
            summary="Solving pose from single frame",
        )
    result, solver, pnp_detected_ids = _solve_auto_cal_solution(
        detected, intrinsics=intrinsics, image_size=(w_img, h_img),
    )
    if result is None:
        seen_ids = sorted(m.id for m in detected)
        raise HTTPException(
            status_code=422,
            detail=(
                "pose solver failed — detected markers "
                f"{seen_ids} but geometry is degenerate (colinear or too few)"
            ),
        )

    reproj_px = _reprojection_error_px(
        intrinsics, result.homography_row_major, detected
    )
    if reproj_px is not None and reproj_px > reproj_reject_px:
        if track_run:
            state.append_auto_cal_event(
                camera_id, "reproj above ceiling", level="warn",
                data={"reproj_px": round(reproj_px, 2), "ceiling_px": reproj_reject_px},
            )
        raise HTTPException(
            status_code=422,
            detail=(
                f"solver produced implausible pose "
                f"(reproj {reproj_px:.1f} px > {reproj_reject_px:.0f} px ceiling) — "
                "retry with a cleaner shot of the plate"
            ),
        )

    center_new, forward_new = _pose_from_homography(
        intrinsics, result.homography_row_major,
    )
    delta_pos_cm = None
    delta_ang_deg = None
    if prior is not None:
        prior_intrinsics, _ = _derive_auto_cal_intrinsics(
            camera_id, w_img=w_img, h_img=h_img, h_fov_deg=None,
        )
        center_old, forward_old = _pose_from_homography(
            prior_intrinsics, prior.homography,
        )
        delta_pos_cm = float(np.linalg.norm(center_new - center_old) * 100.0)
        delta_ang_deg = float(
            np.degrees(np.arccos(np.clip(np.dot(forward_new, forward_old), -1.0, 1.0)))
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


@router.post("/calibration/auto/{camera_id}")
async def calibration_auto(
    camera_id: str,
    request: Request,
    h_fov_deg: float | None = None,
) -> dict[str, Any]:
    """Dashboard-triggered auto-calibration.

    Request a one-shot full-resolution JPEG from the phone via the WS settings path,
    poll the buffer for up to 2 s, run ArUco at native capture resolution.
    The snapshot lives in the SAME pixel coord system as the MOVs the
    phone later uploads for triangulation → K doesn't need rescaling at
    pitch time and the preview-vs-capture dims-mismatch class of bugs
    is gone at the source. 408 on no-delivery; no preview fallback.
    """
    import main as _main
    state = _main.state
    _wants_html = _main._wants_html

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
        raise HTTPException(status_code=400, detail="invalid camera_id")
    auto = await _run_auto_calibration(camera_id, h_fov_deg=h_fov_deg, track_run=False)
    result = auto["result"]
    intrinsics = auto["intrinsics"]
    frames_seen = auto["frames_seen"]
    snapshot = CalibrationSnapshot(
        camera_id=camera_id,
        intrinsics=intrinsics,
        homography=result.homography_row_major,
        image_width_px=result.image_width_px,
        image_height_px=result.image_height_px,
    )
    state.set_calibration(snapshot)
    n_extended_used = sum(
        1 for mid in result.detected_ids if mid not in PLATE_MARKER_WORLD
    )
    if _wants_html(request):
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
            1 for mid in auto["pnp_detected_ids"]
            if mid not in PLATE_MARKER_WORLD
        ),
        "frames_seen": frames_seen,
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
    import main as _main
    state = _main.state

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
        raise HTTPException(status_code=400, detail="invalid camera_id")
    try:
        run = state.start_auto_cal_run(camera_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    async def _runner() -> None:
        try:
            auto = await _run_auto_calibration(camera_id, h_fov_deg=h_fov_deg, track_run=True)
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
                summary="Applied",
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
            # HTTPException is the expected "graceful" failure channel
            # (markers not visible, frames missing, marker coverage too
            # thin). Without this warning the only record of *why* auto-
            # cal failed lived in the in-memory run status — gone on the
            # next restart and invisible in the server log.
            logger.warning(
                "auto calibration failed camera=%s status=%d: %s",
                camera_id, e.status_code, e.detail,
            )
            state.finish_auto_cal_run(
                camera_id,
                status="failed",
                applied=False,
                summary="Failed",
                detail=str(e.detail),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("auto calibration background run failed camera=%s", camera_id)
            state.finish_auto_cal_run(
                camera_id,
                status="failed",
                applied=False,
                summary="Failed",
                detail=f"{type(e).__name__}: {e}",
            )

    asyncio.create_task(_runner())
    return {"ok": True, "camera_id": camera_id, "run_id": run.id}


@router.post("/markers/scan")
async def markers_scan(
    camera_a_id: str = "A",
    camera_b_id: str = "B",
) -> dict[str, Any]:
    import main as _main
    state = _main.state

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_a_id):
        raise HTTPException(status_code=400, detail="invalid camera_a_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_b_id):
        raise HTTPException(status_code=400, detail="invalid camera_b_id")
    if camera_a_id == camera_b_id:
        raise HTTPException(status_code=400, detail="camera_a_id and camera_b_id must differ")

    jpeg_a, jpeg_b = await asyncio.gather(
        _await_calibration_frame(camera_a_id),
        _await_calibration_frame(camera_b_id),
    )
    bgr_a = _decode_calibration_jpeg(jpeg_a)
    bgr_b = _decode_calibration_jpeg(jpeg_b)
    scan = _triangulate_marker_candidates(
        camera_a_id=camera_a_id,
        camera_b_id=camera_b_id,
        bgr_a=bgr_a,
        bgr_b=bgr_b,
    )
    existing_ids = {rec.marker_id for rec in state._marker_registry.all_records()}
    return {
        "ok": True,
        "camera_ids": [camera_a_id, camera_b_id],
        "candidates": scan["candidates"],
        "visibility": scan["visibility"],
        "existing_marker_ids": sorted(existing_ids),
    }
