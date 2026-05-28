"""Multi-camera rig device pool + assignment endpoints (Phase 0 PR1).

The dashboard "Device Pool" panel uses these to:
  - list every currently-online phone (by device_uuid) plus any
    persistent `device_uuid → camera_id` assignment already on file
  - assign a fresh device_uuid to a camera_id slot (A / B / C / ...)
  - release a slot when a device is retired or re-roled

In PR1 the assignment store is *advisory*: it persists operator intent
but is not yet consulted at WS handshake time. PR2 will gate the WS
flow on these assignments so that a phone with no matching record gets
held in pending mode until the dashboard assigns it.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from state_device_assignments import AssignmentError

router = APIRouter()
logger = logging.getLogger("ball_tracker")


@router.get("/devices/pool")
def get_device_pool() -> dict[str, Any]:
    """Snapshot for the dashboard Device Pool panel.

    Returns three lists derived from authoritative state:
      - `assignments`: persistent device_uuid → camera_id records, with
        `online` flagged from the WS connection table
      - `observed_unassigned`: phones currently reporting a device_uuid
        via heartbeat that don't have a persistent assignment yet — the
        candidates the operator can promote with POST /devices/assign
      - `cam_id_in_use`: camera_ids currently visible in the device
        registry (assigned or not) so the UI can warn before reusing
        one mid-session
    """
    import main as _main
    state = _main.state
    device_ws = _main.device_ws

    assignments = state.device_assignments()
    ws_snapshot = device_ws.snapshot()
    online_cams = {dev.camera_id: dev for dev in state.online_devices()}

    assigned_uuids: set[str] = set()
    assigned_list: list[dict[str, Any]] = []
    for rec in assignments:
        assigned_uuids.add(rec.device_uuid)
        live_dev = online_cams.get(rec.camera_id)
        # An assignment counts as "online" only when the cam_id currently
        # observed via WS is hosted by the *same* device_uuid the
        # assignment names. A phone hot-swapping its UserDefaults role
        # would otherwise show "online" against the wrong record.
        live_matches = (
            live_dev is not None
            and live_dev.device_id == rec.device_uuid
        )
        assigned_list.append({
            "device_uuid": rec.device_uuid,
            "camera_id": rec.camera_id,
            "device_model": rec.device_model,
            "assigned_at": rec.assigned_at,
            "online": live_matches,
        })

    observed_unassigned: list[dict[str, Any]] = []
    for dev in state.online_devices():
        if dev.device_id is None:
            continue
        if dev.device_id in assigned_uuids:
            continue
        ws = ws_snapshot.get(dev.camera_id)
        observed_unassigned.append({
            "device_uuid": dev.device_id,
            "camera_id": dev.camera_id,
            "device_model": dev.device_model,
            "last_seen_at": dev.last_seen_at,
            "ws_connected": (ws.connected if ws is not None else False),
        })

    cam_id_in_use = sorted({d.camera_id for d in state.online_devices()})

    return {
        "assignments": assigned_list,
        "observed_unassigned": observed_unassigned,
        "cam_id_in_use": cam_id_in_use,
    }


@router.post("/devices/assign")
async def assign_device(request: Request) -> dict[str, Any]:
    """Create or update a persistent device_uuid → camera_id record.

    Body: `{"device_uuid": str, "camera_id": str, "device_model"?: str}`.

    Re-assigning the same device_uuid to a new camera_id releases the
    old camera_id atomically (single mutation). Re-using a camera_id
    that another device_uuid already holds is rejected (409) — operator
    must unassign first to make the intent explicit.
    """
    import main as _main
    state = _main.state

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    device_uuid = body.get("device_uuid")
    camera_id = body.get("camera_id")
    device_model = body.get("device_model")
    if not isinstance(device_uuid, str) or not device_uuid:
        raise HTTPException(status_code=422, detail="device_uuid required")
    if not isinstance(camera_id, str) or not camera_id:
        raise HTTPException(status_code=422, detail="camera_id required")
    if device_model is not None and not isinstance(device_model, str):
        raise HTTPException(status_code=422, detail="device_model must be a string or null")

    try:
        rec = state.assign_device(
            device_uuid=device_uuid,
            camera_id=camera_id,
            device_model=device_model,
        )
    except AssignmentError as e:
        # Collision (cam_id held by a different device) → 409 so the UI
        # can distinguish "your intent collided with prior state" from
        # plain bad-input 422.
        msg = str(e)
        status = 409 if "already assigned" in msg else 422
        raise HTTPException(status_code=status, detail=msg) from None

    logger.info(
        "device assigned: %s → %s (model=%s)",
        rec.device_uuid,
        rec.camera_id,
        rec.device_model,
    )
    return {
        "assignment": {
            "device_uuid": rec.device_uuid,
            "camera_id": rec.camera_id,
            "device_model": rec.device_model,
            "assigned_at": rec.assigned_at,
        }
    }


@router.post("/devices/unassign")
async def unassign_device(request: Request) -> dict[str, Any]:
    """Release a camera_id slot.

    Body: `{"camera_id": str}` OR `{"device_uuid": str}` (exactly one).
    Returns `{"unassigned": bool}` — False means there was no record to
    remove (idempotent for clients that retry).
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    camera_id = body.get("camera_id")
    device_uuid = body.get("device_uuid")
    if (camera_id is None) == (device_uuid is None):
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of camera_id or device_uuid",
        )

    import main as _main
    state = _main.state
    if camera_id is not None:
        if not isinstance(camera_id, str) or not camera_id:
            raise HTTPException(status_code=422, detail="camera_id must be a non-empty string")
        removed = state.unassign_device_by_camera(camera_id)
        if removed:
            logger.info("device unassigned by camera_id=%s", camera_id)
    else:
        if not isinstance(device_uuid, str) or not device_uuid:
            raise HTTPException(status_code=422, detail="device_uuid must be a non-empty string")
        removed = state.unassign_device_by_uuid(device_uuid)
        if removed:
            logger.info("device unassigned by device_uuid=%s", device_uuid)

    return {"unassigned": removed}
