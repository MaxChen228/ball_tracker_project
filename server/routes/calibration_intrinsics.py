"""Per-device ChArUco intrinsics CRUD.

Different domain from /calibration/auto/* (which solves rig extrinsics
each session): these endpoints persist the sensor-physical K + distortion
once per phone, so future auto-cal runs can use a real measured K instead
of an FOV approximation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from schemas import DeviceIntrinsics

router = APIRouter()
logger = logging.getLogger("ball_tracker")

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
