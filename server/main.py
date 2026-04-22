"""FastAPI ingest + triangulation server for ball_tracker iPhone app.

Endpoints:
  GET  /                            — dashboard (nav + 440 px sidebar with
                                       devices / session / events + full-bleed
                                       3D canvas showing the calibration scene)
  POST /calibration                 — iPhone uploads a freshly-saved
                                       `{camera_id, intrinsics, homography,
                                       image_{width,height}_px}` so the
                                       dashboard can render the camera pose
                                       immediately (before any pitch arrives).
                                       Idempotent overwrite per camera_id.
  GET  /calibration/state           — dashboard polls this every 5 s; returns
                                       the current per-camera snapshots +
                                       a ready-to-`Plotly.react` figure spec.
  GET  /status                      — health + online devices + session +
                                       per-camera commands
  POST /pitch                       — ingest one session upload (multipart:
                                       required `payload` JSON carrying
                                       `session_id`, optional `video`
                                       MOV/MP4 clip)
  WS   /ws/device/{camera_id}       — iPhone live transport. Carries
                                       arm/disarm/settings/sync_command
                                       downstream + liveness + live
                                       frame stream upstream. Replaces
                                       the retired HTTP /heartbeat endpoint.
  POST /sessions/arm                — dashboard: begin an armed session.
                                       Server returns the new session id
                                       (idempotent on re-arm). /status
                                       starts dispatching {cam: "arm"}.
  POST /sessions/stop               — dashboard: end the armed session.
                                       Triggers the "disarm" echo window so
                                       phones flush the in-progress recording
                                       and upload the cycle.
  POST /sessions/clear              — dashboard: drop the last-ended
                                       session pointer so the session
                                       card on / goes blank. No-op (409
                                       for JSON callers) when already
                                       idle with no prior session.
  GET  /chirp.wav                   — reference sync chirp for 時間校正
  GET  /events                      — one row per session: cameras, status,
                                       counts, received_at, triangulation
                                       stats
  GET  /results/latest              — most recently recorded session
  GET  /results/{session_id}        — specific session's SessionResult
  GET  /reconstruction/{session_id} — 3D scene (cameras + rays + optional
                                       triangulated trajectory) as JSON
  GET  /viewer/{session_id}         — same scene as a self-contained Plotly
                                       HTML page
  POST /reset                       — clear all cached state

Pairing key: every PitchPayload carries `session_id` (server-minted via
`POST /sessions/arm`). A/B pairs by `session_id`, NOT by any device-local
counter. iPhones are dumb capture clients — they do not allocate pairing
identifiers.
"""
from __future__ import annotations
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import numpy as np
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError

# Re-exports so `from main import PitchPayload, ...` keeps working for the
# existing test suite and any downstream tooling. New callers should import
# from the split modules directly (schemas / pairing / chirp / render_*).
from schemas import (
    CalibrationSnapshot,
    CaptureTelemetryPayload,
    CaptureMode,
    DetectionPath,
    Device,
    FramePayload,
    IntrinsicsPayload,
    MarkerBatchUpsertRequest,
    MarkerDraft,
    MarkerRecord,
    MarkerUpdateRequest,
    PitchAnalysisPayload,
    PitchPayload,
    Session,
    SessionResult,
    SyncLogBody,
    SyncLogEntry,
    SyncReport,
    SyncResult,
    SyncRun,
    TrackingExposureCapMode,
    TriangulatedPoint,
    _DEFAULT_TRACKING_EXPOSURE_CAP_MODE,
    _DEFAULT_SESSION_TIMEOUT_S,
    _DEFAULT_PATHS,
    mode_for_paths,
    paths_for_mode,
)
from collections import deque
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from fitting import fit_trajectory
from pipeline import ProcessingCanceled, annotate_video, detect_pitch
from video import probe_dims
from chirp import chirp_wav_bytes
import sync_audio_detect
from preview import (
    FRAME_MAX_AGE_S as _PREVIEW_FRAME_MAX_AGE_S,
    PreviewBuffer,
)
from marker_registry import MarkerRegistryDB
from calibration_solver import (
    PLATE_MARKER_WORLD,
    derive_fov_intrinsics,
    detect_all_markers_in_dict,
    solve_homography_from_world_map,
)
from triangulate import build_K, camera_center_world, recover_extrinsics, triangulate_rays, undistorted_ray_cam
from sync_solver import compute_mutual_sync
from cleanup_old_sessions import cleanup_expired_sessions
from live_pairing import LivePairingSession
from sse import SSEHub
from ws import DeviceSocketManager

# State, constants, and helper types now live in state.py.
# Re-export everything tests reference via `main.*` so the test suite
# needs zero changes.
from state import (
    _AutoCalibrationRun,
    _CALIBRATION_FRAME_TTL_S,
    _DEFAULT_DATA_DIR,
    _DEVICE_GC_AFTER_S,
    _DEVICE_REGISTRY_CAP,
    _DEVICE_STALE_S,
    _DISARM_ECHO_S,
    _LegacyTimeSyncIntent,
    _MAX_PITCH_UPLOAD_BYTES,
    _new_session_id,
    _new_sync_id,
    _SYNC_COMMAND_TTL_S,
    _SYNC_COOLDOWN_S,
    _SYNC_LATE_REPORT_GRACE_S,
    _SYNC_TIMEOUT_S,
    _TIME_SYNC_INTENT_WINDOW_S,
    _TIME_SYNC_MAX_AGE_S,
    _validate_calibration_snapshot,
    State,
    state,
)

logger = logging.getLogger("ball_tracker")

def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ip = _lan_ip()
    logger.info("LAN IP: %s  →  set iPhone Settings → Server IP = %s, Port = 8765", ip, ip)
    # $BALL_TRACKER_CLEANUP_DAYS=0 disables startup cleanup; otherwise sessions
    # whose youngest file is older than N days are purged here.
    cleanup_days = int(os.environ.get("BALL_TRACKER_CLEANUP_DAYS", "30"))
    if cleanup_days > 0:
        sessions, files, bytes_removed = cleanup_expired_sessions(
            state.data_dir, days=cleanup_days, dry_run=False
        )
        logger.info(
            "cleanup: removed %d sessions / %d files / %d bytes older than %d days from %s",
            sessions, files, bytes_removed, cleanup_days, state.data_dir,
        )
    yield


