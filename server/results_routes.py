from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from control_routes import wants_html
from pairing import scale_pitch_to_video_dims
from video import probe_dims

_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")
_VIDEO_FILENAME_RE = re.compile(
    r"^session_s_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}(_annotated)?\.(mov|mp4|m4v)$"
)


def find_clip_on_disk(state: Any, session_id: str, camera_id: str) -> Path | None:
    for path in state.video_dir.glob(f"session_{session_id}_{camera_id}.*"):
        if path.stem.endswith("_annotated"):
            continue
        return path
    return None


def scene_for_session(state: Any, session_id: str):
    from reconstruct import build_scene

    pitches = state.pitches_for_session(session_id)
    if not pitches:
        raise HTTPException(404, f"session {session_id} has no pitches")
    calibrations = state.calibrations()
    scaled_pitches = {}
    for cam, pitch in pitches.items():
        clip = find_clip_on_disk(state, session_id, cam)
        if clip is not None:
            actual_dims = probe_dims(clip)
            if actual_dims is not None:
                mw, mh = actual_dims
                if pitch.image_width_px != mw or pitch.image_height_px != mh:
                    pitch = pitch.model_copy(
                        update={"image_width_px": mw, "image_height_px": mh}
                    )
        scaled_pitches[cam] = scale_pitch_to_video_dims(
            pitch,
            (calibrations[cam].image_width_px, calibrations[cam].image_height_px)
            if cam in calibrations
            else None,
        )
    result = state.get(session_id)
    triangulated = result.points if result is not None else []
    triangulated_on_device = result.points_on_device if result is not None else []
    return build_scene(
        session_id,
        scaled_pitches,
        triangulated,
        triangulated_on_device=triangulated_on_device,
    )


def build_viewer_health(state: Any, session_id: str) -> dict[str, Any]:
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
                    if p.capture_telemetry is not None
                    else None
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
        "triangulated_count_on_device": (
            len(result.points_on_device) if result is not None else 0
        ),
        "error": result.error if result is not None else None,
        "duration_s": duration_s,
        "received_at": latest_mtime,
        "mode": mode,
    }


def videos_for_session(
    state: Any,
    session_id: str,
) -> list[tuple[str, str | None, float, float, dict[str, list]]]:
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

    out: list[tuple[str, str | None, float, float, dict[str, list]]] = []
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
        fps = (
            float(pitch.video_fps)
            if (pitch is not None and pitch.video_fps is not None)
            else 240.0
        )
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
                "t_rel_s": t_rel_od,
                "detected": detected_od,
                "px": px_od,
                "py": py_od,
            },
        }
        url = f"/videos/{name}" if name else None
        out.append((cam, url, offset, fps, frames_info))
    return out


def build_results_router(*, get_state: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    @router.post("/sessions/{session_id}/delete")
    async def sessions_delete(request: Request, session_id: str):
        state = get_state()
        if not _SESSION_ID_RE.match(session_id):
            if wants_html(request):
                return RedirectResponse("/", status_code=303)
            raise HTTPException(status_code=422, detail="invalid session_id")
        try:
            removed = state.delete_session(session_id)
        except RuntimeError as e:
            if wants_html(request):
                return RedirectResponse("/", status_code=303)
            raise HTTPException(status_code=409, detail=str(e))
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        if not removed:
            raise HTTPException(status_code=404, detail=f"session {session_id} not found")
        return {"ok": True, "session_id": session_id}

    @router.get("/results/latest")
    def results_latest():
        r = get_state().latest()
        if r is None:
            raise HTTPException(404, "no results yet")
        return r

    @router.get("/results/{session_id}")
    def results_for_session(session_id: str):
        r = get_state().get(session_id)
        if r is None:
            raise HTTPException(404, f"session {session_id} not found")
        return r

    @router.get("/reconstruction/{session_id}")
    def reconstruction(session_id: str) -> dict[str, Any]:
        return scene_for_session(get_state(), session_id).to_dict()

    @router.get("/viewer/{session_id}", response_class=HTMLResponse)
    def viewer(session_id: str) -> HTMLResponse:
        from render_scene import render_viewer_html

        state = get_state()
        scene = scene_for_session(state, session_id)
        videos_with_offsets = videos_for_session(state, session_id)
        health = build_viewer_health(state, session_id)
        return HTMLResponse(render_viewer_html(scene, videos_with_offsets, health))

    @router.get("/videos/{filename}")
    def serve_video(filename: str) -> FileResponse:
        state = get_state()
        if not _VIDEO_FILENAME_RE.match(filename):
            raise HTTPException(status_code=404, detail="not found")
        path = state.video_dir / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path)

    @router.get("/events")
    def events() -> list[dict[str, Any]]:
        return get_state().events()

    return router
