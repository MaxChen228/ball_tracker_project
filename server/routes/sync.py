from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from schemas import SyncLogBody, SyncReport
from state import SyncParams

router = APIRouter()

_SYNC_START_STATUS_FOR_REASON: dict[str, int] = {
    "session_armed": 409,
    "sync_in_progress": 409,
    "cooldown": 409,
    "devices_missing": 409,
}

_SYNC_WAV_RE = re.compile(r"^sy_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}\.wav$")


@router.post("/sync/start")
async def sync_start(request: Request) -> dict[str, Any]:
    from main import state, device_ws
    run, reason = state.start_sync()
    if reason is not None:
        status_code = _SYNC_START_STATUS_FOR_REASON.get(reason, 409)
        raise HTTPException(status_code=status_code, detail={"ok": False, "error": reason})
    assert run is not None
    state._sync.reset_sync_telemetry_peaks(None)
    state._sync.set_expected_sync_id([d.camera_id for d in state.online_devices()], run.id)
    params = state.sync_params()
    per_cam = {
        cam.camera_id: {
            "type": "sync_run",
            "sync_id": run.id,
            "emit_at_s": params.emit_a_at_s if cam.camera_id == "A" else params.emit_b_at_s,
            "record_duration_s": params.record_duration_s,
        }
        for cam in state.online_devices()
    }
    await device_ws.broadcast(per_cam)
    return {"ok": True, "sync": run.to_dict()}


@router.get("/sync/audio/{filename}")
def sync_audio_download(filename: str) -> FileResponse:
    from main import state
    if not _SYNC_WAV_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid sync audio filename")
    wav_path = state.data_dir / "sync_audio" / filename
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="wav not found")
    return FileResponse(wav_path, media_type="audio/wav", filename=filename)


@router.post("/sync/audio_upload")
async def sync_audio_upload(
    payload: str = Form(...),
    audio: UploadFile = File(...),
) -> dict[str, Any]:
    import logging
    import sync_audio_detect
    from main import state
    logger = logging.getLogger("ball_tracker")
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"payload JSON parse: {e}") from e
    required = ("sync_id", "camera_id", "role", "audio_start_pts_s")
    missing = [k for k in required if meta.get(k) is None]
    if missing:
        raise HTTPException(status_code=422, detail=f"payload missing required keys: {missing}")
    sync_id = str(meta["sync_id"])
    camera_id = str(meta["camera_id"])
    role = str(meta["role"])
    if role not in ("A", "B"):
        raise HTTPException(status_code=422, detail=f"role must be 'A' or 'B', got {role!r}")
    try:
        audio_start_pts_s = float(meta["audio_start_pts_s"])
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"audio_start_pts_s not a float: {e}") from e
    emission_pts_s = meta.get("emission_pts_s")
    wav_bytes = await audio.read()
    if not wav_bytes:
        raise HTTPException(status_code=422, detail="audio part empty")
    audio_dir = state.data_dir / "sync_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav_path = audio_dir / f"{sync_id}_{camera_id}.wav"
    wav_path.write_bytes(wav_bytes)
    params = state.sync_params()
    emit_at_self = params.emit_a_at_s if role == "A" else params.emit_b_at_s
    emit_at_other = params.emit_b_at_s if role == "A" else params.emit_a_at_s
    try:
        report, debug = sync_audio_detect.detect_sync_report(
            wav_bytes=wav_bytes, sync_id=sync_id, camera_id=camera_id,
            role=role, audio_start_pts_s=audio_start_pts_s,
            emit_at_s_self=emit_at_self, emit_at_s_other=emit_at_other,
            search_window_s=params.search_window_s,
        )
    except Exception as e:
        logger.exception("sync_audio_upload detection failed cam=%s", camera_id)
        raise HTTPException(status_code=500, detail=f"detection failed: {e}") from e
    logger.info(
        "sync_audio_upload cam=%s role=%s duration_s=%.3f "
        "peak_self=%.4f peak_other=%.4f psr_self=%.2f psr_other=%.2f "
        "t_self=%.6f t_other=%.6f",
        camera_id, role, debug["duration_s"],
        debug["peak_self"], debug["peak_other"],
        debug["psr_self"], debug["psr_other"],
        report.t_self_s or 0.0, report.t_from_other_s or 0.0,
    )
    run_after, result, reason = state._sync.record_sync_report(report)
    if reason == "no_sync":
        raise HTTPException(status_code=409, detail={"ok": False, "error": "no_sync"})
    if reason == "stale_sync_id":
        raise HTTPException(status_code=409, detail={"ok": False, "error": "stale_sync_id"})
    resp: dict[str, Any] = {
        "ok": True,
        "solved": result is not None,
        "detection": {
            "peak_self": debug["peak_self"],
            "peak_other": debug["peak_other"],
            "psr_self": debug["psr_self"],
            "psr_other": debug["psr_other"],
            "duration_s": debug["duration_s"],
            "sample_rate": debug["sample_rate"],
            "n_burst": debug["n_burst"],
            "windowed": debug["windowed"],
            "emission_pts_s": emission_pts_s,
            "wav_path": str(wav_path.relative_to(state.data_dir)),
        },
    }
    if result is not None:
        resp["result"] = result.model_dump()
    elif run_after is not None:
        resp["run"] = run_after.to_dict()
    return resp