app = FastAPI(title="ball_tracker server", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Force browsers to always refetch HTML — the dashboard ships its
    JS inline, so a cached HTML doc means stale JS. Plain reload (not
    Cmd-Shift-R) was serving disk-cached HTML with an older IIFE that
    still ran the retired tickPreviewRefresh keep-alive loop, which
    fought the new single-source-of-truth preview flow."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response
device_ws = DeviceSocketManager()
sse_hub = SSEHub()


def _build_device_status_rows(
    *,
    now: float | None = None,
    ws_snapshot: dict[str, DeviceSocketSnapshot] | None = None,
) -> list[dict[str, Any]]:
    now = state._time_fn() if now is None else now
    ws_snapshot = device_ws.snapshot() if ws_snapshot is None else ws_snapshot
    fresh_devices = {d.camera_id: d for d in state.online_devices()}
    expected = state.expected_sync_id_snapshot()
    device_ids = set(fresh_devices) | {
        cam for cam, snap in ws_snapshot.items()
        if snap.connected
    }
    devices: list[dict[str, Any]] = []
    for cam in sorted(device_ids):
        d = fresh_devices.get(cam) or state.device_snapshot(cam)
        ws = ws_snapshot.get(cam)
        # An attempt is in progress for this cam IFF we've stamped an
        # expected id AND the phone hasn't yet echoed it. Until it
        # matches, the dashboard paints this cam as listening (red LED)
        # even if iOS is still reporting an old sync_id from a prior
        # successful attempt.
        exp = expected.get(cam)
        id_match = (
            d is not None
            and d.time_sync_id is not None
            and (exp is None or d.time_sync_id == exp)
        )
        devices.append(
            {
                "camera_id": cam,
                "last_seen_at": (
                    d.last_seen_at
                    if d is not None
                    else (ws.last_seen_at if ws is not None else None)
                ),
                "time_synced": (
                    bool(d is not None)
                    and d.time_synced
                    and d.time_sync_id is not None
                    and d.time_sync_at is not None
                    and now - d.time_sync_at <= _TIME_SYNC_MAX_AGE_S
                    and id_match
                ),
                "time_sync_id": (d.time_sync_id if d is not None else None),
                "time_sync_age_s": (
                    None
                    if d is None or d.time_sync_at is None
                    else float(now - d.time_sync_at)
                ),
                "sync_anchor_timestamp_s": (
                    d.sync_anchor_timestamp_s if d is not None else None
                ),
                "ws_connected": (ws.connected if ws is not None else False),
                "ws_latency_ms": (ws.last_latency_ms if ws is not None else None),
            }
        )
    return devices


def _build_status_response() -> dict[str, Any]:
    """Shared shape for GET /status and dashboard-facing snapshots. Anything
    an iPhone needs to decide whether to arm / disarm is in here — the
    phone just polls this and reacts to `commands[self.camera_id]`."""
    summary = state.summary()
    session = state.session_snapshot()
    sync_run = state.current_sync()
    last_sync = state.last_sync_result()
    now = state._time_fn()
    ws_snapshot = device_ws.snapshot()
    devices = _build_device_status_rows(now=now, ws_snapshot=ws_snapshot)
    calibrations = sorted(state.calibrations().keys())
    return {
        **summary,
        "devices": devices,
        # Lightweight calibration presence snapshot for header/readiness UI.
        # The richer scene payload still lives on /calibration/state.
        "calibrations": calibrations,
        "session": session.to_dict() if session is not None else None,
        "commands": state.commands_for_devices(),
        # Global dashboard mode choice. iPhones show this on the HUD in idle
        # and fall back to it when there's no armed session; during an armed
        # session they read session.mode instead (it's the snapshot that
        # can't drift from under them).
        "capture_mode": state.current_mode().value,
        "default_paths": sorted(p.value for p in state.default_paths()),
        # Mutual-sync context. `sync.id` is the sole dedupe key the phone
        # uses to decide whether a fresh `sync_run` command has arrived
        # vs. a repeat of an in-flight run. `last_sync` lets the dashboard
        # surface Δ + D without waiting for the next pitch upload.
        "sync": sync_run.to_dict() if sync_run is not None else None,
        "last_sync": last_sync.model_dump() if last_sync is not None else None,
        "sync_cooldown_remaining_s": state.sync_cooldown_remaining_s(),
        # Pending dashboard-triggered time-sync commands, keyed by camera.
        # Observational only: the phone reads its own command via
        # `sync_command` (set on the WS heartbeat / push path), and consumption
        # clears the flag. `/status` surfaces this map so the dashboard
        # can paint a "pending" badge until the phone drains it.
        "sync_commands": state.pending_sync_commands(),
        # Runtime tunables pushed from the dashboard. iOS hot-applies any
        # changes from WS settings messages (matched-filter threshold into
        # AudioChirpDetector; cadence into ServerHealthMonitor).
        "chirp_detect_threshold": state.chirp_detect_threshold(),
        "mutual_sync_threshold": state.mutual_sync_threshold(),
        "heartbeat_interval_s": state.heartbeat_interval_s(),
        "tracking_exposure_cap": state.tracking_exposure_cap().value,
        # Capture resolution (image height px) pushed to iOS. Phone rebuilds
        # its AVCaptureSession at the new height when this differs from the
        # currently-applied value — only while in .standby so an armed clip
        # is never disrupted mid-recording.
        "capture_height_px": state.capture_height_px(),
        # Per-camera live-preview request flags (Phase 4a). Dashboard
        # renders a toggle per Devices row from this map; iPhones read
        # their own flag off the WS settings payload (separate sibling field,
        # see below) to decide whether to push preview JPEGs.
        "preview_requested": state._preview.requested_map(),
        # Per-camera one-shot calibration-frame pending map. Dashboard
        # paints a "capturing…" chip while true. The beating camera
        # reads its own flag off the WS settings payload's sibling
        # `calibration_frame_requested` scalar and uploads one
        # full-resolution JPEG.
        "calibration_frame_requested": {
            cam: True
            for cam in state._cal_frame_requested.keys()
            if state.is_calibration_frame_requested(cam)
        },
        "auto_calibration": state.auto_cal_status(),
        "live_session": state.live_session_summary(),
        "ws_devices": {
            cam: {
                "connected": snap.connected,
                "connected_at": snap.connected_at,
                "last_seen_at": snap.last_seen_at,
                "last_latency_ms": snap.last_latency_ms,
            }
            for cam, snap in ws_snapshot.items()
        },
    }


def _settings_message_for(camera_id: str) -> dict[str, Any]:
    status = _build_status_response()
    device_status = next(
        (d for d in status.get("devices", []) if d.get("camera_id") == camera_id),
        {},
    )
    return {
        "type": "settings",
        "camera_id": camera_id,
        "paths": status.get("default_paths", []),
        "chirp_detect_threshold": status.get("chirp_detect_threshold"),
        "mutual_sync_threshold": status.get("mutual_sync_threshold"),
        "heartbeat_interval_s": status.get("heartbeat_interval_s"),
        "tracking_exposure_cap": status.get("tracking_exposure_cap"),
        "capture_height_px": status.get("capture_height_px"),
        "preview_requested": status.get("preview_requested", {}).get(camera_id, False),
        "calibration_frame_requested": status.get("calibration_frame_requested", {}).get(camera_id, False),
        "device_time_synced": device_status.get("time_synced", False),
        "device_time_sync_id": device_status.get("time_sync_id"),
    }


def _arm_message_for(session: Session) -> dict[str, Any]:
    return {
        "type": "arm",
        "sid": session.id,
        "paths": sorted(p.value for p in session.paths),
        "max_duration_s": session.max_duration_s,
        "tracking_exposure_cap": session.tracking_exposure_cap.value,
    }


def _disarm_message_for(session: Session) -> dict[str, Any]:
    return {
        "type": "disarm",
        "sid": session.id,
    }


@app.get("/status")
def status() -> dict[str, Any]:
    return _build_status_response()


@app.get("/stream")
async def stream() -> StreamingResponse:
    async def event_gen():
        yield "event: hello\ndata: {}\n\n"
        async for payload in sse_hub.subscribe():
            yield payload

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.websocket("/ws/device/{camera_id}")
async def ws_device(camera_id: str, websocket: WebSocket) -> None:
    _validate_camera_id_or_422(camera_id)
    await device_ws.connect(camera_id, websocket)
    # Freshen `Device.last_seen_at` immediately on connect so `/status`
    # sees the cam as online without waiting for the first `hello` to
    # arrive. Otherwise we age out on disconnect, broadcast
    # `device_status online=true` at connect, the dashboard kicks
    # tickStatus — but state.online_devices() still excludes the cam
    # because its last_seen_at is old, so the panel races back to
    # offline for up to one hello cadence.
    state.heartbeat(camera_id)
    try:
        await device_ws.send(camera_id, _settings_message_for(camera_id))
        session = state.current_session()
        if session is not None and session.armed:
            await device_ws.send(camera_id, _arm_message_for(session))
        # If a mutual-sync run is active when a phone (re)connects, push
        # the sync_run signal so it can join late instead of sitting idle
        # until the run times out.
        active_sync = state.current_sync()
        if active_sync is not None and camera_id not in active_sync.reports:
            await device_ws.send(
                camera_id,
                {"type": "sync_run", "sync_id": active_sync.id},
            )
        await sse_hub.broadcast(
            "device_status",
            {"cam": camera_id, "online": True, "ws_connected": True},
        )
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "hello":
                device_ws.note_seen(camera_id)
                reported_sync_id = msg.get("time_sync_id")
                reported_anchor = msg.get("sync_anchor_timestamp_s")
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                )
                await device_ws.send(camera_id, _settings_message_for(camera_id))
                continue
            if mtype == "heartbeat":
                device_ws.note_seen(camera_id)
                reported_sync_id = msg.get("time_sync_id")
                reported_anchor = msg.get("sync_anchor_timestamp_s")
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                )
                telem = msg.get("sync_telemetry")
                if isinstance(telem, dict):
                    state.record_sync_telemetry(camera_id, telem)
                continue
            if mtype == "frame":
                device_ws.note_seen(camera_id)
                frame = FramePayload(
                    frame_index=int(msg.get("i", 0)),
                    timestamp_s=float(msg["ts"]),
                    px=None if msg.get("px") is None else float(msg["px"]),
                    py=None if msg.get("py") is None else float(msg["py"]),
                    ball_detected=bool(msg.get("detected", False)),
                )
                session_id = str(msg.get("sid") or "")
                if not session_id:
                    continue
                new_points, counts = await asyncio.to_thread(
                    state.ingest_live_frame,
                    camera_id,
                    session_id,
                    frame,
                )
                await sse_hub.broadcast(
                    "frame_count",
                    {
                        "sid": session_id,
                        "cam": camera_id,
                        "path": DetectionPath.live.value,
                        "count": counts.get(camera_id, 0),
                    },
                )
                for point in new_points:
                    await sse_hub.broadcast(
                        "point",
                        {
                            "sid": session_id,
                            "path": DetectionPath.live.value,
                            "x": point.x_m,
                            "y": point.y_m,
                            "z": point.z_m,
                            "t_rel_s": point.t_rel_s,
                        },
                    )
                if new_points:
                    result = await asyncio.to_thread(state._rebuild_result_for_session, session_id)
                    await asyncio.to_thread(state.store_result, result)
                continue
            if mtype == "cycle_end":
                session_id = str(msg.get("sid") or "")
                reason = msg.get("reason")
                if session_id:
                    await asyncio.to_thread(state.mark_live_path_ended, camera_id, session_id, reason)
                    result = await asyncio.to_thread(state._rebuild_result_for_session, session_id)
                    await asyncio.to_thread(state.store_result, result)
                    await sse_hub.broadcast(
                        "path_completed",
                        {
                            "sid": session_id,
                            "path": DetectionPath.live.value,
                            "cam": camera_id,
                            "reason": reason,
                            "point_count": len(result.triangulated_by_path.get(DetectionPath.live.value, [])),
                        },
                    )
                continue
    except WebSocketDisconnect:
        pass
    finally:
        device_ws.disconnect(camera_id, websocket)
        # Dashboard `/status` derives online-ness from `Device.last_seen_at`
        # with a 3 s stale window, so without this the UI keeps painting the
        # cam as online for up to 3 s after the phone sleeps / drops WS.
        state.mark_device_offline(camera_id)
        # Also clear any live preview request — there's no client to push
        # to anymore, and leaving the TTL alive would re-arm the phone the
        # instant it reconnects.
        state._preview.request(camera_id, enabled=False)
        await sse_hub.broadcast(
            "device_status",
            {"cam": camera_id, "online": False, "ws_connected": False},
        )


def _wants_html(request: Request) -> bool:
    """Returns True when the request looks like a browser form submission
    (Accept: text/html). Lets one endpoint serve both dashboard buttons
    and JSON API callers without a second URL."""
    return "text/html" in request.headers.get("accept", "").lower()


@app.post("/sessions/arm")
async def sessions_arm(
    request: Request,
    max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S,
):
    """Begin an armed session. HTML-form callers (dashboard buttons) get a
    303 redirect back to /. Machine callers get the session JSON."""
    requested_paths: set[DetectionPath] | None = None
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        raw_paths = body.get("paths")
        if isinstance(raw_paths, list):
            requested_paths = state._normalize_paths(raw_paths)
    session = state.arm_session(max_duration_s=max_duration_s, paths=requested_paths)
    await device_ws.broadcast(
        {
            cam.camera_id: _arm_message_for(session)
            for cam in state.online_devices()
        }
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


@app.post("/sessions/stop")
async def sessions_stop(request: Request):
    """End the armed session (operator Stop). Returns 409 to API callers
    when nothing was armed; HTML callers always get a 303 redirect back
    to the dashboard so the button never looks broken."""
    ended = state.stop_session()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if ended is None:
        raise HTTPException(status_code=409, detail="no armed session")
    await device_ws.broadcast(
        {
            cam.camera_id: _disarm_message_for(ended)
            for cam in state.online_devices()
        }
    )
    await sse_hub.broadcast(
        "session_ended",
        {
            "sid": ended.id,
            "paths_completed": sorted(state.results.get(ended.id, SessionResult(session_id=ended.id, camera_a_received=False, camera_b_received=False)).paths_completed),
        },
    )
    return {"ok": True, "session": ended.to_dict()}


@app.post("/sessions/set_mode")
async def sessions_set_mode(
    request: Request,
    mode: str = Form(...),
):
    """Dashboard mode picker target. Records the global capture mode which
    the next `arm_session()` will snapshot. HTML form callers (dashboard
    radio buttons) get a 303 back to /; JSON API callers get the applied
    mode echoed so they can confirm the write."""
    try:
        applied = CaptureMode(mode)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid mode {mode!r}; expected one of: {[m.value for m in CaptureMode]}",
        )
    state.set_mode(applied)
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "capture_mode": applied.value}


