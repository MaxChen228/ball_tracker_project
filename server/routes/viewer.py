from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from schemas import SessionResult

router = APIRouter()

_VIDEO_FILENAME_RE = re.compile(
    r"^session_s_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}(_annotated)?\.(mov|mp4|m4v)$"
)


def _find_clip_on_disk(session_id: str, camera_id: str) -> Path | None:
    from main import state
    for path in state.video_dir.glob(f"session_{session_id}_{camera_id}.*"):
        if path.stem.endswith("_annotated"):
            continue
        return path
    return None


def _scene_for_session(session_id: str):
    from main import state
    from reconstruct import build_scene
    from pairing import scale_pitch_to_video_dims
    from video import probe_dims
    pitches = state.pitches_for_session(session_id)
    if not pitches:
        raise HTTPException(404, f"session {session_id} has no pitches")
    calibrations = state.calibrations()
    scaled_pitches = {}
    for cam, pitch in pitches.items():
        clip = _find_clip_on_disk(session_id, cam)
        if clip is not None:
            actual_dims = probe_dims(clip)
            if actual_dims is not None:
                mw, mh = actual_dims
                if pitch.image_width_px != mw or pitch.image_height_px != mh:
                    pitch = pitch.model_copy(update={"image_width_px": mw, "image_height_px": mh})
        scaled_pitches[cam] = scale_pitch_to_video_dims(
            pitch,
            (calibrations[cam].image_width_px, calibrations[cam].image_height_px)
            if cam in calibrations else None,
        )
    result = state.get(session_id)
    triangulated = result.points if result is not None else []
    triangulated_on_device = result.points_on_device if result is not None else []
    return build_scene(session_id, scaled_pitches, triangulated, triangulated_on_device=triangulated_on_device)


def _build_viewer_health(session_id: str) -> dict[str, Any]:
    from main import state
    pitches = state.pitches_for_session(session_id)
    result = state.get(session_id)
    cams: dict[str, dict[str, Any]] = {}
    for cam_id in ("A", "B"):
        p = pitches.get(cam_id)
        if p is None:
            cams[cam_id] = {
                "received": False,
                "calibrated": False,
                "time_synced": False,
                "n_frames": 0,
                "n_detected": 0,
                "capture_telemetry": None,
            }
        else:
            cams[cam_id] = {
                "received": True,
                "calibrated": p.intrinsics is not None and p.homography is not None,
                "time_synced": p.sync_anchor_timestamp_s is not None,
                "n_frames": len(p.frames),
                "n_detected": sum(1 for f in p.frames if f.ball_detected),
                "capture_telemetry": (
                    p.capture_telemetry.model_dump(mode="json")
                    if p.capture_telemetry is not None else None
                ),
            }
    duration_s: float | None = None
    if result is not None and result.points:
        ts = [p.t_rel_s for p in result.points]
        duration_s = float(max(ts) - min(ts))
    else:
        per_pitch_spans: list[float] = []
        for p in pitches.values():
            if p.sync_anchor_timestamp_s is None or not p.frames:
                continue
            rels = [f.timestamp_s - p.sync_anchor_timestamp_s for f in p.frames]
            per_pitch_spans.append(max(rels) - min(rels))
        if per_pitch_spans:
            duration_s = float(max(per_pitch_spans))
    latest_mtime: float | None = None
    for cam_id in pitches:
        try:
            mtime = state._pitch_path(cam_id, session_id).stat().st_mtime
        except (FileNotFoundError, OSError):
            continue
        if latest_mtime is None or mtime > latest_mtime:
            latest_mtime = mtime
    has_any_video = any(state.video_dir.glob(f"session_{session_id}_*"))
    has_any_on_device_frames = any(bool(p.frames_on_device) for p in pitches.values())
    if has_any_video and has_any_on_device_frames:
        mode = "dual"
    elif has_any_video:
        mode = "camera_only"
    else:
        mode = "on_device"
    return {
        "session_id": session_id,
        "cameras": cams,
        "triangulated_count": len(result.points) if result is not None else 0,
        "triangulated_count_on_device": (len(result.points_on_device) if result is not None else 0),
        "error": result.error if result is not None else None,
        "duration_s": duration_s,
        "received_at": latest_mtime,
        "mode": mode,
    }


