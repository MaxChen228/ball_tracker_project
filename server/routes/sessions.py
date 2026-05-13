from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse

import session_results
from schemas import (
    DetectionConfigSnapshotPayload,
    DetectionPath,
    SessionResult,
    _DEFAULT_SESSION_TIMEOUT_S,
)

router = APIRouter()

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


# Server-post run is keyed by an explicit detection-config snapshot —
# operator either names a preset (then server loads it) or supplies
# ad-hoc params (then server validates them against the detector's
# params_schema). Two endpoints share this path:
#   - POST /sessions/{sid}/runs/{algorithm_id}: preset XOR params,
#     algorithm pinned in the URL
#   - POST /sessions/{sid}/run_server_post: preset-name-only, kept as a
#     deprecation alias for HTML form callers (events row + viewer rerun)
# Both end up calling `_dispatch_server_post` with the same snapshot
# shape — there is no implicit "use whatever the dashboard slider
# currently shows" anywhere.


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


async def _parse_request_body(request: Request) -> dict:
    """Parse a JSON or form-encoded body into a plain dict. Empty body
    returns an empty dict. JSON bodies that decode to anything other
    than a dict raise 400."""
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        if not await request.body():
            return {}
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        return body
    form = await request.form()
    return {k: form.get(k) for k in form}


def _resolve_return_to(body: dict, session_id: str) -> str:
    """Where to 303-redirect an HTML form caller after dispatch.

    Two HTML callers submit the run_server_post form: the dashboard
    events row (omits the field → falls back to `/`) and the viewer's
    RERUN button (sends `return_to=/viewer/{sid}` so the operator
    stays on the page they pressed from). Whitelisted to `/` and
    `/viewer/{session_id}` to block open-redirect via crafted bodies;
    anything unrecognised maps to `/`."""
    rt = body.get("return_to")
    if isinstance(rt, str) and (rt == "/" or rt == f"/viewer/{session_id}"):
        return rt
    return "/"


def _snapshot_from_preset_name(state, preset_name: object) -> DetectionConfigSnapshotPayload:
    """Build a snapshot by looking up a preset by slug. Used by the
    deprecation-alias endpoint where the algorithm id is implied by the
    preset itself (not pinned in the URL)."""
    if not isinstance(preset_name, str) or not preset_name:
        raise HTTPException(
            status_code=422,
            detail="missing required field 'preset_name'",
        )
    try:
        preset = state.load_preset(preset_name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset: {preset_name!r}")
    return DetectionConfigSnapshotPayload(
        algorithm_id=preset.algorithm_id,
        params=dict(preset.params),
        preset_name=preset_name,
    )


def _snapshot_for_algorithm(
    state,
    url_algorithm_id: str,
    body: dict,
) -> DetectionConfigSnapshotPayload:
    """Build a snapshot for an URL-pinned algorithm. Body must carry
    exactly one of `preset_name` (str) or `params` (dict).

    Error matrix:
      - URL slug malformed → 400
      - URL algorithm unknown → 404
      - URL algorithm is a non-runnable data source → 422
      - body has neither preset_name nor params → 422
      - body has both → 422
      - preset_name unknown → 404
      - preset.algorithm_id mismatches URL → 422
      - params fail detector.params_schema → 422
    """
    import algorithms
    from pydantic import ValidationError

    if not algorithms.is_valid_id_format(url_algorithm_id):
        raise HTTPException(
            status_code=400,
            detail=f"invalid algorithm_id {url_algorithm_id!r}: must match [a-z0-9_]{{1,32}}",
        )
    if not algorithms.is_known(url_algorithm_id):
        if url_algorithm_id in algorithms.NON_RUNNABLE_IDS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"algorithm_id {url_algorithm_id!r} is a non-runnable "
                    "data source — cannot be invoked via this endpoint"
                ),
            )
        raise HTTPException(
            status_code=404,
            detail=f"unknown algorithm_id {url_algorithm_id!r}",
        )

    preset_name = body.get("preset_name")
    params = body.get("params")
    have_preset = isinstance(preset_name, str) and len(preset_name) > 0
    have_params = isinstance(params, dict)

    if have_preset and have_params:
        raise HTTPException(
            status_code=422,
            detail="preset_name and params are mutually exclusive",
        )
    if not have_preset and not have_params:
        raise HTTPException(
            status_code=422,
            detail="must supply preset_name or params",
        )

    if have_preset:
        snapshot = _snapshot_from_preset_name(state, preset_name)
        if snapshot.algorithm_id != url_algorithm_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"preset {preset_name!r} is for algorithm "
                    f"{snapshot.algorithm_id!r}, but URL specifies "
                    f"{url_algorithm_id!r}"
                ),
            )
        return snapshot

    entry = algorithms.get(url_algorithm_id)
    try:
        entry.detector.params_schema.model_validate(params)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"params failed schema for {url_algorithm_id!r}: {exc}",
        )
    return DetectionConfigSnapshotPayload(
        algorithm_id=url_algorithm_id,
        params=dict(params),
        preset_name=None,
    )