@app.post("/detection/paths")
async def detection_paths(request: Request):
    ctype = request.headers.get("content-type", "").lower()
    raw_paths: list[str] | None = None
    if "application/json" in ctype:
        body = await request.json()
        if isinstance(body.get("paths"), list):
            raw_paths = body["paths"]
    else:
        form = await request.form()
        raw = form.getlist("paths")
        raw_paths = [str(v) for v in raw]
    paths = state._normalize_paths(raw_paths or [])
    if not paths:
        raise HTTPException(status_code=400, detail="at least one detection path is required")
    try:
        applied = state.set_default_paths(paths)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await device_ws.broadcast(
        {cam.camera_id: _settings_message_for(cam.camera_id) for cam in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "paths": sorted(p.value for p in applied)}


@app.post("/settings/chirp_threshold")
async def settings_chirp_threshold(request: Request):
    """Set the chirp matched-filter detection threshold. Accepts either a
    JSON body `{threshold: float}` or a form field `threshold`. HTML form
    callers get 303 back to /; JSON callers get `{ok, value}`."""
    threshold: float | None = None
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            threshold = float(body.get("threshold"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'threshold'")
    else:
        form = await request.form()
        raw = form.get("threshold")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'threshold'")
        try:
            threshold = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'threshold'")
    try:
        applied = state.set_chirp_detect_threshold(threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@app.post("/settings/mutual_sync_threshold")
async def settings_mutual_sync_threshold(request: Request):
    """Set the mutual-sync (two-phone cross-detection) matched-filter
    threshold. Independent from quick-chirp — the two modalities see
    very different peak magnitudes so tuning one shouldn't clobber the
    other."""
    threshold: float | None = None
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            threshold = float(body.get("threshold"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'threshold'")
    else:
        form = await request.form()
        raw = form.get("threshold")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'threshold'")
        try:
            threshold = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'threshold'")
    try:
        applied = state.set_mutual_sync_threshold(threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@app.post("/settings/heartbeat_interval")
async def settings_heartbeat_interval(request: Request):
    """Set the iPhone heartbeat base cadence (seconds). Accepts JSON
    `{interval_s: float}` or form field `interval_s`. HTML callers get
    303 back to /; JSON callers get `{ok, value}`."""
    interval: float | None = None
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        try:
            interval = float(body.get("interval_s"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="missing or invalid 'interval_s'")
    else:
        form = await request.form()
        raw = form.get("interval_s")
        if raw is None:
            raise HTTPException(status_code=400, detail="missing 'interval_s'")
        try:
            interval = float(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'interval_s'")
    try:
        applied = state.set_heartbeat_interval_s(interval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@app.post("/settings/tracking_exposure_cap")
async def settings_tracking_exposure_cap(request: Request):
    """Set the server-owned 240 fps exposure-cap policy. Accepts JSON
    `{mode: str}` or form field `mode`. HTML callers get 303 back to `/`;
    JSON callers get `{ok, value}`."""
    mode_raw: Any
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        mode_raw = body.get("mode")
    else:
        form = await request.form()
        mode_raw = form.get("mode")
    if mode_raw is None:
        raise HTTPException(status_code=400, detail="missing 'mode'")
    try:
        mode = TrackingExposureCapMode(str(mode_raw))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid 'mode'; expected one of {[m.value for m in TrackingExposureCapMode]}",
        )
    applied = state.set_tracking_exposure_cap(mode)
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied.value}


@app.post("/settings/capture_height")
async def settings_capture_height(request: Request):
    """Set the iPhone capture resolution (image height in px). Accepts JSON
    `{height: int}` or form field `height`. Allowed: 720 / 1080.
    HTML callers get 303 back to /; JSON callers get `{ok, value}`."""
    height_raw: Any
    ctype = request.headers.get("content-type", "").lower()
    if "application/json" in ctype:
        body = await request.json()
        height_raw = body.get("height")
    else:
        form = await request.form()
        height_raw = form.get("height")
    if height_raw is None:
        raise HTTPException(status_code=400, detail="missing 'height'")
    try:
        height = int(height_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid 'height'")
    try:
        applied = state.set_capture_height_px(height)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await device_ws.broadcast(
        {cmd.camera_id: _settings_message_for(cmd.camera_id) for cmd in state.online_devices()}
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "value": applied}


@app.post("/sessions/clear")
async def sessions_clear(request: Request):
    """Drop the last-ended session pointer so the dashboard card returns
    to blank. HTML callers get a 303 back to /; JSON callers get 409 when
    nothing was there to clear (idle with no previous session)."""
    cleared = state.clear_last_ended_session()
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not cleared:
        raise HTTPException(status_code=409, detail="nothing to clear")
    return {"ok": True}


# HTTP status codes used by the /sync/start conflict mapping. One integer
# per reason keeps the handler legible without a bespoke error type.
_SYNC_START_STATUS_FOR_REASON: dict[str, int] = {
    "session_armed": 409,
    "sync_in_progress": 409,
    "cooldown": 409,
    "devices_missing": 409,
}


@app.post("/sync/start")
async def sync_start(request: Request) -> dict[str, Any]:
    """Begin a mutual chirp sync run. Pushes `{type: "sync_run", sync_id}`
    over WS to both online phones; iOS `applyMutualSync` enters the
    mutual-sync flow on receipt. Returns 409 with a `reason` field on
    conflict."""
    run, reason = state.start_sync()
    if reason is not None:
        status_code = _SYNC_START_STATUS_FOR_REASON.get(reason, 409)
        raise HTTPException(
            status_code=status_code,
            detail={"ok": False, "error": reason},
        )
    assert run is not None  # reason is None → run is always set
    # Fresh listening window → reset per-cam peak maxima so the
    # telemetry card reads peaks for THIS attempt only, and stamp
    # the run id as the expected-sync-id for every currently-online
    # cam so LEDs flip red until this run's id comes back.
    state.reset_sync_telemetry_peaks(None)
    state.set_expected_sync_id(
        [d.camera_id for d in state.online_devices()],
        run.id,
    )
    # WS-only live transport: phones get the sync_run signal here. Without
    # this push iOS never knows the run started — the HTTP /heartbeat
    # retirement (a66d5db) removed the `commands` channel and this push
    # was missed. CameraViewController's WS handler routes type=sync_run
    # → applyMutualSync(syncId:) → beginMutualSync.
    await device_ws.broadcast(
        {
            cam.camera_id: {"type": "sync_run", "sync_id": run.id}
            for cam in state.online_devices()
        }
    )
    return {"ok": True, "sync": run.to_dict()}


_SYNC_WAV_RE = re.compile(r"^sy_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}\.wav$")


@app.get("/sync/audio/{filename}")
def sync_audio_download(filename: str) -> FileResponse:
    """Serve persisted mutual-sync WAVs for offline replay + Copy-AI-
    Debug attachment. `filename` must match `sy_<hex>_<cam>.wav` —
    same shape the upload endpoint writes — to prevent path traversal
    via the URL (re pattern is anchored + character-class-bounded)."""
    if not _SYNC_WAV_RE.match(filename):
        raise HTTPException(status_code=400, detail="invalid sync audio filename")
    wav_path = state.data_dir / "sync_audio" / filename
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="wav not found")
    return FileResponse(
        wav_path, media_type="audio/wav", filename=filename
    )


@app.post("/sync/audio_upload")
async def sync_audio_upload(
    payload: str = Form(...),
    audio: UploadFile = File(...),
) -> dict[str, Any]:
    """Phase A mutual-sync path: iOS uploads the raw PCM it recorded
    during the listen window, server runs matched-filter detection and
    feeds the resulting `SyncReport` into the same state machine the
    legacy `/sync/report` endpoint drives.

    Multipart shape:
      - `payload` (Form, JSON): {
            sync_id, camera_id, role ("A"|"B"),
            audio_start_pts_s (float, host-clock seconds of first sample),
            sample_rate (int, informational — WAV header is authoritative),
            emission_pts_s (float, optional, this phone's own chirp schedule
              time — kept for debug cross-check only)
        }
      - `audio` (File, audio/wav): mono 16-bit PCM WAV of the listening
        window (typically 3 s @ 48 kHz → ~288 KB).

    Side effects:
      - Persists the WAV to `data/sync_audio/<sync_id>_<cam>.wav`
        (never cleaned — small footprint, priceless for offline debug).
      - Delegates pairing / solving to `state.record_sync_report`; the
        second upload triggers the solver exactly as `/sync/report` did.
    """
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"payload JSON parse: {e}") from e

    required = ("sync_id", "camera_id", "role", "audio_start_pts_s")
    missing = [k for k in required if meta.get(k) is None]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"payload missing required keys: {missing}",
        )
    sync_id = str(meta["sync_id"])
    camera_id = str(meta["camera_id"])
    role = str(meta["role"])
    if role not in ("A", "B"):
        raise HTTPException(
            status_code=422, detail=f"role must be 'A' or 'B', got {role!r}"
        )
    try:
        audio_start_pts_s = float(meta["audio_start_pts_s"])
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=422, detail=f"audio_start_pts_s not a float: {e}"
        ) from e
    emission_pts_s = meta.get("emission_pts_s")

    wav_bytes = await audio.read()
    if not wav_bytes:
        raise HTTPException(status_code=422, detail="audio part empty")

    # Persist first — even if detection crashes, we keep the raw bytes
    # for offline iteration. `data/sync_audio/` is the accumulating
    # failure library that makes Phase B algorithm work possible.
    audio_dir = state.data_dir / "sync_audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    wav_path = audio_dir / f"{sync_id}_{camera_id}.wav"
    wav_path.write_bytes(wav_bytes)

    try:
        report, debug = sync_audio_detect.detect_sync_report(
            wav_bytes=wav_bytes,
            sync_id=sync_id,
            camera_id=camera_id,
            role=role,
            audio_start_pts_s=audio_start_pts_s,
        )
    except Exception as e:
        logger.exception("sync_audio_upload detection failed cam=%s", camera_id)
        raise HTTPException(
            status_code=500, detail=f"detection failed: {e}"
        ) from e

    logger.info(
        "sync_audio_upload cam=%s role=%s duration_s=%.3f "
        "peak_self=%.4f peak_other=%.4f psr_self=%.2f psr_other=%.2f "
        "t_self=%.6f t_other=%.6f",
        camera_id, role, debug["duration_s"],
        debug["peak_self"], debug["peak_other"],
        debug["psr_self"], debug["psr_other"],
        report.t_self_s or 0.0, report.t_from_other_s or 0.0,
    )

    run_after, result, reason = state.record_sync_report(report)
    if reason == "no_sync":
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "error": "no_sync"},
        )
    if reason == "stale_sync_id":
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "error": "stale_sync_id"},
        )
    resp: dict[str, Any] = {
        "ok": True,
        "solved": result is not None,
        "detection": {
            "peak_self": debug["peak_self"],
            "peak_other": debug["peak_other"],
            "psr_self": debug["psr_self"],
            "psr_other": debug["psr_other"],
            "duration_s": debug["duration_s"],
            "sample_rate": debug["sample_rate"],
            "emission_pts_s": emission_pts_s,
            "wav_path": str(wav_path.relative_to(state.data_dir)),
        },
    }
    if result is not None:
        resp["result"] = result.model_dump()
    elif run_after is not None:
        resp["run"] = run_after.to_dict()
    return resp


@app.post("/sync/report")
async def sync_report(report: SyncReport) -> dict[str, Any]:
    """Phone-side callback after both matched filters have fired on its
    mic stream. Returns `solved: false` on the first report, and
    `solved: true` with the result on the second (triggering the
    solver)."""
    run_after, result, reason = state.record_sync_report(report)
    if reason == "no_sync":
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "error": "no_sync"},
        )
    if reason == "stale_sync_id":
        raise HTTPException(
            status_code=409,
            detail={"ok": False, "error": "stale_sync_id"},
        )
    resp: dict[str, Any] = {"ok": True, "solved": result is not None}
    if result is not None:
        resp["result"] = result.model_dump()
    elif run_after is not None:
        resp["run"] = run_after.to_dict()
    return resp


@app.get("/sync/debug_export")
def sync_debug_export() -> Response:
    """Returns a compact plain-text report of the last sync attempt,
    designed to be copied and pasted to an AI (Claude Code) for diagnosis.
    Includes trace peak metrics, telemetry, log tail, and auto-analysis."""
    from sync_analysis import build_debug_report

    last = state.last_sync_result()
    logs = state.sync_logs(limit=60)
    telem = state.sync_telemetry_snapshot()
    devices = _build_device_status_rows()
    report = build_debug_report(
        last_sync=last.model_dump() if last is not None else None,
        telemetry=telem,
        logs=[e.model_dump() for e in logs],
        mutual_threshold=state.mutual_sync_threshold(),
        chirp_threshold=state.chirp_detect_threshold(),
        devices=devices,
    )
    return Response(content=report, media_type="text/plain; charset=utf-8")


@app.get("/sync/state")
def sync_state(log_limit: int = 200) -> dict[str, Any]:
    """Dashboard + CLI probe endpoint. Includes the diagnostic log ring
    so the UI can render the full A/B/server timeline without a second
    round-trip. `log_limit` caps how many recent entries are returned
    (default 200, enough for several runs' worth of events)."""
    run = state.current_sync()
    last = state.last_sync_result()
    logs = state.sync_logs(limit=log_limit)
    return {
        "sync": run.to_dict() if run is not None else None,
        "last_sync": last.model_dump() if last is not None else None,
        "cooldown_remaining_s": state.sync_cooldown_remaining_s(),
        "logs": [entry.model_dump() for entry in logs],
        "telemetry": state.sync_telemetry_snapshot(),
    }


@app.post("/sync/trigger")
async def sync_trigger(request: Request) -> Any:
    """Dashboard-remote time-sync trigger: flags each target camera with
    a pending `sync_command: "start"` that the phone consumes on its next
    heartbeat and acts on the same way the local 時間校正 button does.

    Body shapes:
      - Empty / no body → target every currently-online camera.
      - JSON `{"camera_ids": ["A"]}` → target only the listed cameras.
      - Form field `camera_ids` (comma or space separated) → same.

    Idempotent — re-POSTing to a camera already flagged just refreshes
    its TTL. Silently skips cameras participating in a currently-armed
    session (sync would disrupt an in-flight recording).

    HTML form callers (dashboard button) get a 303 back to /; JSON
    callers get `{ok, dispatched_to: [...]}` so a CLI probe can see
    which cameras were actually flagged vs. skipped."""
    ctype = request.headers.get("content-type", "").lower()
    is_form = (
        "application/x-www-form-urlencoded" in ctype
        or "multipart/form-data" in ctype
    )
    camera_ids: list[str] | None = None
    if "application/json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            raw = body.get("camera_ids")
            if isinstance(raw, list):
                camera_ids = [str(c) for c in raw]
            elif raw is not None:
                raise HTTPException(
                    status_code=422,
                    detail="camera_ids must be a list of strings",
                )
    elif is_form:
        form = await request.form()
        raw = form.get("camera_ids")
        if raw is not None:
            # Accept "A,B" or "A B" — either way, splitting on whitespace +
            # commas keeps the form shape flexible for hand-curl'd probes.
            camera_ids = [
                c for c in (str(raw).replace(",", " ").split()) if c
            ]

    dispatched = state.trigger_sync_command(camera_ids)
    # Fresh attempt → fresh peak window on the telemetry card so the
    # operator isn't looking at maxima from a previous try. Also drop
    # the latched mutual-sync "Last" chip so a quick-chirp kickoff
    # doesn't inherit stale ABORTED text from the other modality.
    state.reset_sync_telemetry_peaks(dispatched if dispatched else None)
    state.clear_last_sync_result()
    # Push the sync_command over WS too so phones on the live transport
    # don't have to wait for the next periodic heartbeat tick to pick it up.
    # The pending flag still exists as the authoritative one-shot drain path.
    pending_ids = state.pending_sync_command_ids()
    # Stamp the expected id per cam so the dashboard LED flips red
    # until this specific attempt's id echoes back in a heartbeat.
    for cam, sid in pending_ids.items():
        if cam in dispatched:
            state.set_expected_sync_id([cam], sid)
    ws_messages = {
        cam: {"type": "sync_command", "command": "start", "sync_command_id": sid}
        for cam, sid in pending_ids.items()
        if cam in dispatched
    }
    if ws_messages:
        await device_ws.broadcast(ws_messages)
    if is_form:
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "dispatched_to": dispatched}


