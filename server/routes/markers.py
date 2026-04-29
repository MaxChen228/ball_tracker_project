from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from calibration_solver import PLATE_MARKER_WORLD
from schemas import MarkerBatchUpsertRequest, MarkerRecord, MarkerUpdateRequest

router = APIRouter()


def _serialize_marker(record: MarkerRecord) -> dict[str, Any]:
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


@router.get("/markers/state")
def markers_state() -> dict[str, Any]:
    from main import state
    records = state._marker_registry.all_records()
    return {
        "markers": [_serialize_marker(rec) for rec in records],
        "planar_marker_ids": [rec.marker_id for rec in records if rec.on_plate_plane],
        "reserved_marker_ids": sorted(PLATE_MARKER_WORLD.keys()),
    }


@router.post("/markers")
def markers_batch_upsert(body: MarkerBatchUpsertRequest) -> dict[str, Any]:
    from main import state
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
        persisted.append(_serialize_marker(state._marker_registry.upsert(record)))
    return {"ok": True, "markers": persisted}


@router.patch("/markers/{marker_id}")
def marker_update(marker_id: int, body: MarkerUpdateRequest) -> dict[str, Any]:
    from main import state
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
    return {"ok": True, "marker": _serialize_marker(updated)}


@router.delete("/markers/{marker_id}")
def marker_delete(marker_id: int) -> dict[str, Any]:
    from main import state
    existed = state._marker_registry.remove(marker_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"marker {marker_id} not registered")
    return {"ok": True, "marker_id": marker_id}


@router.post("/markers/clear")
def markers_clear() -> dict[str, Any]:
    from main import state
    cleared = state._marker_registry.clear()
    return {"ok": True, "cleared_count": cleared}


@router.post("/calibration/markers/register/{camera_id}")
async def calibration_markers_register_legacy(camera_id: str) -> dict[str, Any]:
    # Note: server/static/dashboard/82_markers.js:11 still POSTs here — keep
    # this 409 sentinel so the dashboard surfaces a clear error. Do not remove.
    raise HTTPException(
        status_code=409,
        detail="single-camera marker registration was removed; use /markers and scan with both cameras",
    )


@router.get("/calibration/markers")
def calibration_markers_list_legacy() -> dict[str, Any]:
    from main import state
    return {
        "markers": [
            {"id": rec.marker_id, "wx": rec.x_m, "wy": rec.y_m}
            for rec in state._marker_registry.all_records()
            if rec.on_plate_plane
        ],
    }


@router.delete("/calibration/markers/{marker_id}")
def calibration_markers_delete_legacy(marker_id: int) -> dict[str, Any]:
    return marker_delete(marker_id)


@router.post("/calibration/markers/clear")
def calibration_markers_clear_legacy() -> dict[str, Any]:
    return markers_clear()


@router.post("/markers/scan")
async def markers_scan(
    camera_a_id: str = "A",
    camera_b_id: str = "B",
) -> dict[str, Any]:
    # `_await_calibration_frame` is the FastAPI-wrapped frame-fetch
    # helper that lives next to the route handlers. The pure
    # numpy/CV helpers (`_decode_calibration_jpeg`,
    # `_triangulate_marker_candidates`) live in `calibration_auto`.
    from routes.calibration import _await_calibration_frame
    from calibration_auto import (
        _decode_calibration_jpeg,
        _triangulate_marker_candidates,
    )
    from main import state

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
