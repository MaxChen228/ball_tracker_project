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
)

logger = logging.getLogger("ball_tracker")

# Single authoritative instance. Held here so tests can monkeypatch `main.state`
# and all route handlers (including those in routes/* that use late imports) see
# the same object.
state = State()

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

from routes import markers as _markers_routes
from routes import settings as _settings_routes
from routes import camera as _camera_routes
from routes import sessions as _sessions_routes
from routes import sync as _sync_routes
from routes import viewer as _viewer_routes
from routes import pitch as _pitch_routes
from routes import calibration as _calibration_routes
app.include_router(_markers_routes.router)
app.include_router(_settings_routes.router)
app.include_router(_camera_routes.router)
app.include_router(_sessions_routes.router)
app.include_router(_sync_routes.router)
app.include_router(_viewer_routes.router)
app.include_router(_pitch_routes.router)
app.include_router(_calibration_routes.router)
from routes.camera import _validate_camera_id_or_422
from routes.sessions import _SESSION_ID_RE
from routes.viewer import _build_viewer_health, _find_clip_on_disk, _scene_for_session
from routes.pitch import _summarize_result, _run_server_detection
from routes.calibration import (
    _all_marker_world_xyz, _await_calibration_frame, _decode_calibration_jpeg,
    _derive_auto_cal_intrinsics, _marker_camera_pose, _pose_from_homography,
    _reprojection_error_px, _residual_bucket, _run_auto_calibration,
    _solve_auto_cal_solution, _solve_pnp_homography, _triangulate_marker_candidates,
)


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
    # Use heartbeat-based presence only. `state.heartbeat()` is called
    # immediately on WS connect (line 468), so a new device appears here
    # without needing the WS-connected fallback. The fallback caused a
    # ghost-device bug: when a phone switches role A→B, the old A WS stays
    # in _sockets until its async handler reaches `finally`, so for that
    # brief window both A and B appeared online.
    device_ids = set(fresh_devices)
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
                "battery_level": (d.battery_level if d is not None else None),
                "battery_state": (d.battery_state if d is not None else None),
            }
        )
    return devices