@app.post("/sync/claim")
def sync_claim() -> dict[str, Any]:
    """Claim the currently-live legacy chirp sync id, minting a fresh one
    when the prior listening window expired.

    Used by the phone-local 時間校正 button so both phones can converge on
    the same shared `sync_id` even when the operator taps them a few
    seconds apart instead of using the dashboard-remote trigger."""
    intent = state.claim_time_sync_intent()
    return {
        "ok": True,
        "sync_id": intent.id,
        "started_at": intent.started_at,
        "expires_at": intent.expires_at,
    }


@app.post("/sync/log")
async def sync_log_post(body: SyncLogBody) -> dict[str, Any]:
    """Phone-pushed diagnostic event. Phones call this at each major step
    of the mutual-sync flow (entering state, chirp emit, band fired,
    complete, aborted) so the dashboard can reconstruct the full
    A/B/server timeline in one place."""
    state.log_sync_event(
        source=body.camera_id, event=body.event, detail=body.detail
    )
    return {"ok": True}


# Matches the `Session.id` schema regex — keeps the path parameter from
# accepting anything that could traverse out of the data dir via glob().
_SESSION_ID_RE = re.compile(r"^s_[0-9a-f]{4,32}$")


@app.post("/sessions/{session_id}/delete")
async def sessions_delete(request: Request, session_id: str):
    """Remove a past session's pitches, results, and videos from memory
    and disk. HTML callers (dashboard ✕ button) always get a 303 back to
    the dashboard so the list visibly shrinks. JSON callers get 404 for
    unknown sessions and 409 when the session is still armed."""
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


@app.post("/sessions/{session_id}/trash")
async def sessions_trash(request: Request, session_id: str):
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


@app.post("/sessions/{session_id}/restore")
async def sessions_restore(request: Request, session_id: str):
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


@app.post("/sessions/{session_id}/cancel_processing")
async def sessions_cancel_processing(request: Request, session_id: str):
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    canceled = state.cancel_processing(session_id)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if not canceled:
        raise HTTPException(status_code=409, detail="no cancelable processing")
    return {"ok": True, "session_id": session_id}


@app.post("/sessions/{session_id}/resume_processing")
async def sessions_resume_processing(request: Request, session_id: str):
    if not _SESSION_ID_RE.match(session_id):
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=422, detail="invalid session_id")
    queued = state.resume_processing(session_id)
    if not queued:
        if _wants_html(request):
            return RedirectResponse("/", status_code=303)
        raise HTTPException(status_code=409, detail="no resumable processing")
    for clip_path, pitch in queued:
        asyncio.create_task(_run_server_detection(clip_path, pitch))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {
        "ok": True,
        "session_id": session_id,
        "queued": len(queued),
    }


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


