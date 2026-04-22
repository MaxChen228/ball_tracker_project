from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter

from schemas import CalibrationSnapshot


def build_calibration_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    get_sse_hub: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.post("/calibration")
    async def post_calibration(snapshot: CalibrationSnapshot) -> dict[str, Any]:
        state = get_state()
        state.set_calibration(snapshot)
        await get_sse_hub().broadcast(
            "calibration_changed",
            {
                "cam": snapshot.camera_id,
                "image_width_px": snapshot.image_width_px,
                "image_height_px": snapshot.image_height_px,
            },
        )
        await get_device_ws().broadcast(
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
        from reconstruct import build_calibration_scene
        from render_scene import _build_figure

        state = get_state()
        cals = state.calibrations()
        scene = build_calibration_scene(cals)
        fig = _build_figure(scene)
        fig.update_layout(
            title=None,
            margin=dict(l=0, r=0, t=8, b=0),
            scene_xaxis_range=[-6.0, 6.0],
            scene_yaxis_range=[-6.0, 6.0],
            scene_zaxis_range=[-0.2, 3.5],
            scene_aspectmode="manual",
            scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
            scene_uirevision="dashboard-canvas",
        )
        fig_json = json.loads(fig.to_json())

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
            "plot": {
                "data": fig_json.get("data", []),
                "layout": fig_json.get("layout", {}),
            },
        }

    return router
