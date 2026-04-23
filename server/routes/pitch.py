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
    PitchAnalysisPayload,
    PitchPayload,
    SessionResult,
)
from video import probe_dims

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
        metadata including the legacy chirp `sync_id` provenance tag; in
        mode-two (`on_device`) also carries the per-frame
        `frames: [FramePayload]` list produced by the iPhone's own
        HSV+MOG2 detector.

    Optional:
      - `video` — H.264 MOV/MP4 of the cycle. Required in mode-one (server
        decodes it and runs HSV detection). Omitted in mode-two — server
        trusts the iPhone's detection output and only pairs + triangulates.

    Requests with neither a video nor a non-empty `frames` list return 422.
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
        bool(payload_obj.frames)
        or bool(payload_obj.frames_live)
        or bool(payload_obj.frames_ios_post)
        or bool(payload_obj.frames_server_post)
        or bool(payload_obj.frames_on_device)
    )
    if not has_video and not has_frames:
        raise HTTPException(
            status_code=422,
            detail="must supply either `video` (mode-one / dual) or a "
                   "non-empty `frames` / `frames_on_device` list in payload",
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

        payload_obj.frames = []
        payload_obj.frames_server_post = []
        if DetectionPath.server_post in payload_paths:
            detection_pending = True
    else:
        if payload_obj.frames and not payload_obj.frames_ios_post and DetectionPath.server_post not in payload_paths:
            payload_obj.frames_ios_post = list(payload_obj.frames)

    result = await asyncio.to_thread(state.record, payload_obj)

    if detection_pending and clip_path is not None:
        state.mark_server_post_queued(payload_obj.session_id, payload_obj.camera_id)
        background_tasks.add_task(_run_server_detection, clip_path, payload_obj)

    ball_frames = sum(
        1
        for f in (
            payload_obj.frames_server_post
            or payload_obj.frames_ios_post
            or payload_obj.frames_live
            or payload_obj.frames
        )
        if f.ball_detected
    )
    logger.info(
        "pitch camera=%s session=%s clip=%s frames=%d ball=%d detected_on=%s triangulated=%d%s%s paths=%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        f"{clip_info['bytes']}B" if clip_info else "none",
        len(payload_obj.frames_server_post or payload_obj.frames_ios_post or payload_obj.frames_live or payload_obj.frames),
        ball_frames,
        "server-pending" if detection_pending else ("device" if payload_obj.frames else "skipped"),
        len(result.points),
        f" on_device={len(result.points_on_device)}" if result.points_on_device else "",
        f" err={result.error}" if result.error else "",
        payload_obj.paths,
    )
    if result.points_on_device:
        zs = [p.z_m for p in result.points_on_device]
        logger.info(
            "  session %s (on_device) → %d pts, duration %.2fs, peak z = %.2fm",
            result.session_id,
            len(result.points_on_device),
            result.points_on_device[-1].t_rel_s - result.points_on_device[0].t_rel_s,
            max(zs),
        )
    response: dict[str, Any] = {"ok": True, **_summarize_result(result)}
    response["clip"] = clip_info
    response["detection_pending"] = detection_pending
    return response


@router.post("/pitch_analysis")
async def pitch_analysis(payload: PitchAnalysisPayload) -> dict[str, Any]:
    """Attach a late on-device post-pass analysis to an already-recorded pitch.

    This is the PR61 second leg: raw capture arrives first, then iOS decodes
    its finalized local MOV and uploads the authoritative on-device frame list
    later. Dashboard/viewer state updates immediately once the merge lands."""
    import main as _main
    state = _main.state

    if not payload.frames_on_device:
        raise HTTPException(
            status_code=422,
            detail="frames_on_device must be non-empty",
        )
    try:
        result = await asyncio.to_thread(state.attach_on_device_analysis, payload)
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail="base pitch not found for analysis upload",
        )

    logger.info(
        "pitch_analysis camera=%s session=%s frames=%d detected=%d triangulated_on_device=%d%s",
        payload.camera_id,
        payload.session_id,
        len(payload.frames_on_device),
        sum(1 for f in payload.frames_on_device if f.ball_detected),
        len(result.points_on_device),
        f" err={result.error_on_device}" if result.error_on_device else "",
    )
    return {
        "ok": True,
        "session_id": payload.session_id,
        "camera_id": payload.camera_id,
        "frames_on_device": len(payload.frames_on_device),
        "triangulated_on_device": len(result.points_on_device),
        "error_on_device": result.error_on_device,
    }


async def _run_server_detection(clip_path: Path, pitch: PitchPayload) -> None:
    """Background task: decode the MOV, run HSV detection, annotate, then
    re-record the pitch so `result.points` (and the annotated MP4) land on
    disk. Runs after /pitch has already returned — the dashboard sees the
    session + on-device points immediately, and this task backfills the
    server-side trace 8-20 s later."""
    import main as _main
    state = _main.state
    detect_pitch = _main.detect_pitch

    if not state.start_server_post_job(pitch.session_id, pitch.camera_id):
        logger.info(
            "background detection skipped session=%s cam=%s reason=not-runnable",
            pitch.session_id, pitch.camera_id,
        )
        return
    try:
        frames = await asyncio.to_thread(
            detect_pitch,
            clip_path,
            pitch.video_start_pts_s,
            should_cancel=lambda: state.should_cancel_server_post_job(pitch.session_id, pitch.camera_id),
        )
    except ProcessingCanceled:
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=True)
        logger.info(
            "background detection canceled session=%s cam=%s",
            pitch.session_id, pitch.camera_id,
        )
        return
    except Exception as exc:
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=False)
        logger.warning(
            "background detect_pitch failed session=%s cam=%s err=%s",
            pitch.session_id, pitch.camera_id, exc,
        )
        return

    if state.should_cancel_server_post_job(pitch.session_id, pitch.camera_id):
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=True)
        logger.info(
            "background detection discarded after cancel session=%s cam=%s",
            pitch.session_id, pitch.camera_id,
        )
        return
    pitch.frames = frames
    pitch.frames_server_post = frames
    try:
        await asyncio.to_thread(state.record, pitch)
    except Exception as exc:
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=False)
        logger.warning(
            "background re-record failed session=%s cam=%s err=%s",
            pitch.session_id, pitch.camera_id, exc,
        )
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
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=True)
        logger.info(
            "background annotation canceled session=%s cam=%s",
            pitch.session_id, pitch.camera_id,
        )
        if annotated_path.exists():
            try:
                annotated_path.unlink()
            except OSError:
                pass
        return
    except Exception as exc:
        state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=False)
        logger.warning(
            "annotate_video failed session=%s cam=%s err=%s",
            pitch.session_id, pitch.camera_id, exc,
        )
        if annotated_path.exists():
            try:
                annotated_path.unlink()
            except OSError:
                pass
    ball = sum(1 for f in frames if f.ball_detected)
    logger.info(
        "background detection complete session=%s cam=%s frames=%d ball=%d",
        pitch.session_id, pitch.camera_id, len(frames), ball,
    )
    state.finish_server_post_job(pitch.session_id, pitch.camera_id, canceled=False)