@app.post("/pitch")
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

    Requests with neither a video nor a non-empty `frames` list return 422:
    there's no way to triangulate off nothing. Requests without a time-sync
    anchor skip detection+triangulation and surface `error="no time sync"`.
    """
    # Fail fast on oversize bodies when the client advertises Content-Length.
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

    # Phase 1 of the iOS-decoupling refactor: calibration DB
    # (`data/calibrations/<camera_id>.json`, populated via POST /calibration)
    # is the single source of truth for per-camera intrinsics, homography,
    # and image dims. iPhones no longer echo these on every /pitch upload.
    # If any field is missing we fill it from the cached snapshot so all
    # downstream code (detection scaling, triangulation, on-disk pitch JSON
    # persistence, scale_pitch_to_video_dims) stays unchanged. No cached
    # snapshot ⇒ hard 422: the new contract is "calibrate before you pitch".
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
    # Either stream counts as "data the server can work with": `frames`
    # from mode-two (iOS detection, authoritative for its session) or
    # `frames_on_device` from a degraded-dual upload (dual-mode cycle
    # where the MOV writer failed but the on-device detector still
    # produced a frame list). Both land in the triangulation pipeline.
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

        # Reconcile image dims: the iPhone's IntrinsicsStore sometimes
        # ships calibration-time dims (e.g. 1920×1080) even when the MOV
        # encoder produced a lower resolution (720p). Server
        # detection then returns px/py in MOV-pixel coords while the
        # payload claims the MOV is 1080p — downstream scaling ends up
        # 1.5× off. Probe the real MOV dims once and overwrite the
        # payload fields so every downstream consumer (triangulation
        # rescale, viewer virtual-cam canvas) speaks the same grid.
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

        # Early-surface: do NOT block on detect_pitch (8-20 s). Record the
        # payload with frames=[] so the dashboard + viewer + on-device
        # triangulation see this cycle immediately, then schedule the
        # server detection as a background task that will re-record the
        # pitch once frames are filled.
        payload_obj.frames = []
        payload_obj.frames_server_post = []
        if (
            payload_obj.sync_anchor_timestamp_s is not None
            and DetectionPath.server_post in payload_paths
        ):
            detection_pending = True
    else:
        # Mode-two: iPhone already detected; we trust the frames list and
        # only run pairing + triangulation. No disk write, no annotated
        # clip — the viewer for this session will fall back to the
        # per-frame trace from the payload JSON.
        if payload_obj.sync_anchor_timestamp_s is None:
            # Anchor missing ⇒ the session can't pair no matter what the
            # frames say; drop them so downstream counts stay honest.
            payload_obj.frames = []
            payload_obj.frames_ios_post = []
            payload_obj.frames_live = []
            payload_obj.frames_server_post = []
        elif payload_obj.frames and not payload_obj.frames_ios_post and DetectionPath.server_post not in payload_paths:
            payload_obj.frames_ios_post = list(payload_obj.frames)

    result = await asyncio.to_thread(state.record, payload_obj)
    if payload_obj.sync_anchor_timestamp_s is None and result.error is None:
        result.error = "no time sync"

    if detection_pending and clip_path is not None:
        state.mark_server_post_queued(payload_obj.session_id, payload_obj.camera_id)
        # Background task: runs AFTER the response body is sent, so the
        # dashboard's /events + /viewer can surface the on-device trace
        # and session row immediately without waiting on the 8-20 s
        # server detection.
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


@app.post("/pitch_analysis")
async def pitch_analysis(payload: PitchAnalysisPayload) -> dict[str, Any]:
    """Attach a late on-device post-pass analysis to an already-recorded pitch.

    This is the PR61 second leg: raw capture arrives first, then iOS decodes
    its finalized local MOV and uploads the authoritative on-device frame list
    later. Dashboard/viewer state updates immediately once the merge lands."""
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


@app.get("/chirp.wav")
def chirp_wav() -> Response:
    """Reference sync chirp for the 時間校正 step.

    Users download this on any device (browser) and play it near the two
    iPhones. Each phone's AudioChirpDetector runs matched filtering and
    pins the session-clock PTS of the peak as the per-cycle anchor.

    Signal: linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, surrounded by
    0.5 s of silence either side so the phones can catch it mid-stream.
    """
    return Response(
        content=chirp_wav_bytes(),
        media_type="audio/wav",
        headers={"Content-Disposition": 'inline; filename="chirp.wav"'},
    )


# ---------------------------------------------------------------------------
# Live preview (Phase 4a)
# ---------------------------------------------------------------------------

# Camera-id pattern mirrors the one on PitchPayload / ws_device route. Path
# params don't go through Pydantic so we validate here to avoid storing a
# preview keyed by an arbitrary client-chosen string.
_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _validate_camera_id_or_422(camera_id: str) -> None:
    if not _CAMERA_ID_RE.match(camera_id):
        raise HTTPException(status_code=422, detail="invalid camera_id")


@app.post("/camera/{camera_id}/calibration_frame")
async def camera_calibration_frame(camera_id: str, request: Request) -> dict[str, Any]:
    """iPhone pushes ONE full-resolution JPEG (native capture res, e.g.
    1920×1080) here in response to `calibration_frame_requested: true`
    on its last WS settings payload. Server stashes it so the next
    `/calibration/auto/{camera_id}` call consumes it — running ArUco at
    native resolution gives 4x the corner-precision of a 480p preview
    frame and keeps the derived intrinsics in the same pixel coord
    system as the MOVs triangulation will consume later. Eliminates the
    preview-vs-capture dims-mismatch class of bugs at the source.

    Accepts raw `image/jpeg` body or multipart with `file` field. 8 MB
    cap (native 1080p @ q=0.9 is ~500 KB; 8 MB leaves room for ChArUco
    board captures from iPhone main cam if we ever support that).
    """
    _validate_camera_id_or_422(camera_id)
    if not state.is_calibration_frame_requested(camera_id):
        raise HTTPException(
            status_code=409,
            detail="calibration frame not requested for this camera",
        )
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        file_field = form.get("file")
        if file_field is None or not hasattr(file_field, "read"):
            raise HTTPException(status_code=422, detail="missing `file` part")
        body = await file_field.read()
    else:
        body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="empty body")
    if len(body) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="calibration frame too large")
    state.store_calibration_frame(camera_id, bytes(body))
    return {"ok": True, "bytes": len(body)}


@app.post("/camera/{camera_id}/preview_frame")
async def camera_preview_frame(camera_id: str, request: Request) -> dict[str, Any]:
    """iPhone pushes one JPEG frame here while the dashboard is watching.

    Accepts either raw `image/jpeg` body or multipart with a `file` field.
    Rejected (409) when the dashboard hasn't requested preview for this
    camera — phones shouldn't waste bandwidth on frames nobody sees.
    Oversize frames (> 2 MB) get 413.
    """
    _validate_camera_id_or_422(camera_id)
    if not state._preview.is_requested(camera_id):
        raise HTTPException(status_code=409, detail="preview not requested")
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/"):
        form = await request.form()
        file_field = form.get("file")
        if file_field is None or not hasattr(file_field, "read"):
            raise HTTPException(status_code=422, detail="missing `file` part")
        body = await file_field.read()
    else:
        body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="empty body")
    ok = state._preview.push(camera_id, bytes(body), ts=time.time())
    if not ok:
        raise HTTPException(status_code=413, detail="preview frame too large")
    return {"ok": True, "bytes": len(body)}


@app.get("/camera/{camera_id}/preview")
def camera_preview_latest(camera_id: str) -> Response:
    """Return the most recently pushed JPEG as an `image/jpeg` response.

    404 when the buffer has no frame for this camera (either preview was
    never requested, the phone hasn't started pushing yet, or the TTL
    lapsed and the buffer was swept).
    """
    _validate_camera_id_or_422(camera_id)
    got = state._preview.latest(camera_id, max_age_s=_PREVIEW_FRAME_MAX_AGE_S)
    if got is None:
        raise HTTPException(status_code=404, detail="no preview frame")
    jpeg_bytes, _ = got
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={
            # Each preview fetch must hit the buffer; intermediate caches
            # would defeat the "latest frame" semantics.
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/camera/{camera_id}/preview.mjpeg")
def camera_preview_mjpeg(camera_id: str) -> Response:
    """Multipart/x-mixed-replace MJPEG stream.

    Polls the buffer at ~10 fps. Re-hits `is_requested()` each tick so
    the generator exits when the dashboard's TTL lapses — no dangling
    iterator keeps the phone pushing after the viewer closes. Client
    disconnect (browser closes the `<img>`) surfaces as a GeneratorExit
    out of the `yield` and we bail cleanly.
    """
    _validate_camera_id_or_422(camera_id)
    boundary = "ballpreviewframe"

    def stream():
        last_ts: float | None = None
        # Dashboard TTL is 5 s; no-frame waits beyond that indicate the
        # viewer gave up. The is_requested() lazy-sweep path also terminates
        # the stream when its TTL lapses.
        idle_deadline: float | None = None
        tick_s = 1.0 / 10.0
        try:
            while True:
                if not state._preview.is_requested(camera_id):
                    break
                got = state._preview.latest(camera_id, max_age_s=_PREVIEW_FRAME_MAX_AGE_S)
                now = time.time()
                if got is not None:
                    jpeg_bytes, ts = got
                    if ts != last_ts:
                        last_ts = ts
                        idle_deadline = None
                        header = (
                            f"--{boundary}\r\n"
                            f"Content-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg_bytes)}\r\n\r\n"
                        ).encode()
                        yield header + jpeg_bytes + b"\r\n"
                    else:
                        # No new frame this tick — keep stream alive.
                        if idle_deadline is None:
                            idle_deadline = now + 10.0
                        elif now > idle_deadline:
                            break
                else:
                    if idle_deadline is None:
                        idle_deadline = now + 10.0
                    elif now > idle_deadline:
                        break
                time.sleep(tick_s)
        except GeneratorExit:
            return

    return Response(
        stream(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/camera/{camera_id}/preview_request")
async def camera_preview_request(
    camera_id: str,
    request: Request,
    enabled: str | None = Form(default=None),
) -> Response:
    """Dashboard toggle. Refreshes the per-camera TTL when enabled=true;
    clears the flag + cached frame on enabled=false.

    Accepts both form submission (legacy `<form>` fallback) and JSON
    `{enabled: bool}` so the dashboard JS can POST without a hidden form.
    Form callers get a 303 back to `/`; JSON callers get `{ok, enabled}`.
    """
    _validate_camera_id_or_422(camera_id)
    # Coerce value from either form or JSON body. An empty/absent field
    # means "toggle on" (defensive — the dashboard always sends explicit).
    raw: Any = enabled
    if raw is None:
        # Try JSON body. Empty / non-JSON bodies fall through to default False.
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = body.get("enabled")
        except Exception:
            raw = None
    # Normalise to bool. "false"/"0"/"" → False; anything else truthy → True.
    if isinstance(raw, bool):
        flag = raw
    elif isinstance(raw, str):
        flag = raw.strip().lower() not in ("", "false", "0", "off", "no")
    elif raw is None:
        flag = True
    else:
        flag = bool(raw)
    state._preview.request(camera_id, enabled=flag)
    await device_ws.send(camera_id, _settings_message_for(camera_id))
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    import json as _stdjson
    return Response(
        _stdjson.dumps({"ok": True, "enabled": flag}),
        media_type="application/json",
    )


@app.get("/results/latest")
def results_latest() -> SessionResult:
    r = state.latest()
    if r is None:
        raise HTTPException(404, "no results yet")
    return r


@app.get("/results/{session_id}")
def results_for_session(session_id: str) -> SessionResult:
    r = state.get(session_id)
    if r is None:
        raise HTTPException(404, f"session {session_id} not found")
    return r


def _find_clip_on_disk(session_id: str, camera_id: str) -> Path | None:
    """Locate the raw MOV for a given (session, cam) pair, skipping the
    `_annotated` sibling. Returns None if no raw clip exists."""
    for path in state.video_dir.glob(f"session_{session_id}_{camera_id}.*"):
        if path.stem.endswith("_annotated"):
            continue
        return path
    return None


def _scene_for_session(session_id: str):
    """Shared fetch+build for the two scene endpoints. Raises 404 when no
    pitches have been received for this session yet.

    Rescales intrinsics/homography into each pitch's MOV pixel grid the
    same way `_triangulate_pair` does, so the virtual-camera canvas in
    the viewer reprojects using the grid the rays were triangulated in —
    otherwise reprojection overlay drifts by the calibration/MOV ratio
    whenever the two differ (e.g. calibration @ 1080p, MOV @ 720p).
    """
    # Local imports so the FastAPI app still boots when plotly is missing
    # (the JSON endpoint doesn't need it; the HTML one will surface a 500).
    from reconstruct import build_scene

    pitches = state.pitches_for_session(session_id)
    if not pitches:
        raise HTTPException(404, f"session {session_id} has no pitches")
    calibrations = state.calibrations()
    # Retrofit MOV-dim reconciliation for sessions captured before the
    # /pitch handler started probing on ingest. The payload may claim
    # 1920×1080 while the MOV was actually encoded at 720p; without
    # fixing this the viewer's virtual canvas scales server-detected
    # px/py by the wrong denominator and the ball dot lands elsewhere.
    scaled_pitches = {}
    for cam, pitch in pitches.items():
        clip = _find_clip_on_disk(session_id, cam)
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
            if cam in calibrations else None,
        )
    result = state.get(session_id)
    triangulated = result.points if result is not None else []
    triangulated_on_device = result.points_on_device if result is not None else []
    return build_scene(
        session_id, scaled_pitches, triangulated,
        triangulated_on_device=triangulated_on_device,
    )


@app.get("/reconstruction/{session_id}")
def reconstruction(session_id: str) -> dict[str, Any]:
    scene = _scene_for_session(session_id)
    return scene.to_dict()


@app.get("/viewer/{session_id}", response_class=HTMLResponse)
def viewer(session_id: str) -> HTMLResponse:
    from render_scene import render_viewer_html

    scene = _scene_for_session(session_id)
    videos_with_offsets = _videos_for_session(session_id)
    health = _build_viewer_health(session_id)
    return HTMLResponse(render_viewer_html(scene, videos_with_offsets, health))


def _build_viewer_health(session_id: str) -> dict[str, Any]:
    """Per-camera diagnostic snapshot shown at the top of /viewer/{sid}.

    The viewer is a post-mortem tool — the operator wants to know at a
    glance what actually happened during this session. `render_viewer_html`
    turns each field into a chip/row so a failure mode ("B never uploaded",
    "no time sync", "triangulation skipped") reads immediately, without the
    user having to infer it from an empty 3D scene."""
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

    # Duration must NOT span raw `timestamp_s` across pitches — each
    # iPhone's session clock has its own epoch (seconds since that device
    # booted), so `max(all) - min(all)` mixes two independent clocks and
    # yields absurd "6-hour" durations when A and B booted hours apart.
    # Prefer the triangulated-points `t_rel_s` range (already in the
    # anchor-relative clock, same one `/events` uses). Fall back to the
    # max per-pitch anchor-relative span for partial (single-cam / untri-
    # angulated) sessions.
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

    # Infer capture mode — same rule as events(): MOV + frames_on_device =
    # dual; MOV alone = camera_only; frames_on_device alone (no MOV) =
    # on_device. Previously this path only checked MOV presence, so any
    # dual session surfaced in the viewer hero as "camera-only" even
    # though the scene had on-device rays overlaid.
    has_any_video = any(state.video_dir.glob(f"session_{session_id}_*"))
    has_any_on_device_frames = any(
        bool(p.frames_on_device) for p in pitches.values()
    )
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
        # On-device count shown beside the main server count in dual mode
        # so the operator can see the two streams' yields side by side
        # instead of having to infer from legend counts later.
        "triangulated_count_on_device": (
            len(result.points_on_device) if result is not None else 0
        ),
        "error": result.error if result is not None else None,
        "duration_s": duration_s,
        "received_at": latest_mtime,
        "mode": mode,
    }


# Allowed filenames under /videos. Either the raw clip (`session_<sid>_<cam>.<ext>`)
# or its annotated sibling with a `_annotated` suffix — both produced by /pitch.
_VIDEO_FILENAME_RE = re.compile(
    r"^session_s_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}(_annotated)?\.(mov|mp4|m4v)$"
)


def _videos_for_session(
    session_id: str,
) -> list[tuple[str, str, float, float, dict[str, list]]]:
    """Return `[(camera_id, "/videos/<filename>", t_rel_offset_s, video_fps, frames), ...]`
    sorted by camera_id. Prefers the `_annotated` clip when present (the
    one with detection circles drawn) and falls back to the raw MOV.

    `t_rel_offset_s = video_start_pts_s − sync_anchor_timestamp_s` is the
    amount of anchor-relative time that had already elapsed when the
    clip's first frame was captured. The viewer seeks each camera's
    video by `currentTime = t_rel − offset`, which keeps A and B locked
    to the shared chirp anchor regardless of how different their arm-to-
    first-frame latency was.

    `video_fps` is the per-camera nominal capture rate (240.0 in the
    default rig) — kept for display, but the viewer's timeline uses real
    per-frame PTS, not the nominal grid.

    `frames = {"t_rel_s": [...], "detected": [...]}` ships the actual
    post-detection frame timeline to the browser so the viewer's scrubber
    can step real MOV frames (including drops + non-detected frames)
    instead of synthesising a virtual 240 Hz grid that doesn't match the
    decoded video. Both arrays have one entry per decoded frame."""
    prefix = f"session_{session_id}_"
    pitches = state.pitches_for_session(session_id)

    # Pick one filename per camera — prefer the annotated version.
    best: dict[str, str] = {}
    for path in sorted(state.video_dir.glob(f"{prefix}*")):
        name = path.name
        if not _VIDEO_FILENAME_RE.match(name):
            continue
        stem = name.rsplit(".", 1)[0]  # "session_<sid>_<cam>" or "..._annotated"
        is_annotated = stem.endswith("_annotated")
        cam = stem[len(prefix):]
        if is_annotated:
            cam = cam[: -len("_annotated")]
        # Annotated beats raw for the same camera.
        if cam not in best or (is_annotated and "_annotated" not in best[cam]):
            best[cam] = name

    out: list[tuple[str, str, float, float, dict[str, list]]] = []
    # Include all cameras that either have a video file OR have either
    # detection stream on the payload (mode on_device has no MOV but still
    # needs a VIDEO_META entry so the detection strip in the viewer is
    # populated; dual mode carries both streams).
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
            offset = float(
                pitch.video_start_pts_s - pitch.sync_anchor_timestamp_s
            )
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
        # Ship both detection streams so the viewer can render two
        # parallel density strips and overlay the dual-mode rays. Legacy
        # `t_rel_s`/`detected` keys preserved for backwards compatibility;
        # `on_device` sub-dict is empty for mono-mode sessions.
        # `px`/`py` power the virtual-camera canvas: at playback time T,
        # find the nearest detection and draw a dot at (px, py) — that
        # IS literally what this camera saw at that moment (the
        # camera's own ray collapses to a point on its own image plane).
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


@app.get("/videos/{filename}")
def serve_video(filename: str) -> FileResponse:
    """Stream the on-disk MOV/MP4 clip for the viewer page. FastAPI's
    FileResponse honours Range requests so browsers can seek without
    downloading the entire file upfront."""
    if not _VIDEO_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="not found")
    path = state.video_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


@app.get("/events")
def events(bucket: str = "active") -> list[dict[str, Any]]:
    if bucket not in {"active", "trash"}:
        raise HTTPException(status_code=422, detail="bucket must be 'active' or 'trash'")
    return state.events(bucket=bucket)


@app.post("/calibration")
async def post_calibration(snapshot: CalibrationSnapshot) -> dict[str, Any]:
    """iPhone pushes its freshly-solved calibration (intrinsics + homography)
    so the dashboard canvas can show where the camera is positioned in world
    space, even before the first pitch is ever recorded. Idempotent overwrite:
    each camera only keeps its latest snapshot."""
    state.set_calibration(snapshot)
    # Notify dashboards (SSE) and any other connected phones (WS) that the
    # calibration for this camera changed. Dashboard can repaint the canvas
    # without its 5s polling tick; the other phone can refresh its stored
    # extrinsics if it cares about cross-cam pose consistency.
    await sse_hub.broadcast(
        "calibration_changed",
        {
            "cam": snapshot.camera_id,
            "image_width_px": snapshot.image_width_px,
            "image_height_px": snapshot.image_height_px,
        },
    )
    await device_ws.broadcast(
        {
            cam: {"type": "calibration_updated", "cam": snapshot.camera_id}
            for cam in state.known_camera_ids()
            if cam != snapshot.camera_id
        }
    )
    return {
        "ok": True,
        "camera_id": snapshot.camera_id,
        "image_width_px": snapshot.image_width_px,
        "image_height_px": snapshot.image_height_px,
    }


@app.get("/calibration/state")
def calibration_state() -> dict[str, Any]:
    """Dashboard polls this to repaint the canvas whenever a new calibration
    lands. Returns both the raw scene (so callers can rebuild custom views)
    and a ready-to-`Plotly.react` figure spec — the dashboard uses the
    latter so the trace/layout construction stays centralised server-side
    and the browser only speaks figure JSON."""
    from reconstruct import build_calibration_scene
    from render_scene import _build_figure

    cals = state.calibrations()
    scene = build_calibration_scene(cals)
    # `fig.to_plotly_json()` can leak numpy arrays into the payload; the
    # JSON round-trip guarantees everything is native-Python and FastAPI
    # can serialise it without reaching for a custom encoder.
    fig = _build_figure(scene)
    # Mirror render_events_index_html's dashboard-specific layout overrides
    # so the 5 s tick can't undo them: drop the duplicate title, fill the
    # panel, and pin a fixed rig-scale bbox so a single 3m-distant camera
    # can't shrink the 0.5 m plate via aspectmode="data" autoscaling.
    fig.update_layout(
        title=None, margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-6.0, 6.0],
        scene_yaxis_range=[-6.0, 6.0],
        scene_zaxis_range=[-0.2, 3.5],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
        # Match the value used by render_events_index_html SSR so the tick
        # never flips uirevision and Plotly keeps the user's camera/orbit.
        scene_uirevision="dashboard-canvas",
    )
    fig_json = json.loads(fig.to_json())
    def _cal_mtime(cam_id: str) -> float | None:
        p = state._calibration_path(cam_id)
        try:
            return p.stat().st_mtime
        except OSError:
            return None
    return {
        "calibrations": [
            {
                "camera_id": cam_id,
                "image_width_px": snap.image_width_px,
                "image_height_px": snap.image_height_px,
                "last_ts": _cal_mtime(cam_id),
            }
            for cam_id, snap in sorted(cals.items())
        ],
        "scene": scene.to_dict(),
        "plot": {
            "data": fig_json.get("data", []),
            "layout": fig_json.get("layout", {}),
        },
    }


async def _await_calibration_frame(camera_id: str, *, timeout_s: float = 2.0) -> bytes:
    import asyncio as _asyncio  # noqa: WPS433

    state.request_calibration_frame(camera_id)
    await device_ws.send(camera_id, _settings_message_for(camera_id))
    loops = max(1, int(round(timeout_s / 0.1)))
    for _ in range(loops):
        got = state.consume_calibration_frame(camera_id)
        if got is not None:
            jpeg_bytes, _ts = got
            return jpeg_bytes
        await _asyncio.sleep(0.1)
    raise HTTPException(
        status_code=408,
        detail=(
            f"camera {camera_id!r} did not deliver a calibration "
            f"frame within {timeout_s:.0f} s — check the phone is online and awake"
        ),
    )


def _decode_calibration_jpeg(jpeg_bytes: bytes) -> np.ndarray:
    import cv2  # noqa: WPS433

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise HTTPException(status_code=422, detail="calibration frame is not a decodable JPEG")
    return bgr


def _marker_camera_pose(snapshot: CalibrationSnapshot) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    K = build_K(
        snapshot.intrinsics.fx,
        snapshot.intrinsics.fz,
        snapshot.intrinsics.cx,
        snapshot.intrinsics.cy,
    )
    H = np.asarray(snapshot.homography, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H)
    C_world = camera_center_world(R_wc, t_wc)
    return K, R_wc, C_world


def _all_marker_world_xyz() -> dict[int, tuple[float, float, float]]:
    world_xyz = {
        mid: (float(xy[0]), float(xy[1]), 0.0)
        for mid, xy in PLATE_MARKER_WORLD.items()
    }
    for rec in state._marker_registry.all_records():
        world_xyz[rec.marker_id] = (rec.x_m, rec.y_m, rec.z_m)
    return world_xyz


def _residual_bucket(residual_m: float) -> str:
    if residual_m <= 0.01:
        return "excellent"
    if residual_m <= 0.03:
        return "good"
    if residual_m <= 0.06:
        return "warn"
    return "poor"


def _triangulate_marker_candidates(
    *,
    camera_a_id: str,
    camera_b_id: str,
    bgr_a: np.ndarray,
    bgr_b: np.ndarray,
) -> dict[str, Any]:
    snap_a = state.calibrations().get(camera_a_id)
    snap_b = state.calibrations().get(camera_b_id)
    if snap_a is None or snap_b is None:
        missing = [cid for cid, snap in ((camera_a_id, snap_a), (camera_b_id, snap_b)) if snap is None]
        raise HTTPException(
            status_code=422,
            detail=f"missing calibration for camera(s): {', '.join(missing)}",
        )

    K_a, R_a, C_a = _marker_camera_pose(snap_a)
    K_b, R_b, C_b = _marker_camera_pose(snap_b)
    dist_a = np.asarray(snap_a.intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    dist_b = np.asarray(snap_b.intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)

    det_a = {m.id: m for m in detect_all_markers_in_dict(bgr_a)}
    det_b = {m.id: m for m in detect_all_markers_in_dict(bgr_b)}
    existing = {rec.marker_id: rec for rec in state._marker_registry.all_records()}
    common_ids = sorted(set(det_a.keys()) & set(det_b.keys()))
    candidates: list[dict[str, Any]] = []
    for marker_id in common_ids:
        if marker_id in PLATE_MARKER_WORLD:
            continue
        centroid_a = det_a[marker_id].corners.mean(axis=0)
        centroid_b = det_b[marker_id].corners.mean(axis=0)
        ray_a_cam = undistorted_ray_cam(float(centroid_a[0]), float(centroid_a[1]), K_a, dist_a)
        ray_b_cam = undistorted_ray_cam(float(centroid_b[0]), float(centroid_b[1]), K_b, dist_b)
        ray_a_world = R_a.T @ ray_a_cam
        ray_b_world = R_b.T @ ray_b_cam
        point, gap = triangulate_rays(C_a, ray_a_world, C_b, ray_b_world)
        if point is None:
            continue
        existing_rec = existing.get(int(marker_id))
        delta_existing_m = None
        if existing_rec is not None:
            delta_existing_m = float(
                np.linalg.norm(
                    np.array([existing_rec.x_m, existing_rec.y_m, existing_rec.z_m], dtype=np.float64)
                    - point
                )
            )
        candidates.append(
            {
                "marker_id": int(marker_id),
                "x_m": float(point[0]),
                "y_m": float(point[1]),
                "z_m": float(point[2]),
                "residual_m": float(gap),
                "residual_bucket": _residual_bucket(float(gap)),
                "source_camera_ids": [camera_a_id, camera_b_id],
                "suggest_on_plate_plane": abs(float(point[2])) <= 0.03,
                "detected_in": [camera_a_id, camera_b_id],
                "existing_marker": existing_rec is not None,
                "existing_label": existing_rec.label if existing_rec is not None else None,
                "existing_on_plate_plane": existing_rec.on_plate_plane if existing_rec is not None else None,
                "delta_existing_m": delta_existing_m,
                "update_action": (
                    "keep"
                    if existing_rec is None
                    else ("refresh" if delta_existing_m is not None and delta_existing_m <= 0.03 else "conflict")
                ),
            }
        )
    only_a_ids = sorted(
        mid for mid in det_a.keys() - det_b.keys()
        if mid not in PLATE_MARKER_WORLD
    )
    only_b_ids = sorted(
        mid for mid in det_b.keys() - det_a.keys()
        if mid not in PLATE_MARKER_WORLD
    )
    return {
        "candidates": candidates,
        "visibility": {
            "shared_ids": [row["marker_id"] for row in candidates],
            "camera_a_only_ids": only_a_ids,
            "camera_b_only_ids": only_b_ids,
            "camera_a_detected_ids": sorted(det_a.keys()),
            "camera_b_detected_ids": sorted(det_b.keys()),
        },
    }


def _solve_pnp_homography(
    detected: list[Any],
    *,
    intrinsics: IntrinsicsPayload,
    image_size: tuple[int, int],
) -> tuple[list[float], list[int]] | None:
    import cv2  # noqa: WPS433

    world_xyz = _all_marker_world_xyz()
    markers_by_id = {m.id: m for m in detected if m.id in world_xyz}
    detected_ids = sorted(markers_by_id.keys())
    if len(detected_ids) < 4:
        return None
    object_pts = np.array([world_xyz[mid] for mid in detected_ids], dtype=np.float64)
    image_pts = np.array(
        [markers_by_id[mid].corners.mean(axis=0) for mid in detected_ids],
        dtype=np.float64,
    )
    if np.linalg.matrix_rank(object_pts - object_pts.mean(axis=0, keepdims=True)) < 3:
        return None
    K = build_K(intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy).astype(np.float64)
    dist = np.asarray(intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    ok, rvec, tvec, inliers = cv2.solvePnPRansac(
        object_pts,
        image_pts,
        K,
        dist,
        flags=cv2.SOLVEPNP_ITERATIVE,
        reprojectionError=4.0,
        iterationsCount=200,
        confidence=0.995,
    )
    if not ok:
        return None
    try:
        rvec, tvec = cv2.solvePnPRefineLM(object_pts, image_pts, K, dist, rvec, tvec)
    except Exception:
        pass
    R_wc, _ = cv2.Rodrigues(rvec)
    H = K @ np.column_stack([R_wc[:, 0], R_wc[:, 1], tvec.reshape(3)])
    if abs(H[2, 2]) < 1e-12:
        return None
    H = H / H[2, 2]
    inlier_ids = (
        [detected_ids[int(i)] for i in inliers.flatten().tolist()]
        if inliers is not None
        else detected_ids
    )
    return H.flatten().tolist(), inlier_ids


def _derive_auto_cal_intrinsics(
    camera_id: str,
    *,
    w_img: int,
    h_img: int,
    h_fov_deg: float | None = None,
) -> tuple[IntrinsicsPayload, CalibrationSnapshot | None]:
    prior = state.calibrations().get(camera_id)
    if prior is not None and h_fov_deg is None:
        prior_w = prior.image_width_px
        prior_h = prior.image_height_px
        if prior_w > 0 and prior_h > 0:
            sx = w_img / prior_w
            sy = h_img / prior_h
            intrinsics = IntrinsicsPayload(
                fx=prior.intrinsics.fx * sx,
                fz=prior.intrinsics.fz * sy,
                cx=prior.intrinsics.cx * sx,
                cy=prior.intrinsics.cy * sy,
                distortion=prior.intrinsics.distortion,
            )
            return intrinsics, prior
        prior = None
    h_fov_rad = float(np.radians(h_fov_deg)) if h_fov_deg is not None else 1.1345
    fx, fy, cx, cy = derive_fov_intrinsics(w_img, h_img, h_fov_rad)
    return IntrinsicsPayload(fx=fx, fz=fy, cx=cx, cy=cy), None


def _solve_auto_cal_solution(
    detected: list[Any],
    *,
    intrinsics: IntrinsicsPayload,
    image_size: tuple[int, int],
) -> tuple[Any | None, str, list[int]]:
    world_map: dict[int, tuple[float, float]] = dict(PLATE_MARKER_WORLD)
    world_map.update(state._marker_registry.planar_world_map())
    planar = solve_homography_from_world_map(
        detected, world_map, image_size=image_size
    )
    pnp_solution = _solve_pnp_homography(
        detected, intrinsics=intrinsics, image_size=image_size
    )
    if pnp_solution is None:
        return planar, "planar_homography", []
    H_pnp, pnp_detected_ids = pnp_solution
    if planar is None:
        from calibration_solver import CalibrationSolveResult

        planar = CalibrationSolveResult(
            homography_row_major=H_pnp,
            detected_ids=sorted(pnp_detected_ids),
            missing_ids=[i for i in sorted(PLATE_MARKER_WORLD.keys()) if i not in pnp_detected_ids],
            image_width_px=int(image_size[0]),
            image_height_px=int(image_size[1]),
        )
    else:
        planar = planar.__class__(
            homography_row_major=H_pnp,
            detected_ids=sorted(set(planar.detected_ids) | set(pnp_detected_ids)),
            missing_ids=planar.missing_ids,
            image_width_px=planar.image_width_px,
            image_height_px=planar.image_height_px,
        )
    return planar, "pnp_pose", pnp_detected_ids


def _pose_from_homography(
    intrinsics: IntrinsicsPayload,
    homography_row_major: list[float],
) -> tuple[np.ndarray, np.ndarray]:
    K = build_K(intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy)
    H_mat = np.array(homography_row_major, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H_mat)
    center = camera_center_world(R_wc, t_wc)
    forward = R_wc.T @ np.array([0.0, 0.0, 1.0], dtype=np.float64)
    forward = forward / np.linalg.norm(forward)
    return center, forward


def _reprojection_error_px(
    intrinsics: IntrinsicsPayload,
    homography_row_major: list[float],
    detected: list[Any],
) -> float | None:
    import cv2  # noqa: WPS433

    world_xyz = _all_marker_world_xyz()
    K = build_K(
        intrinsics.fx, intrinsics.fz, intrinsics.cx, intrinsics.cy
    ).astype(np.float64)
    H_mat = np.array(homography_row_major, dtype=np.float64).reshape(3, 3)
    R_wc, t_wc = recover_extrinsics(K, H_mat)
    rvec, _ = cv2.Rodrigues(R_wc)
    dist = np.asarray(intrinsics.distortion or [0, 0, 0, 0, 0], dtype=np.float64)
    errs: list[float] = []
    for m in detected:
        if m.id not in world_xyz:
            continue
        obj = np.array([[world_xyz[m.id]]], dtype=np.float64)
        proj, _ = cv2.projectPoints(obj, rvec, t_wc.reshape(3, 1), K, dist)
        pred = proj.reshape(2)
        obs = m.corners.mean(axis=0)
        errs.append(float(np.linalg.norm(pred - obs)))
    return float(np.mean(errs)) if errs else None


async def _run_auto_calibration(
    camera_id: str,
    *,
    h_fov_deg: float | None = None,
    track_run: bool = False,
) -> dict[str, Any]:
    import cv2  # noqa: WPS433
    import asyncio as _asyncio  # noqa: WPS433
    from calibration_solver import DetectedMarker

    max_frames = 10
    burst_deadline = _asyncio.get_event_loop().time() + 6.0
    frames_seen = 0
    good_frames = 0
    stable_frames = 0
    # Bail quickly when the view has nothing to solve against — the full
    # 6 s / 10-frame burst is only useful once at least one known marker
    # has appeared. Two consecutive empty frames is a strong enough signal
    # that the phone isn't pointed at the plate.
    consecutive_empty_frames = 0
    first_shape: tuple[int, int] | None = None
    intrinsics: IntrinsicsPayload | None = None
    prior: CalibrationSnapshot | None = None
    aggregated_corners: dict[int, list[np.ndarray]] = {}
    recent_centers: list[np.ndarray] = []
    recent_forwards: list[np.ndarray] = []
    recent_errors: list[float] = []

    if track_run:
        state.update_auto_cal_run(
            camera_id,
            status="searching",
            summary="Requesting full-res frame",
        )

    while frames_seen < max_frames and _asyncio.get_event_loop().time() < burst_deadline:
        state.request_calibration_frame(camera_id)
        await device_ws.send(camera_id, _settings_message_for(camera_id))
        got: tuple[bytes, float] | None = None
        for _ in range(20):
            got = state.consume_calibration_frame(camera_id)
            if got is not None:
                break
            await _asyncio.sleep(0.1)
        if got is None:
            break
        jpeg_bytes, _ts = got
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        if first_shape is None:
            first_shape = bgr.shape[:2]
            intrinsics, prior = _derive_auto_cal_intrinsics(
                camera_id,
                w_img=first_shape[1],
                h_img=first_shape[0],
                h_fov_deg=h_fov_deg,
            )
        assert intrinsics is not None
        h_img, w_img = bgr.shape[:2]
        detected = [
            m for m in detect_all_markers_in_dict(bgr)
            if (m.id in PLATE_MARKER_WORLD or state._marker_registry.get(m.id) is not None)
        ]
        frames_seen += 1
        if not detected:
            consecutive_empty_frames += 1
            if track_run:
                state.update_auto_cal_run(
                    camera_id,
                    status="searching",
                    frames_seen=frames_seen,
                    markers_visible=0,
                    summary="Searching full-res frames for known markers",
                )
            if consecutive_empty_frames >= 2:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"no known markers visible on camera {camera_id!r} "
                        "after 2 frames — aim the lens at the plate (IDs 0-5) "
                        "before retrying"
                    ),
                )
            continue
        consecutive_empty_frames = 0
        frame_solution, solver, _pnp_ids = _solve_auto_cal_solution(
            detected,
            intrinsics=intrinsics,
            image_size=(w_img, h_img),
        )
        markers_visible = len(detected)
        if frame_solution is None:
            if track_run:
                state.update_auto_cal_run(
                    camera_id,
                    status="tracking",
                    frames_seen=frames_seen,
                    markers_visible=markers_visible,
                    summary="Tracking markers in full-res frames",
                    detected_ids=sorted(m.id for m in detected),
                )
            continue
        reproj_px = _reprojection_error_px(
            intrinsics, frame_solution.homography_row_major, detected
        )
        center, forward = _pose_from_homography(
            intrinsics, frame_solution.homography_row_major
        )
        recent_centers.append(center)
        recent_forwards.append(forward)
        if reproj_px is not None:
            recent_errors.append(reproj_px)
        if len(recent_centers) > 5:
            recent_centers.pop(0)
            recent_forwards.pop(0)
        if len(recent_errors) > 5:
            recent_errors.pop(0)
        pos_jitter_cm = None
        ang_jitter_deg = None
        if len(recent_centers) >= 3:
            pos_jitter_cm = float(
                np.mean(np.std(np.stack(recent_centers), axis=0)) * 100.0
            )
            dots = [
                float(np.clip(np.dot(recent_forwards[0], v), -1.0, 1.0))
                for v in recent_forwards[1:]
            ]
            if dots:
                ang_jitter_deg = float(np.degrees(np.mean(np.arccos(dots))))
        frame_good = (
            reproj_px is not None
            and reproj_px <= 3.5
            and pos_jitter_cm is not None
            and pos_jitter_cm <= 1.5
            and ang_jitter_deg is not None
            and ang_jitter_deg <= 0.8
        )
        if frame_good:
            good_frames += 1
            stable_frames += 1
        else:
            stable_frames = 0
        if markers_visible >= 4:
            for m in detected:
                aggregated_corners.setdefault(m.id, []).append(m.corners)
        if track_run:
            state.update_auto_cal_run(
                camera_id,
                status="stabilizing" if good_frames > 0 else "tracking",
                frames_seen=frames_seen,
                good_frames=good_frames,
                stable_frames=stable_frames,
                markers_visible=markers_visible,
                solver=solver,
                reprojection_px=reproj_px,
                position_jitter_cm=pos_jitter_cm,
                angle_jitter_deg=ang_jitter_deg,
                summary=(
                    f"Hold camera steady · {stable_frames}/4 stable"
                    if good_frames > 0
                    else "Tracking markers in full-res frames"
                ),
                detected_ids=sorted(m.id for m in detected),
            )
        if stable_frames >= 4 and good_frames >= 4:
            break

    if frames_seen == 0 or first_shape is None or intrinsics is None:
        raise HTTPException(
            status_code=408,
            detail=(
                f"camera {camera_id!r} did not deliver a calibration "
                "frame within 6 s — check the phone is online, awake, "
                "and running the current build"
            ),
        )

    aggregated: list[DetectedMarker] = [
        DetectedMarker(id=mid, corners=np.mean(np.stack(corner_list), axis=0))
        for mid, corner_list in aggregated_corners.items()
    ]
    if track_run:
        state.update_auto_cal_run(
            camera_id,
            status="solving",
            summary="Solving pose from full-res frame burst",
        )
    result, solver, pnp_detected_ids = _solve_auto_cal_solution(
        aggregated,
        intrinsics=intrinsics,
        image_size=(first_shape[1], first_shape[0]),
    )
    if result is None:
        seen_counts = ", ".join(
            f"id{mid}×{len(cs)}" for mid, cs in sorted(aggregated_corners.items())
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "need known markers for calibration "
                f"across {frames_seen} frame(s); got: {seen_counts or '(none)'}"
            ),
        )
    reproj_px = _reprojection_error_px(
        intrinsics, result.homography_row_major, aggregated
    )
    center_new, forward_new = _pose_from_homography(
        intrinsics, result.homography_row_major
    )
    delta_pos_cm = None
    delta_ang_deg = None
    if prior is not None:
        prior_intrinsics, _ = _derive_auto_cal_intrinsics(
            camera_id,
            w_img=first_shape[1],
            h_img=first_shape[0],
            h_fov_deg=None,
        )
        center_old, forward_old = _pose_from_homography(
            prior_intrinsics, prior.homography
        )
        delta_pos_cm = float(np.linalg.norm(center_new - center_old) * 100.0)
        delta_ang_deg = float(
            np.degrees(np.arccos(np.clip(np.dot(forward_new, forward_old), -1.0, 1.0)))
        )
    return {
        "frames_seen": frames_seen,
        "good_frames": good_frames,
        "stable_frames": stable_frames,
        "intrinsics": intrinsics,
        "result": result,
        "solver": solver,
        "reprojection_px": reproj_px,
        "delta_position_cm": delta_pos_cm,
        "delta_angle_deg": delta_ang_deg,
        "pnp_detected_ids": pnp_detected_ids,
    }


@app.post("/calibration/auto/{camera_id}")
async def calibration_auto(
    camera_id: str,
    request: Request,
    h_fov_deg: float | None = None,
) -> dict[str, Any]:
    """Dashboard-triggered auto-calibration.

    Request a one-shot full-resolution JPEG from the phone via the WS settings path,
    poll the buffer for up to 2 s, run ArUco at native capture resolution.
    The snapshot lives in the SAME pixel coord system as the MOVs the
    phone later uploads for triangulation → K doesn't need rescaling at
    pitch time and the preview-vs-capture dims-mismatch class of bugs
    is gone at the source. 408 on no-delivery; no preview fallback.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
        raise HTTPException(status_code=400, detail="invalid camera_id")
    auto = await _run_auto_calibration(camera_id, h_fov_deg=h_fov_deg, track_run=False)
    result = auto["result"]
    intrinsics = auto["intrinsics"]
    frames_seen = auto["frames_seen"]
    snapshot = CalibrationSnapshot(
        camera_id=camera_id,
        intrinsics=intrinsics,
        homography=result.homography_row_major,
        image_width_px=result.image_width_px,
        image_height_px=result.image_height_px,
    )
    state.set_calibration(snapshot)
    n_extended_used = sum(
        1 for mid in result.detected_ids if mid not in PLATE_MARKER_WORLD
    )
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    return {
        "ok": True,
        "camera_id": camera_id,
        "detected_ids": result.detected_ids,
        "missing_plate_ids": result.missing_ids,
        "homography": result.homography_row_major,
        "image_width_px": result.image_width_px,
        "image_height_px": result.image_height_px,
        "n_extended_used": n_extended_used,
        "used_pose_solver": auto["solver"] == "pnp_pose",
        "n_3d_markers_used": sum(
            1 for mid in auto["pnp_detected_ids"]
            if mid not in PLATE_MARKER_WORLD
        ),
        "frames_seen": frames_seen,
        "good_frames": auto["good_frames"],
        "stable_frames": auto["stable_frames"],
        "reprojection_px": auto["reprojection_px"],
        "delta_position_cm": auto["delta_position_cm"],
        "delta_angle_deg": auto["delta_angle_deg"],
    }


