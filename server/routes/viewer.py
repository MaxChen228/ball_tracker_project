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
    triangulated_by_path = result.triangulated_by_path if result is not None else {}
    return build_scene(
        session_id,
        scaled_pitches,
        triangulated,
        triangulated_by_path,
        session_result=result,
    )


def _build_viewer_health(session_id: str) -> dict[str, Any]:
    from main import state
    pitches = state.pitches_for_session(session_id)
    result = state.get(session_id)
    def _effective_fps(frames) -> float | None:
        if not frames or len(frames) < 2:
            return None
        ts = [f.timestamp_s for f in frames]
        span = max(ts) - min(ts)
        if span <= 0:
            return None
        return (len(frames) - 1) / span

    cams: dict[str, dict[str, Any]] = {}
    _EMPTY_COUNTS = {
        "live": {"total": 0, "detected": 0, "fps": None},
        "server_post": {"total": 0, "detected": 0, "fps": None},
    }
    for cam_id in ("A", "B"):
        p = pitches.get(cam_id)
        if p is None:
            cams[cam_id] = {
                "received": False,
                "calibrated": False,
                "time_synced": False,
                "counts_by_path": {k: dict(v) for k, v in _EMPTY_COUNTS.items()},
                "n_frames": 0,
                "n_detected": 0,
                "capture_telemetry": None,
            }
        else:
            counts = {
                "live": {
                    "total": len(p.frames_live or []),
                    "detected": sum(1 for f in (p.frames_live or []) if f.ball_detected),
                    "fps": _effective_fps(p.frames_live or []),
                },
                "server_post": {
                    "total": len(p.frames),
                    "detected": sum(1 for f in p.frames if f.ball_detected),
                    "fps": _effective_fps(p.frames),
                },
            }
            cams[cam_id] = {
                "received": True,
                "calibrated": p.intrinsics is not None and p.homography is not None,
                "time_synced": p.sync_anchor_timestamp_s is not None,
                "counts_by_path": counts,
                # Preserved for legacy consumers (failure_strip rate calc,
                # /events, tests). Mirrors server_post which is still the
                # canonical "did the upload decode + detect" signal.
                "n_frames": counts["server_post"]["total"],
                "n_detected": counts["server_post"]["detected"],
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
        # Live-only sessions have empty p.frames; fall back to whichever
        # pipeline actually carried frames so the header still shows a
        # real duration.
        per_pitch_spans: list[float] = []
        for p in pitches.values():
            frame_lists = [p.frames, p.frames_live or []]
            frames = next((fs for fs in frame_lists if fs), None)
            if not frames:
                continue
            anchor = p.sync_anchor_timestamp_s if p.sync_anchor_timestamp_s is not None else p.video_start_pts_s
            rels = [f.timestamp_s - anchor for f in frames]
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
    if has_any_video:
        mode = "camera_only"
    else:
        mode = "live_only"
    ballistic_by_path = (
        {k: v.model_dump(mode="json") for k, v in result.ballistic_by_path.items()}
        if result is not None else {}
    )
    return {
        "session_id": session_id,
        "cameras": cams,
        "triangulated_count": len(result.points) if result is not None else 0,
        "error": result.error if result is not None else None,
        "duration_s": duration_s,
        "received_at": latest_mtime,
        "mode": mode,
        "ballistic_by_path": ballistic_by_path,
    }


def _videos_for_session(
    session_id: str,
) -> list[tuple[str, str, float, float, dict[str, list]]]:
    """One row per camera; `frames_info` carries **two independent detection
    streams** keyed by DetectionPath: `live` (iOS real-time) and
    `server_post` (server-side decode + detection on the uploaded MOV).
    Each stream has its own t_rel / detected / px / py arrays and its own
    visibility toggle in the viewer — no fallback, no merging. An absent
    stream is the empty-arrays shape so the JS side can treat both
    uniformly."""
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

    def _stream(frames, rel_anchor) -> dict[str, list]:
        # iOS live WS stream can arrive out-of-order (packet reordering);
        # sort by timestamp so the viewer's O(n) walk of t_rel_s stays
        # monotonic — the strip's frame lookup assumes ascending ts.
        ordered = sorted(frames, key=lambda f: f.timestamp_s)
        return {
            "t_rel_s": [float(f.timestamp_s - rel_anchor) for f in ordered],
            "detected": [bool(f.ball_detected) for f in ordered],
            "px": [float(f.px) if f.px is not None else None for f in ordered],
            "py": [float(f.py) if f.py is not None else None for f in ordered],
        }

    out: list[tuple[str, str, float, float, dict[str, list]]] = []
    all_cams = sorted(
        set(best)
        | {c for c, p in pitches.items() if p.frames}
        | {c for c, p in pitches.items() if p.frames_live}
    )
    for cam in all_cams:
        name = best.get(cam)
        pitch = pitches.get(cam)
        if pitch is None or pitch.sync_anchor_timestamp_s is None:
            offset = 0.0
        else:
            offset = float(pitch.video_start_pts_s - pitch.sync_anchor_timestamp_s)
        fps = float(pitch.video_fps) if (pitch is not None and pitch.video_fps is not None) else 240.0
        if pitch is not None:
            rel_anchor = (
                pitch.sync_anchor_timestamp_s
                if pitch.sync_anchor_timestamp_s is not None
                else pitch.video_start_pts_s
            )
            frames_info = {
                "live": _stream(pitch.frames_live, rel_anchor),
                "server_post": _stream(pitch.frames, rel_anchor),
            }
        else:
            empty = {"t_rel_s": [], "detected": [], "px": [], "py": []}
            frames_info = {
                "live": dict(empty),
                "server_post": dict(empty),
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
