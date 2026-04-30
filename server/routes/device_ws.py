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
                _now = state.now()
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
                # Skipping pydantic validation (model_construct) — iOS
                # lockstep guarantees the wire fields; missing key
                # surfaces as KeyError, bad type as ValueError.
                # `aspect` / `fill` are required as of the shape-prior
                # selector landing — old iOS builds without them KeyError
                # loud here, exactly the lockstep failure we want.
                from schemas import BlobCandidate as _BlobCandidate
                cands_payload = [
                    _BlobCandidate.model_construct(
                        px=float(c["px"]),
                        py=float(c["py"]),
                        area=int(c["area"]),
                        area_score=float(c["area_score"]),
                        aspect=float(c["aspect"]),
                        fill=float(c["fill"]),
                    )
                    for c in msg["candidates"]
                ]
                frame = FramePayload(
                    frame_index=int(msg["i"]),
                    timestamp_s=float(msg["ts"]),
                    ball_detected=bool(cands_payload),
                    candidates=cands_payload,
                )
                # Schema-strict: missing/empty `sid` is a wire-format
                # bug, not a runtime fallback. Raise loud — iOS lockstep
                # guarantees this field on every frame post-arm, and a
                # silent skip used to mask "phone never received arm"
                # symptoms by quietly dropping all subsequent frames.
                if "sid" not in msg or not msg["sid"]:
                    raise ValueError(f"frame message missing required 'sid' (cam={camera_id})")
                session_id = str(msg["sid"])
                new_points, counts, resolved_frame = await asyncio.to_thread(
                    state.ingest_live_frame,
                    camera_id,
                    session_id,
                    frame,
                )
                rays = await asyncio.to_thread(
                    state.live_rays_for_frame,
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
                # Fan-out: one SSE 'ray' event per candidate so the
                # dashboard 3D scene can apply the same cost-threshold
                # filter as the post-pitch viewer. Pre-fan-out we
                # emitted only the winner-dot ray here.
                for ray in rays:
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
                            "cand_idx": ray.cand_idx,
                            "cost": ray.cost,
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
                # Per-frame rebuild + atomic JSON write was removed: each
                # detection went through fit_ballistic_ransac (since
                # retired) + a disk write, hammering both CPU and disk
                # 50-200×/pitch for no UI gain. Streaming clients already
                # see incremental updates via the `point` SSE event above;
                # the authoritative SessionResult is rebuilt at cycle_end
                # (see below) and viewer GET /results/{sid} rebuilds on
                # demand if it lands mid-stream (state.get).
                continue
            if mtype == "cycle_end":
                # Schema-strict: cycle_end without sid is a wire bug.
                # Loud raise rather than silent skip (was: `if session_id:`
                # which masked the symptom).
                if "sid" not in msg or not msg["sid"]:
                    raise ValueError(f"cycle_end message missing required 'sid' (cam={camera_id})")
                session_id = str(msg["sid"])
                reason = msg.get("reason")
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
                # Dashboard listens for `fit` to paint the latest pitch's
                # ballistic curve + speed badge the instant the last cam
                # reports cycle_end. Rebuild already ran find_segments
                # (see session_results.stamp_segments_on_result) so we
                # just forward the persisted SegmentRecord list here.
                await sse_hub.broadcast(
                    "fit",
                    {
                        "sid": session_id,
                        "segments": [s.model_dump() for s in result.segments],
                        "cost_threshold": result.cost_threshold,
                        "gap_threshold_m": result.gap_threshold_m,
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
