from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from control_routes import arm_message_for, settings_message_for
from schemas import DetectionPath, FramePayload


def build_device_ws_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    get_sse_hub: Callable[[], Any],
    validate_camera_id_or_422: Callable[[str], None],
    time_sync_max_age_s: float,
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/device/{camera_id}")
    async def ws_device(camera_id: str, websocket: WebSocket) -> None:
        validate_camera_id_or_422(camera_id)
        device_ws = get_device_ws()
        state = get_state()
        sse_hub = get_sse_hub()

        await device_ws.connect(camera_id, websocket)
        try:
            await device_ws.send(
                camera_id,
                settings_message_for(
                    camera_id=camera_id,
                    state=state,
                    device_ws=device_ws,
                    time_sync_max_age_s=time_sync_max_age_s,
                ),
            )
            session = state.current_session()
            if session is not None and session.armed:
                await device_ws.send(camera_id, arm_message_for(session))
            active_sync = state.current_sync()
            if active_sync is not None and camera_id not in active_sync.reports:
                await device_ws.send(
                    camera_id,
                    {"type": "sync_run", "sync_id": active_sync.id},
                )
            await sse_hub.broadcast(
                "device_status",
                {"cam": camera_id, "online": True, "ws_connected": True},
            )
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "hello":
                    device_ws.note_seen(camera_id)
                    state.heartbeat(
                        camera_id,
                        time_synced=bool(msg.get("time_synced", False)),
                        time_sync_id=msg.get("time_sync_id"),
                        sync_anchor_timestamp_s=msg.get("sync_anchor_timestamp_s"),
                    )
                    await device_ws.send(
                        camera_id,
                        settings_message_for(
                            camera_id=camera_id,
                            state=state,
                            device_ws=device_ws,
                            time_sync_max_age_s=time_sync_max_age_s,
                        ),
                    )
                    continue
                if mtype == "heartbeat":
                    device_ws.note_seen(camera_id)
                    state.heartbeat(
                        camera_id,
                        time_synced=bool(msg.get("time_synced", False)),
                        time_sync_id=msg.get("time_sync_id"),
                        sync_anchor_timestamp_s=msg.get("sync_anchor_timestamp_s"),
                    )
                    continue
                if mtype == "frame":
                    device_ws.note_seen(camera_id)
                    frame = FramePayload(
                        frame_index=int(msg.get("i", 0)),
                        timestamp_s=float(msg["ts"]),
                        px=None if msg.get("px") is None else float(msg["px"]),
                        py=None if msg.get("py") is None else float(msg["py"]),
                        ball_detected=bool(msg.get("detected", False)),
                    )
                    session_id = str(msg.get("sid") or "")
                    if not session_id:
                        continue
                    new_points, counts = await asyncio.to_thread(
                        state.ingest_live_frame,
                        camera_id,
                        session_id,
                        frame,
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
                        result = await asyncio.to_thread(state._rebuild_result_for_session, session_id)
                        await asyncio.to_thread(state.store_result, result)
                    continue
                if mtype == "cycle_end":
                    session_id = str(msg.get("sid") or "")
                    reason = msg.get("reason")
                    if session_id:
                        await asyncio.to_thread(state.mark_live_path_ended, camera_id, session_id, reason)
                        result = await asyncio.to_thread(state._rebuild_result_for_session, session_id)
                        await asyncio.to_thread(state.store_result, result)
                        await sse_hub.broadcast(
                            "path_completed",
                            {
                                "sid": session_id,
                                "path": DetectionPath.live.value,
                                "cam": camera_id,
                                "reason": reason,
                                "point_count": len(
                                    result.triangulated_by_path.get(DetectionPath.live.value, [])
                                ),
                            },
                        )
                        continue
        except WebSocketDisconnect:
            pass
        finally:
            device_ws.disconnect(camera_id, websocket)
            await sse_hub.broadcast(
                "device_status",
                {"cam": camera_id, "online": False, "ws_connected": False},
            )

    return router
