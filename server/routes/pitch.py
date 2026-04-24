from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
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

    payload_paths = state._normalize_paths(payload_obj.paths) or state._paths_for_pitch(payload_obj)
    payload_obj.paths = sorted(p.value for p in payload_paths)
    has_video = video is not None and (video.filename or video.size)
    has_frames = (
        bool(payload_obj.frames)
        or bool(payload_obj.frames_live)
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
    result = await asyncio.to_thread(state.record, payload_obj)

    ball_frames = sum(
        1
        for f in (
            payload_obj.frames_server_post
            or payload_obj.frames_live
            or payload_obj.frames
        )
        if f.ball_detected
    )
    logger.info(
        "pitch camera=%s session=%s clip=%s frames=%d ball=%d detected_on=%s triangulated=%d%s paths=%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        f"{clip_info['bytes']}B" if clip_info else "none",
        len(payload_obj.frames_server_post or payload_obj.frames_live or payload_obj.frames),
        ball_frames,
        "live" if payload_obj.frames_live else "skipped",
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


async def _run_server_detection(clip_path: Path, pitch: PitchPayload) -> None:
    """Background task: decode the MOV, run HSV detection, annotate, then
    re-record the pitch so `result.points` (and the annotated MP4) land on
    disk. Runs after /pitch has already returned — the dashboard sees the
    session + on-device points immediately, and this task backfills the
    server-side trace 8-20 s later."""
    import main as _main
    state = _main.state
    detect_pitch = _main.detect_pitch
    proc = state._processing
    sid = pitch.session_id
    cam = pitch.camera_id

    expected_radius_px: float | None = None
    snap = state.calibrations().get(cam)
    if snap is not None and snap.homography is not None:
        try:
            from geometry_priors import expected_ball_radius_px
            expected_radius_px = expected_ball_radius_px(
                fx=snap.intrinsics.fx,
                fy=snap.intrinsics.fy,
                cx=snap.intrinsics.cx,
                cy=snap.intrinsics.cy,
                homography_row_major=snap.homography,
            )
        except Exception as exc:
            logger.info(
                "radius prior unavailable session=%s cam=%s err=%s",
                sid, cam, exc,
            )
            expected_radius_px = None

    if not proc.start_server_post_job(sid, cam):
        logger.info(
            "background detection skipped session=%s cam=%s reason=not-runnable",
            sid, cam,
        )
        return
    # New run begins — wipe any stale error from the previous attempt so
    # /events doesn't keep showing a resolved failure.
    proc.clear_error(sid, cam)
    try:
        frames = await asyncio.to_thread(
            detect_pitch,
            clip_path,
            pitch.video_start_pts_s,
            hsv_range=state.hsv_range(),
            should_cancel=lambda: proc.should_cancel_server_post_job(sid, cam),
            expected_radius_px=expected_radius_px,
            enable_bg_subtraction=state.detection_bg_subtraction_enabled(),
        )
    except ProcessingCanceled:
        proc.finish_server_post_job(sid, cam, canceled=True)
        logger.info("background detection canceled session=%s cam=%s", sid, cam)
        return
    except Exception as exc:
        proc.finish_server_post_job(sid, cam, canceled=False)
        proc.record_error(sid, cam, f"detect_pitch: {exc}")
        logger.warning(
            "background detect_pitch failed session=%s cam=%s err=%s",
            sid, cam, exc,
        )
        return

    if proc.should_cancel_server_post_job(sid, cam):
        proc.finish_server_post_job(sid, cam, canceled=True)
        logger.info(
            "background detection discarded after cancel session=%s cam=%s",
            sid, cam,
        )
        return
    pitch.frames = frames
    pitch.frames_server_post = frames
    try:
        await asyncio.to_thread(state.record, pitch)
    except Exception as exc:
        proc.finish_server_post_job(sid, cam, canceled=False)
        proc.record_error(sid, cam, f"record: {exc}")
        logger.warning(
            "background re-record failed session=%s cam=%s err=%s",
            sid, cam, exc,
        )
        return

    annotated_path = clip_path.with_stem(clip_path.stem + "_annotated")
    try:
        await asyncio.to_thread(
            annotate_video,
            clip_path,
            annotated_path,
            frames,
            should_cancel=lambda: proc.should_cancel_server_post_job(sid, cam),
        )
    except ProcessingCanceled:
        proc.finish_server_post_job(sid, cam, canceled=True)
        logger.info("background annotation canceled session=%s cam=%s", sid, cam)
        if annotated_path.exists():
            try:
                annotated_path.unlink()
            except OSError:
                pass
        return
    except Exception as exc:
        proc.finish_server_post_job(sid, cam, canceled=False)
        proc.record_error(sid, cam, f"annotate: {exc}")
        logger.warning(
            "annotate_video failed session=%s cam=%s err=%s",
            sid, cam, exc,
        )
        if annotated_path.exists():
            try:
                annotated_path.unlink()
            except OSError:
                pass
    ball = sum(1 for f in frames if f.ball_detected)
    logger.info(
        "background detection complete session=%s cam=%s frames=%d ball=%d",
        sid, cam, len(frames), ball,
    )
    proc.finish_server_post_job(sid, cam, canceled=False)