def _videos_for_session(
    session_id: str,
) -> list[tuple[str, str, float, float, dict[str, list]]]:
    from main import state
    prefix = f"session_{session_id}_"
    pitches = state.pitches_for_session(session_id)
    best: dict[str, str] = {}
    for path in sorted(state.video_dir.glob(f"{prefix}*")):
        name = path.name
        if not _VIDEO_FILENAME_RE.match(name):
            continue
        stem = name.rsplit(".", 1)[0]
        is_annotated = stem.endswith("_annotated")
        cam = stem[len(prefix):]
        if is_annotated:
            cam = cam[: -len("_annotated")]
        if cam not in best or (is_annotated and "_annotated" not in best[cam]):
            best[cam] = name
    out: list[tuple[str, str, float, float, dict[str, list]]] = []
    all_cams = sorted(
        set(best)
        | {c for c in pitches if pitches[c].frames}
        | {c for c in pitches if pitches[c].frames_on_device}
    )
    for cam in all_cams:
        name = best.get(cam)
        pitch = pitches.get(cam)
        if pitch is None or pitch.sync_anchor_timestamp_s is None:
            offset = 0.0
        else:
            offset = float(pitch.video_start_pts_s - pitch.sync_anchor_timestamp_s)
        fps = float(pitch.video_fps) if (pitch is not None and pitch.video_fps is not None) else 240.0
        anchor = pitch.sync_anchor_timestamp_s if pitch is not None else None
        if pitch is not None and anchor is not None:
            t_rel = [float(f.timestamp_s - anchor) for f in pitch.frames]
            detected = [bool(f.ball_detected) for f in pitch.frames]
            px = [float(f.px) if f.px is not None else None for f in pitch.frames]
            py = [float(f.py) if f.py is not None else None for f in pitch.frames]
            t_rel_od = [float(f.timestamp_s - anchor) for f in pitch.frames_on_device]
            detected_od = [bool(f.ball_detected) for f in pitch.frames_on_device]
            px_od = [float(f.px) if f.px is not None else None for f in pitch.frames_on_device]
            py_od = [float(f.py) if f.py is not None else None for f in pitch.frames_on_device]
        else:
            t_rel = detected = px = py = []
            t_rel_od = detected_od = px_od = py_od = []
        frames_info = {
            "t_rel_s": t_rel,
            "detected": detected,
            "px": px,
            "py": py,
            "on_device": {
                "t_rel_s": t_rel_od, "detected": detected_od,
                "px": px_od, "py": py_od,
            },
        }
        url = f"/videos/{name}" if name else None
        out.append((cam, url, offset, fps, frames_info))
    return out


@router.get("/results/latest")
def results_latest() -> SessionResult:
    from main import state
    r = state.latest()
    if r is None:
        raise HTTPException(404, "no results yet")
    return r


@router.get("/results/{session_id}")
def results_for_session(session_id: str) -> SessionResult:
    from main import state
    r = state.get(session_id)
    if r is None:
        raise HTTPException(404, f"session {session_id} not found")
    return r


@router.get("/reconstruction/{session_id}")
def reconstruction(session_id: str) -> dict[str, Any]:
    scene = _scene_for_session(session_id)
    return scene.to_dict()


@router.get("/viewer/{session_id}", response_class=HTMLResponse)
def viewer(session_id: str) -> HTMLResponse:
    from render_scene import render_viewer_html
    scene = _scene_for_session(session_id)
    videos_with_offsets = _videos_for_session(session_id)
    health = _build_viewer_health(session_id)
    return HTMLResponse(render_viewer_html(scene, videos_with_offsets, health))


@router.get("/videos/{filename}")
def serve_video(filename: str) -> FileResponse:
    from main import state
    if not _VIDEO_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="not found")
    path = state.video_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@router.get("/events")
def events(bucket: str = "active") -> list[dict[str, Any]]:
    from main import state
    if bucket not in {"active", "trash"}:
        raise HTTPException(status_code=422, detail="bucket must be 'active' or 'trash'")
    return state.events(bucket=bucket)
