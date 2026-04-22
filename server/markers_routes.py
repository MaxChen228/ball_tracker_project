from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from calibration_solver import PLATE_MARKER_WORLD
from schemas import MarkerBatchUpsertRequest, MarkerRecord, MarkerUpdateRequest


def serialize_marker(record: MarkerRecord) -> dict[str, Any]:
    return {
        "marker_id": record.marker_id,
        "label": record.label,
        "x_m": record.x_m,
        "y_m": record.y_m,
        "z_m": record.z_m,
        "on_plate_plane": record.on_plate_plane,
        "residual_m": record.residual_m,
        "source_camera_ids": list(record.source_camera_ids),
    }


def build_markers_router(
    *,
    get_state: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/markers/state")
    def markers_state() -> dict[str, Any]:
        state = get_state()
        records = state._marker_registry.all_records()
        return {
            "markers": [serialize_marker(rec) for rec in records],
            "planar_marker_ids": [rec.marker_id for rec in records if rec.on_plate_plane],
            "reserved_marker_ids": sorted(PLATE_MARKER_WORLD.keys()),
        }

    @router.post("/markers")
    def markers_batch_upsert(body: MarkerBatchUpsertRequest) -> dict[str, Any]:
        state = get_state()
        persisted: list[dict[str, Any]] = []
        for draft in body.markers:
            z_m = 0.0 if draft.snap_to_plate_plane or draft.on_plate_plane else draft.z_m
            record = MarkerRecord(
                marker_id=draft.marker_id,
                x_m=draft.x_m,
                y_m=draft.y_m,
                z_m=z_m,
                label=(draft.label or "").strip() or None,
                on_plate_plane=bool(draft.on_plate_plane),
                residual_m=draft.residual_m,
                source_camera_ids=list(draft.source_camera_ids),
            )
            persisted.append(serialize_marker(state._marker_registry.upsert(record)))
        return {"ok": True, "markers": persisted}

    @router.patch("/markers/{marker_id}")
    def marker_update(marker_id: int, body: MarkerUpdateRequest) -> dict[str, Any]:
        state = get_state()
        existing = state._marker_registry.get(marker_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"marker {marker_id} not registered")
        x_m = existing.x_m if body.x_m is None else body.x_m
        y_m = existing.y_m if body.y_m is None else body.y_m
        z_m = existing.z_m if body.z_m is None else body.z_m
        on_plate_plane = existing.on_plate_plane if body.on_plate_plane is None else body.on_plate_plane
        if body.snap_to_plate_plane or on_plate_plane:
            z_m = 0.0
        updated = MarkerRecord(
            marker_id=existing.marker_id,
            x_m=x_m,
            y_m=y_m,
            z_m=z_m,
            label=(body.label.strip() if body.label is not None else existing.label) or None,
            on_plate_plane=bool(on_plate_plane),
            residual_m=existing.residual_m,
            source_camera_ids=list(existing.source_camera_ids),
        )
        state._marker_registry.upsert(updated)
        return {"ok": True, "marker": serialize_marker(updated)}

    @router.delete("/markers/{marker_id}")
    def marker_delete(marker_id: int) -> dict[str, Any]:
        state = get_state()
        existed = state._marker_registry.remove(marker_id)
        if not existed:
            raise HTTPException(status_code=404, detail=f"marker {marker_id} not registered")
        return {"ok": True, "marker_id": marker_id}

    @router.post("/markers/clear")
    def markers_clear() -> dict[str, Any]:
        cleared = get_state()._marker_registry.clear()
        return {"ok": True, "cleared_count": cleared}

    @router.post("/calibration/markers/register/{camera_id}")
    async def calibration_markers_register_legacy(camera_id: str) -> dict[str, Any]:
        raise HTTPException(
            status_code=409,
            detail="single-camera marker registration was removed; use /markers and scan with both cameras",
        )

    @router.get("/calibration/markers")
    def calibration_markers_list_legacy() -> dict[str, Any]:
        return {
            "markers": [
                {"id": rec.marker_id, "wx": rec.x_m, "wy": rec.y_m}
                for rec in get_state()._marker_registry.all_records()
                if rec.on_plate_plane
            ],
        }

    @router.delete("/calibration/markers/{marker_id}")
    def calibration_markers_delete_legacy(marker_id: int) -> dict[str, Any]:
        return marker_delete(marker_id)

    @router.post("/calibration/markers/clear")
    def calibration_markers_clear_legacy() -> dict[str, Any]:
        return markers_clear()

    @router.get("/markers", response_class=HTMLResponse)
    def markers_page() -> HTMLResponse:
        from reconstruct import build_calibration_scene
        from render_markers import render_markers_html

        state = get_state()
        session = state.session_snapshot()
        markers = [serialize_marker(rec) for rec in state._marker_registry.all_records()]
        compare_markers = [
            {
                "marker_id": int(mid),
                "x_m": float(xy[0]),
                "y_m": float(xy[1]),
                "z_m": 0.0,
                "label": f"Plate {mid}",
                "on_plate_plane": True,
                "kind": "plate",
                "side_m": 0.08,
            }
            for mid, xy in sorted(PLATE_MARKER_WORLD.items())
        ] + [
            {
                **serialize_marker(rec),
                "kind": "stored",
                "side_m": 0.08,
            }
            for rec in state._marker_registry.all_records()
        ]
        scene = build_calibration_scene(state.calibrations()).to_dict()
        scene["plate"] = [
            {"x": -0.432 / 2.0, "y": 0.0, "z": 0.0},
            {"x": 0.432 / 2.0, "y": 0.0, "z": 0.0},
            {"x": 0.432 / 2.0, "y": 0.216, "z": 0.0},
            {"x": 0.0, "y": 0.432, "z": 0.0},
            {"x": -0.432 / 2.0, "y": 0.216, "z": 0.0},
        ]
        return HTMLResponse(
            render_markers_html(
                markers=markers,
                compare_markers=compare_markers,
                scene=scene,
                devices=[
                    {
                        "camera_id": d.camera_id,
                        "last_seen_at": d.last_seen_at,
                        "time_synced": d.time_synced,
                    }
                    for d in state.online_devices()
                ],
                session=session.to_dict() if session is not None else None,
                calibrations=sorted(state.calibrations().keys()),
            )
        )

    return router
