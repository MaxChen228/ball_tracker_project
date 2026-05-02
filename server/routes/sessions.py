from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

import session_results
from schemas import DetectionPath, SessionResult, _DEFAULT_SESSION_TIMEOUT_S

router = APIRouter()

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


# `run_server_post` now picks a preset by name from the request body.
# This is the single mechanism for "re-detect this session under config
# X": operator chooses an on-disk preset, server loads it, detect runs.
# There is no implicit "use whatever the dashboard slider currently
# shows" — Live and server_post detection are independent choices.


def _unknown_detection_paths(raw_paths: list[object]) -> list[str]:
    out: list[str] = []
    for item in raw_paths:
        try:
            DetectionPath(str(item))
        except ValueError:
            out.append(str(item))
    return out


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
        if "paths" in body:
            raw_paths = body["paths"]
            if not isinstance(raw_paths, list):
                raise HTTPException(status_code=422, detail="paths must be an array")
            unknown = _unknown_detection_paths(raw_paths)
            if unknown:
                raise HTTPException(status_code=422, detail=f"unknown detection paths: {unknown}")
            normalized = session_results.normalize_paths(raw_paths)
            if not normalized:
                raise HTTPException(status_code=422, detail="paths must be non-empty")
            requested_paths = normalized
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


@router.post("/sessions/{session_id}/star")
async def sessions_star(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    starred = state.star_session(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not starred:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/unstar")
async def sessions_unstar(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    unstarred = state.unstar_session(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not unstarred:
        raise HTTPException(status_code=404, detail=f"session {session_id} not starred")
    return {"ok": True, "session_id": session_id}


@router.post("/sessions/{session_id}/cancel_processing")
async def sessions_cancel_processing(request: Request, session_id: str):
    from main import state, _wants_html
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    canceled = state.processing.cancel_processing(session_id)
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
    camera's archived MOV for this session, using the named preset from
    the request body.

    Body (JSON or form): `preset_name` — required, must be an existing
    preset slug under `data/presets/`. The named preset's full
    detection-config snapshot is loaded into the background detection
    job; the operator's dashboard active preset is irrelevant to this
    run. The resulting `SessionResult.server_post_config_used` carries
    the exact HSV + shape-gate + preset identity that produced the
    rerun; re-running with a different preset overwrites both the
    detection results and that snapshot.
    """
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
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json() if await request.body() else {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        preset_name = body.get("preset_name")
    else:
        form = await request.form()
        preset_name = form.get("preset_name")
    if not isinstance(preset_name, str) or not preset_name:
        raise HTTPException(
            status_code=422,
            detail="missing required field 'preset_name'",
        )
    try:
        preset = state.load_preset(preset_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {preset_name!r}")
    candidates = state.processing.session_candidates(session_id)
    if not candidates:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    queued = state.processing.resume_processing(session_id)
    if not queued:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    from schemas import DetectionConfigSnapshotPayload
    # Preset and snapshot share canonical `(algorithm_id, params)`
    # shape — passthrough with a defensive copy so later snapshot
    # mutation can't bleed into the preset.
    snapshot = DetectionConfigSnapshotPayload(
        algorithm_id=preset.algorithm_id,
        params=dict(preset.params),
        preset_name=preset_name,
    )
    for clip_path, pitch in queued:
        background_tasks.add_task(
            _run_server_detection,
            clip_path,
            pitch,
            config_snapshot=snapshot,
        )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {
        "ok": True,
        "session_id": session_id,
        "queued": len(queued),
        "preset_name": preset_name,
    }


@router.post("/sessions/{session_id}/recompute")
async def sessions_recompute(request: Request, session_id: str):
    """Re-run pairing fan-out + segmenter on this session's already-
    detected frames using a per-session `gap_threshold_m` override. No
    MOV decode, no HSV — candidates are read from the persisted
    `frames_live` / `frames_server_post` directly. Sub-second on a
    typical session.

    Body (JSON):
      - `gap_threshold_m` (required): float in [0, 2.0] — skew-line
        residual cap, metres.

    The cost gate is no longer per-session — each algorithm owns its
    own threshold via `algorithms.cost_threshold_for_algorithm`. The
    viewer's tuning strip only ships gap.
    """
    from main import state
    from session_results import recompute_result_for_session

    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    body = await request.json()
    raw_gap = body.get("gap_threshold_m")
    try:
        gap_threshold_m = float(raw_gap)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="missing or invalid 'gap_threshold_m'")
    if not 0.0 <= gap_threshold_m <= 2.0:
        raise HTTPException(
            status_code=400,
            detail="gap_threshold_m out of range [0, 2.0]",
        )

    # Existence check matches `state.store_result`'s own guard:
    # a session is "alive" iff it has a pitch entry, a result entry, or
    # a live pairing buffer. Live-only WS sessions before persist_live_frames
    # flush only live in `_live_pairings` — so checking `pitches` alone
    # would 404 a still-active live session.
    with state._lock:
        known = (
            any(s == session_id for _, s in state.pitches)
            or session_id in state.results
            or session_id in state._live_pairings
        )
    if not known:
        raise HTTPException(status_code=404, detail=f"session {session_id} not found")

    from main import sse_hub

    new_result = recompute_result_for_session(
        state, session_id,
        gap_threshold_m=gap_threshold_m,
    )
    state.store_result(new_result)
    # Recompute changed `triangulated` ⇒ segments (and therefore the
    # dashboard / viewer fit visuals) need to refresh too. Broadcast the
    # same `fit` event the cycle_end path uses; dashboard listens
    # blindly for the active session id and patches its scene.
    # Ship the per-session gap threshold alongside segments so dashboard
    # / viewer caches that maintain a client-side mask know the new gate
    # without an extra `/results/<sid>` round-trip.
    await sse_hub.broadcast(
        "fit",
        {
            "sid": session_id,
            "cause": "recompute",
            "segments": [s.model_dump() for s in new_result.segments],
            "gap_threshold_m": new_result.gap_threshold_m,
        },
    )
    return {"ok": True, "result": new_result.model_dump()}
