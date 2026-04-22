from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from schemas import TrackingExposureCapMode

router = APIRouter()


@router.post("/detection/paths")
async def detection_paths(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    raw_paths: list[str] | None = None
    if "application/json" in ctype:
        body = await request.json()
        if isinstance(body.get("paths"), list):
            raw_paths = body["paths"]
    else:
        form = await request.form()
        raw_paths = [str(v) for v in form.getlist("paths")]
    paths = state._normalize_paths(raw_paths or [])
    if not paths:
        raise HTTPException(status_code=400, detail="at least one detection path is required")
    try:
        applied = state.set_default_paths(paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "paths": sorted(p.value for p in applied)}


@router.post("/settings/chirp_threshold")
async def settings_chirp_threshold(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
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
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/mutual_sync_threshold")
async def settings_mutual_sync_threshold(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
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
        applied = state.set_mutual_sync_threshold(threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/heartbeat_interval")
async def settings_heartbeat_interval(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
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
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@router.post("/settings/tracking_exposure_cap")
async def settings_tracking_exposure_cap(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
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
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied.value}


@router.post("/settings/capture_height")
async def settings_capture_height(request: Request):
    from main import state, device_ws, _settings_message_for, _wants_html
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
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}
