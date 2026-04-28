from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

import session_results
from schemas import DetectionPath, SessionResult, _DEFAULT_SESSION_TIMEOUT_S

router = APIRouter()

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


@router.post("/sessions/arm")
async def sessions_arm(
    request: Request,
    max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S,
):
    from main import state, device_ws, sse_hub, _arm_message_for, _arm_readiness, _wants_html
    requested_paths: set[DetectionPath] | None = None
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        raw_paths = body.get("paths")
        if isinstance(raw_paths, list):
            requested_paths = session_results.normalize_paths(raw_paths)
    readiness = _arm_readiness()
    if not readiness.get("ready"):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_ready_to_arm",
                "blockers": readiness.get("blockers", []),
            },
        )
    session = state.arm_session(max_duration_s=max_duration_s, paths=requested_paths)
    await device_ws.broadcast(
        {cam.camera_id: _arm_message_for(session) for cam in state.online_devices()}
    )
    await sse_hub.broadcast(
        "session_armed",
        {
            "sid": session.id,
            "paths": sorted(p.value for p in session.paths),
            "armed_at": session.started_at,
        },
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session": session.to_dict()}


@router.post("/sessions/stop")
async def sessions_stop(request: Request):
    from main import state, device_ws, sse_hub, _disarm_message_for, _wants_html
    ended = state.stop_session()
    if ended is None:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no armed session")
    await device_ws.broadcast(
        {cam.camera_id: _disarm_message_for(ended) for cam in state.online_devices()}
    )
    await sse_hub.broadcast(
        "session_ended",
        {
            "sid": ended.id,
            "paths_completed": sorted(
                state.results.get(ended.id, SessionResult(session_id=ended.id, camera_a_received=False, camera_b_received=False)).paths_completed
            ),
        },
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session": ended.to_dict()}


@router.post("/sessions/clear")
async def sessions_clear(request: Request):
    from main import state, _wants_html
    cleared = state.clear_last_ended_session()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not cleared:
        raise HTTPException(status_code=409, detail="nothing to clear")
    return {"ok": True}


@router.post("/sessions/{session_id}/delete")
async def sessions_delete(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    try:
        removed = state.delete_session(session_id)
    except RuntimeError as e:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail=str(e))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not removed:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/trash")
async def sessions_trash(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    try:
        moved = state.trash_session(session_id)
    except RuntimeError as e:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail=str(e))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not moved:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/restore")
async def sessions_restore(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    restored = state.restore_session(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not restored:
        raise HTTPException(status_code=404, detail=f"session {session_id} not in trash")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/cancel_processing")
async def sessions_cancel_processing(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    canceled = state._processing.cancel_processing(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not canceled:
        raise HTTPException(status_code=409, detail="no cancelable processing")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/run_server_post")
async def sessions_run_server_post(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
):
    """Operator-triggered: run server-side HSV detection against every
    camera's archived MOV for this session. Replaces the old "arm with
    server_post checked" auto-flow now that MOVs are always recorded and
    the detection cost is paid only when the operator asks for it."""
    return await _enqueue_server_post(request, session_id, background_tasks)


async def _enqueue_server_post(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
):
    from main import state, _wants_html, _run_server_detection
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    queued = state._processing.resume_processing(session_id)
    if not queued:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    for clip_path, pitch in queued:
        background_tasks.add_task(_run_server_detection, clip_path, pitch)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session_id": session_id, "queued": len(queued)}