@app.post("/calibration/auto/start/{camera_id}")
async def calibration_auto_start(
    camera_id: str,
    h_fov_deg: float | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_id):
        raise HTTPException(status_code=400, detail="invalid camera_id")
    try:
        run = state.start_auto_cal_run(camera_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    async def _runner() -> None:
        try:
            auto = await _run_auto_calibration(camera_id, h_fov_deg=h_fov_deg, track_run=True)
            result = auto["result"]
            snapshot = CalibrationSnapshot(
                camera_id=camera_id,
                intrinsics=auto["intrinsics"],
                homography=result.homography_row_major,
                image_width_px=result.image_width_px,
                image_height_px=result.image_height_px,
            )
            state.set_calibration(snapshot)
            state.finish_auto_cal_run(
                camera_id,
                status="completed",
                applied=True,
                summary="Applied",
                detail=(
                    f"frames={auto['frames_seen']} stable={auto['stable_frames']} "
                    f"reproj={auto['reprojection_px']:.2f}px"
                    if auto["reprojection_px"] is not None
                    else f"frames={auto['frames_seen']} stable={auto['stable_frames']}"
                ),
                result={
                    "frames_seen": auto["frames_seen"],
                    "good_frames": auto["good_frames"],
                    "stable_frames": auto["stable_frames"],
                    "reprojection_px": auto["reprojection_px"],
                    "used_pose_solver": auto["solver"] == "pnp_pose",
                    "delta_position_cm": auto["delta_position_cm"],
                    "delta_angle_deg": auto["delta_angle_deg"],
                    "detected_ids": result.detected_ids,
                },
            )
        except HTTPException as e:
            state.finish_auto_cal_run(
                camera_id,
                status="failed",
                applied=False,
                summary="Failed",
                detail=str(e.detail),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("auto calibration background run failed camera=%s", camera_id)
            state.finish_auto_cal_run(
                camera_id,
                status="failed",
                applied=False,
                summary="Failed",
                detail=f"{type(e).__name__}: {e}",
            )

    asyncio.create_task(_runner())
    return {"ok": True, "camera_id": camera_id, "run_id": run.id}


def _serialize_marker(record: MarkerRecord) -> dict[str, Any]:
    return {
        "marker_id": record.marker_id,
        "label": record.label,
        "x_m": record.x_m,
        "y_m": record.y_m,
        "z_m": record.z_m,
        "on_plate_plane": record.on_plate_plane,
        "residual_m": record.residual_m,
        "source_camera_ids": list(record.source_camera_ids),
    }


@app.post("/markers/scan")
async def markers_scan(
    camera_a_id: str = "A",
    camera_b_id: str = "B",
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_a_id):
        raise HTTPException(status_code=400, detail="invalid camera_a_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", camera_b_id):
        raise HTTPException(status_code=400, detail="invalid camera_b_id")
    if camera_a_id == camera_b_id:
        raise HTTPException(status_code=400, detail="camera_a_id and camera_b_id must differ")

    jpeg_a, jpeg_b = await asyncio.gather(
        _await_calibration_frame(camera_a_id),
        _await_calibration_frame(camera_b_id),
    )
    bgr_a = _decode_calibration_jpeg(jpeg_a)
    bgr_b = _decode_calibration_jpeg(jpeg_b)
    scan = _triangulate_marker_candidates(
        camera_a_id=camera_a_id,
        camera_b_id=camera_b_id,
        bgr_a=bgr_a,
        bgr_b=bgr_b,
    )
    existing_ids = {rec.marker_id for rec in state._marker_registry.all_records()}
    return {
        "ok": True,
        "camera_ids": [camera_a_id, camera_b_id],
        "candidates": scan["candidates"],
        "visibility": scan["visibility"],
        "existing_marker_ids": sorted(existing_ids),
    }


@app.get("/markers/state")
def markers_state() -> dict[str, Any]:
    records = state._marker_registry.all_records()
    return {
        "markers": [_serialize_marker(rec) for rec in records],
        "planar_marker_ids": [rec.marker_id for rec in records if rec.on_plate_plane],
        "reserved_marker_ids": sorted(PLATE_MARKER_WORLD.keys()),
    }


@app.post("/markers")
def markers_batch_upsert(body: MarkerBatchUpsertRequest) -> dict[str, Any]:
    persisted: list[dict[str, Any]] = []
    for draft in body.markers:
        z_m = 0.0 if draft.snap_to_plate_plane or draft.on_plate_plane else draft.z_m
        record = MarkerRecord(
            marker_id=draft.marker_id,
            x_m=draft.x_m,
            y_m=draft.y_m,
            z_m=z_m,
            label=(draft.label or "").strip() or None,
            on_plate_plane=bool(draft.on_plate_plane),
            residual_m=draft.residual_m,
            source_camera_ids=list(draft.source_camera_ids),
        )
        persisted.append(_serialize_marker(state._marker_registry.upsert(record)))
    return {"ok": True, "markers": persisted}


@app.patch("/markers/{marker_id}")
def marker_update(marker_id: int, body: MarkerUpdateRequest) -> dict[str, Any]:
    existing = state._marker_registry.get(marker_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"marker {marker_id} not registered")
    x_m = existing.x_m if body.x_m is None else body.x_m
    y_m = existing.y_m if body.y_m is None else body.y_m
    z_m = existing.z_m if body.z_m is None else body.z_m
    on_plate_plane = existing.on_plate_plane if body.on_plate_plane is None else body.on_plate_plane
    if body.snap_to_plate_plane or on_plate_plane:
        z_m = 0.0
    updated = MarkerRecord(
        marker_id=existing.marker_id,
        x_m=x_m,
        y_m=y_m,
        z_m=z_m,
        label=(body.label.strip() if body.label is not None else existing.label) or None,
        on_plate_plane=bool(on_plate_plane),
        residual_m=existing.residual_m,
        source_camera_ids=list(existing.source_camera_ids),
    )
    state._marker_registry.upsert(updated)
    return {"ok": True, "marker": _serialize_marker(updated)}


@app.delete("/markers/{marker_id}")
def marker_delete(marker_id: int) -> dict[str, Any]:
    existed = state._marker_registry.remove(marker_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"marker {marker_id} not registered")
    return {"ok": True, "marker_id": marker_id}


@app.post("/markers/clear")
def markers_clear() -> dict[str, Any]:
    cleared = state._marker_registry.clear()
    return {"ok": True, "cleared_count": cleared}


@app.post("/calibration/markers/register/{camera_id}")
async def calibration_markers_register_legacy(camera_id: str) -> dict[str, Any]:
    raise HTTPException(
        status_code=409,
        detail="single-camera marker registration was removed; use /markers and scan with both cameras",
    )


@app.get("/calibration/markers")
def calibration_markers_list_legacy() -> dict[str, Any]:
    return {
        "markers": [
            {"id": rec.marker_id, "wx": rec.x_m, "wy": rec.y_m}
            for rec in state._marker_registry.all_records()
            if rec.on_plate_plane
        ],
    }


@app.delete("/calibration/markers/{marker_id}")
def calibration_markers_delete_legacy(marker_id: int) -> dict[str, Any]:
    return marker_delete(marker_id)


@app.post("/calibration/markers/clear")
def calibration_markers_clear_legacy() -> dict[str, Any]:
    return markers_clear()


@app.get("/", response_class=HTMLResponse)
def events_index() -> HTMLResponse:
    from render_dashboard import render_events_index_html

    session = state.session_snapshot()
    sync_run = state.current_sync()
    return HTMLResponse(
        render_events_index_html(
            events=state.events(),
            trash_count=state.trash_count(),
            devices=_build_device_status_rows(),
            session=session.to_dict() if session is not None else None,
            calibrations=sorted(state.calibrations().keys()),
            capture_mode=state.current_mode().value,
            default_paths=sorted(p.value for p in state.default_paths()),
            live_session=state.live_session_summary(),
            sync=sync_run.to_dict() if sync_run is not None else None,
            sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
            chirp_detect_threshold=state.chirp_detect_threshold(),
            heartbeat_interval_s=state.heartbeat_interval_s(),
            tracking_exposure_cap=state.tracking_exposure_cap().value,
            capture_height_px=state.capture_height_px(),
            calibration_last_ts={
                cam: p.stat().st_mtime
                for cam in state.calibrations().keys()
                for p in [state._calibration_path(cam)]
                if p.exists()
            },
            preview_requested=state._preview.requested_map(),
        )
    )


@app.get("/sync", response_class=HTMLResponse)
def sync_page() -> HTMLResponse:
    """Dedicated time-sync surface. Keeps chirp workflows and runtime
    tuning separate from geometric camera calibration."""
    from render_sync import render_sync_html

    session = state.session_snapshot()
    sync_run = state.current_sync()
    last_sync = state.last_sync_result()
    return HTMLResponse(
        render_sync_html(
            devices=_build_device_status_rows(),
            session=session.to_dict() if session is not None else None,
            calibrations=sorted(state.calibrations().keys()),
            sync=sync_run.to_dict() if sync_run is not None else None,
            last_sync=last_sync.model_dump() if last_sync is not None else None,
            sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
            chirp_detect_threshold=state.chirp_detect_threshold(),
            mutual_sync_threshold=state.mutual_sync_threshold(),
            heartbeat_interval_s=state.heartbeat_interval_s(),
            capture_height_px=state.capture_height_px(),
            tracking_exposure_cap=state.tracking_exposure_cap().value,
        )
    )


@app.get("/setup", response_class=HTMLResponse)
def setup_page() -> HTMLResponse:
    """Calibration surface for device positioning and reprojection checks."""
    from render_sync import render_setup_html

    session = state.session_snapshot()
    return HTMLResponse(
        render_setup_html(
            devices=_build_device_status_rows(),
            session=session.to_dict() if session is not None else None,
            calibrations=sorted(state.calibrations().keys()),
            sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
            calibration_last_ts={
                cam: p.stat().st_mtime
                for cam in state.calibrations().keys()
                for p in [state._calibration_path(cam)]
                if p.exists()
            },
            markers_count=len(state._marker_registry.all_records()),
            preview_requested=state._preview.requested_map(),
        )
    )


@app.get("/markers", response_class=HTMLResponse)
def markers_page() -> HTMLResponse:
    from render_markers import render_markers_html
    from reconstruct import build_calibration_scene

    session = state.session_snapshot()
    markers = [_serialize_marker(rec) for rec in state._marker_registry.all_records()]
    compare_markers = [
        {
            "marker_id": int(mid),
            "x_m": float(xy[0]),
            "y_m": float(xy[1]),
            "z_m": 0.0,
            "label": f"Plate {mid}",
            "on_plate_plane": True,
            "kind": "plate",
            "side_m": 0.08,
        }
        for mid, xy in sorted(PLATE_MARKER_WORLD.items())
    ] + [
        {
            **_serialize_marker(rec),
            "kind": "stored",
            "side_m": 0.08,
        }
        for rec in state._marker_registry.all_records()
    ]
    scene = build_calibration_scene(state.calibrations()).to_dict()
    scene["plate"] = [
        {"x": -0.432 / 2.0, "y": 0.0, "z": 0.0},
        {"x": 0.432 / 2.0, "y": 0.0, "z": 0.0},
        {"x": 0.432 / 2.0, "y": 0.216, "z": 0.0},
        {"x": 0.0, "y": 0.432, "z": 0.0},
        {"x": -0.432 / 2.0, "y": 0.216, "z": 0.0},
    ]
    return HTMLResponse(
        render_markers_html(
            markers=markers,
            compare_markers=compare_markers,
            scene=scene,
            devices=[
                {
                    "camera_id": d.camera_id,
                    "last_seen_at": d.last_seen_at,
                    "time_synced": d.time_synced,
                }
                for d in state.online_devices()
            ],
            session=session.to_dict() if session is not None else None,
            calibrations=sorted(state.calibrations().keys()),
        )
    )


@app.post("/reset")
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
