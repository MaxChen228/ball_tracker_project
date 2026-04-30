from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

import session_results
from detection import HSVRange, ShapeGate
from schemas import DetectionPath, PitchPayload, SessionResult, _DEFAULT_SESSION_TIMEOUT_S

router = APIRouter()

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


# Detection config sources accepted by /sessions/{sid}/run_server_post.
# - "live": current dashboard config (state.hsv_range() / shape_gate() / ...).
#   Mutating effect on subsequent live sessions; used for re-running with
#   the operator's just-tuned values.
# - "frozen": the per-pitch frozen snapshot (`pitch.*_used`). Reproduces the
#   exact config detection ran with originally — required for sanity-checking
#   that an algorithm change didn't shift results. Fails fast (409) if any
#   queued pitch lacks one of the three frozen fields (legacy pitch from
#   before PR #93 stamping landed).
# - "preset:<name>": canonical preset loaded from `data/presets/<name>.json`.
#   The preset carries its own shape_gate (Phase 1 of the unified-config
#   redesign — earlier the preset only carried HSV and shape_gate
#   silently inherited from state, which defeated the "research-compare
#   without disk mutation" property because a concurrent dashboard
#   slider edit could change the cost basis mid-reprocess). Does NOT
#   mutate the live `detection_config.json`.
def _resolve_detection_config(
    source: str,
    pitch: PitchPayload,
    state,
) -> tuple[HSVRange, ShapeGate, str]:
    if source == "live":
        return (state.hsv_range(), state.shape_gate(), "live")
    if source == "frozen":
        if (
            pitch.hsv_range_used is None
            or pitch.shape_gate_used is None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"frozen config unavailable for cam={pitch.camera_id}: "
                    "pitch lacks one or more *_used fields (likely a pitch "
                    "recorded before PR #93 stamping landed)"
                ),
            )
        return (
            HSVRange(**pitch.hsv_range_used.model_dump()),
            ShapeGate(**pitch.shape_gate_used.model_dump()),
            "frozen",
        )
    if source.startswith("preset:"):
        name = source.split(":", 1)[1]
        try:
            preset = state.load_preset(name)
        except KeyError:
            known = sorted(p.name for p in state.list_presets())
            raise HTTPException(
                status_code=400,
                detail=f"unknown preset: {name!r} (known: {known})",
            )
        return (preset.hsv, preset.shape_gate, source)
    raise HTTPException(
        status_code=400,
        detail=(
            f"unknown 'source': {source!r}. "
            "Accepted: 'live' | 'frozen' | 'preset:<name>'."
        ),
    )