def _arm_readiness(
    devices: list[dict[str, Any]] | None = None,
    calibrations: list[str] | None = None,
) -> dict[str, Any]:
    """Return the dashboard/API arming gate.

    A single calibrated online camera is a valid monocular session: it can
    record detections and render viewer rays, but cannot triangulate. Time
    sync is only required once two calibrated online cameras will participate
    in the same session.
    """
    devices = devices if devices is not None else _build_device_status_rows()
    calibrations = calibrations if calibrations is not None else sorted(state.calibrations().keys())
    calibrated = set(calibrations)
    online = {str(d.get("camera_id")) for d in devices if d.get("camera_id")}
    synced = {str(d.get("camera_id")) for d in devices if d.get("camera_id") and d.get("time_synced")}
    usable = sorted(cam for cam in online if cam in calibrated)
    uncalibrated = sorted(cam for cam in online if cam not in calibrated)
    blockers: list[str] = []
    warnings: list[str] = []

    if not online:
        blockers.append("no camera online")
    elif uncalibrated:
        blockers.extend(f"{cam} not calibrated" for cam in uncalibrated)
    elif len(usable) >= 2:
        unsynced = [cam for cam in usable if cam not in synced]
        blockers.extend(f"{cam} not time-synced" for cam in unsynced)
    else:
        missing = [cam for cam in ("A", "B") if cam not in usable]
        if missing:
            warnings.append(
                f"single-camera session ({usable[0]}); no triangulation"
            )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "online_cameras": sorted(online),
        "calibrated_online_cameras": usable,
        "synced_calibrated_online_cameras": sorted(cam for cam in usable if cam in synced),
        "requires_time_sync": len(usable) >= 2,
        "mode": "stereo" if len(usable) >= 2 else ("single_camera" if usable else "blocked"),
    }


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
    arm_readiness = _arm_readiness(devices, calibrations)
    return {
        **summary,
        "devices": devices,
        # Lightweight calibration presence snapshot for header/readiness UI.
        # The richer scene payload still lives on /calibration/state.
        "calibrations": calibrations,
        "arm_readiness": arm_readiness,
        "session": session.to_dict() if session is not None else None,
        "commands": state.commands_for_devices(),
        # Global dashboard mode choice. iPhones show this on the HUD in idle
        # and fall back to it when there's no armed session; during an armed
        # session they read session.mode instead (it's the snapshot that
        # can't drift from under them).
        "capture_mode": state.current_mode().value,
        "default_paths": sorted(p.value for p in state.default_paths()),
        "hsv_range": state.hsv_range().__dict__,
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
            cam: True for cam in state.requested_calibration_frame_ids()
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
        "hsv_range": status.get("hsv_range"),
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


_BATTERY_STATES = {"unknown", "unplugged", "charging", "full"}


def _parse_battery(msg: dict[str, Any]) -> tuple[float | None, str | None]:
    """Extract battery_level (0..1) and battery_state from a WS payload.

    iOS UIDevice.batteryLevel returns -1 when monitoring is off; treat that
    as None so the UI shows "no data" instead of 0%. Anything out of [0, 1]
    is also discarded. Unknown state strings collapse to None — don't want
    to render surprise labels.
    """
    raw_level = msg.get("battery_level")
    level: float | None = None
    if isinstance(raw_level, (int, float)):
        lvl = float(raw_level)
        if 0.0 <= lvl <= 1.0:
            level = lvl
    raw_state = msg.get("battery_state")
    state_: str | None = None
    if isinstance(raw_state, str):
        s = raw_state.lower()
        if s in _BATTERY_STATES:
            state_ = s
    return level, state_


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
            _p = state.sync_params()
            await device_ws.send(camera_id, {
                "type": "sync_run",
                "sync_id": active_sync.id,
                "emit_at_s": _p.emit_a_at_s if camera_id == "A" else _p.emit_b_at_s,
                "record_duration_s": _p.record_duration_s,
            })
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
                battery_level, battery_state = _parse_battery(msg)
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                    battery_level=battery_level,
                    battery_state=battery_state,
                )
                await device_ws.send(camera_id, _settings_message_for(camera_id))
                continue
            if mtype == "heartbeat":
                device_ws.note_seen(camera_id)
                reported_sync_id = msg.get("time_sync_id")
                reported_anchor = msg.get("sync_anchor_timestamp_s")
                battery_level, battery_state = _parse_battery(msg)
                state.heartbeat(
                    camera_id,
                    time_synced=(reported_sync_id is not None and reported_anchor is not None),
                    time_sync_id=reported_sync_id,
                    sync_anchor_timestamp_s=reported_anchor,
                    battery_level=battery_level,
                    battery_state=battery_state,
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
                ray = await asyncio.to_thread(
                    state.live_ray_for_frame,
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
                if ray is not None:
                    await sse_hub.broadcast(
                        "ray",
                        {
                            "sid": session_id,
                            "cam": camera_id,
                            "path": DetectionPath.live.value,
                            "frame_index": ray.frame_index,
                            "t_rel_s": ray.t_rel_s,
                            "origin": ray.origin,
                            "endpoint": ray.endpoint,
                            "source": ray.source,
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
                    persisted = await asyncio.to_thread(state.persist_live_frames, camera_id, session_id)
                    if persisted is not None:
                        result = persisted
                    else:
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

@app.get("/", response_class=HTMLResponse)
def events_index() -> HTMLResponse:
    from render_dashboard import render_events_index_html

    session = state.session_snapshot()
    sync_run = state.current_sync()
    devices = _build_device_status_rows()
    calibrations = sorted(state.calibrations().keys())
    return HTMLResponse(
        render_events_index_html(
            events=state.events(),
            trash_count=state.trash_count(),
            devices=devices,
            session=session.to_dict() if session is not None else None,
            calibrations=calibrations,
            arm_readiness=_arm_readiness(devices, calibrations),
            capture_mode=state.current_mode().value,
            hsv_range=state.hsv_range().__dict__,
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
            sync_params={
                "emit_a_at_s": state.sync_params().emit_a_at_s,
                "emit_b_at_s": state.sync_params().emit_b_at_s,
                "record_duration_s": state.sync_params().record_duration_s,
                "search_window_s": state.sync_params().search_window_s,
            },
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
