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
app.include_router(_markers_routes.router)
app.include_router(_settings_routes.router)
app.include_router(_camera_routes.router)
app.include_router(_sessions_routes.router)
app.include_router(_sync_routes.router)
app.include_router(_viewer_routes.router)
app.include_router(_pitch_routes.router)
from routes.camera import _validate_camera_id_or_422
from routes.sessions import _SESSION_ID_RE
from routes.viewer import _build_viewer_health, _find_clip_on_disk, _scene_for_session
from routes.pitch import _summarize_result, _run_server_detection


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
