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

    Returns:
      - `assignments`: persistent device_uuid → camera_id records, with
        `online` flagged from the WS connection table
      - `pending`: phones currently connected but awaiting a cam_id
        assignment (PR3 device-uuid handshake). The operator promotes
        these via POST /devices/assign.
      - `observed_unassigned`: legacy iOS clients that landed under the
        pre-PR3 `/ws/device/{camera_id}` flow with a device_uuid that
        isn't in the assignment store yet. Kept until all clients move
        to the new handshake; will go away with Phase 5.
      - `cam_id_in_use`: camera_ids currently visible in the device
        registry so the UI can warn before reusing one mid-session
    """
    import main as _main
    state = _main.state
    device_ws = _main.device_ws
    pending_devices = _main.pending_devices

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

    pending_list = pending_devices.snapshot_for_pool()

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
        "pending": pending_list,
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

    pending_devices = _main.pending_devices
    device_ws = _main.device_ws

    try:
        # RACE-FREE: state write + pending notify must be a single sync
        # sequence with no `await` between them. Otherwise a WS handler
        # in the gap could see the new assignment via state but then
        # also register a pending entry that no one will ever wake. See
        # state_pending_devices.py module docstring.
        rec = state.assign_device(
            device_uuid=device_uuid,
            camera_id=camera_id,
            device_model=device_model,
        )
        pending_devices.notify_assigned(rec.device_uuid, rec.camera_id)
    except AssignmentError as e:
        # Collision (cam_id held by a different device) → 409 so the UI
        # can distinguish "your intent collided with prior state" from
        # plain bad-input 422.
        msg = str(e)
        status = 409 if "already assigned" in msg else 422
        raise HTTPException(status_code=status, detail=msg) from None

    # If the assigned device is currently bound to a DIFFERENT cam_id
    # already (mid-session re-assign), close the old socket so iOS
    # reconnects through the handshake and picks up the new cam_id. The
    # pending notify above only wakes phones in pending mode; an
    # already-promoted socket needs an explicit reconnect to rebind.
    ws_snap = device_ws.snapshot()
    for cam, snap in ws_snap.items():
        if cam == rec.camera_id:
            continue
        # Same physical device sitting on a different cam_id? The live
        # heartbeat record carries `device_id` — match on that. We do
        # NOT match by snapshot alone (no device_id field there); read
        # from state.devices.
        live = state.online_devices()
        for d in live:
            if d.camera_id == cam and d.device_id == rec.device_uuid:
                logger.info(
                    "closing stale ws cam=%s (device_uuid=%s now reassigned to %s)",
                    cam, rec.device_uuid, rec.camera_id,
                )
                _ws_obj = device_ws.snapshot_socket(cam)
                if _ws_obj is not None:
                    try:
                        await _ws_obj.close(code=1000)
                    except Exception:
                        pass

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
    pending_devices = _main.pending_devices
    device_ws = _main.device_ws

    # Resolve the (uuid, cam_id) pair BEFORE mutating so we know which
    # active socket to kick. The unassign methods only return bool, so
    # we look up first.
    target_uuid: str | None = None
    target_cam: str | None = None
    if camera_id is not None:
        if not isinstance(camera_id, str) or not camera_id:
            raise HTTPException(status_code=422, detail="camera_id must be a non-empty string")
        rec = state.assignment_for_camera(camera_id)
        if rec is not None:
            target_uuid, target_cam = rec.device_uuid, rec.camera_id
        removed = state.unassign_device_by_camera(camera_id)
    else:
        if not isinstance(device_uuid, str) or not device_uuid:
            raise HTTPException(status_code=422, detail="device_uuid must be a non-empty string")
        rec = state.assignment_for_device(device_uuid)
        if rec is not None:
            target_uuid, target_cam = rec.device_uuid, rec.camera_id
        removed = state.unassign_device_by_uuid(device_uuid)

    if removed and target_uuid is not None and target_cam is not None:
        # Two paths to kick:
        # (1) The cam may currently be promoted in device_ws — close
        #     the WS so iOS sees a disconnect and reconnects through
        #     the handshake. The new connect will land in pending mode.
        # (2) The device may currently sit in pending — wake it with no
        #     cam_id so the WS handler closes cleanly. (notify_unassigned
        #     sets the event with assigned_cam_id still None.)
        active = device_ws.snapshot_socket(target_cam)
        # Sanity: only close if the bound socket belongs to THIS uuid.
        # A hot-swapped phone on the same cam_id is somebody else.
        d = state.device_snapshot(target_cam)
        if active is not None and d is not None and d.device_id == target_uuid:
            try:
                await active.close(code=1000)
            except Exception:
                pass
        pending_devices.notify_unassigned(target_uuid)
        logger.info(
            "device unassigned: %s ↛ %s", target_uuid, target_cam,
        )

    return {"unassigned": removed}
