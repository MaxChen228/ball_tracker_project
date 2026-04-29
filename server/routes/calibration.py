"""FastAPI route layer for calibration.

Pure auto-calibration math + orchestrator lives in `calibration_auto`.
Per-device ChArUco intrinsics CRUD lives in `calibration_intrinsics`.
This file is just the thin handler layer:

  POST /calibration              — iPhone uploads a freshly-saved snapshot
  GET  /calibration/state        — dashboard polls 3D scene + plot spec
  POST /calibration/auto/{cam}   — single-shot recalibrate (sync)
  POST /calibration/auto/start/{cam} — single-shot recalibrate (background)
  POST /calibration/reset_rig    — wipe rig calibrations
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from calibration_auto import _run_auto_calibration
from calibration_solver import PLATE_MARKER_WORLD
from schemas import CalibrationSnapshot

router = APIRouter()
logger = logging.getLogger("ball_tracker")

# Strong references for fire-and-forget background tasks. asyncio only
# holds a weak ref to the task it returns from `create_task`, so without
# this set a sufficiently quick GC cycle can collect the task mid-run
# and silently cancel auto-calibration. See PEP-discussion notes around
# `asyncio.create_task` GC for the canonical bug pattern.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


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
            return got.jpeg_bytes
        await asyncio.sleep(0.1)
    raise HTTPException(
        status_code=408,
        detail=(
            f"camera {camera_id!r} did not deliver a calibration "
            f"frame within {timeout_s:.0f} s — check the phone is online and awake"
        ),
    )


@router.post("/calibration/auto/{camera_id}")
async def calibration_auto(
    camera_id: str,
    request: Request,
    h_fov_deg: float | None = None,
) -> dict[str, Any]:
    """Dashboard-triggered single-shot calibration.

    Pull one full-res JPEG from the phone, run ArUco, solve. The
    snapshot lives in the canonical 1920×1080 video basis so live-path
    CameraPose + pitch-time pairing.scale_pitch_to_video_dims see a
    consistent K. 408 on no-frame, 422 on no-markers / degenerate /
    reproj > 20 px — operator just retries.
    """
    import main as _main
    state = _main.state
    _wants_html = _main._wants_html

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
        raise HTTPException(status_code=400, detail="invalid camera_id")
    auto = await _run_auto_calibration(camera_id, h_fov_deg=h_fov_deg, track_run=False)
    result = auto["result"]
    intrinsics = auto["intrinsics"]
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

    task = asyncio.create_task(_runner())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return {"ok": True, "camera_id": camera_id, "run_id": run.id}


@router.post("/calibration/reset_rig")
async def calibration_reset_rig() -> dict[str, Any]:
    """Wipe all calibrations + extended marker registry + last-solve
    records. Used by dashboard 'Reset rig' for full re-setup (board
    moved, cams reseated). Per-device ChArUco intrinsics survive — those
    are sensor-physical and don't change with rig geometry."""
    import main as _main
    state = _main.state
    counts = state.reset_rig()
    return {"ok": True, **counts}