async def _read_source_field(request: Request) -> str:
    """Pull the required `source` field from either JSON or form body.
    Per CLAUDE.md (no silent fallback): missing/blank → HTTP 400 rather
    than implicitly defaulting. Callers (viewer form, events row) submit
    `source=live` explicitly."""
    ctype = request.headers.get("content-type", "").lower()
    raw: object = None
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            raw = body.get("source")
    else:
        try:
            form = await request.form()
        except Exception:
            form = None
        if form is not None:
            raw = form.get("source")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "missing required field 'source' "
                "(one of: 'live' | 'frozen' | 'preset:<name>')"
            ),
        )
    return raw.strip()


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
            normalized = session_results.normalize_paths(raw_paths)
            # Empty list, or a list of unknown values that `normalize_paths`
            # silently drops, is treated as "no caller preference" and falls
            # back to runtime defaults — matches the pre-NIT-batch behaviour
            # at the HTTP boundary (`arm_session` itself stays strict and
            # rejects an explicit empty set as misuse).
            requested_paths = normalized if normalized else None
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
    camera's archived MOV for this session. Replaces the old "arm with
    server_post checked" auto-flow now that MOVs are always recorded and
    the detection cost is paid only when the operator asks for it.

    Body (form or JSON) — required:
      - `source`: one of `live` | `frozen` | `preset:<name>`. See
        `_resolve_detection_config` for semantics. No default — every
        caller (viewer form, events row, JSON API) must specify
        explicitly to keep research provenance auditable.
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
    source = await _read_source_field(request)
    # Pre-flight resolve BEFORE `resume_processing` (which transitions
    # job states to "queued"). Order matters: if `source=frozen` and any
    # cam lacks a snapshot, raising mid-loop AFTER resume_processing
    # would leave the cam(s) already-transitioned stuck in "queued"
    # state with no BackgroundTask backing them — a zombie chip on
    # /events that no future operator action can clear. Pre-flight reads
    # candidates non-mutatingly via `session_candidates`, validates the
    # whole set, then commits with `resume_processing`. The pitches we
    # peek at here are the same instances `resume_processing` deep-copies
    # for hand-off; the *_used fields we read are immutable post-/pitch
    # ingest so the peek-then-copy is safe.
    candidates = state.processing.session_candidates(session_id)
    if not candidates:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    resolved_by_cam: dict[str, tuple[HSVRange, ShapeGate, str]] = {}
    for cam, pitch, _clip_path in candidates:
        resolved_by_cam[cam] = _resolve_detection_config(source, pitch, state)
    queued = state.processing.resume_processing(session_id)
    if not queued:
        # `session_candidates` saw something but `resume_processing`
        # found nothing transitionable (already running / finished).
        # 409 matches the previous semantics; resolved_by_cam is
        # discarded with no side effects.
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    for clip_path, pitch in queued:
        hsv, gate, label = resolved_by_cam[pitch.camera_id]
        background_tasks.add_task(
            _run_server_detection,
            clip_path,
            pitch,
            hsv_range=hsv,
            shape_gate=gate,
            config_label=label,
        )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {
        "ok": True,
        "session_id": session_id,
        "queued": len(queued),
        "source": source,
    }


@router.post("/sessions/{session_id}/recompute")
async def sessions_recompute(request: Request, session_id: str):
    """Re-run pairing fan-out + segmenter on this session's already-
    detected frames using per-session `cost_threshold` + `gap_threshold_m`
    overrides. No MOV decode, no HSV — candidates are read from the
    persisted `frames_live` / `frames_server_post` directly. Sub-second
    on a typical session.

    Body (JSON):
      - `cost_threshold` (required): float in [0, 1].
      - `gap_threshold_m` (optional): float in [0, 2.0] — skew-line
        residual cap, metres. Omitted → falls back to the global
        `state.pairing_tuning().gap_threshold_m`. The viewer's tuning
        strip always sends both; the optional path is a transitional
        courtesy for callers that haven't migrated.
    """
    from main import state
    from session_results import recompute_result_for_session

    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=422, detail="invalid session_id")
    body = await request.json()
    raw = body.get("cost_threshold")
    try:
        cost_threshold = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="missing or invalid 'cost_threshold'")
    if not 0.0 <= cost_threshold <= 1.0:
        raise HTTPException(
            status_code=400,
            detail="cost_threshold out of range [0, 1]",
        )
    raw_gap = body.get("gap_threshold_m")
    if raw_gap is None:
        gap_threshold_m = state.pairing_tuning().gap_threshold_m
    else:
        try:
            gap_threshold_m = float(raw_gap)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid 'gap_threshold_m'")
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
        cost_threshold=cost_threshold,
        gap_threshold_m=gap_threshold_m,
    )
    state.store_result(new_result)
    # Recompute changed `triangulated` ⇒ segments (and therefore the
    # dashboard / viewer fit visuals) need to refresh too. Broadcast the
    # same `fit` event the cycle_end path uses; dashboard listens
    # blindly for the active session id and patches its scene.
    # Ship the per-session thresholds alongside segments so dashboard /
    # viewer caches that maintain a client-side mask over `result.points`
    # (full triangulated set; pairing emits everything post Phase 1-5)
    # know the new gate without an extra `/results/<sid>` round-trip.
    await sse_hub.broadcast(
        "fit",
        {
            "sid": session_id,
            "segments": [s.model_dump() for s in new_result.segments],
            "cost_threshold": new_result.cost_threshold,
            "gap_threshold_m": new_result.gap_threshold_m,
        },
    )
    return {"ok": True, "result": new_result.model_dump()}
