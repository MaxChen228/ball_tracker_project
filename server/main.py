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
import secrets
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
from pitch_ingest import attach_pitch_analysis, ingest_pitch
from video import probe_dims
from chirp import chirp_wav_bytes
from preview import PreviewBuffer, REQUEST_TTL_S as _PREVIEW_REQUEST_TTL_S
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
from pitch_routes import build_pitch_router
from control_routes import (
    arm_message_for,
    build_control_router,
    disarm_message_for,
    settings_message_for,
    wants_html,
)
from device_ws_routes import build_device_ws_router
from sync_routes import build_sync_router
from preview_routes import _validate_camera_id_or_422, build_preview_router
from results_routes import (
    build_results_router,
    build_viewer_health as _build_viewer_health,
)
from calibration_routes import build_calibration_router

logger = logging.getLogger("ball_tracker")


_DEFAULT_DATA_DIR = Path(os.environ.get("BALL_TRACKER_DATA_DIR", "data"))

# Seconds a heartbeat remains fresh. A phone beating at 1 Hz drops off the
# "online" list after missing ~3 beats — conservative enough to tolerate a
# stalled wifi roam without flapping.
_DEVICE_STALE_S = 3.0

# Entries in `_devices` older than this get pruned on every heartbeat write.
# Legitimate phones beat at 1 Hz so anything beyond 60 s is not coming back;
# pruning on write is what keeps a malformed/spoofed client from ballooning
# the registry forever without needing a background task.
_DEVICE_GC_AFTER_S = 60.0

# Hard cap on `_devices` size. Even with GC-on-write, a burst of distinct
# camera_ids within the GC window could push memory up. Cap at 64 — more
# than enough for any plausible rig (we run 2-phone stereo) while still
# bounding adversarial input.
_DEVICE_REGISTRY_CAP = 64

# When a session ends, server keeps advertising `disarm` on /status for a
# brief window so the phone that didn't fire the cycle still gets the signal
# on its next poll. Long enough to cover any sensible poll cadence.
_DISARM_ECHO_S = 5.0

# Maximum wall time a mutual-sync run may stay active waiting for both
# phones to post their matched-filter reports. If one side fails to hear
# the peer (weak speaker, noise floor), the run is dropped and the
# dashboard surfaces "Sync timed out".
_SYNC_TIMEOUT_S = 8.0

# After a mutual sync solves (or times out), block subsequent /sync/start
# for this long. Prevents rapid-fire retries thrashing the phones through
# the state transition and gives the operator time to read the result.
_SYNC_COOLDOWN_S = 10.0

# Time-sync (single-listener chirp) command TTL. When the dashboard's
# CALIBRATE TIME button fires, each target camera gets a pending
# `sync_command: "start"` flag. A camera consumes it on its next
# heartbeat (one-shot), or the flag self-expires after this many
# seconds so a stale command doesn't fire if the operator gave up.
_SYNC_COMMAND_TTL_S = 10.0

# Legacy third-device chirp sync ids stay shareable for one listening
# window so two phones that begin 時間校正 a few seconds apart can still
# claim the same run id.
_TIME_SYNC_INTENT_WINDOW_S = 20.0

# Maximum server-observed age of a legacy chirp sync before it no longer
# counts as "ready" for a fresh arm.
_TIME_SYNC_MAX_AGE_S = 30.0

# Hard cap on `/pitch` video upload size. A 5 s cycle at 4K / 240 fps sits
# comfortably under this; anything beyond is almost certainly a misbehaving
# client or a denial-of-service attempt. FastAPI buffers the full multipart
# body in memory before the handler runs, so without this cap a single
# oversized request can OOM the process.
_MAX_PITCH_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

# Session auto-timeout (`_DEFAULT_SESSION_TIMEOUT_S`), Device, and Session
# now live in schemas.py — imported above for back-compat re-export.


def _new_session_id() -> str:
    # 8 hex chars ≈ 4 bytes of entropy — plenty for a personal-LAN tool
    # where sessions are seconds apart, not microseconds.
    return "s_" + secrets.token_hex(4)


def _new_sync_id() -> str:
    # Distinct `sy_` prefix so log lines immediately differentiate a
    # mutual-sync run id from a pitch session id at a glance.
    return "sy_" + secrets.token_hex(4)


def _validate_calibration_snapshot(snap: CalibrationSnapshot) -> None:
    """Gatekeep CalibrationSnapshot writes: K, H, and dims must all be in
    the same pixel coordinate system. An optical centre outside the image,
    non-positive focal lengths, or wildly asymmetric fx/fy all indicate
    the snapshot was built from mismatched sources and would produce
    nonsense extrinsics downstream. Raise ValueError on the way in rather
    than debugging bad poses on the way out."""
    w, h = snap.image_width_px, snap.image_height_px
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid image dims {w}x{h}")
    k = snap.intrinsics
    if k.fx <= 0 or k.fz <= 0:
        raise ValueError(f"non-positive focal length fx={k.fx} fy={k.fz}")
    # fx and fy should be within a factor of ~2 for any real lens + square
    # pixels. Bigger asymmetry almost always signals a unit-system bug.
    if max(k.fx, k.fz) / min(k.fx, k.fz) > 2.0:
        raise ValueError(f"fx/fy ratio out of bounds: fx={k.fx} fy={k.fz}")
    # Optical centre must be inside the image. We allow a small outside
    # margin (5% of dimension) because lens off-axis shifts happen, but
    # more than that is almost certainly a dims-mismatch bug.
    if not (-0.05 * w <= k.cx <= 1.05 * w):
        raise ValueError(
            f"cx={k.cx} outside image width {w} — K likely from a "
            f"different resolution than image_dims claim"
        )
    if not (-0.05 * h <= k.cy <= 1.05 * h):
        raise ValueError(
            f"cy={k.cy} outside image height {h} — K likely from a "
            f"different resolution than image_dims claim"
        )
    # Homography must be invertible (h33 normalized ≈ 1).
    H_flat = snap.homography
    if len(H_flat) != 9 or abs(H_flat[8]) < 1e-9:
        raise ValueError(f"degenerate homography: h33={H_flat[8] if len(H_flat) == 9 else 'wrong length'}")


# Calibration-frame buffer TTL. One-shot: iOS pushes a full-resolution
# JPEG on request, server consumes it, flag auto-clears. 10 s is plenty
# for capture→encode→POST round-trip even on a busy LAN.
_CALIBRATION_FRAME_TTL_S = 10.0


@dataclass
class _LegacyTimeSyncIntent:
    id: str
    started_at: float
    expires_at: float


@dataclass
class _AutoCalibrationRun:
    id: str
    camera_id: str
    status: str
    started_at: float
    updated_at: float
    frames_seen: int = 0
    good_frames: int = 0
    stable_frames: int = 0
    markers_visible: int = 0
    solver: str | None = None
    reprojection_px: float | None = None
    position_jitter_cm: float | None = None
    angle_jitter_deg: float | None = None
    applied: bool = False
    summary: str | None = None
    detail: str | None = None
    detected_ids: list[int] | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "frames_seen": self.frames_seen,
            "good_frames": self.good_frames,
            "stable_frames": self.stable_frames,
            "markers_visible": self.markers_visible,
            "solver": self.solver,
            "reprojection_px": self.reprojection_px,
            "position_jitter_cm": self.position_jitter_cm,
            "angle_jitter_deg": self.angle_jitter_deg,
            "applied": self.applied,
            "summary": self.summary,
            "detail": self.detail,
            "detected_ids": list(self.detected_ids or []),
            "result": dict(self.result or {}),
        }