async def _dispatch_server_post(
    request: Request,
    session_id: str,
    snapshot: DetectionConfigSnapshotPayload,
    background_tasks: BackgroundTasks,
    return_to: str,
):
    """Shared tail used by both server-post endpoints: validate session
    id, gate on `processing.session_candidates`, queue
    `_run_server_detection` for every cam under the same snapshot.
    Caller supplies the snapshot and the whitelisted `return_to` for
    HTML form callers (see `_resolve_return_to`)."""
    from main import state, _wants_html, _run_server_detection
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse(return_to, status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    candidates = state.processing.session_candidates(session_id)
    if not candidates:
        if _wants_html(request):
            return RedirectResponse(return_to, status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    queued = state.processing.resume_processing(session_id)
    if not queued:
        if _wants_html(request):
            return RedirectResponse(return_to, status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    for clip_path, pitch in queued:
        background_tasks.add_task(
            _run_server_detection,
            clip_path,
            pitch,
            config_snapshot=snapshot,
        )
    if _wants_html(request):
        return RedirectResponse(return_to, status_code=303)
    return {
        "ok": True,
        "session_id": session_id,
        "queued": len(queued),
        "algorithm_id": snapshot.algorithm_id,
        "preset_name": snapshot.preset_name,
    }


@router.post("/sessions/{session_id}/runs/{algorithm_id}")
async def sessions_run_algorithm(
    request: Request,
    session_id: str,
    algorithm_id: str,
    background_tasks: BackgroundTasks,
):
    """Run `algorithm_id` against every archived MOV for this session.

    Body (JSON or form) XOR:
      - `preset_name: str` — load that preset from disk; preset's
        `algorithm_id` must match the URL or 422.
      - `params: dict` — ad-hoc one-off run, validated against the
        detector's `params_schema`; snapshot's `preset_name` is null.

    The resulting `SessionResult.config_used_by_algorithm[algorithm_id]`
    carries the exact `(algorithm_id, params, preset_name)` that
    produced this run. Re-running the same algorithm on the same
    session overwrites that bucket; running a different algorithm
    leaves prior algorithm buckets in place (multi-algorithm history
    is preserved at the dict layer)."""
    from main import state
    body = await _parse_request_body(request)
    snapshot = _snapshot_for_algorithm(state, algorithm_id, body)
    return_to = _resolve_return_to(body, session_id)
    return await _dispatch_server_post(
        request, session_id, snapshot, background_tasks, return_to
    )


@router.post("/sessions/{session_id}/run_server_post")
async def sessions_run_server_post(
    request: Request,
    session_id: str,
    background_tasks: BackgroundTasks,
):
    """[Deprecation alias] Prefer `POST /sessions/{sid}/runs/{algorithm_id}`.

    Kept for HTML form callers (events-row "Run srv" button + viewer
    "Rerun server" button), which submit `preset_name` only. The
    snapshot's `algorithm_id` is derived from the preset, then routed
    through the same `_dispatch_server_post` as the new endpoint.

    Rejects body fields other than `preset_name` (400) — historically
    the HTML forms also shipped an `algorithm_id` field that this
    handler silently ignored, letting the UI display algorithm X while
    the rerun executed preset Y's algorithm. No silent fallback: any
    `algorithm_id` in the body is a caller bug, route through the
    explicit `/sessions/{sid}/runs/{algorithm_id}` endpoint instead.
    """
    from main import state
    body = await _parse_request_body(request)
    if "algorithm_id" in body:
        raise HTTPException(
            status_code=400,
            detail=(
                "this alias does not accept 'algorithm_id'; the algorithm "
                "is derived from the preset. Use "
                "POST /sessions/{sid}/runs/{algorithm_id} if you need to "
                "pin an algorithm explicitly"
            ),
        )
    snapshot = _snapshot_from_preset_name(state, body.get("preset_name"))
    return_to = _resolve_return_to(body, session_id)
    return await _dispatch_server_post(
        request, session_id, snapshot, background_tasks, return_to
    )


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

    # Existence check uses the public `session_known` accessor (a session
    # is 'alive' iff it has a pitch entry, a result entry, or a live
    # pairing buffer — live-only WS sessions before persist_live_frames
    # flush only live in `_live_pairings`).
    if not state.session_known(session_id):
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


@router.post("/sessions/{session_id}/active_run")
async def sessions_active_run(request: Request, session_id: str):
    """Flip the active server_post pointer to an algorithm that has
    already been run on this session. No detection runs — pure pointer
    flip for the viewer's history dropdown.

    Body (JSON or form):
      - `algorithm_id` (required): the algorithm to mark active. Must
        already have at least one frame in some cam's
        `frames_by_algorithm` (the algorithm has been run on this
        session before). The live bucket id is rejected.
      - `return_to` (optional, HTML form only): viewer redirect after
        the flip, whitelisted to `/` or `/viewer/{session_id}`.

    Errors:
      - 422 invalid session_id slug / missing `algorithm_id`
      - 404 session not found
      - 422 `algorithm_id` has no frames in this session, or is the
        live bucket
    """
    from main import state, _wants_html, sse_hub
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")

    body = await _parse_request_body(request)
    algorithm_id = body.get("algorithm_id")
    if not isinstance(algorithm_id, str) or not algorithm_id:
        raise HTTPException(status_code=422, detail="missing 'algorithm_id'")
    return_to = _resolve_return_to(body, session_id)

    try:
        new_result = state.set_active_server_post_algorithm(
            session_id, algorithm_id,
        )
    except KeyError:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(
            status_code=404, detail=f"session {session_id} not found",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if new_result is None:
        raise HTTPException(
            status_code=404, detail=f"session {session_id} deleted mid-flip",
        )

    # `fit` ride-along so any dashboard / viewer SSE subscriber repaints
    # the scene with the new triangulation bucket. Same event the
    # `recompute` endpoint emits — viewer's `85_sse_fit.js` cause-switches
    # on `recompute` (returns early because the inline /recompute
    # response already patched the scene), so `active_run_switch` falls
    # through to the autorefresh / /results refetch path. Adding a new
    # `cause` here means checking that file before assuming listeners
    # pick it up.
    await sse_hub.broadcast(
        "fit",
        {
            "sid": session_id,
            "cause": "active_run_switch",
            "segments": [s.model_dump() for s in new_result.segments],
            "gap_threshold_m": new_result.gap_threshold_m,
        },
    )

    if _wants_html(request):
        return RedirectResponse(return_to, status_code=303)
    return {"ok": True, "active_algorithm_id": algorithm_id}
