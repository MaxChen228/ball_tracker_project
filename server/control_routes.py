from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from schemas import CaptureMode, DetectionPath, Session, SessionResult, TrackingExposureCapMode


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


def build_status_response(
    *,
    state: Any,
    device_ws: Any,
    time_sync_max_age_s: float,
) -> dict[str, Any]:
    summary = state.summary()
    session = state.session_snapshot()
    sync_run = state.current_sync()
    last_sync = state.last_sync_result()
    now = state._time_fn()
    ws_snapshot = device_ws.snapshot()
    return {
        **summary,
        "devices": [
            {
                "camera_id": d.camera_id,
                "last_seen_at": d.last_seen_at,
                "time_synced": (
                    d.time_synced
                    and d.time_sync_id is not None
                    and d.time_sync_at is not None
                    and now - d.time_sync_at <= time_sync_max_age_s
                ),
                "time_sync_id": d.time_sync_id,
                "time_sync_age_s": (
                    None if d.time_sync_at is None else float(now - d.time_sync_at)
                ),
                "sync_anchor_timestamp_s": d.sync_anchor_timestamp_s,
                "ws_connected": (
                    ws_snapshot.get(d.camera_id).connected
                    if ws_snapshot.get(d.camera_id) is not None
                    else False
                ),
                "ws_latency_ms": (
                    ws_snapshot.get(d.camera_id).last_latency_ms
                    if ws_snapshot.get(d.camera_id) is not None
                    else None
                ),
            }
            for d in state.online_devices()
        ],
        "session": session.to_dict() if session is not None else None,
        "commands": state.commands_for_devices(),
        "capture_mode": state.current_mode().value,
        "default_paths": sorted(p.value for p in state.default_paths()),
        "sync": sync_run.to_dict() if sync_run is not None else None,
        "last_sync": last_sync.model_dump() if last_sync is not None else None,
        "sync_cooldown_remaining_s": state.sync_cooldown_remaining_s(),
        "sync_commands": state.pending_sync_commands(),
        "chirp_detect_threshold": state.chirp_detect_threshold(),
        "heartbeat_interval_s": state.heartbeat_interval_s(),
        "tracking_exposure_cap": state.tracking_exposure_cap().value,
        "capture_height_px": state.capture_height_px(),
        "preview_requested": state._preview.requested_map(),
        "calibration_frame_requested": {
            cam: True
            for cam in state._cal_frame_requested.keys()
            if state.is_calibration_frame_requested(cam)
        },
        "auto_calibration": state.auto_cal_status(),
        "live_session": state.live_session_summary(),
        "ws_devices": {
            cam: {
                "connected": snap.connected,
                "connected_at": snap.connected_at,
                "last_seen_at": snap.last_seen_at,
                "last_latency_ms": snap.last_latency_ms,
            }
            for cam, snap in ws_snapshot.items()
        },
    }


def settings_message_for(
    *,
    camera_id: str,
    state: Any,
    device_ws: Any,
    time_sync_max_age_s: float,
) -> dict[str, Any]:
    status = build_status_response(
        state=state,
        device_ws=device_ws,
        time_sync_max_age_s=time_sync_max_age_s,
    )
    return {
        "type": "settings",
        "camera_id": camera_id,
        "paths": status.get("default_paths", []),
        "chirp_detect_threshold": status.get("chirp_detect_threshold"),
        "heartbeat_interval_s": status.get("heartbeat_interval_s"),
        "tracking_exposure_cap": status.get("tracking_exposure_cap"),
        "capture_height_px": status.get("capture_height_px"),
        "preview_requested": status.get("preview_requested", {}).get(camera_id, False),
        "calibration_frame_requested": status.get("calibration_frame_requested", {}).get(camera_id, False),
    }


def arm_message_for(session: Session) -> dict[str, Any]:
    return {
        "type": "arm",
        "sid": session.id,
        "paths": sorted(p.value for p in session.paths),
        "max_duration_s": session.max_duration_s,
        "tracking_exposure_cap": session.tracking_exposure_cap.value,
    }


def disarm_message_for(session: Session) -> dict[str, Any]:
    return {
        "type": "disarm",
        "sid": session.id,
    }


