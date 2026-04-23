from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError

from pipeline import ProcessingCanceled, annotate_video
from schemas import (
    DetectionPath,
    PitchPayload,
    SessionResult,
)
from video import probe_dims

router = APIRouter()
logger = logging.getLogger("ball_tracker")


# Server-post detection timeout floor. Below this we always get a chance
# to fail fast on trivially small / bad clips even if `video_duration_s`
# can't be estimated. Chosen conservatively — a real 5 s cycle decodes
# in a few seconds on the dev machine, so 30 s covers ~10x overhead.
_SERVER_POST_TIMEOUT_FLOOR_S = 30.0

# Upper-bound fallback when no duration estimate is available. Used for
# the `wait_for` that wraps `detect_pitch` so a wedged PyAV decoder
# can't hang the background task forever.
_SERVER_POST_TIMEOUT_FALLBACK_S = 120.0


def _estimate_video_duration_s(pitch: PitchPayload) -> float | None:
    """Best-effort estimate of the MOV's wall duration from the pitch
    metadata alone. Returns None if we can't make a confident guess —
    the timeout wrapper will fall back to a flat ceiling in that case.

    Uses `frames_live` length / `video_fps` first (most likely to be
    populated because the live path streams throughout recording), then
    falls back to `frames_server_post` against the same fps."""
    fps = pitch.video_fps
    if not fps or fps <= 0:
        return None
    for candidate in (pitch.frames_live, pitch.frames_server_post):
        if candidate:
            return float(len(candidate)) / float(fps)
    return None


def _server_post_timeout_s(pitch: PitchPayload) -> float:
    """Return the `asyncio.wait_for` timeout to apply to the background
    detection pipeline. 2× the estimated duration, floored at
    `_SERVER_POST_TIMEOUT_FLOOR_S`; if we can't estimate, use a flat
    `_SERVER_POST_TIMEOUT_FALLBACK_S` ceiling."""
    duration = _estimate_video_duration_s(pitch)
    if duration is None:
        return _SERVER_POST_TIMEOUT_FALLBACK_S
    return max(_SERVER_POST_TIMEOUT_FLOOR_S, 2.0 * duration)