@router.post("/sync/report")
async def sync_report(report: SyncReport) -> dict[str, Any]:
    from main import state
    run_after, result, reason = state._sync.record_sync_report(report)
    if reason == "no_sync":
        raise HTTPException(status_code=409, detail={"ok": False, "error": "no_sync"})
    if reason == "stale_sync_id":
        raise HTTPException(status_code=409, detail={"ok": False, "error": "stale_sync_id"})
    resp: dict[str, Any] = {"ok": True, "solved": result is not None}
    if result is not None:
        resp["result"] = result.model_dump()
    elif run_after is not None:
        resp["run"] = run_after.to_dict()
    return resp


@router.get("/sync/debug_export")
def sync_debug_export() -> Response:
    from main import state, _build_device_status_rows
    from sync_analysis import build_debug_report
    last = state._sync.last_sync_result()
    logs = state._sync.sync_logs(limit=60)
    telem = state._sync.sync_telemetry_snapshot()
    devices = _build_device_status_rows()
    report = build_debug_report(
        last_sync=last.model_dump() if last is not None else None,
        telemetry=telem,
        logs=[e.model_dump() for e in logs],
        mutual_threshold=state.mutual_sync_threshold(),
        chirp_threshold=state.chirp_detect_threshold(),
        devices=devices,
    )
    return Response(content=report, media_type="text/plain; charset=utf-8")


@router.get("/sync/state")
def sync_state(log_limit: int = 200) -> dict[str, Any]:
    from main import state
    run = state._sync.current_sync()
    last = state._sync.last_sync_result()
    logs = state._sync.sync_logs(limit=log_limit)
    return {
        "sync": run.to_dict() if run is not None else None,
        "last_sync": last.model_dump() if last is not None else None,
        "cooldown_remaining_s": state._sync.sync_cooldown_remaining_s(),
        "logs": [entry.model_dump() for entry in logs],
        "telemetry": state._sync.sync_telemetry_snapshot(),
    }


@router.post("/sync/trigger")
async def sync_trigger(request: Request) -> Any:
    from main import state, device_ws, _wants_html
    ctype = request.headers.get("content-type", "").lower()
    is_form = "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype
    camera_ids: list[str] | None = None
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            raw = body.get("camera_ids")
            if isinstance(raw, list):
                camera_ids = [str(c) for c in raw]
            elif raw is not None:
                raise HTTPException(status_code=422, detail="camera_ids must be a list of strings")
    elif is_form:
        form = await request.form()
        raw = form.get("camera_ids")
        if raw is not None:
            camera_ids = [c for c in (str(raw).replace(",", " ").split()) if c]
    dispatched = state.trigger_sync_command(camera_ids)
    state._sync.reset_sync_telemetry_peaks(dispatched if dispatched else None)
    state._sync.clear_last_sync_result()
    pending_ids = state._sync.pending_sync_command_ids()
    for cam, sid in pending_ids.items():
        if cam in dispatched:
            state._sync.set_expected_sync_id([cam], sid)
    ws_messages = {
        cam: {"type": "sync_command", "command": "start", "sync_command_id": sid}
        for cam, sid in pending_ids.items()
        if cam in dispatched
    }
    if ws_messages:
        await device_ws.broadcast(ws_messages)
    if is_form:
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "dispatched_to": dispatched}


@router.post("/sync/claim")
def sync_claim() -> dict[str, Any]:
    from main import state
    intent = state._sync.claim_time_sync_intent()
    return {
        "ok": True,
        "sync_id": intent.id,
        "started_at": intent.started_at,
        "expires_at": intent.expires_at,
    }


@router.post("/sync/log")
async def sync_log_post(body: SyncLogBody) -> dict[str, Any]:
    from main import state
    state._sync.log_sync_event(source=body.camera_id, event=body.event, detail=body.detail)
    return {"ok": True}


@router.get("/sync/params")
def sync_params_get() -> dict[str, Any]:
    from main import state
    p = state.sync_params()
    return {
        "emit_a_at_s": p.emit_a_at_s,
        "emit_b_at_s": p.emit_b_at_s,
        "record_duration_s": p.record_duration_s,
        "search_window_s": p.search_window_s,
    }


@router.post("/settings/sync_params")
async def sync_params_set(request: Request) -> dict[str, Any]:
    from main import state
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"JSON parse error: {e}") from e
    cur = state.sync_params()
    emit_a = body.get("emit_a_at_s", cur.emit_a_at_s)
    emit_b = body.get("emit_b_at_s", cur.emit_b_at_s)
    dur = float(body.get("record_duration_s", cur.record_duration_s))
    win = float(body.get("search_window_s", cur.search_window_s))
    if not isinstance(emit_a, list) or not isinstance(emit_b, list):
        raise HTTPException(status_code=422, detail="emit_a_at_s and emit_b_at_s must be arrays")
    if dur < 1.0 or dur > 30.0:
        raise HTTPException(status_code=422, detail="record_duration_s must be 1-30 s")
    if win < 0.05 or win > 2.0:
        raise HTTPException(status_code=422, detail="search_window_s must be 0.05-2.0 s")
    state.set_sync_params(SyncParams(
        emit_a_at_s=[float(t) for t in emit_a],
        emit_b_at_s=[float(t) for t in emit_b],
        record_duration_s=dur,
        search_window_s=win,
    ))
    return {"ok": True, **sync_params_get()}