def build_control_router(
    *,
    get_state: Callable[[], Any],
    get_device_ws: Callable[[], Any],
    get_sse_hub: Callable[[], Any],
    default_session_timeout_s: float,
    time_sync_max_age_s: float,
) -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    def status() -> dict[str, Any]:
        return build_status_response(
            state=get_state(),
            device_ws=get_device_ws(),
            time_sync_max_age_s=time_sync_max_age_s,
        )

    @router.get("/stream")
    async def stream() -> StreamingResponse:
        async def event_gen():
            yield "event: hello\ndata: {}\n\n"
            async for payload in get_sse_hub().subscribe():
                yield payload

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    @router.post("/sessions/arm")
    async def sessions_arm(
        request: Request,
        max_duration_s: float = default_session_timeout_s,
    ):
        state = get_state()
        requested_paths: set[DetectionPath] | None = None
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            body = await request.json()
            raw_paths = body.get("paths")
            if isinstance(raw_paths, list):
                requested_paths = state._normalize_paths(raw_paths)
        session = state.arm_session(max_duration_s=max_duration_s, paths=requested_paths)
        await get_device_ws().broadcast(
            {
                cam.camera_id: arm_message_for(session)
                for cam in state.online_devices()
            }
        )
        await get_sse_hub().broadcast(
            "session_armed",
            {
                "sid": session.id,
                "paths": sorted(p.value for p in session.paths),
                "armed_at": session.started_at,
            },
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "session": session.to_dict()}

    @router.post("/sessions/stop")
    async def sessions_stop(request: Request):
        state = get_state()
        ended = state.stop_session()
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        if ended is None:
            raise HTTPException(status_code=409, detail="no armed session")
        await get_device_ws().broadcast(
            {
                cam.camera_id: disarm_message_for(ended)
                for cam in state.online_devices()
            }
        )
        await get_sse_hub().broadcast(
            "session_ended",
            {
                "sid": ended.id,
                "paths_completed": sorted(
                    state.results.get(
                        ended.id,
                        SessionResult(
                            session_id=ended.id,
                            camera_a_received=False,
                            camera_b_received=False,
                        ),
                    ).paths_completed
                ),
            },
        )
        return {"ok": True, "session": ended.to_dict()}

    @router.post("/sessions/set_mode")
    async def sessions_set_mode(
        request: Request,
        mode: str = Form(...),
    ):
        state = get_state()
        try:
            applied = CaptureMode(mode)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"invalid mode {mode!r}; expected one of: {[m.value for m in CaptureMode]}",
            )
        state.set_mode(applied)
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "capture_mode": applied.value}

    @router.post("/detection/paths")
    async def detection_paths(request: Request):
        state = get_state()
        ctype = request.headers.get("content-type", "").lower()
        raw_paths: list[str] | None = None
        if "application/json" in ctype:
            body = await request.json()
            if isinstance(body.get("paths"), list):
                raw_paths = body["paths"]
        else:
            form = await request.form()
            raw = form.getlist("paths")
            raw_paths = [str(v) for v in raw]
        paths = state._normalize_paths(raw_paths or [])
        if not paths:
            raise HTTPException(status_code=400, detail="at least one detection path is required")
        try:
            applied = state.set_default_paths(paths)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "paths": sorted(p.value for p in applied)}

    @router.post("/settings/chirp_threshold")
    async def settings_chirp_threshold(request: Request):
        state = get_state()
        threshold: float | None = None
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            body = await request.json()
            try:
                threshold = float(body.get("threshold"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="missing or invalid 'threshold'")
        else:
            form = await request.form()
            raw = form.get("threshold")
            if raw is None:
                raise HTTPException(status_code=400, detail="missing 'threshold'")
            try:
                threshold = float(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid 'threshold'")
        try:
            applied = state.set_chirp_detect_threshold(threshold)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "value": applied}

    @router.post("/settings/heartbeat_interval")
    async def settings_heartbeat_interval(request: Request):
        state = get_state()
        interval: float | None = None
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            body = await request.json()
            try:
                interval = float(body.get("interval_s"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="missing or invalid 'interval_s'")
        else:
            form = await request.form()
            raw = form.get("interval_s")
            if raw is None:
                raise HTTPException(status_code=400, detail="missing 'interval_s'")
            try:
                interval = float(raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid 'interval_s'")
        try:
            applied = state.set_heartbeat_interval_s(interval)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "value": applied}

    @router.post("/settings/tracking_exposure_cap")
    async def settings_tracking_exposure_cap(request: Request):
        state = get_state()
        mode_raw: Any
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            body = await request.json()
            mode_raw = body.get("mode")
        else:
            form = await request.form()
            mode_raw = form.get("mode")
        if mode_raw is None:
            raise HTTPException(status_code=400, detail="missing 'mode'")
        try:
            mode = TrackingExposureCapMode(str(mode_raw))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"invalid 'mode'; expected one of {[m.value for m in TrackingExposureCapMode]}",
            )
        applied = state.set_tracking_exposure_cap(mode)
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "value": applied.value}

    @router.post("/settings/capture_height")
    async def settings_capture_height(request: Request):
        state = get_state()
        height_raw: Any
        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            body = await request.json()
            height_raw = body.get("height")
        else:
            form = await request.form()
            height_raw = form.get("height")
        if height_raw is None:
            raise HTTPException(status_code=400, detail="missing 'height'")
        try:
            height = int(height_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid 'height'")
        try:
            applied = state.set_capture_height_px(height)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        await get_device_ws().broadcast(
            {
                cam.camera_id: settings_message_for(
                    camera_id=cam.camera_id,
                    state=state,
                    device_ws=get_device_ws(),
                    time_sync_max_age_s=time_sync_max_age_s,
                )
                for cam in state.online_devices()
            }
        )
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return {"ok": True, "value": applied}

    @router.post("/sessions/clear")
    async def sessions_clear(request: Request):
        state = get_state()
        cleared = state.clear_last_ended_session()
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        if not cleared:
            raise HTTPException(status_code=409, detail="nothing to clear")
        return {"ok": True}

    return router
