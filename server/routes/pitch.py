from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError

import session_results
from detection import HSVRange, ShapeGate
from pipeline import ProcessingCanceled
from schemas import (
    DetectionPath,
    HSVRangePayload,
    PitchPayload,
    SessionResult,
    ShapeGatePayload,
)
from video import probe_dims, probe_frame_count

router = APIRouter()
logger = logging.getLogger("ball_tracker")


def _stamp_detection_config(
    pitch: PitchPayload,
    *,
    hsv_range,
    shape_gate,
) -> None:
    """Freeze the detection-time config onto the pitch so reprocess can
    reproduce exactly which HSV / shape-gate basis was in effect when
    this pitch's candidates were scored. Mirrors the cd87995
    PairingTuning-on-SessionResult pattern. Idempotent — callers can
    stamp on every state.record() and the wire shape stays stable.

    Selector cost weights are no longer freezable: they're hardcoded
    `_W_ASPECT` / `_W_FILL` constants in `candidate_selector`, so a
    pitch's cost basis is fully determined by the (HSV, shape_gate)
    pair plus the constants."""
    pitch.hsv_range_used = HSVRangePayload(
        h_min=hsv_range.h_min, h_max=hsv_range.h_max,
        s_min=hsv_range.s_min, s_max=hsv_range.s_max,
        v_min=hsv_range.v_min, v_max=hsv_range.v_max,
    )
    pitch.shape_gate_used = ShapeGatePayload(
        aspect_min=shape_gate.aspect_min,
        fill_min=shape_gate.fill_min,
    )


def _summarize_result(result: SessionResult) -> dict[str, Any]:
    paired = result.camera_a_received and result.camera_b_received
    summary: dict[str, Any] = {
        "session_id": result.session_id,
        "paired": paired,
        "triangulated_points": len(result.triangulated),
        "error": result.error,
    }
    if result.points:
        residuals = [p.residual_m for p in result.points]
        ts = [p.t_rel_s for p in result.points]
        summary["mean_residual_m"] = float(np.mean(residuals))
        summary["max_residual_m"] = float(np.max(residuals))
        summary["duration_s"] = float(ts[-1] - ts[0])
    return summary