class State:
    def __init__(
        self,
        data_dir: Path = _DEFAULT_DATA_DIR,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._lock = Lock()
        # Pitch uploads keyed by (camera_id, session_id). One session → at
        # most two entries (A, B). Cross-device pairing is by session_id —
        # iPhones don't mint identifiers any more.
        self.pitches: dict[tuple[str, str], PitchPayload] = {}
        self.results: dict[str, SessionResult] = {}
        self._data_dir = data_dir
        self._pitch_dir = data_dir / "pitches"
        self._result_dir = data_dir / "results"
        self._video_dir = data_dir / "videos"
        self._calibration_dir = data_dir / "calibrations"
        self._pitch_dir.mkdir(parents=True, exist_ok=True)
        self._result_dir.mkdir(parents=True, exist_ok=True)
        self._video_dir.mkdir(parents=True, exist_ok=True)
        self._calibration_dir.mkdir(parents=True, exist_ok=True)
        # Dashboard-control state. All in-memory — devices re-heartbeat on
        # connection, sessions don't survive restart.
        self._devices: dict[str, Device] = {}
        self._current_session: Session | None = None
        self._last_ended_session: Session | None = None
        # Global capture-mode toggle. Dashboard flips this; every subsequent
        # arm_session() snapshots the current value into the session. Default
        # `camera_only` preserves the pre-mode-split behaviour for anyone
        # upgrading a running server without touching the dashboard.
        self._current_mode: CaptureMode = CaptureMode.camera_only
        # New authority: orthogonal detection path set snapshotted onto each
        # session. Kept in sync with `_current_mode` for backward-compat.
        self._default_paths: set[DetectionPath] = set(_DEFAULT_PATHS)
        # Per-camera calibration snapshots. Written by POST /calibration,
        # read by the dashboard canvas so the 3D preview shows where each
        # phone "thinks it is" relative to the plate, independent of any
        # session. Persisted as one JSON per camera so a server restart
        # keeps whatever calibrations were live.
        self._calibrations: dict[str, CalibrationSnapshot] = {}
        # Mutual chirp sync: at most one run active at a time. Both phones
        # must be online and no session may be armed when a run starts.
        # `_last_sync_result` survives across runs so the dashboard + the
        # triangulation pairing can keep applying Δ until the next sync
        # refreshes it. In-memory only — a restart drops any cached Δ,
        # which matches the "re-sync before each shoot" operator flow.
        self._current_sync: SyncRun | None = None
        self._last_sync_result: SyncResult | None = None
        self._sync_cooldown_until: float = 0.0
        # Ring buffer of diagnostic events from the mutual-sync flow, both
        # server-emitted and phone-pushed (via POST /sync/log). Dashboard's
        # Time Sync panel renders the last N entries. 500 lines ≈ 20 runs'
        # worth of detail — plenty for diagnosing a single failed run.
        self._sync_log: deque[SyncLogEntry] = deque(maxlen=500)
        # Legacy third-device chirp sync intent. A live intent supplies the
        # shared `sync_id` both phones should stamp onto their recovered
        # anchors. The dashboard-remote path also fans this intent out as
        # per-camera pending commands consumed on the next WS heartbeat.
        self._current_time_sync_intent: _LegacyTimeSyncIntent | None = None
        self._sync_command_pending: dict[str, _LegacyTimeSyncIntent] = {}
        # Injectable clock so timeout and staleness tests don't need sleeps.
        self._time_fn = time_fn
        # Runtime tunables pushed from the dashboard, hot-applied on the
        # iPhone via WS `settings` messages. Persisted so a server restart
        # doesn't silently drop the operator's last-chosen values.
        self._chirp_detect_threshold: float = 0.18
        self._heartbeat_interval_s: float = 1.0
        self._tracking_exposure_cap: TrackingExposureCapMode = _DEFAULT_TRACKING_EXPOSURE_CAP_MODE
        # Capture resolution (image height in px) pushed to iOS via WS settings.
        # Allowed set is {720, 1080}. Default 1080p — always works.
        self._capture_height_px: int = 1080
        self._runtime_settings_path = data_dir / "runtime_settings.json"
        self._load_runtime_settings_from_disk()
        # Live-preview buffer (Phase 4a). Keeps one latest JPEG per camera
        # in memory, gated by a per-camera "dashboard is watching" flag
        # with a 5 s TTL. Shares the State-level `_time_fn` so clock-drift
        # tests apply here too without a parallel shim.
        self._preview = PreviewBuffer(time_fn=time_fn)
        # One-shot high-resolution calibration frame per camera. Separate
        # buffer from preview (preview is 480p, advisory; calibration
        # frames are native capture res, accuracy-critical). `_cal_frames`
        # holds (jpeg_bytes, ts) keyed by camera_id; `_cal_frame_requested`
        # flags pending requests that iOS drains on its next captureOutput.
        self._cal_frames: dict[str, tuple[bytes, float]] = {}
        self._cal_frame_requested: dict[str, float] = {}  # cam_id → expiry ts
        # Operator-managed marker registry. Stores 3D world coords plus a
        # "on plate plane" flag so the current planar auto-calibration path
        # can keep consuming only the eligible subset.
        self._marker_registry = MarkerRegistryDB(data_dir)
        # Per-camera auto-calibration run state. Long-running server-side
        # observation window updates this so `/setup` can show
        # searching/stabilizing/solving/verified instead of a blind spinner.
        self._auto_cal_runs: dict[str, _AutoCalibrationRun] = {}
        self._auto_cal_last: dict[str, _AutoCalibrationRun] = {}
        # Live streaming state keyed by session id.
        self._live_pairings: dict[str, LivePairingSession] = {}
        # Calibrations first — _load_from_disk re-triangulates every cached
        # pitch, and triangulation needs the calibration snapshot to decide
        # the intrinsic-scale factor (MOV dims vs. calibration dims).
        self._load_calibrations_from_disk()
        self._load_from_disk()

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def video_dir(self) -> Path:
        return self._video_dir

    def save_clip(
        self, camera_id: str, session_id: str, data: bytes, ext: str = "mov"
    ) -> Path:
        """Persist a session's H.264 clip to disk. Writes atomically so a
        partial transfer cannot leave a corrupt file visible to downstream
        tools. Overwrites any existing clip for (camera_id, session_id).

        The tmp-write + rename must happen under `self._lock` — two
        simultaneous POSTs for the same (camera, session) would otherwise
        race on the shared `<path>.tmp` filename. `os.replace` is atomic,
        but `tmp.write_bytes(data)` is not; a concurrent second write would
        clobber the first's tmp mid-stream and the first's `replace` would
        then publish a corrupt clip."""
        safe_ext = (ext or "mov").lstrip(".").lower()
        if not safe_ext or "/" in safe_ext or "\\" in safe_ext:
            safe_ext = "mov"
        path = self._video_dir / f"session_{session_id}_{camera_id}.{safe_ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            tmp.write_bytes(data)
            tmp.replace(path)
        return path

    def _pitch_path(self, camera_id: str, session_id: str) -> Path:
        return self._pitch_dir / f"session_{session_id}_{camera_id}.json"

    def _result_path(self, session_id: str) -> Path:
        return self._result_dir / f"session_{session_id}.json"

    def _load_from_disk(self) -> None:
        for path in sorted(self._pitch_dir.glob("session_*.json")):
            try:
                obj = json.loads(path.read_text())
                if "frames" in obj and "frames_server_post" not in obj and not obj.get("frames_on_device"):
                    obj["frames_server_post"] = obj.get("frames", [])
                pitch = PitchPayload.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt pitch file %s: %s", path.name, e)
                continue
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch

        seen_sessions = {sid for _, sid in self.pitches.keys()}
        for sid in sorted(seen_sessions):
            self.results[sid] = self._rebuild_result_for_session(sid)

        if self.pitches:
            logger.info(
                "restored %d pitch payloads across %d sessions from %s",
                len(self.pitches),
                len(seen_sessions),
                self._data_dir,
            )

    def _calibration_path(self, camera_id: str) -> Path:
        return self._calibration_dir / f"{camera_id}.json"

    def _load_calibrations_from_disk(self) -> None:
        for path in sorted(self._calibration_dir.glob("*.json")):
            try:
                obj = json.loads(path.read_text())
                snap = CalibrationSnapshot.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt calibration file %s: %s", path.name, e)
                continue
            # Also screen via the same K/H/dims consistency rules the
            # write path enforces — old snapshots from earlier buggy
            # write paths may have K in one pixel scale and dims in
            # another, which would poison every downstream solve. Drop
            # them on load with a loud warning so `auto-cal` takes the
            # "no prior" path and rebuilds cleanly.
            try:
                _validate_calibration_snapshot(snap)
            except ValueError as e:
                logger.warning(
                    "skip inconsistent calibration %s: %s — "
                    "delete the file and re-run Auto Calibrate",
                    path.name, e,
                )
                continue
            self._calibrations[snap.camera_id] = snap
        if self._calibrations:
            logger.info(
                "restored %d camera calibration(s) from %s",
                len(self._calibrations),
                self._calibration_dir,
            )

    def set_calibration(self, snapshot: CalibrationSnapshot) -> None:
        """Record (or overwrite) one camera's calibration and persist it
        atomically so the dashboard survives a restart. Last write wins.

        Validates K/H/dims self-consistency before storing — an earlier bug
        mixed 1080p intrinsics with 480p homography which silently produced
        garbage extrinsics downstream. Catching it at the boundary saves
        hours of "why is Cam A at Z=0.66m" debugging."""
        _validate_calibration_snapshot(snapshot)
        payload = snapshot.model_dump_json(indent=2)
        with self._lock:
            self._calibrations[snapshot.camera_id] = snapshot
            self._atomic_write(self._calibration_path(snapshot.camera_id), payload)

    def calibrations(self) -> dict[str, CalibrationSnapshot]:
        with self._lock:
            return dict(self._calibrations)

    def _triangulate_pair(
        self, a: PitchPayload, b: PitchPayload, *, source: str = "server",
    ) -> list[TriangulatedPoint]:
        """Scale each pitch's intrinsics + homography to its MOV's actual
        pixel grid (using the cached calibration snapshot as the reference
        resolution) and then triangulate. When no snapshot is cached for a
        camera the scale factor falls back to 1.0 and the pitch is passed
        through unchanged — the legacy behaviour for pre-resolution-picker
        builds that always recorded at the calibration resolution.

        `source` picks the detection stream (`"server"` default reads
        `pitch.frames`; `"on_device"` reads `pitch.frames_on_device`). Dual
        mode calls this twice per session to keep the two point clouds
        separate."""
        with self._lock:
            cal_a = self._calibrations.get(a.camera_id)
            cal_b = self._calibrations.get(b.camera_id)
        a_scaled = scale_pitch_to_video_dims(
            a,
            (cal_a.image_width_px, cal_a.image_height_px) if cal_a else None,
        )
        b_scaled = scale_pitch_to_video_dims(
            b,
            (cal_b.image_width_px, cal_b.image_height_px) if cal_b else None,
        )
        return triangulate_cycle(a_scaled, b_scaled, source=source)

    @staticmethod
    def _normalize_paths(
        raw_paths: list[str] | set[DetectionPath] | None,
    ) -> set[DetectionPath]:
        if raw_paths is None:
            return set()
        parsed: set[DetectionPath] = set()
        for item in raw_paths:
            try:
                parsed.add(item if isinstance(item, DetectionPath) else DetectionPath(str(item)))
            except ValueError:
                continue
        return parsed

    def _paths_for_pitch(self, pitch: PitchPayload) -> set[DetectionPath]:
        explicit = self._normalize_paths(pitch.paths)
        if explicit:
            return explicit
        with self._lock:
            for session in (self._current_session, self._last_ended_session):
                if session is not None and session.id == pitch.session_id:
                    return set(session.paths)
            return set(self._default_paths)

    def _get_path_frames(self, pitch: PitchPayload, path: DetectionPath) -> list[FramePayload]:
        if path == DetectionPath.live:
            return list(pitch.frames_live)
        if path == DetectionPath.ios_post:
            if pitch.frames_ios_post:
                return list(pitch.frames_ios_post)
            if pitch.frames_on_device:
                return list(pitch.frames_on_device)
            if DetectionPath.server_post not in self._paths_for_pitch(pitch) and pitch.frames:
                return list(pitch.frames)
            return []
        if pitch.frames_server_post:
            return list(pitch.frames_server_post)
        if pitch.frames and (pitch.frames_on_device or DetectionPath.ios_post not in self._paths_for_pitch(pitch)):
            return list(pitch.frames)
        return []

    @staticmethod
    def _has_on_device_frames(pitch: PitchPayload) -> bool:
        """Dual-mode detection: if any pitch carries `frames_on_device`,
        the session was armed dual and we owe the caller a second
        triangulation pass over the iOS detection stream."""
        return bool(pitch and pitch.frames_on_device)

    @staticmethod
    def _has_server_frames(pitch: PitchPayload) -> bool:
        """True once the server-side MOV detection has populated
        `pitch.frames`. Used to gate `_triangulate_pair(source="server")`
        so the early-surface path (record runs before detection finishes,
        with `frames=[]`) doesn't flag a spurious error — it just leaves
        `result.points=[]` until the background detect task updates the
        pitch and we re-record."""
        return bool(pitch and pitch.frames)

    def _pitch_with_path_frames(
        self,
        pitch: PitchPayload,
        path: DetectionPath,
    ) -> PitchPayload:
        clone = pitch.model_copy(deep=True)
        clone.frames_on_device = []
        if path == DetectionPath.live:
            clone.frames = list(pitch.frames_live)
        elif path == DetectionPath.ios_post:
            clone.frames = self._get_path_frames(pitch, DetectionPath.ios_post)
        else:
            clone.frames = self._get_path_frames(pitch, DetectionPath.server_post)
        return clone

    def _session_sync_id_locked(self, session_id: str) -> str | None:
        for session in (self._current_session, self._last_ended_session):
            if session is not None and session.id == session_id:
                return session.sync_id
        return None

    def _validate_pair_sync(self, a: PitchPayload, b: PitchPayload) -> str | None:
        """Return a stable error string when the paired payloads do not
        belong to the same legacy chirp sync run."""
        if a.sync_anchor_timestamp_s is None or b.sync_anchor_timestamp_s is None:
            return "no time sync"
        with self._lock:
            expected_sync_id = self._session_sync_id_locked(a.session_id)
        if a.sync_id is None and b.sync_id is None:
            # Backward-compat: historical sessions recorded before sync_id
            # existed still carry valid anchor-relative timing, so keep
            # loading them unless this armed session explicitly expected a
            # shared sync id snapshot.
            return "sync id missing" if expected_sync_id is not None else None
        if a.sync_id is None or b.sync_id is None:
            return "sync id missing"
        if a.sync_id != b.sync_id:
            return "sync id mismatch"
        if expected_sync_id is not None and a.sync_id != expected_sync_id:
            return "sync id mismatch for armed session"
        return None

    def _empty_result_for_session(
        self,
        session_id: str,
        *,
        camera_a_received: bool,
        camera_b_received: bool,
    ) -> SessionResult:
        return SessionResult(
            session_id=session_id,
            camera_a_received=camera_a_received,
            camera_b_received=camera_b_received,
            solved_at=self._time_fn(),
        )

    def _rebuild_result_for_session(self, session_id: str) -> SessionResult:
        with self._lock:
            a = self.pitches.get(("A", session_id))
            b = self.pitches.get(("B", session_id))
            live = self._live_pairings.get(session_id)
            current = self._current_session if self._current_session and self._current_session.id == session_id else None
            ended = self._last_ended_session if self._last_ended_session and self._last_ended_session.id == session_id else None
            session_obj = current or ended

        result = self._empty_result_for_session(
            session_id,
            camera_a_received=a is not None,
            camera_b_received=b is not None,
        )

        candidate_paths: set[DetectionPath] = set()
        if session_obj is not None:
            candidate_paths |= set(session_obj.paths)
        for pitch in (a, b):
            if pitch is not None:
                candidate_paths |= self._paths_for_pitch(pitch)
                # Auto-include paths when frames are actually present, even if
                # neither the session nor the pitch explicitly listed them —
                # /pitch_analysis can attach iOS frames post-hoc into a session
                # that was armed server_post-only, and we still owe the caller
                # a triangulation over those frames.
                if pitch.frames_ios_post or pitch.frames_on_device:
                    candidate_paths.add(DetectionPath.ios_post)
                # Only auto-include server_post when the bucket is populated.
                # Legacy `pitch.frames` alone is ambiguous — in on_device-only
                # sessions it should map to ios_post, not resurrect server_post.
                if pitch.frames_server_post:
                    candidate_paths.add(DetectionPath.server_post)
        if live is not None and live.frame_counts:
            candidate_paths.add(DetectionPath.live)
        if not candidate_paths:
            candidate_paths = set(_DEFAULT_PATHS)

        if live is not None:
            result.frame_counts_by_path[DetectionPath.live.value] = {
                cam: int(count) for cam, count in live.frame_counts.items() if count
            }
            if live.triangulated:
                result.triangulated_by_path[DetectionPath.live.value] = list(live.triangulated)
                result.paths_completed.add(DetectionPath.live.value)
            if live.abort_reasons:
                result.abort_reasons.update({f"live:{cam}": why for cam, why in live.abort_reasons.items()})

        sync_error = None
        if a is not None and b is not None:
            sync_error = self._validate_pair_sync(a, b)
            if sync_error is not None:
                result.error = sync_error
                result.error_on_device = sync_error

        if sync_error is None and a is not None and b is not None:
            for path in sorted(candidate_paths, key=lambda p: p.value):
                if path == DetectionPath.live:
                    continue
                frames_a = self._get_path_frames(a, path)
                frames_b = self._get_path_frames(b, path)
                if frames_a or frames_b:
                    result.frame_counts_by_path[path.value] = {
                        "A": len(frames_a),
                        "B": len(frames_b),
                    }
                if not frames_a or not frames_b:
                    continue
                try:
                    pts = self._triangulate_pair(
                        self._pitch_with_path_frames(a, path),
                        self._pitch_with_path_frames(b, path),
                        source="server",
                    )
                except Exception as exc:
                    result.abort_reasons[path.value] = f"{type(exc).__name__}: {exc}"
                    continue
                result.triangulated_by_path[path.value] = pts
                result.paths_completed.add(path.value)

        authority: list[TriangulatedPoint] = []
        for path in (
            DetectionPath.ios_post.value,
            DetectionPath.server_post.value,
            DetectionPath.live.value,
        ):
            pts = result.triangulated_by_path.get(path)
            if pts:
                authority = pts
                break
        result.triangulated = authority
        # Legacy `points` semantics: in dual-like sessions (server_post is in
        # the candidate set) keep it strictly on the server stream so iOS
        # post-pass doesn't leak across the points/points_on_device boundary.
        # In mono-like sessions (on_device-only, live-only, etc) collapse to
        # whichever path actually produced data — older consumers (viewer,
        # /events) expect `points` to hold the session's single result when
        # no dual split is in play.
        if DetectionPath.server_post in candidate_paths:
            legacy_points = result.triangulated_by_path.get(DetectionPath.server_post.value, [])
        else:
            legacy_points = (
                result.triangulated_by_path.get(DetectionPath.ios_post.value)
                or result.triangulated_by_path.get(DetectionPath.live.value)
                or []
            )
        result.points = list(legacy_points)
        if result.triangulated_by_path.get(DetectionPath.ios_post.value):
            result.points_on_device = list(result.triangulated_by_path[DetectionPath.ios_post.value])
        elif result.triangulated_by_path.get(DetectionPath.live.value):
            result.points_on_device = list(result.triangulated_by_path[DetectionPath.live.value])

        if result.points:
            try:
                result.fit = fit_trajectory(result.points)
            except Exception:
                result.fit = None
        if result.points_on_device:
            try:
                result.fit_on_device = fit_trajectory(result.points_on_device)
            except Exception:
                result.fit_on_device = None
        if not result.triangulated and result.error is None and (a is not None or b is not None):
            if result.abort_reasons:
                result.aborted = True
            elif a is not None and b is not None:
                result.error = "no detection completed"
        return result

    def ingest_live_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> tuple[list[TriangulatedPoint], dict[str, int]]:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            cal_a = self._calibrations.get("A")
            cal_b = self._calibrations.get("B")
            dev_a = self._devices.get("A")
            dev_b = self._devices.get("B")
            session_obj = None
            for candidate in (self._current_session, self._last_ended_session):
                if candidate is not None and candidate.id == session_id:
                    session_obj = candidate
                    break

        def triangulate_live(cam: str, first: FramePayload, second: FramePayload) -> TriangulatedPoint | None:
            left_frame, right_frame = (first, second) if cam == "A" else (second, first)
            if cal_a is None or cal_b is None or dev_a is None or dev_b is None:
                return None
            if dev_a.sync_anchor_timestamp_s is None or dev_b.sync_anchor_timestamp_s is None:
                return None
            pa = PitchPayload(
                camera_id="A",
                session_id=session_id,
                sync_id=session_obj.sync_id if session_obj is not None else dev_a.time_sync_id,
                sync_anchor_timestamp_s=dev_a.sync_anchor_timestamp_s,
                video_start_pts_s=left_frame.timestamp_s,
                paths=[DetectionPath.live.value],
                frames=[left_frame],
                intrinsics=cal_a.intrinsics,
                homography=list(cal_a.homography),
                image_width_px=cal_a.image_width_px,
                image_height_px=cal_a.image_height_px,
            )
            pb = PitchPayload(
                camera_id="B",
                session_id=session_id,
                sync_id=session_obj.sync_id if session_obj is not None else dev_b.time_sync_id,
                sync_anchor_timestamp_s=dev_b.sync_anchor_timestamp_s,
                video_start_pts_s=right_frame.timestamp_s,
                paths=[DetectionPath.live.value],
                frames=[right_frame],
                intrinsics=cal_b.intrinsics,
                homography=list(cal_b.homography),
                image_width_px=cal_b.image_width_px,
                image_height_px=cal_b.image_height_px,
            )
            pts = self._triangulate_pair(pa, pb, source="server")
            return pts[0] if pts else None

        created = live.ingest(camera_id, frame, triangulate_live)
        return created, dict(live.frame_counts)

    def mark_live_path_ended(self, camera_id: str, session_id: str, reason: str | None = None) -> None:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            live.mark_completed(camera_id)
            if reason and reason != "disarmed":
                live.mark_aborted(camera_id, reason)

    def _atomic_write(self, path: Path, payload: str) -> None:
        # Unique tmp filename per call so concurrent writers targeting the
        # same `path` (e.g. two simultaneous /pitch POSTs producing the same
        # result file) can't clobber each other's in-flight tmp before the
        # rename. Each caller writes its own tmp then atomically replaces
        # `path`; last writer wins on `path` (deterministic content).
        tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    def record(self, pitch: PitchPayload) -> SessionResult:
        """Persist a pitch upload and, if its pair is already present,
        triangulate the session.

        Lock discipline: the critical section is kept tight. Disk I/O and
        NumPy triangulation happen OUTSIDE `self._lock` so heartbeats and
        status polls don't block on a slow disk or a millisecond-scale
        triangulation run.

        Race note: two simultaneous A+B uploads for the same session can
        each observe the other inside their own critical section and both
        trigger triangulation. That's redundant CPU but not incorrect —
        both computations take the same (a, b) snapshot and deterministically
        yield the same points; last-writer-wins on `self.results[sid]`
        and on the result JSON file (both atomic)."""
        pitch_path = self._pitch_path(pitch.camera_id, pitch.session_id)
        normalized_paths = self._normalize_paths(pitch.paths)
        if not normalized_paths:
            normalized_paths = self._paths_for_pitch(pitch)
        pitch.paths = sorted(p.value for p in normalized_paths)

        # --- Critical section 1: mutate pitches + drive session FSM. ---
        # Grab the pair snapshot here so triangulation below runs against a
        # consistent view without re-entering the lock.
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch
            # Drive the session state machine forward — any upload arriving
            # while armed disarms the session (one-shot pattern). The other
            # camera, if it was also recording, gets "disarm" on its next
            # /status poll and cleans up.
            self._register_upload_in_session_locked(pitch)

        # --- Outside the lock: write pitch JSON. Filename is unique per
        # (camera, session) and each pitch uses its own tmp file, so two
        # concurrent calls here cannot collide. ---
        self._atomic_write(pitch_path, pitch.model_dump_json())

        # --- Outside the lock: build the result + triangulate if paired. ---
        result = self._rebuild_result_for_session(pitch.session_id)

        # --- Outside the lock: persist the result JSON. ---
        self._atomic_write(
            self._result_path(pitch.session_id),
            result.model_dump_json(),
        )

        # --- Critical section 2: publish the result into the in-memory map. ---
        with self._lock:
            self.results[pitch.session_id] = result
        return result

    def attach_on_device_analysis(
        self,
        analysis: PitchAnalysisPayload,
    ) -> SessionResult:
        """Merge a late-arriving on-device post-pass into an existing pitch.

        The base pitch must already exist (raw MOV upload in `camera_only` /
        `dual`, or an earlier frames-only `/pitch` in `on_device`). We overwrite
        `frames_on_device` wholesale because each upload is an authoritative
        rerun over the finalized local MOV, not an incremental append."""
        with self._lock:
            existing = self.pitches.get((analysis.camera_id, analysis.session_id))
        if existing is None:
            raise KeyError((analysis.camera_id, analysis.session_id))

        merged = existing.model_copy(deep=True)
        merged.frames_ios_post = list(analysis.frames_on_device)
        merged.frames_on_device = list(analysis.frames_on_device)
        if analysis.capture_telemetry is not None:
            merged.capture_telemetry = analysis.capture_telemetry
        return self.record(merged)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            sessions = sorted({sid for _, sid in self.pitches.keys()})
            completed = [
                k for k, r in self.results.items()
                if r.camera_a_received and r.camera_b_received and not r.error
            ]
            return {
                "state": "receiving" if self.pitches else "idle",
                "received_sessions": sessions,
                "completed_sessions": sorted(completed),
            }

    def latest(self) -> SessionResult | None:
        """Most recently written result. File mtime on disk would be more
        correct, but the in-memory ordering is good enough for the /latest
        endpoint's "last thing uploaded" semantic — sessions sort
        lexicographically by id which is time-of-generation-adjacent."""
        with self._lock:
            if not self.results:
                return None
            return self.results[max(self.results.keys())]

    def get(self, session_id: str) -> SessionResult | None:
        with self._lock:
            return self.results.get(session_id)

    def store_result(self, result: SessionResult) -> None:
        self._atomic_write(self._result_path(result.session_id), result.model_dump_json())
        with self._lock:
            self.results[result.session_id] = result

    def pitches_for_session(self, session_id: str) -> dict[str, PitchPayload]:
        """Snapshot of all pitches currently stored for `session_id`, keyed
        by camera_id. Returns an empty dict if the session has not been
        seen."""
        with self._lock:
            return {
                cam_id: p
                for (cam_id, sid), p in self.pitches.items()
                if sid == session_id
            }

    # ------------------------------------------------------------------
    # Dashboard-control plumbing: heartbeat registry + session state
    # ------------------------------------------------------------------

    # --- Calibration frame (one-shot high-res) ---------------------------
    # Preview is 480p JPEG (advisory). For auto-calibration we want the
    # full capture resolution — ArUco corner precision scales linearly
    # with pixel count, and the intrinsics we derive downstream must
    # match MOV dims exactly or `recover_extrinsics` produces garbage
    # poses. iOS pushes a native-resolution JPEG when the WS settings message
    # sets `calibration_frame_requested: true` for this camera.

    def request_calibration_frame(self, camera_id: str) -> None:
        """Flag a camera to send one full-res JPEG on its next captureOutput.
        Idempotent — re-POST refreshes the TTL."""
        now = self._time_fn()
        with self._lock:
            self._cal_frame_requested[camera_id] = now + _CALIBRATION_FRAME_TTL_S

    def is_calibration_frame_requested(self, camera_id: str) -> bool:
        """True if the flag is pending and within TTL. Lazy-sweeps stale."""
        now = self._time_fn()
        with self._lock:
            exp = self._cal_frame_requested.get(camera_id)
            if exp is None:
                return False
            if now >= exp:
                self._cal_frame_requested.pop(camera_id, None)
                return False
            return True

    def store_calibration_frame(self, camera_id: str, jpeg_bytes: bytes) -> None:
        """Phone pushed a calibration frame; stash it and clear the flag."""
        now = self._time_fn()
        with self._lock:
            self._cal_frames[camera_id] = (jpeg_bytes, now)
            self._cal_frame_requested.pop(camera_id, None)

    def consume_calibration_frame(
        self, camera_id: str, max_age_s: float = _CALIBRATION_FRAME_TTL_S,
    ) -> tuple[bytes, float] | None:
        """Atomic pop-if-fresh. Returns None if no frame cached or stale."""
        now = self._time_fn()
        with self._lock:
            got = self._cal_frames.pop(camera_id, None)
            if got is None:
                return None
            _, ts = got
            if now - ts > max_age_s:
                return None
            return got

    # --- Auto-calibration runs -------------------------------------------

    def start_auto_cal_run(self, camera_id: str) -> _AutoCalibrationRun:
        now = self._time_fn()
        with self._lock:
            current = self._auto_cal_runs.get(camera_id)
            if current is not None and current.status not in {"completed", "failed"}:
                raise ValueError(f"auto calibration already running for camera {camera_id}")
            run = _AutoCalibrationRun(
                id=f"acr_{secrets.token_hex(4)}",
                camera_id=camera_id,
                status="searching",
                started_at=now,
                updated_at=now,
                summary="Waiting for enough stable frames",
            )
            self._auto_cal_runs[camera_id] = run
            return _AutoCalibrationRun(**run.to_dict())

    def update_auto_cal_run(self, camera_id: str, **updates: Any) -> _AutoCalibrationRun | None:
        now = self._time_fn()
        with self._lock:
            run = self._auto_cal_runs.get(camera_id)
            if run is None:
                return None
            for key, value in updates.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            run.updated_at = now
            return _AutoCalibrationRun(**run.to_dict())

    def finish_auto_cal_run(
        self,
        camera_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        summary: str | None = None,
        detail: str | None = None,
        applied: bool | None = None,
    ) -> _AutoCalibrationRun | None:
        now = self._time_fn()
        with self._lock:
            run = self._auto_cal_runs.get(camera_id)
            if run is None:
                return None
            run.status = status
            run.updated_at = now
            run.result = result
            if summary is not None:
                run.summary = summary
            if detail is not None:
                run.detail = detail
            if applied is not None:
                run.applied = applied
            snap = _AutoCalibrationRun(**run.to_dict())
            self._auto_cal_last[camera_id] = snap
            if status in {"completed", "failed"}:
                self._auto_cal_runs.pop(camera_id, None)
            return snap

    def auto_cal_status(self) -> dict[str, Any]:
        with self._lock:
            active = {cam: run.to_dict() for cam, run in self._auto_cal_runs.items()}
            last = {cam: run.to_dict() for cam, run in self._auto_cal_last.items()}
            return {"active": active, "last": last}

    def live_session_summary(self) -> dict[str, Any] | None:
        session = self.session_snapshot()
        if session is None:
            return None
        with self._lock:
            live = self._live_pairings.get(session.id)
        if live is None:
            return {
                "session_id": session.id,
                "armed": session.armed,
                "paths": sorted(p.value for p in session.paths),
                "frame_counts": {},
                "point_count": 0,
                "abort_reasons": {},
            }
        return {
            "session_id": session.id,
            "armed": session.armed,
            "paths": sorted(p.value for p in session.paths),
            "frame_counts": dict(live.frame_counts),
            "point_count": len(live.triangulated),
            "completed_cameras": sorted(live.completed_cameras),
            "abort_reasons": dict(live.abort_reasons),
        }

    def _live_time_sync_intent_locked(self, now: float) -> _LegacyTimeSyncIntent | None:
        intent = self._current_time_sync_intent
        if intent is None:
            return None
        if intent.expires_at <= now:
            self._current_time_sync_intent = None
            return None
        return intent

    def _claim_time_sync_intent_locked(self, now: float) -> _LegacyTimeSyncIntent:
        intent = self._live_time_sync_intent_locked(now)
        if intent is not None:
            return intent
        intent = _LegacyTimeSyncIntent(
            id=_new_sync_id(),
            started_at=now,
            expires_at=now + _TIME_SYNC_INTENT_WINDOW_S,
        )
        self._current_time_sync_intent = intent
        return intent

    def claim_time_sync_intent(self) -> _LegacyTimeSyncIntent:
        """Return the currently-live legacy chirp sync run id, minting a
        fresh one when the prior listening window expired."""
        now = self._time_fn()
        with self._lock:
            return self._claim_time_sync_intent_locked(now)

    def heartbeat(
        self,
        camera_id: str,
        time_synced: bool = False,
        time_sync_id: str | None = None,
        sync_anchor_timestamp_s: float | None = None,
    ) -> None:
        """Record one liveness ping. Overwrites the previous entry for this
        camera so `last_seen_at`, `time_synced`, and the currently-held
        legacy chirp sync id always reflect the latest beat. Prunes any
        entry older than `_DEVICE_GC_AFTER_S` and enforces a hard size cap
        (evicts the oldest by `last_seen_at`) so a misbehaving client can't
        grow the registry without bound."""
        now = self._time_fn()
        with self._lock:
            self._devices[camera_id] = Device(
                camera_id=camera_id,
                last_seen_at=now,
                time_synced=time_synced,
                time_sync_id=(time_sync_id if time_synced else None),
                time_sync_at=(now if time_synced and time_sync_id is not None else None),
                sync_anchor_timestamp_s=(
                    float(sync_anchor_timestamp_s)
                    if time_synced and sync_anchor_timestamp_s is not None
                    else None
                ),
            )
            # GC stale entries first — cheap and keeps the cap hit rare.
            stale = [
                cam for cam, dev in self._devices.items()
                if now - dev.last_seen_at > _DEVICE_GC_AFTER_S
            ]
            for cam in stale:
                del self._devices[cam]
            # Hard cap: if GC didn't bring us under, drop oldest.
            while len(self._devices) > _DEVICE_REGISTRY_CAP:
                oldest = min(
                    self._devices.items(),
                    key=lambda kv: kv[1].last_seen_at,
                )[0]
                del self._devices[oldest]

    def _common_time_sync_id_locked(self, now: float) -> str | None:
        fresh = [
            d for d in self._devices.values()
            if now - d.last_seen_at <= _DEVICE_STALE_S
        ]
        if len(fresh) < 2:
            return None
        sync_ids: set[str] = set()
        for dev in fresh:
            if (
                not dev.time_synced
                or dev.time_sync_id is None
                or dev.time_sync_at is None
                or now - dev.time_sync_at > _TIME_SYNC_MAX_AGE_S
            ):
                return None
            sync_ids.add(dev.time_sync_id)
        if len(sync_ids) != 1:
            return None
        return next(iter(sync_ids))

    def online_devices(
        self, stale_after_s: float = _DEVICE_STALE_S
    ) -> list[Device]:
        """Snapshot of devices whose last heartbeat is within
        `stale_after_s` of now. Returned sorted by camera_id for
        deterministic rendering."""
        now = self._time_fn()
        with self._lock:
            fresh = [
                d for d in self._devices.values()
                if now - d.last_seen_at <= stale_after_s
            ]
        fresh.sort(key=lambda d: d.camera_id)
        return fresh

    def known_camera_ids(self) -> list[str]:
        """All camera ids that have ever heartbeated this run — used by WS
        broadcast targets that want to notify siblings regardless of current
        liveness (e.g. calibration updates, which the other cam might care
        about even if its heartbeat lapsed briefly)."""
        with self._lock:
            return list(self._devices.keys())

    def device_snapshot(self, camera_id: str) -> Device | None:
        with self._lock:
            dev = self._devices.get(camera_id)
            if dev is None:
                return None
            return Device(
                camera_id=dev.camera_id,
                last_seen_at=dev.last_seen_at,
                time_synced=dev.time_synced,
                time_sync_id=dev.time_sync_id,
                time_sync_at=dev.time_sync_at,
                sync_anchor_timestamp_s=dev.sync_anchor_timestamp_s,
            )

    def _check_session_timeout_locked(self, now: float) -> None:
        """If the current session has exceeded its max_duration_s, transition
        it to ended. Assumes the caller holds `self._lock`."""
        s = self._current_session
        if s is None or s.ended_at is not None:
            return
        if now - s.started_at > s.max_duration_s:
            s.ended_at = now
            self._last_ended_session = s
            self._current_session = None

    def current_session(self) -> Session | None:
        """Current armed session (None if idle). Side-effect: lazily applies
        the timeout so polling callers (status, commands) drive the state
        machine forward without a background task."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            return self._current_session

    def arm_session(
        self,
        max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S,
        paths: set[DetectionPath] | None = None,
    ) -> Session:
        """Begin a new armed session. If one is already armed, return it
        unchanged (idempotent so dashboard double-clicks don't double-arm).
        Snapshots the current global `capture_mode` so a late dashboard
        toggle can't disturb the in-flight recording."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            if self._current_session is not None:
                return self._current_session
            chosen_paths = set(paths or self._default_paths or _DEFAULT_PATHS)
            session = Session(
                id=_new_session_id(),
                started_at=now,
                max_duration_s=max_duration_s,
                paths=chosen_paths,
                mode=mode_for_paths(chosen_paths),
                tracking_exposure_cap=self._tracking_exposure_cap,
                sync_id=self._common_time_sync_id_locked(now),
            )
            self._live_pairings[session.id] = LivePairingSession(session.id)
            self._current_session = session
            self._current_time_sync_intent = None
            return session

    def current_mode(self) -> CaptureMode:
        """Dashboard-selected capture mode (global, not session-scoped).
        iPhones read this from WS settings messages to render the HUD mode
        chip even while idle."""
        with self._lock:
            return mode_for_paths(self._default_paths)

    def default_paths(self) -> set[DetectionPath]:
        with self._lock:
            return set(self._default_paths)

    def set_mode(self, mode: CaptureMode) -> CaptureMode:
        """Record the dashboard's mode choice. Only affects sessions armed
        after this call — in-flight sessions keep their snapshot mode."""
        with self._lock:
            self._current_mode = mode
            self._default_paths = paths_for_mode(mode)
            self._persist_runtime_settings_locked()
            return mode

    def set_default_paths(self, paths: set[DetectionPath]) -> set[DetectionPath]:
        if not paths:
            raise ValueError("at least one detection path must be enabled")
        with self._lock:
            self._default_paths = set(paths)
            self._current_mode = mode_for_paths(self._default_paths)
            self._persist_runtime_settings_locked()
            return set(self._default_paths)

    # ---- Runtime tunables (chirp detection threshold + WS heartbeat cadence) --
    #
    # Both are pushed from the dashboard, surface in WS settings messages,
    # and are hot-applied by iOS. Persisted together to a single JSON file
    # so a restart doesn't drop the operator's choice. iOS Settings UI still
    # holds a local bootstrap default but the server push wins on first
    # successful WS settings round-trip.

    _CHIRP_THRESHOLD_MIN = 0.01
    _CHIRP_THRESHOLD_MAX = 1.0
    _HEARTBEAT_INTERVAL_MIN = 1.0
    _HEARTBEAT_INTERVAL_MAX = 60.0

    def _load_runtime_settings_from_disk(self) -> None:
        path = self._runtime_settings_path
        if not path.exists():
            return
        try:
            obj = json.loads(path.read_text())
        except Exception as e:
            logger.warning("skip corrupt runtime_settings %s: %s", path, e)
            return
        thr = obj.get("chirp_detect_threshold")
        if isinstance(thr, (int, float)) and self._CHIRP_THRESHOLD_MIN <= thr <= self._CHIRP_THRESHOLD_MAX:
            self._chirp_detect_threshold = float(thr)
        ivl = obj.get("heartbeat_interval_s")
        if isinstance(ivl, (int, float)) and self._HEARTBEAT_INTERVAL_MIN <= ivl <= self._HEARTBEAT_INTERVAL_MAX:
            self._heartbeat_interval_s = float(ivl)
        ch = obj.get("capture_height_px")
        if isinstance(ch, int) and ch in self._ALLOWED_CAPTURE_HEIGHTS:
            self._capture_height_px = ch
        tec = obj.get("tracking_exposure_cap")
        if isinstance(tec, str):
            try:
                self._tracking_exposure_cap = TrackingExposureCapMode(tec)
            except ValueError:
                pass
        paths = obj.get("default_paths")
        if isinstance(paths, list):
            parsed: set[DetectionPath] = set()
            for item in paths:
                if not isinstance(item, str):
                    continue
                try:
                    parsed.add(DetectionPath(item))
                except ValueError:
                    continue
            if parsed:
                self._default_paths = parsed
                self._current_mode = mode_for_paths(parsed)
        logger.info(
            "restored runtime_settings: chirp=%.3f interval_s=%.2f capture_h=%d tracking_exposure=%s paths=%s",
            self._chirp_detect_threshold,
            self._heartbeat_interval_s,
            self._capture_height_px,
            self._tracking_exposure_cap.value,
            sorted(p.value for p in self._default_paths),
        )

    _ALLOWED_CAPTURE_HEIGHTS = (720, 1080)

    def _persist_runtime_settings_locked(self) -> None:
        """Caller must hold `self._lock`. Atomic write."""
        payload = json.dumps(
            {
                "chirp_detect_threshold": self._chirp_detect_threshold,
                "heartbeat_interval_s": self._heartbeat_interval_s,
                "capture_height_px": self._capture_height_px,
                "tracking_exposure_cap": self._tracking_exposure_cap.value,
                "default_paths": sorted(p.value for p in self._default_paths),
            },
            indent=2,
        )
        self._atomic_write(self._runtime_settings_path, payload)

    def capture_height_px(self) -> int:
        with self._lock:
            return self._capture_height_px

    def set_capture_height_px(self, value: int) -> int:
        if not isinstance(value, int):
            raise ValueError("capture_height must be an int")
        if value not in self._ALLOWED_CAPTURE_HEIGHTS:
            raise ValueError(
                f"capture_height {value} not in {self._ALLOWED_CAPTURE_HEIGHTS}"
            )
        with self._lock:
            self._capture_height_px = value
            self._persist_runtime_settings_locked()
            return value

    def chirp_detect_threshold(self) -> float:
        with self._lock:
            return self._chirp_detect_threshold

    def set_chirp_detect_threshold(self, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("threshold must be numeric")
        v = float(value)
        if not (self._CHIRP_THRESHOLD_MIN <= v <= self._CHIRP_THRESHOLD_MAX):
            raise ValueError(
                f"threshold {v} out of range "
                f"[{self._CHIRP_THRESHOLD_MIN}, {self._CHIRP_THRESHOLD_MAX}]"
            )
        with self._lock:
            self._chirp_detect_threshold = v
            self._persist_runtime_settings_locked()
            return v

    def heartbeat_interval_s(self) -> float:
        with self._lock:
            return self._heartbeat_interval_s

    def set_heartbeat_interval_s(self, value: float) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError("interval must be numeric")
        v = float(value)
        if not (self._HEARTBEAT_INTERVAL_MIN <= v <= self._HEARTBEAT_INTERVAL_MAX):
            raise ValueError(
                f"interval {v} out of range "
                f"[{self._HEARTBEAT_INTERVAL_MIN}, {self._HEARTBEAT_INTERVAL_MAX}]"
            )
        with self._lock:
            self._heartbeat_interval_s = v
            self._persist_runtime_settings_locked()
            return v

    def tracking_exposure_cap(self) -> TrackingExposureCapMode:
        with self._lock:
            return self._tracking_exposure_cap

    def set_tracking_exposure_cap(self, mode: TrackingExposureCapMode) -> TrackingExposureCapMode:
        with self._lock:
            self._tracking_exposure_cap = mode
            self._persist_runtime_settings_locked()
            return mode

    def stop_session(self) -> Session | None:
        """End the current armed session (operator pressed Stop on the
        dashboard). Returns the ended session, or None if nothing was
        armed. Data captured during the session is preserved; `Stop` is a
        normal lifecycle event, not an abort."""
        now = self._time_fn()
        with self._lock:
            s = self._current_session
            if s is None or s.ended_at is not None:
                return None
            s.ended_at = now
            self._last_ended_session = s
            self._current_session = None
            return s

    def log_sync_event(
        self, source: str, event: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Append one diagnostic line to the in-memory sync log. Both server
        code paths and the phone-pushed `POST /sync/log` endpoint end up
        here. Safe to call with the lock held or released — the ring append
        is the only shared-state mutation."""
        entry = SyncLogEntry(
            ts=self._time_fn(),
            source=source,
            event=event,
            detail=detail or {},
        )
        with self._lock:
            self._sync_log.append(entry)
        logger.info(
            "sync_log source=%s event=%s detail=%s",
            source, event, entry.detail,
        )

    def sync_logs(self, limit: int = 200) -> list[SyncLogEntry]:
        """Most recent N diagnostic entries, oldest-first."""
        with self._lock:
            return list(self._sync_log)[-limit:]

    def _check_sync_timeout_locked(self, now: float) -> None:
        """Drop `_current_sync` if it has been waiting past the timeout.
        Caller must hold `self._lock`. Also latches the cooldown so a new
        run can't start immediately after a timeout — gives the operator
        a window to see the failure surface on the dashboard. Synthesises
        an aborted `SyncResult` carrying whatever partial reports landed
        (incl. traces) so the `/sync` panel can show sub-threshold peaks /
        noise floor from a failed run instead of going blank."""
        s = self._current_sync
        if s is None:
            return
        if now - s.started_at > _SYNC_TIMEOUT_S:
            received = sorted(s.reports.keys())
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="timeout",
                detail={"id": s.id, "reports_received": received},
            ))
            logger.warning(
                "sync timeout id=%s received=%s", s.id, received
            )
            self._last_sync_result = self._build_aborted_result_locked(s, now)
            self._current_sync = None
            self._sync_cooldown_until = now + _SYNC_COOLDOWN_S

    def _log_trace_post_mortem_locked(
        self, run_id: str, label: str,
        trace: list | None, threshold: float,
    ) -> None:
        """Compute + log {best_peak, t_best, median, p90, margin_to_threshold}
        for one matched-filter trace so post-mortem failures show up in the
        sync log (and the terminal) with quantitative context. Silently
        skips empty / missing traces — the log entry exists purely to let
        me read `how close did that band come to firing?`."""
        if not trace:
            self._sync_log.append(SyncLogEntry(
                ts=self._time_fn(), source="server", event="post_mortem",
                detail={"id": run_id, "stream": label, "status": "no_trace"},
            ))
            logger.info("sync post_mortem id=%s stream=%s status=no_trace", run_id, label)
            return
        peaks = sorted(float(s.peak) for s in trace)
        n = len(peaks)
        best = peaks[-1]
        median = peaks[n // 2]
        p90 = peaks[min(n - 1, int(n * 0.9))]
        # Find t of best sample (first occurrence)
        t_best = None
        for s in trace:
            if float(s.peak) == best:
                t_best = float(s.t)
                break
        margin = best / threshold if threshold > 0 else 0.0
        detail = {
            "id": run_id, "stream": label, "status": "ok",
            "n": n, "best": round(best, 4), "t_best": round(t_best or 0.0, 3),
            "noise_median": round(median, 4), "noise_p90": round(p90, 4),
            "threshold": round(threshold, 4),
            "margin_x_threshold": round(margin, 3),
        }
        self._sync_log.append(SyncLogEntry(
            ts=self._time_fn(), source="server", event="post_mortem",
            detail=detail,
        ))
        logger.info(
            "sync post_mortem id=%s stream=%s best=%.3f@%.2fs noise_med=%.3f p90=%.3f thr=%.3f margin=%.2fx n=%d",
            run_id, label, best, t_best or 0.0, median, p90, threshold, margin, n,
        )

    def _build_aborted_result_locked(
        self, run: "SyncRun", solved_at: float
    ) -> SyncResult:
        """Build a diagnostic-only `SyncResult` from a timed-out or
        partially-reported run. Pulls whatever traces + abort reasons the
        phones shipped so the dashboard can render the failed run's
        matched-filter plot. delta / distance / raw timestamps stay None;
        aborted=True is the flag dashboards should branch on."""
        rep_a = run.reports.get("A")
        rep_b = run.reports.get("B")
        reasons: dict[str, str] = {}
        if rep_a is not None and rep_a.aborted and rep_a.abort_reason:
            reasons["A"] = rep_a.abort_reason
        if rep_b is not None and rep_b.aborted and rep_b.abort_reason:
            reasons["B"] = rep_b.abort_reason
        if rep_a is None:
            reasons.setdefault("A", "no_report")
        if rep_b is None:
            reasons.setdefault("B", "no_report")
        # Post-mortem per stream: logs best peak, noise floor, and the
        # margin to threshold so I can read the log and learn why this
        # run failed (too far? wrong band? speaker silent?).
        thr = self._chirp_detect_threshold
        self._log_trace_post_mortem_locked(
            run.id, "A.self",  rep_a.trace_self if rep_a else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "A.other", rep_a.trace_other if rep_a else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "B.self",  rep_b.trace_self if rep_b else None, thr)
        self._log_trace_post_mortem_locked(
            run.id, "B.other", rep_b.trace_other if rep_b else None, thr)
        return SyncResult(
            id=run.id,
            delta_s=None,
            distance_m=None,
            solved_at=solved_at,
            t_a_self_s=rep_a.t_self_s if rep_a else None,
            t_a_from_b_s=rep_a.t_from_other_s if rep_a else None,
            t_b_self_s=rep_b.t_self_s if rep_b else None,
            t_b_from_a_s=rep_b.t_from_other_s if rep_b else None,
            aborted=True,
            abort_reasons=reasons,
            trace_a_self=rep_a.trace_self if rep_a else None,
            trace_a_other=rep_a.trace_other if rep_a else None,
            trace_b_self=rep_b.trace_self if rep_b else None,
            trace_b_other=rep_b.trace_other if rep_b else None,
        )

    def current_sync(self) -> SyncRun | None:
        """Snapshot of the in-progress sync run (None when idle). Lazily
        applies the timeout on read, mirroring `current_session()`."""
        now = self._time_fn()
        with self._lock:
            self._check_sync_timeout_locked(now)
            return self._current_sync

    def last_sync_result(self) -> SyncResult | None:
        """Most recently solved sync result, or None if no sync has ever
        succeeded on this server instance."""
        with self._lock:
            return self._last_sync_result

    def sync_cooldown_remaining_s(self) -> float:
        """Seconds remaining on the post-sync cooldown. 0 when ready."""
        now = self._time_fn()
        with self._lock:
            return max(0.0, self._sync_cooldown_until - now)

    # --- Time-sync command dispatch (single-listener, third-device chirp) ---
    #
    # Distinct from the mutual-chirp `/sync/start` flow above — this is the
    # dashboard-remote equivalent of the iPhone's local 時間校正 button: a
    # one-shot "listen for a chirp now" command each target phone consumes
    # on its next heartbeat.

    def trigger_sync_command(
        self, camera_ids: list[str] | None = None,
    ) -> list[str]:
        """Set a pending time-sync command flag for the given cameras (or
        all currently-online cameras when `camera_ids` is None). Skips any
        camera participating in a currently-armed session — firing a chirp
        listen in the middle of a recording would disrupt the armed clip.
        Returns the list of camera_ids actually targeted (sorted, deduped).

        Idempotent: re-dispatching to a camera that already has a pending
        flag just refreshes its TTL expiry. The phone still fires once on
        the next WS heartbeat tick because flag consumption is one-shot."""
        now = self._time_fn()
        online_ids = [d.camera_id for d in self.online_devices()]
        current = self.current_session()  # applies timeout
        with self._lock:
            armed = current is not None and current.ended_at is None
            if camera_ids is None:
                targets = list(online_ids)
            else:
                # Only dispatch to cams we've actually seen heartbeat from;
                # an unknown id is silently dropped (phone will register on
                # its next heartbeat and the operator can re-click).
                online_set = set(online_ids)
                targets = [c for c in camera_ids if c in online_set]
            if armed:
                # Skip every online camera — every online cam during an
                # armed session is considered "recording" in this rig.
                targets = []
            intent = self._claim_time_sync_intent_locked(now) if targets else None
            dispatched: list[str] = []
            for cam in sorted(set(targets)):
                assert intent is not None
                self._sync_command_pending[cam] = _LegacyTimeSyncIntent(
                    id=intent.id,
                    started_at=intent.started_at,
                    expires_at=now + _SYNC_COMMAND_TTL_S,
                )
                dispatched.append(cam)
            # Also sweep any expired entries so the map can't grow forever
            # even if `consume_sync_command` never runs for a stale cam.
            stale = [
                c for c, pending in self._sync_command_pending.items()
                if pending.expires_at <= now
            ]
            for c in stale:
                del self._sync_command_pending[c]
        return dispatched

    def consume_sync_command(self, camera_id: str) -> tuple[str | None, str | None]:
        """Atomically pop + return a pending time-sync command for the
        named camera, or `(None, None)` when there's nothing queued. Used by the
        WS heartbeat handler so the same beat that reports liveness also
        clears the flag — one-shot dispatch, matching how arm/disarm
        commands self-cancel on consumption.

        Expired entries (past `_SYNC_COMMAND_TTL_S`) are silently dropped
        without firing — the operator is presumed to have moved on."""
        now = self._time_fn()
        with self._lock:
            pending = self._sync_command_pending.pop(camera_id, None)
        if pending is None:
            return None, None
        if pending.expires_at <= now:
            return None, None
        return "start", pending.id

    def pending_sync_commands(self) -> dict[str, str]:
        """Snapshot of cameras with a currently-live pending time-sync
        command. Read-only — used by /status so the dashboard can render
        a "pending" indicator on each device chip until the phone's next
        heartbeat drains the flag."""
        now = self._time_fn()
        with self._lock:
            return {
                cam: "start"
                for cam, pending in self._sync_command_pending.items()
                if pending.expires_at > now
            }

    def start_sync(self) -> tuple[SyncRun | None, str | None]:
        """Begin a mutual-sync run. Returns `(run, None)` on success or
        `(None, reason)` on conflict. Precondition priority (match the
        endpoint's response mapping):
          1. An armed session → `"session_armed"`
          2. Sync already in progress → `"sync_in_progress"`
          3. Cooldown window still active → `"cooldown"`
          4. Fewer than 2 cameras online → `"devices_missing"`
        """
        now = self._time_fn()
        # current_session / online_devices both take the lock internally;
        # fetch them first so the critical section below is short.
        current = self.current_session()
        online_ids = [d.camera_id for d in self.online_devices()]
        with self._lock:
            self._check_sync_timeout_locked(now)
            reject_reason: str | None = None
            if current is not None:
                reject_reason = "session_armed"
            elif self._current_sync is not None:
                reject_reason = "sync_in_progress"
            elif now < self._sync_cooldown_until:
                reject_reason = "cooldown"
            elif len(online_ids) < 2:
                reject_reason = "devices_missing"
            if reject_reason is not None:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="start_rejected",
                    detail={"reason": reject_reason, "online": online_ids},
                ))
                logger.info(
                    "sync start rejected reason=%s online=%s",
                    reject_reason, online_ids,
                )
                return None, reject_reason
            run = SyncRun(id=_new_sync_id(), started_at=now)
            self._current_sync = run
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="start",
                detail={"id": run.id, "online": online_ids},
            ))
            logger.info("sync start id=%s online=%s", run.id, online_ids)
            return run, None

    def record_sync_report(
        self, report: SyncReport
    ) -> tuple[SyncRun | None, SyncResult | None, str | None]:
        """Attach a phone's matched-filter report to the current run.
        Returns `(run_after, solved_result_or_None, reason_or_None)`:
          - `reason == "no_sync"` when no run is active
          - `reason == "stale_sync_id"` when report belongs to a past run
          - `reason is None` on success (run_after is always the updated
            run; solved_result is set on the second report when the
            solver fires)

        When both roles have reported the solver runs inside the lock
        (O(1) arithmetic), the result is latched into `_last_sync_result`,
        the run is cleared, and cooldown begins."""
        now = self._time_fn()
        with self._lock:
            self._check_sync_timeout_locked(now)
            run = self._current_sync
            if run is None:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="report_no_sync",
                    detail={"role": report.role, "sync_id": report.sync_id},
                ))
                logger.info(
                    "sync report no active sync role=%s sync_id=%s",
                    report.role, report.sync_id,
                )
                return None, None, "no_sync"
            if run.id != report.sync_id:
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="report_stale",
                    detail={
                        "role": report.role,
                        "posted_sync_id": report.sync_id,
                        "current_sync_id": run.id,
                    },
                ))
                logger.info(
                    "sync report stale role=%s posted=%s current=%s",
                    report.role, report.sync_id, run.id,
                )
                return run, None, "stale_sync_id"
            run.reports[report.role] = report
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="report_received",
                detail={
                    "role": report.role,
                    "t_self_s": report.t_self_s,
                    "t_from_other_s": report.t_from_other_s,
                    "emitted_band": report.emitted_band,
                    "received_so_far": sorted(run.reports.keys()),
                },
            ))
            logger.info(
                "sync report received id=%s role=%s t_self=%.6f t_from_other=%.6f",
                run.id, report.role, report.t_self_s, report.t_from_other_s,
            )
            if not run.complete:
                return run, None, None
            rep_a = run.reports["A"]
            rep_b = run.reports["B"]
            # Abort path: either phone flagged aborted, OR one of its
            # required timestamps is None. Solver needs four non-null
            # timestamps; anything less → synthesize a diagnostic-only
            # result carrying the traces + reasons so the /sync panel
            # still visualises the failure.
            any_aborted = (
                rep_a.aborted or rep_b.aborted
                or rep_a.t_self_s is None or rep_a.t_from_other_s is None
                or rep_b.t_self_s is None or rep_b.t_from_other_s is None
            )
            if any_aborted:
                result = self._build_aborted_result_locked(run, now)
                self._last_sync_result = result
                self._current_sync = None
                self._sync_cooldown_until = now + _SYNC_COOLDOWN_S
                self._sync_log.append(SyncLogEntry(
                    ts=now, source="server", event="aborted",
                    detail={
                        "id": result.id,
                        "reasons": result.abort_reasons,
                        "had_traces": {
                            "a_self": rep_a.trace_self is not None,
                            "a_other": rep_a.trace_other is not None,
                            "b_self": rep_b.trace_self is not None,
                            "b_other": rep_b.trace_other is not None,
                        },
                    },
                ))
                logger.warning(
                    "sync aborted id=%s reasons=%s",
                    result.id, result.abort_reasons,
                )
                return None, result, None
            result = compute_mutual_sync(rep_a, rep_b, solved_at=now)
            # Attach per-role matched-filter traces so the /sync debug
            # plot can render post-hoc (page reload / past-run inspection)
            # — the /sync/state live tick also rides this payload via
            # model_dump. Silently None when the iPhone didn't include
            # them (old builds).
            result = result.model_copy(update={
                "trace_a_self": rep_a.trace_self,
                "trace_a_other": rep_a.trace_other,
                "trace_b_self": rep_b.trace_self,
                "trace_b_other": rep_b.trace_other,
            })
            self._last_sync_result = result
            self._current_sync = None
            self._sync_cooldown_until = now + _SYNC_COOLDOWN_S
            self._sync_log.append(SyncLogEntry(
                ts=now, source="server", event="solved",
                detail={
                    "id": result.id,
                    "delta_s": result.delta_s,
                    "distance_m": result.distance_m,
                },
            ))
            logger.info(
                "sync solved id=%s delta_s=%.6f distance_m=%.3f",
                result.id, result.delta_s, result.distance_m,
            )
            return None, result, None

    def clear_last_ended_session(self) -> bool:
        """Drop the `_last_ended_session` pointer so the dashboard's
        session card goes blank again. No-op (returns False) when a
        session is currently armed or there's nothing to clear — the
        pointer is strictly a dashboard-idle-state concern."""
        with self._lock:
            if self._current_session is not None and self._current_session.ended_at is None:
                return False
            if self._last_ended_session is None:
                return False
            self._last_ended_session = None
            return True

    def _register_upload_in_session_locked(self, pitch: PitchPayload) -> None:
        """Called from `record()` while the state lock is held. Appends
        the camera to the session's uploads_received list so the events
        panel can show which phones have flushed. Does NOT end the
        session — in the current pivot, the iPhone only flushes a
        recording after receiving `disarm`, so the session is already
        ended by the time this fires."""
        s = self._current_session
        if s is None or s.ended_at is not None:
            return
        if pitch.session_id != s.id:
            # Upload belongs to a different session (previous armed window
            # the phone is only now flushing). Don't touch the current one.
            return
        if pitch.camera_id not in s.uploads_received:
            s.uploads_received.append(pitch.camera_id)

    def session_snapshot(self) -> Session | None:
        """Return the session most relevant to a status caller: the current
        armed session if any, otherwise the most recently ended one (so
        the iPhone sees session.armed == False during the disarm echo
        window, and the dashboard can keep the session id visible until
        the operator hits Clear)."""
        current = self.current_session()
        if current is not None:
            return current
        with self._lock:
            return self._last_ended_session

    def commands_for_devices(self) -> dict[str, str]:
        """Derive per-device commands from the current session state. The
        iPhone reads `commands[self.camera_id]` on each /status poll:
          - "sync_run" if a mutual-sync run is active AND this phone has
            not yet posted its report for that run (preempts arm/disarm —
            guarded by `start_sync` to be mutually exclusive with an
            armed session anyway)
          - "arm"    if a session is currently armed
          - "disarm" if a session ended within _DISARM_ECHO_S ago
          - absent   otherwise (steady state, no action required)

        Once a phone has reported for the current sync, we stop re-
        advertising `sync_run` to it so the phone doesn't re-trigger on
        the next heartbeat tick while the peer's report is still in
        flight — `lastAppliedCommand` de-dupe on the phone dedupes on
        `(command, sync_id)`, but dropping the command here is an extra
        defense and keeps the command dict's semantics clean."""
        now = self._time_fn()
        current = self.current_session()  # applies timeout
        online_ids = [d.camera_id for d in self.online_devices()]
        with self._lock:
            self._check_sync_timeout_locked(now)
            sync_run = self._current_sync
            last_ended = self._last_ended_session
        cmds: dict[str, str] = {}
        if sync_run is not None:
            for cam in online_ids:
                role = cam  # rig convention: camera_id == role ("A" | "B")
                if role in sync_run.reports:
                    continue  # already reported for this run
                cmds[cam] = "sync_run"
            return cmds
        if current is not None:
            for cam in online_ids:
                cmds[cam] = "arm"
        elif last_ended is not None and last_ended.ended_at is not None:
            if now - last_ended.ended_at <= _DISARM_ECHO_S:
                for cam in online_ids:
                    cmds[cam] = "disarm"
        return cmds

    def events(self) -> list[dict[str, Any]]:
        """Summary row per session for the events panel — one entry per
        session_id, collapsing A/B uploads into a single event.

        `received_at` is derived from the pitch file's mtime so we don't
        have to extend the Pydantic payload with server-side timestamps.
        Disk `stat()` happens AFTER releasing `self._lock` so the
        dashboard's 5 s tick can't block heartbeats / /pitch handlers
        that need to mutate the state map.
        """
        # --- Critical section: snapshot only the in-memory data we need.
        with self._lock:
            sessions = sorted({sid for _, sid in self.pitches.keys()})
            snapshots: list[
                tuple[
                    str,
                    list[str],
                    dict[str, int],
                    dict[str, int],
                    bool,
                    dict[str, CaptureTelemetryPayload | None],
                    SessionResult | None,
                ]
            ] = []
            for sid in sessions:
                cams_present = sorted(
                    cam for (cam, s) in self.pitches.keys() if s == sid
                )
                cam_frame_counts = {
                    cam: sum(
                        1 for f in self.pitches[(cam, sid)].frames if f.ball_detected
                    )
                    for cam in cams_present
                }
                cam_frame_counts_on_device = {
                    cam: sum(
                        1 for f in self.pitches[(cam, sid)].frames_on_device if f.ball_detected
                    )
                    for cam in cams_present
                }
                # Any pitch for this session carrying non-empty frames_on_device
                # implies the session armed in dual mode (on-device uploads
                # always omit the MOV; dual adds frames_on_device on top of
                # the MOV). Computed inside the lock so the mode-inference
                # step outside doesn't need to re-read self.pitches.
                has_any_on_device_frames = any(
                    bool(self.pitches[(cam, sid)].frames_on_device)
                    for cam in cams_present
                )
                cam_capture_telemetry = {
                    cam: self.pitches[(cam, sid)].capture_telemetry
                    for cam in cams_present
                }
                snapshots.append(
                    (
                        sid,
                        cams_present,
                        cam_frame_counts,
                        cam_frame_counts_on_device,
                        has_any_on_device_frames,
                        cam_capture_telemetry,
                        self.results.get(sid),
                    )
                )

        # --- Outside the lock: file stats + summary derivation.
        events: list[dict[str, Any]] = []
        for sid, cams_present, cam_frame_counts, cam_frame_counts_on_device, has_any_on_device_frames, cam_capture_telemetry, result in snapshots:
            latest_mtime: float | None = None
            for cam in cams_present:
                try:
                    mtime = self._pitch_path(cam, sid).stat().st_mtime
                except FileNotFoundError:
                    continue
                if latest_mtime is None or mtime > latest_mtime:
                    latest_mtime = mtime

            authority_points = result.triangulated if result is not None else []
            n_triangulated = len(authority_points) if result is not None else 0
            n_triangulated_on_device = len(result.points_on_device) if result is not None else 0
            error = result.error if result is not None else None
            error_on_device = result.error_on_device if result is not None else None

            if error:
                status = "error"
            elif len(cams_present) >= 2 and n_triangulated > 0:
                status = "paired"
            elif len(cams_present) >= 2:
                status = "paired_no_points"
            else:
                status = "partial"

            peak_z: float | None = None
            mean_res: float | None = None
            duration: float | None = None
            if authority_points:
                zs = [p.z_m for p in authority_points]
                peak_z = float(max(zs))
                mean_res = float(
                    sum(p.residual_m for p in authority_points)
                    / len(authority_points)
                )
                ts = [p.t_rel_s for p in authority_points]
                duration = float(ts[-1] - ts[0])

            # Dashboard-LIVE view summary: derived exclusively from the
            # on-device fit (mode-two is authoritative for the LIVE panel;
            # mode-one is forensic). Release-point velocity is the
            # derivative of the quadratic evaluated at release_t_s.
            speed_mps: float | None = None
            plate_xz_m: list[float] | None = None
            rms_m: float | None = None
            fit_duration_s: float | None = None
            fod = result.fit_on_device if result is not None else None
            if fod is not None:
                rms_m = float(fod.rms_m)
                fit_duration_s = float(fod.t_max_s - fod.t_min_s)
                t_rel = fod.release_t_s
                vx = 2.0 * fod.coeffs_x[0] * t_rel + fod.coeffs_x[1]
                vy = 2.0 * fod.coeffs_y[0] * t_rel + fod.coeffs_y[1]
                vz = 2.0 * fod.coeffs_z[0] * t_rel + fod.coeffs_z[1]
                speed_mps = float((vx * vx + vy * vy + vz * vz) ** 0.5)
                if fod.plate_xyz_m is not None:
                    plate_xz_m = [float(fod.plate_xyz_m[0]), float(fod.plate_xyz_m[2])]

            # Infer the legacy mode label for compatibility. The richer UI
            # should prefer `path_status`.
            has_any_video = any(
                self._video_dir.glob(f"session_{sid}_*")
            )
            if has_any_video and has_any_on_device_frames:
                mode = "dual"
            elif has_any_video:
                mode = "camera_only"
            else:
                mode = "on_device"
            path_status = {
                DetectionPath.live.value: (
                    "done" if result is not None and DetectionPath.live.value in result.paths_completed
                    else ("error" if result is not None and any(key.startswith("live:") for key in result.abort_reasons) else "-")
                ),
                DetectionPath.ios_post.value: (
                    "done" if result is not None and DetectionPath.ios_post.value in result.paths_completed
                    else ("error" if result is not None and DetectionPath.ios_post.value in result.abort_reasons else "-")
                ),
                DetectionPath.server_post.value: (
                    "done" if result is not None and DetectionPath.server_post.value in result.paths_completed
                    else ("error" if result is not None and DetectionPath.server_post.value in result.abort_reasons else "-")
                ),
            }

            events.append(
                {
                    "session_id": sid,
                    "cameras": cams_present,
                    "status": status,
                    "mode": mode,
                    "received_at": latest_mtime,
                    "n_ball_frames": cam_frame_counts,
                    "n_ball_frames_on_device": cam_frame_counts_on_device,
                    "n_triangulated": n_triangulated,
                    "n_triangulated_on_device": n_triangulated_on_device,
                    "peak_z_m": peak_z,
                    "mean_residual_m": mean_res,
                    "duration_s": duration,
                    "capture_telemetry": {
                        cam: (tele.model_dump(mode="json") if tele is not None else None)
                        for cam, tele in cam_capture_telemetry.items()
                    },
                    "error": error,
                    "error_on_device": error_on_device,
                    "path_status": path_status,
                    # Fit-derived summary (LIVE dashboard). All None when
                    # fit_on_device is missing — frontend hides the row in
                    # that case.
                    "rms_m": rms_m,
                    "speed_mps": speed_mps,
                    "plate_xz_m": plate_xz_m,
                    "fit_duration_s": fit_duration_s,
                    "has_fit": fod is not None,
                }
            )
        # Latest events first — session ids carry 4 bytes of random hex
        # so we sort by `received_at` (fallback to id) to surface the
        # most recently uploaded session at the top.
        events.sort(
            key=lambda e: (e["received_at"] or 0, e["session_id"]),
            reverse=True,
        )
        return events

    def delete_session(self, session_id: str) -> bool:
        """Remove a single session's in-memory + on-disk artefacts.

        Returns True if anything was removed, False if the session was
        unknown. Raises RuntimeError if `session_id` is the currently
        armed session — stop it first, or the phones may flush uploads
        into a half-deleted slot.

        Wipes the pitches / results / videos files for `session_id` (both
        the live and any `.tmp` siblings) plus the annotated clip, and
        clears the entry from `_pitches`, `_results`, and the
        `_last_ended_session` pointer if it matches."""
        with self._lock:
            current = self._current_session
            if (
                current is not None
                and current.ended_at is None
                and current.id == session_id
            ):
                raise RuntimeError(
                    f"cannot delete armed session {session_id}; stop it first"
                )

            keys_to_drop = [
                (cam, sid)
                for (cam, sid) in self.pitches
                if sid == session_id
            ]
            removed_any = bool(keys_to_drop) or session_id in self.results
            for key in keys_to_drop:
                self.pitches.pop(key, None)
            self.results.pop(session_id, None)
            if (
                self._last_ended_session is not None
                and self._last_ended_session.id == session_id
            ):
                self._last_ended_session = None

        # Disk cleanup outside the lock — same pattern record() uses.
        for pattern in (
            f"session_{session_id}_*.json",
            f"session_{session_id}_*.json.*.tmp",
        ):
            for path in self._pitch_dir.glob(pattern):
                path.unlink(missing_ok=True)
                removed_any = True
        for pattern in (
            f"session_{session_id}.json",
            f"session_{session_id}.json.*.tmp",
        ):
            for path in self._result_dir.glob(pattern):
                path.unlink(missing_ok=True)
                removed_any = True
        for path in self._video_dir.glob(f"session_{session_id}_*"):
            # Includes raw + `_annotated` + any in-flight `.tmp` sibling.
            path.unlink(missing_ok=True)
            removed_any = True
        return removed_any

    def reset(self, purge_disk: bool = False) -> None:
        with self._lock:
            self.pitches.clear()
            self.results.clear()
            self._devices.clear()
            self._current_session = None
            self._last_ended_session = None
            if purge_disk:
                for path in self._pitch_dir.glob("session_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._result_dir.glob("session_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._video_dir.glob("session_*"):
                    path.unlink(missing_ok=True)
                # Legacy cycle-keyed files from before the session_id rename.
                # Drop them on purge so a fresh State doesn't try to load
                # payloads that would now fail Pydantic validation.
                for path in self._pitch_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._result_dir.glob("cycle_*.json*"):
                    path.unlink(missing_ok=True)
                for path in self._video_dir.glob("cycle_*"):
                    path.unlink(missing_ok=True)


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
state = State()
device_ws = DeviceSocketManager()
sse_hub = SSEHub()
app.include_router(
    build_control_router(
        get_state=lambda: state,
        get_device_ws=lambda: device_ws,
        get_sse_hub=lambda: sse_hub,
        default_session_timeout_s=_DEFAULT_SESSION_TIMEOUT_S,
        time_sync_max_age_s=_TIME_SYNC_MAX_AGE_S,
    )
)
app.include_router(
    build_device_ws_router(
        get_state=lambda: state,
        get_device_ws=lambda: device_ws,
        get_sse_hub=lambda: sse_hub,
        validate_camera_id_or_422=lambda camera_id: _validate_camera_id_or_422(camera_id),
        time_sync_max_age_s=_TIME_SYNC_MAX_AGE_S,
    )
)
app.include_router(
    build_sync_router(
        get_state=lambda: state,
        get_device_ws=lambda: device_ws,
        sync_start_status_for_reason={
            "session_armed": 409,
            "sync_in_progress": 409,
            "cooldown": 409,
            "devices_missing": 409,
        },
    )
)
app.include_router(
    build_preview_router(
        get_state=lambda: state,
        get_device_ws=lambda: device_ws,
        preview_request_ttl_s=_PREVIEW_REQUEST_TTL_S,
        time_sync_max_age_s=_TIME_SYNC_MAX_AGE_S,
    )
)
app.include_router(
    build_results_router(
        get_state=lambda: state,
    )
)
app.include_router(
    build_calibration_router(
        get_state=lambda: state,
        get_device_ws=lambda: device_ws,
        get_sse_hub=lambda: sse_hub,
    )
)
app.include_router(
    build_pitch_router(
        get_state=lambda: state,
        get_max_pitch_upload_bytes=lambda: _MAX_PITCH_UPLOAD_BYTES,
    )
)


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

async def _await_calibration_frame(camera_id: str, *, timeout_s: float = 2.0) -> bytes:
    import asyncio as _asyncio  # noqa: WPS433

    state.request_calibration_frame(camera_id)
    await device_ws.send(
        camera_id,
        settings_message_for(
            camera_id=camera_id,
            state=state,
            device_ws=device_ws,
            time_sync_max_age_s=_TIME_SYNC_MAX_AGE_S,
        ),
    )
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
            summary="Searching for known markers",
        )

    while frames_seen < max_frames and _asyncio.get_event_loop().time() < burst_deadline:
        state.request_calibration_frame(camera_id)
        await device_ws.send(
            camera_id,
            settings_message_for(
                camera_id=camera_id,
                state=state,
                device_ws=device_ws,
                time_sync_max_age_s=_TIME_SYNC_MAX_AGE_S,
            ),
        )
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
            if track_run:
                state.update_auto_cal_run(
                    camera_id,
                    status="searching",
                    frames_seen=frames_seen,
                    markers_visible=0,
                    summary="Searching for known markers",
                )
            continue
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
                    summary="Tracking markers; need more stable geometry",
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
                    f"Holding steady · {stable_frames} stable frame(s)"
                    if good_frames > 0
                    else "Tracking markers; waiting for stability"
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
                "preview is enabled, and running the current build"
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
            summary="Solving camera pose from stabilized observations",
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
    if wants_html(request):
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
                summary="Verified and applied",
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
                summary="Auto-cal failed",
                detail=str(e.detail),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("auto calibration background run failed camera=%s", camera_id)
            state.finish_auto_cal_run(
                camera_id,
                status="failed",
                applied=False,
                summary="Auto-cal failed",
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
            sync=sync_run.to_dict() if sync_run is not None else None,
            last_sync=last_sync.model_dump() if last_sync is not None else None,
            sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
            chirp_detect_threshold=state.chirp_detect_threshold(),
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
