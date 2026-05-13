from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError

import session_results
from pipeline import ProcessingCanceled
from schemas import (
    DetectionConfigSnapshotPayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    SessionResult,
)
from video import probe_dims, probe_frame_count

router = APIRouter()
logger = logging.getLogger("ball_tracker")


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

        # Drop any stale server_post bucket so the upcoming detection
        # run owns a clean slot. Touching the active pointer's bucket
        # only — other algorithms' history (if any) is preserved.
        srv_alg = payload_obj.active_server_post_algorithm_id
        if srv_alg is not None:
            payload_obj.frames_by_algorithm.pop(srv_alg, None)
            payload_obj.active_server_post_algorithm_id = None

    # Stamp the live detection-config snapshot frozen at arm time.
    # `state.live_session_frozen_config` returns the atomic (HSV,
    # shape_gate, preset_name) tuple stamped by `ingest_live_frame`
    # when the dashboard-armed live-streaming path ran. If it returns
    # None, no live detector ever ran for this session — leave
    # `live_config_used = None` rather than fabricating a snapshot
    # from current disk config. A fabricated snapshot would make the
    # viewer CFG chip claim a live config that no detection actually
    # used (CLAUDE.md no-silent-fallback): the dashboard reader would
    # see "Live: tennis" on a server_post-only session, biasing
    # post-hoc delta investigations.
    #
    # The server_post path overwrites `server_post_config_used` later
    # in `_run_server_detection` with the snapshot it actually called
    # `run_detection` with — that path is unaffected by this guard.
    live_snap = state.live_session_frozen_config(payload_obj.session_id)
    if live_snap is not None:
        payload_obj.config_used_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID] = live_snap
    else:
        payload_obj.config_used_by_algorithm.pop(IOS_CAPTURE_TIME_ALGORITHM_ID, None)

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
    config_snapshot: DetectionConfigSnapshotPayload,
) -> None:
    """Background task: decode the MOV, run HSV detection, annotate, then
    re-record the pitch so `result.points` (and the annotated MP4) land on
    disk. Runs after /pitch has already returned.

    `config_snapshot` is resolved by the caller from the operator-
    chosen preset; this task never re-reads state mid-run. Stamped
    onto `pitch.server_post_config_used` and
    `SessionResult.server_post_config_used` after detection completes.
    """
    import algorithms
    import main as _main
    state = _main.state
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

    # Wall-clock throttle so the bar advances at a predictable cadence
    # regardless of decode-fps fluctuations. 0.1 s ≈ 10 Hz per cam, 20 Hz
    # combined — still within SSE pipe budget (queues maxsize=1000, JSON
    # payload <100 B). At 240 fps decode, each emit covers ~20 frames so
    # the counter text steps in small increments and reads as continuous
    # rather than the old 0.5 s / ~100-frame jumps. The old frame-mod-30
    # throttle silently slowed when the CPU did, which amplified wait
    # anxiety.
    _PROGRESS_THROTTLE_S = 0.1
    last_emit_ts = [0.0]

    def on_progress(idx: int) -> None:
        # Skip idx=0 because the priming broadcast below already shipped
        # frames_done=0 with the same payload before run_detection ran.
        if idx == 0:
            return
        now = time.monotonic()
        if now - last_emit_ts[0] < _PROGRESS_THROTTLE_S:
            return
        last_emit_ts[0] = now
        # `pct` is None when the container metadata probe failed to
        # report a frame count — the dashboard / viewer fall back to
        # indeterminate "N decoded" then. Otherwise clamped to 0..99 so
        # the bar fill width is renderable as `pct + '%'` without
        # client-side rounding. The `min(99, ...)` cap matters because
        # the decoder occasionally emits one more frame than
        # probe_frame_count estimated (the probe rounds duration*rate);
        # a raw division could land on 100+ and overflow the bar.
        pct = (
            min(99, int(idx / frames_total * 100))
            if frames_total is not None and frames_total > 0
            else None
        )
        # Persist the same numbers we're about to broadcast so a viewer
        # page rendered mid-decode (e.g. an operator opening /viewer
        # after pressing RERUN) can paint the bar with real progress
        # immediately, not the "waiting for first frame…" placeholder.
        proc.set_server_post_progress(
            sid, cam, done=idx, total=frames_total, pct=pct,
        )
        fut = asyncio.run_coroutine_threadsafe(
            sse_hub.broadcast(
                "server_post_progress",
                {"sid": sid, "cam": cam,
                 "frames_done": idx, "frames_total": frames_total,
                 "pct": pct},
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
    # Persist the priming snapshot too so a viewer arriving before the
    # first throttled emit (≤ 0.1 s window) still gets a non-empty seed
    # — without this the operator sees "waiting for first frame…" until
    # the first wall-clock tick clears.
    proc.set_server_post_progress(
        sid, cam, done=0, total=frames_total, pct=0,
    )
    await sse_hub.broadcast(
        "server_post_progress",
        {"sid": sid, "cam": cam, "frames_done": 0,
         "frames_total": frames_total, "pct": 0},
    )

    # Whatever the snapshot says is what runs — never re-read state here,
    # since the operator-chosen preset must survive concurrent dashboard
    # edits. `algorithms.run_detection` materialises the typed params and
    # dispatches to the registered detector for `algorithm_id`.
    logger.info(
        "background detection start session=%s cam=%s algo=%s preset=%s params=%s",
        sid, cam, config_snapshot.algorithm_id, config_snapshot.preset_name,
        config_snapshot.params,
    )
    try:
        frames = await asyncio.to_thread(
            algorithms.run_detection,
            config_snapshot.algorithm_id,
            clip_path,
            pitch.video_start_pts_s,
            config_snapshot.params,
            should_cancel=lambda: proc.should_cancel_server_post_job(sid, cam),
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
        proc.record_error(sid, cam, f"run_detection: {exc}")
        logger.warning(
            "background run_detection failed session=%s cam=%s err=%s",
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
    # Stamp the wall-clock for this cam's just-completed run so the
    # SessionResult rebuild picks it up via max(A, B). Only set on
    # success — cancellation / errors above return before this line.
    pitch.server_post_ran_at = state.now()
    # Atomically: update server_post snapshot + frames in both legacy
    # field and `frames_by_algorithm`. live_config_used is preserved
    # — it is the arm-time live identity and never reflects
    # server_post's run choice. Previous runs under a different
    # algorithm id keep their frames in `frames_by_algorithm[<old id>]`
    # so v11→v12 runs leave both algorithms persisted on disk.
    from detection_paths import stamp_server_post_run
    stamp_server_post_run(pitch, config_snapshot, frames)
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
    # Mirror the snapshot onto the SessionResult. Both cams of a session
    # run with the same snapshot (request body locks it), so last-writer-
    # wins is idempotent.
    result = await asyncio.to_thread(
        state.stamp_server_post_config, sid, config_snapshot
    )

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