@router.post("/pitch")
async def pitch(
    request: Request,
    payload: str = Form(...),
    video: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest one armed-session upload as multipart/form-data.

    Required form fields:
      - `payload` — JSON-encoded `PitchPayload`. Carries session-level
        metadata including the legacy chirp `sync_id` provenance tag.

    Optional:
      - `video` — H.264 MOV/MP4 of the cycle. iOS records + uploads
        unconditionally post-PR61, so this is populated for every
        real-device upload. The server just archives it; server-side
        HSV detection runs only when the operator hits
        `POST /sessions/{sid}/run_server_post`. Test uploads that ship
        only `frames` (no MOV) stay accepted.

    A missing time-sync anchor only prevents stereo pairing/triangulation;
    monocular detections are still kept so the viewer can render rays.
    """
    import main as _main
    state = _main.state
    _MAX_PITCH_UPLOAD_BYTES = _main._MAX_PITCH_UPLOAD_BYTES

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > _MAX_PITCH_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="video too large")

    try:
        payload_obj = PitchPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    needs_calibration_fill = (
        payload_obj.intrinsics is None
        or payload_obj.homography is None
        or payload_obj.image_width_px is None
        or payload_obj.image_height_px is None
    )
    if needs_calibration_fill:
        cal_snap = state.calibrations().get(payload_obj.camera_id)
        if cal_snap is None:
            raise HTTPException(
                status_code=422,
                detail=f"no calibration on file for camera {payload_obj.camera_id!r}",
            )
        if payload_obj.intrinsics is None:
            payload_obj.intrinsics = cal_snap.intrinsics
        if payload_obj.homography is None:
            payload_obj.homography = list(cal_snap.homography)
        if payload_obj.image_width_px is None:
            payload_obj.image_width_px = cal_snap.image_width_px
        if payload_obj.image_height_px is None:
            payload_obj.image_height_px = cal_snap.image_height_px

    # Explicit branch instead of `normalize_paths(...) or paths_for_pitch(...)`:
    # the `or` form quietly fell through to inferred paths whenever the
    # client sent a non-empty list that happened to filter down to an
    # empty set after normalize (e.g. all entries were unrecognised).
    # That silent substitution masked schema bugs.
    normalized_paths = session_results.normalize_paths(payload_obj.paths)
    if normalized_paths:
        payload_paths = normalized_paths
    else:
        payload_paths = session_results.paths_for_pitch(state, payload_obj)
    payload_obj.paths = sorted(p.value for p in payload_paths)
    has_video = video is not None and (video.filename or video.size)
    has_frames = bool(payload_obj.frames_live) or bool(payload_obj.frames_server_post)
    if not has_video and not has_frames:
        raise HTTPException(
            status_code=422,
            detail="must supply either a `video` attachment or a "
                   "non-empty frames list in payload",
        )

    clip_info: dict[str, Any] | None = None
    clip_path: Path | None = None

    if has_video:
        data = await video.read()
        if len(data) > _MAX_PITCH_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="video too large")
        if not data:
            raise HTTPException(status_code=422, detail="video attachment is empty")
        ext = "mov"
        if video.filename:
            suffix = Path(video.filename).suffix.lstrip(".").lower()
            if suffix:
                ext = suffix
        clip_path = await asyncio.to_thread(
            state.save_clip,
            payload_obj.camera_id, payload_obj.session_id, data, ext,
        )
        clip_info = {"filename": clip_path.name, "bytes": len(data)}

        actual_dims = await asyncio.to_thread(probe_dims, clip_path)
        if actual_dims is not None:
            mw, mh = actual_dims
            if payload_obj.image_width_px != mw or payload_obj.image_height_px != mh:
                logger.info(
                    "reconciling image dims camera=%s session=%s payload=%sx%s mov=%dx%d",
                    payload_obj.camera_id, payload_obj.session_id,
                    payload_obj.image_width_px, payload_obj.image_height_px,
                    mw, mh,
                )
                payload_obj.image_width_px = mw
                payload_obj.image_height_px = mh

        payload_obj.frames_server_post = []

    # Freeze detection config onto the pitch BEFORE first persist. For
    # live frames the iPhone-side detector snapshot is unobservable from
    # here, so we stamp the values the live session was frozen to at
    # first ingest. When the dashboard-armed live-streaming path ran,
    # state.live_session_frozen_config returns the atomic (HSV, shape_gate)
    # pair stamped by ingest_live_frame. When no live frame ever streamed (test fixture
    # or server_post-only flow), it returns None and we fall back atomically
    # to the current state snapshot. The server_post path overwrites these
    # later in `_run_server_detection` with the snapshot it actually called
    # `detect_pitch` with.
    frozen = state.live_session_frozen_config(payload_obj.session_id)
    if frozen is not None:
        hsv_used, gate_used = frozen
    else:
        hsv_used = state.hsv_range()
        gate_used = state.shape_gate()
    _stamp_detection_config(
        payload_obj,
        hsv_range=hsv_used,
        shape_gate=gate_used,
    )

    result = await asyncio.to_thread(state.record, payload_obj)

    # Explicit source selection for log line. Old `or` fallback hid
    # which bucket the count came from — operator reading the log saw
    # `frames=240 ball=12` with no way to tell if those 12 were the
    # iOS live detector's count or server_post's, biasing all post-hoc
    # delta investigations.
    if payload_obj.frames_server_post:
        log_frames = payload_obj.frames_server_post
        log_source = "server_post"
    elif payload_obj.frames_live:
        log_frames = payload_obj.frames_live
        log_source = "live"
    else:
        log_frames = []
        log_source = "none"
    ball_frames = sum(1 for f in log_frames if f.ball_detected)
    logger.info(
        "pitch camera=%s session=%s clip=%s source=%s frames=%d ball=%d triangulated=%d%s paths=%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        f"{clip_info['bytes']}B" if clip_info else "none",
        log_source,
        len(log_frames),
        ball_frames,
        len(result.points),
        f" err={result.error}" if result.error else "",
        payload_obj.paths,
    )
    response: dict[str, Any] = {"ok": True, **_summarize_result(result)}
    response["clip"] = clip_info
    # Retained for wire-compat with older iOS builds; server no longer
    # auto-triggers server-post detection on upload. Always False.
    response["detection_pending"] = False
    return response


async def _run_server_detection(
    clip_path: Path,
    pitch: PitchPayload,
    *,
    hsv_range: "HSVRange",
    shape_gate: "ShapeGate",
) -> None:
    """Background task: decode the MOV, run HSV detection, annotate, then
    re-record the pitch so `result.points` (and the annotated MP4) land on
    disk. Runs after /pitch has already returned — the dashboard sees the
    session + on-device points immediately, and this task backfills the
    server-side trace 8-20 s later.

    The detection config pair (`hsv_range` / `shape_gate`) is resolved by
    the caller (`_enqueue_server_post`) from `state.hsv_range()` /
    `state.shape_gate()` — the dashboard's single source of truth — so
    this background task never re-reads `state` mid-run. The pair is
    captured at request time so a concurrent dashboard slider edit can't
    contaminate an already-queued reprocess.
    """
    import main as _main
    state = _main.state
    detect_pitch = _main.detect_pitch
    proc = state.processing
    sid = pitch.session_id
    cam = pitch.camera_id

    if not proc.start_server_post_job(sid, cam):
        logger.info(
            "background detection skipped session=%s cam=%s reason=not-runnable",
            sid, cam,
        )
        return
    # New run begins — wipe any stale error from the previous attempt so
    # /events doesn't keep showing a resolved failure.
    proc.clear_error(sid, cam)

    sse_hub = _main.sse_hub
    # Probe frame count from container metadata (no decode pass) so the
    # dashboard can show "13/240"-style progress. None falls back to
    # indeterminate "13 decoded".
    frames_total = await asyncio.to_thread(probe_frame_count, clip_path)
    # Capture the running loop so the to_thread worker (which has no
    # event loop of its own) can schedule SSE broadcasts back onto the
    # main loop via run_coroutine_threadsafe. Cross-thread broadcast is
    # best-effort: SSE queues are bounded (maxsize=1000) and progress
    # events are lossy by design, so a dropped event just delays the
    # next visible tick.
    loop = asyncio.get_running_loop()

    def on_progress(idx: int) -> None:
        # Throttle to every 30 frames. Server-side decode runs at
        # ~30 fps wall-clock, so this fires ≈ 1 Hz — fast enough for a
        # visibly moving bar, slow enough to not pressure the SSE pipe.
        # Skip idx=0 because the priming broadcast below already shipped
        # frames_done=0 with the same payload before detect_pitch ran.
        if idx == 0 or idx % 30 != 0:
            return
        fut = asyncio.run_coroutine_threadsafe(
            sse_hub.broadcast(
                "server_post_progress",
                {"sid": sid, "cam": cam,
                 "frames_done": idx, "frames_total": frames_total},
            ),
            loop,
        )
        # Consume any exception so asyncio doesn't print "Future
        # exception was never retrieved" on GC. Progress is lossy by
        # design — we don't care if a single emit failed.
        fut.add_done_callback(lambda f: f.exception())

    async def broadcast_done(reason: str, frames_done: int) -> None:
        # `reason ∈ {"ok", "canceled", "error"}` — the dashboard listener
        # always clears its progress entry on this event regardless of
        # reason; reason just drives optional UX (green flash on "ok",
        # silent dismiss otherwise).
        # Swallow any broadcast failure so the caller's `finish_server_post_job`
        # always runs — leaking the job state is worse than dropping a UI
        # event, since the dashboard's polling tick will eventually
        # reconcile the row state but a stuck job is stuck forever.
        try:
            await sse_hub.broadcast(
                "server_post_done",
                {"sid": sid, "cam": cam, "reason": reason,
                 "frames_done": frames_done, "frames_total": frames_total},
            )
        except Exception as exc:
            logger.warning(
                "broadcast_done failed sid=%s cam=%s reason=%s err=%s",
                sid, cam, reason, exc,
            )

    # Priming event so the row flips into "in progress" mode within
    # ~1 frame of the BackgroundTask actually starting, instead of
    # waiting for the first 30-frame milestone (~1 s wall).
    await sse_hub.broadcast(
        "server_post_progress",
        {"sid": sid, "cam": cam, "frames_done": 0, "frames_total": frames_total},
    )

    # The pair was resolved at request time by
    # `_resolve_detection_config` — never re-read from state here, since
    # the operator's choice (`preset:blue_ball`, `frozen`, ...) must be
    # honored regardless of any concurrent dashboard edit. The persisted
    # `*_used` stamp records exactly what `detect_pitch` ran with, so
    # later reprocess can reproduce this run from frozen snapshot alone.
    hsv_used = hsv_range
    gate_used = shape_gate
    logger.info(
        "background detection start session=%s cam=%s hsv=%r",
        sid, cam, hsv_used,
    )
    try:
        frames = await asyncio.to_thread(
            detect_pitch,
            clip_path,
            pitch.video_start_pts_s,
            hsv_range=hsv_used,
            should_cancel=lambda: proc.should_cancel_server_post_job(sid, cam),
            shape_gate=gate_used,
            progress=on_progress,
        )
    except ProcessingCanceled:
        await broadcast_done("canceled", 0)
        proc.finish_server_post_job(sid, cam, canceled=True)
        logger.info("background detection canceled session=%s cam=%s", sid, cam)
        return
    except Exception as exc:
        await broadcast_done("error", 0)
        proc.finish_server_post_job(sid, cam, canceled=False)
        proc.record_error(sid, cam, f"detect_pitch: {exc}")
        logger.warning(
            "background detect_pitch failed session=%s cam=%s err=%s",
            sid, cam, exc,
        )
        return

    if proc.should_cancel_server_post_job(sid, cam):
        await broadcast_done("canceled", len(frames))
        proc.finish_server_post_job(sid, cam, canceled=True)
        logger.info(
            "background detection discarded after cancel session=%s cam=%s",
            sid, cam,
        )
        return
    pitch.frames_server_post = frames
    # Stamp the wall-clock for this cam's just-completed run so the
    # SessionResult rebuild picks it up via max(A, B). Only set on
    # success — cancellation / errors above return before this line.
    pitch.server_post_ran_at = state.now()
    # Overwrite the live-side stamp with what server_post actually used
    # for this run — server_post is the authoritative cost basis once it
    # has run, since reprocess will read from `frames_server_post`.
    _stamp_detection_config(
        pitch,
        hsv_range=hsv_used,
        shape_gate=gate_used,
    )
    try:
        result = await asyncio.to_thread(state.record, pitch)
    except Exception as exc:
        await broadcast_done("error", len(frames))
        proc.finish_server_post_job(sid, cam, canceled=False)
        proc.record_error(sid, cam, f"record: {exc}")
        logger.warning(
            "background re-record failed session=%s cam=%s err=%s",
            sid, cam, exc,
        )
        return

    ball = sum(1 for f in frames if f.ball_detected)
    logger.info(
        "background detection complete session=%s cam=%s frames=%d ball=%d",
        sid, cam, len(frames), ball,
    )
    # `cause` lets the viewer's SSE handler skip refetch on recompute
    # (the inline /recompute response handler already patched the scene)
    # while still firing on cycle_end / server_post.
    # Wrapped like `broadcast_done` so a broadcast failure can't strand
    # the proc job state — leaking a stuck job is worse than dropping
    # one repaint event.
    try:
        await sse_hub.broadcast(
            "fit",
            {
                "sid": sid,
                "cause": "server_post",
                "segments": [s.model_dump() for s in result.segments],
                "cost_threshold": result.cost_threshold,
                "gap_threshold_m": result.gap_threshold_m,
            },
        )
    except Exception as exc:
        logger.warning(
            "fit broadcast failed sid=%s cam=%s err=%s",
            sid, cam, exc,
        )
    await broadcast_done("ok", len(frames))
    proc.finish_server_post_job(sid, cam, canceled=False)