async def _broadcast_server_post_failed(
    session_id: str, camera_id: str, reason: str,
) -> None:
    """Fire a best-effort SSE broadcast for dashboards listening on the
    shared sse_hub. Swallows everything — broadcast is observability-only
    and must not propagate back into the background task's finally
    handler and mask the original failure."""
    try:
        import main as _main
        hub = getattr(_main, "sse_hub", None)
        if hub is None:
            return
        await hub.broadcast(
            "server_post_failed",
            {
                "session_id": session_id,
                "camera_id": camera_id,
                "reason": reason,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "broadcast server_post_failed suppressed session=%s cam=%s exc=%s",
            session_id, camera_id, exc,
        )


async def _record_server_post_failure(
    session_id: str, camera_id: str, reason: str,
) -> None:
    """Persist a visible abort reason on the SessionResult AND broadcast
    the failure event. Called from the background task's except / timeout
    branches so the dashboard's events view surfaces a red pill instead
    of silently logging + moving on."""
    import main as _main
    state = _main.state
    try:
        await asyncio.to_thread(
            state.record_server_post_abort, session_id, camera_id, reason
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "record_server_post_abort failed session=%s cam=%s exc=%s",
            session_id, camera_id, exc,
        )
    await _broadcast_server_post_failed(session_id, camera_id, reason)


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
        zs = [p.z_m for p in result.points]
        ts = [p.t_rel_s for p in result.points]
        summary["mean_residual_m"] = float(np.mean(residuals))
        summary["max_residual_m"] = float(np.max(residuals))
        summary["peak_z_m"] = float(max(zs))
        summary["duration_s"] = float(ts[-1] - ts[0])
    return summary


@router.post("/pitch")
async def pitch(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: str = Form(...),
    video: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest one armed-session upload as multipart/form-data.

    Required form fields:
      - `payload` — JSON-encoded `PitchPayload`. Carries session-level
        metadata including the legacy chirp `sync_id` provenance tag.

    Optional:
      - `video` — H.264 MOV/MP4 of the cycle. Required when the session
        paths include `server_post`; server decodes and runs HSV detection.
        Omitted for live-only sessions.

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

    payload_paths = state._normalize_paths(payload_obj.paths) or state._paths_for_pitch(payload_obj)
    payload_obj.paths = sorted(p.value for p in payload_paths)
    has_video = video is not None and (video.filename or video.size)
    has_frames = (
        bool(payload_obj.frames_live)
        or bool(payload_obj.frames_server_post)
    )
    if not has_video and not has_frames:
        raise HTTPException(
            status_code=422,
            detail="must supply either a `video` attachment or a "
                   "non-empty frames list in payload",
        )

    clip_info: dict[str, Any] | None = None
    clip_path: Path | None = None
    detection_pending = False

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

        # Upload carries video only — server-side detection runs in the
        # background and overwrites `frames_server_post` on re-record.
        payload_obj.frames_server_post = []
        if DetectionPath.server_post in payload_paths:
            detection_pending = True
    result = await asyncio.to_thread(state.record, payload_obj)

    if detection_pending and clip_path is not None:
        state.mark_server_post_queued(payload_obj.session_id, payload_obj.camera_id)
        background_tasks.add_task(_run_server_detection, clip_path, payload_obj)

    ball_frames = sum(
        1
        for f in (
            payload_obj.frames_server_post
            or payload_obj.frames_live
        )
        if f.ball_detected
    )
    logger.info(
        "pitch camera=%s session=%s clip=%s frames=%d ball=%d detected_on=%s triangulated=%d%s paths=%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        f"{clip_info['bytes']}B" if clip_info else "none",
        len(payload_obj.frames_server_post or payload_obj.frames_live),
        ball_frames,
        "server-pending" if detection_pending else ("live" if payload_obj.frames_live else "skipped"),
        len(result.points),
        f" err={result.error}" if result.error else "",
        payload_obj.paths,
    )
    response: dict[str, Any] = {"ok": True, **_summarize_result(result)}
    response["clip"] = clip_info
    response["detection_pending"] = detection_pending
    return response


async def _run_server_detection(clip_path: Path, pitch: PitchPayload) -> None:
    """Background task: decode the MOV, run HSV detection, annotate, then
    re-record the pitch so `result.points` (and the annotated MP4) land on
    disk. Runs after /pitch has already returned — the dashboard sees the
    session + on-device points immediately, and this task backfills the
    server-side trace 8-20 s later.

    Reliability guarantees:

    - Every failure branch writes an abort reason onto the `SessionResult`
      via `state.record_server_post_abort` and broadcasts a
      `server_post_failed` event (see `_record_server_post_failure`) so
      the dashboard's `/events` pill flips red instead of silently logging.
    - `detect_pitch` runs under `asyncio.wait_for` — a wedged PyAV decoder
      can't hang the background task forever; a TimeoutError is treated
      as an ordinary failure (abort reason + finish_server_post_job).
    - A try / finally wraps the whole body with a sentinel so the job
      state can't stay stuck in `queued` even if `finish_server_post_job`
      itself raises partway through an except branch."""
    import main as _main
    state = _main.state
    detect_pitch = _main.detect_pitch

    if not state.start_server_post_job(pitch.session_id, pitch.camera_id):
        logger.info(
            "background detection skipped session=%s cam=%s reason=not-runnable",
            pitch.session_id, pitch.camera_id,
        )
        return

    # Sentinel so a raise inside finish_server_post_job can't leave the
    # job state stuck. The finally block reads `finished` last and
    # re-issues `finish_server_post_job(canceled=False)` if no branch
    # reached it cleanly.
    finished = False
    canceled_final = False

    def _finish(canceled: bool) -> None:
        nonlocal finished, canceled_final
        if finished:
            return
        finished = True
        canceled_final = canceled
        state.finish_server_post_job(
            pitch.session_id, pitch.camera_id, canceled=canceled
        )

    try:
        timeout_s = _server_post_timeout_s(pitch)
        try:
            frames = await asyncio.wait_for(
                asyncio.to_thread(
                    detect_pitch,
                    clip_path,
                    pitch.video_start_pts_s,
                    hsv_range=state.hsv_range(),
                    should_cancel=lambda: state.should_cancel_server_post_job(pitch.session_id, pitch.camera_id),
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            # Flip the per-job `should_cancel` flag BEFORE we finish,
            # so the PyAV decode thread (which we don't own and can't
            # interrupt) sees it on its next per-frame check inside
            # `detect_pitch` and bails out cooperatively. Without this
            # the thread keeps spinning until the MOV runs out even
            # though FastAPI has already given up on awaiting it.
            state.request_server_post_cancel(pitch.session_id, pitch.camera_id)
            reason = f"detect_pitch timeout after {timeout_s:.1f}s"
            await _record_server_post_failure(
                pitch.session_id, pitch.camera_id, reason,
            )
            logger.warning(
                "background detect_pitch timed out session=%s cam=%s timeout=%.1fs",
                pitch.session_id, pitch.camera_id, timeout_s,
            )
            _finish(canceled=True)
            return
        except ProcessingCanceled:
            logger.info(
                "background detection canceled session=%s cam=%s",
                pitch.session_id, pitch.camera_id,
            )
            _finish(canceled=True)
            return
        except Exception as exc:
            reason = f"detect_pitch: {type(exc).__name__}: {exc}"
            await _record_server_post_failure(
                pitch.session_id, pitch.camera_id, reason,
            )
            logger.warning(
                "background detect_pitch failed session=%s cam=%s err=%s",
                pitch.session_id, pitch.camera_id, exc,
            )
            _finish(canceled=False)
            return

        if state.should_cancel_server_post_job(pitch.session_id, pitch.camera_id):
            logger.info(
                "background detection discarded after cancel session=%s cam=%s",
                pitch.session_id, pitch.camera_id,
            )
            _finish(canceled=True)
            return

        pitch.frames_server_post = frames
        try:
            await asyncio.to_thread(state.record, pitch)
        except Exception as exc:
            reason = f"record: {type(exc).__name__}: {exc}"
            await _record_server_post_failure(
                pitch.session_id, pitch.camera_id, reason,
            )
            logger.warning(
                "background re-record failed session=%s cam=%s err=%s",
                pitch.session_id, pitch.camera_id, exc,
            )
            _finish(canceled=False)
            return

        annotated_path = clip_path.with_stem(clip_path.stem + "_annotated")
        try:
            await asyncio.to_thread(
                annotate_video,
                clip_path,
                annotated_path,
                frames,
                should_cancel=lambda: state.should_cancel_server_post_job(pitch.session_id, pitch.camera_id),
            )
        except ProcessingCanceled:
            logger.info(
                "background annotation canceled session=%s cam=%s",
                pitch.session_id, pitch.camera_id,
            )
            if annotated_path.exists():
                try:
                    annotated_path.unlink()
                except OSError:
                    pass
            _finish(canceled=True)
            return
        except Exception as exc:
            reason = f"annotate_video: {type(exc).__name__}: {exc}"
            await _record_server_post_failure(
                pitch.session_id, pitch.camera_id, reason,
            )
            logger.warning(
                "annotate_video failed session=%s cam=%s err=%s",
                pitch.session_id, pitch.camera_id, exc,
            )
            if annotated_path.exists():
                try:
                    annotated_path.unlink()
                except OSError:
                    pass
            _finish(canceled=False)
            return

        ball = sum(1 for f in frames if f.ball_detected)
        logger.info(
            "background detection complete session=%s cam=%s frames=%d ball=%d",
            pitch.session_id, pitch.camera_id, len(frames), ball,
        )
        _finish(canceled=False)
    finally:
        # Belt-and-braces — every explicit branch above already calls
        # `_finish`, so this only matters if a branch raised before
        # calling it (e.g. `_record_server_post_failure` itself blowing
        # up during an abort broadcast). Without this, the job status
        # would stay in "queued" forever, and the dashboard "running"
        # spinner would lie. `_finish` is idempotent so a redundant call
        # is harmless.
        if not finished:
            try:
                state.finish_server_post_job(
                    pitch.session_id, pitch.camera_id, canceled=False,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(
                    "finish_server_post_job failed in finally session=%s cam=%s exc=%s",
                    pitch.session_id, pitch.camera_id, exc,
                )
