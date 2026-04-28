"""Per-device WebSocket endpoint.

The phone connects to `/ws/device/{camera_id}` for the lifetime of its
session: heartbeat liveness upstream, server settings + arm/disarm /
sync_run signals downstream, and live detection frames inbound. Lifted
out of `main.py` so the dispatch loop and the per-message handlers have
their own home.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import session_results
from schemas import DetectionPath, FramePayload

router = APIRouter()


@router.websocket("/ws/device/{camera_id}")
async def ws_device(camera_id: str, websocket: WebSocket) -> None:
    from main import (
        state,
        device_ws,
        sse_hub,
        _arm_message_for,
        _gated_time_synced,
        _parse_battery,
        _parse_device_identity,
        _settings_message_for,
    )
    from routes.camera import _validate_camera_id_or_422

    _validate_camera_id_or_422(camera_id)
    await device_ws.connect(camera_id, websocket)
    # Freshen `Device.last_seen_at` immediately on connect so `/status`
    # sees the cam as online without waiting for the first `hello` to
    # arrive. Otherwise we age out on disconnect, broadcast
    # `device_status online=true` at connect, the dashboard kicks
    # tickStatus — but state.online_devices() still excludes the cam
    # because its last_seen_at is old, so the panel races back to
    # offline for up to one hello cadence.
    state.heartbeat(camera_id)
    try:
        await device_ws.send(camera_id, _settings_message_for(camera_id))
        session = state.current_session()
        if session is not None and session.armed:
            await device_ws.send(camera_id, _arm_message_for(session))
        # If a mutual-sync run is active when a phone (re)connects, push
        # the sync_run signal so it can join late instead of sitting idle
        # until the run times out.
        active_sync = state._sync.current_sync()
        if active_sync is not None and camera_id not in active_sync.reports:
            _p = state.sync_params()
            await device_ws.send(camera_id, {
                "type": "sync_run",
                "sync_id": active_sync.id,
                "emit_at_s": _p.emit_a_at_s if camera_id == "A" else _p.emit_b_at_s,
                "record_duration_s": _p.record_duration_s,
            })
        await sse_hub.broadcast(
            "device_status",
            {"cam": camera_id, "online": True, "ws_connected": True},
        )
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "hello":
                device_ws.note_seen(camera_id)
                reported_sync_id = msg.get("time_sync_id")
                reported_anchor = msg.get("sync_anchor_timestamp_s")
                battery_level, battery_state = _parse_battery(msg)
                device_id, device_model = _parse_device_identity(msg)
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                    battery_level=battery_level,
                    battery_state=battery_state,
                    device_id=device_id,
                    device_model=device_model,
                )
                await device_ws.send(camera_id, _settings_message_for(camera_id))
                continue
            if mtype == "heartbeat":
                device_ws.note_seen(camera_id)
                reported_sync_id = msg.get("time_sync_id")
                reported_anchor = msg.get("sync_anchor_timestamp_s")
                battery_level, battery_state = _parse_battery(msg)
                device_id, device_model = _parse_device_identity(msg)
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                    battery_level=battery_level,
                    battery_state=battery_state,
                    device_id=device_id,
                    device_model=device_model,
                )
                telem = msg.get("sync_telemetry")
                if isinstance(telem, dict):
                    state._sync.record_sync_telemetry(camera_id, telem)
                # SSE: broadcast heartbeat-derived fields (battery, ws
                # latency, last_seen) so the dashboard can update the
                # Devices card without waiting for the 5 s /status fallback.
                # `time_synced` MUST run through the same id_match gate
                # as /status (`_gated_time_synced`); otherwise SSE flips
                # the dashboard's cached cam.time_synced=true on every
                # 1 Hz beat and the next /status tick flips it back to
                # false, making the LED flicker for a cam whose reported
                # id doesn't match the active expected id.
                _ws_snap = device_ws.snapshot().get(camera_id)
                _now = state._time_fn()
                _expected = state._sync.expected_sync_id_snapshot().get(camera_id)
                _d_snapshot = state.device_snapshot(camera_id)
                _gated = _gated_time_synced(_d_snapshot, _expected, _now)
                await sse_hub.broadcast(
                    "device_heartbeat",
                    {
                        "cam": camera_id,
                        "battery_level": battery_level,
                        "battery_state": battery_state,
                        "ws_latency_ms": _ws_snap.last_latency_ms if _ws_snap is not None else None,
                        "last_seen_at": _ws_snap.last_seen_at if _ws_snap is not None else _now,
                        "time_synced": _gated,
                        "time_sync_id": reported_sync_id,
                    },
                )
                continue
            if mtype == "frame":
                device_ws.note_seen(camera_id)
                # iOS always sends `candidates` (possibly empty) on every
                # frame — live_pairing's selector resolves the winner.
                # Pydantic raises on a malformed entry; let it.
                from schemas import BlobCandidate as _BlobCandidate
                cands_payload = [
                    _BlobCandidate.model_construct(
                        px=float(c["px"]),
                        py=float(c["py"]),
                        area=int(c["area"]),
                        area_score=float(c["area_score"]),
                    )
                    for c in msg["candidates"]
                ]
                frame = FramePayload(
                    frame_index=int(msg["i"]),
                    timestamp_s=float(msg["ts"]),
                    ball_detected=bool(cands_payload),
                    candidates=cands_payload,
                )
                session_id = str(msg.get("sid") or "")
                if not session_id:
                    continue
                new_points, counts, resolved_frame = await asyncio.to_thread(
                    state.ingest_live_frame,
                    camera_id,
                    session_id,
                    frame,
                )
                ray = await asyncio.to_thread(
                    state.live_ray_for_frame,
                    camera_id,
                    session_id,
                    resolved_frame,
                )
                await sse_hub.broadcast(
                    "frame_count",
                    {
                        "sid": session_id,
                        "cam": camera_id,
                        "path": DetectionPath.live.value,
                        "count": counts.get(camera_id, 0),
                    },
                )
                if ray is not None:
                    await sse_hub.broadcast(
                        "ray",
                        {
                            "sid": session_id,
                            "cam": camera_id,
                            "path": DetectionPath.live.value,
                            "frame_index": ray.frame_index,
                            "t_rel_s": ray.t_rel_s,
                            "origin": ray.origin,
                            "endpoint": ray.endpoint,
                            "source": ray.source,
                        },
                    )
                for point in new_points:
                    await sse_hub.broadcast(
                        "point",
                        {
                            "sid": session_id,
                            "path": DetectionPath.live.value,
                            "x": point.x_m,
                            "y": point.y_m,
                            "z": point.z_m,
                            "t_rel_s": point.t_rel_s,
                        },
                    )
                if new_points:
                    result = await asyncio.to_thread(session_results.rebuild_result_for_session, state, session_id)
                    await asyncio.to_thread(state.store_result, result)
                continue
            if mtype == "cycle_end":
                session_id = str(msg.get("sid") or "")
                reason = msg.get("reason")
                if session_id:
                    await asyncio.to_thread(state.mark_live_path_ended, camera_id, session_id, reason)
                    persisted = await asyncio.to_thread(state.persist_live_frames, camera_id, session_id)
                    if persisted is not None:
                        result = persisted
                    else:
                        result = await asyncio.to_thread(session_results.rebuild_result_for_session, state, session_id)
                        await asyncio.to_thread(state.store_result, result)
                    await sse_hub.broadcast(
                        "path_completed",
                        {
                            "sid": session_id,
                            "path": DetectionPath.live.value,
                            "cam": camera_id,
                            "reason": reason,
                            "point_count": len(result.triangulated_by_path.get(DetectionPath.live.value, [])),
                        },
                    )
                continue
    except WebSocketDisconnect:
        pass
    finally:
        device_ws.disconnect(camera_id, websocket)
        # Dashboard `/status` derives online-ness from `Device.last_seen_at`
        # with a 3 s stale window, so without this the UI keeps painting the
        # cam as online for up to 3 s after the phone sleeps / drops WS.
        state.mark_device_offline(camera_id)
        # Also clear any live preview request — there's no client to push
        # to anymore, and leaving the TTL alive would re-arm the phone the
        # instant it reconnects.
        state._preview.request(camera_id, enabled=False)
        await sse_hub.broadcast(
            "device_status",
            {"cam": camera_id, "online": False, "ws_connected": False},
        )
