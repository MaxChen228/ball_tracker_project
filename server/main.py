"""FastAPI ingest + triangulation server for ball_tracker iPhone app.

Endpoints:
  GET  /                            — dashboard (devices, session state,
                                       Arm / Cancel controls, events table)
  GET  /status                      — health + online devices + session +
                                       per-camera commands
  POST /pitch                       — ingest one session upload (multipart:
                                       required `payload` JSON carrying
                                       `session_id`, optional `video`
                                       MOV/MP4 clip)
  POST /heartbeat                   — iPhone 1 Hz liveness ping;
                                       body: {"camera_id"}. Reply mirrors
                                       /status so the phone can drive
                                       arm/disarm from one round-trip.
  POST /sessions/arm                — dashboard: begin an armed session.
                                       Server returns the new session id
                                       (idempotent on re-arm). /status
                                       starts dispatching {cam: "arm"}.
  POST /sessions/cancel             — dashboard: force-disarm. Triggers the
                                       "disarm" echo window so phones can
                                       exit recording cleanly.
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

import io
import json
import logging
import os
import secrets
import socket
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError

from triangulate import (
    angle_ray_cam,
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)

logger = logging.getLogger("ball_tracker")


class IntrinsicsPayload(BaseModel):
    fx: float
    fz: float
    cx: float
    cy: float
    # OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Optional so
    # payloads without distortion still validate and fall back to the angle
    # path.
    distortion: list[float] | None = None


class FramePayload(BaseModel):
    frame_index: int
    timestamp_s: float
    theta_x_rad: float | None = None
    theta_z_rad: float | None = None
    # Raw (distorted) ball pixel coords. When present AND the camera's
    # intrinsics.distortion is present, the server undistorts these instead
    # of using the angles. Nil when no ball was detected.
    px: float | None = None
    py: float | None = None
    ball_detected: bool


class PitchPayload(BaseModel):
    # Constrained so we can safely interpolate into filenames (clips,
    # pitch json). Matches the iOS-side values ("A" / "B") with slack for
    # future role additions but blocks path-traversal attempts.
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    # Server-assigned session identifier (from `POST /sessions/arm`). This
    # is the sole pairing key for A/B uploads — iPhones no longer generate
    # their own counters. Pattern matches `_new_session_id()`; also safe to
    # interpolate into filenames.
    session_id: str = Field(..., pattern=r"^s_[0-9a-f]{4,32}$")
    # Shared time anchor for A/B pairing, recovered from an audio-chirp
    # matched-filter hit on the 時間校正 step. Server uses
    # `sync_anchor_timestamp_s` as the per-cycle clock origin and pairs
    # frames within an 8 ms window of the relative time.
    sync_anchor_frame_index: int
    sync_anchor_timestamp_s: float
    # Optional device-local recording counter. Not used for pairing; kept
    # purely for operator debugging (e.g. "this was my 5th attempt this
    # app launch"). iPhones may omit it entirely.
    local_recording_index: int | None = None
    frames: list[FramePayload]
    intrinsics: IntrinsicsPayload | None = None
    homography: list[float] | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None


class TriangulatedPoint(BaseModel):
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


class SessionResult(BaseModel):
    """One armed-session's triangulation result. Replaces the old
    `CycleResult` now that "cycle" is a per-device recording-window concept
    and the pitch unit is server-level "session"."""
    session_id: str
    camera_a_received: bool
    camera_b_received: bool
    points: list[TriangulatedPoint] = []
    error: str | None = None


def _camera_pose(intr: IntrinsicsPayload, H_list: list[float]):
    K = build_K(intr.fx, intr.fz, intr.cx, intr.cy)
    H = np.array(H_list, dtype=float).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, R, t, C


def _ray_for_frame(
    theta_x: float | None,
    theta_z: float | None,
    px: float | None,
    py: float | None,
    K: np.ndarray,
    dist_coeffs: list[float] | None,
) -> np.ndarray:
    """Per-frame ray choice. Prefer undistorting raw pixels if available,
    otherwise fall back to the angle ray computed on-device."""
    if dist_coeffs is not None and px is not None and py is not None:
        return undistorted_ray_cam(px, py, K, np.asarray(dist_coeffs, dtype=float))
    if theta_x is None or theta_z is None:
        raise ValueError("frame has neither usable angles nor pixels")
    return angle_ray_cam(theta_x, theta_z)


def _valid_frame(f: FramePayload) -> bool:
    has_angles = f.theta_x_rad is not None and f.theta_z_rad is not None
    has_pixels = f.px is not None and f.py is not None
    return f.ball_detected and (has_angles or has_pixels)


def _frame_items(p: PitchPayload):
    """Ball-bearing frames as `(t_rel, θx, θz, px, py)`, sorted by
    anchor-relative time. `t_rel = timestamp_s − sync_anchor_timestamp_s`."""
    anchor = p.sync_anchor_timestamp_s
    out = [
        (f.timestamp_s - anchor, f.theta_x_rad, f.theta_z_rad, f.px, f.py)
        for f in p.frames if _valid_frame(f)
    ]
    out.sort(key=lambda x: x[0])
    return out


def triangulate_cycle(a: PitchPayload, b: PitchPayload) -> list[TriangulatedPoint]:
    """Pair A and B frames within an 8 ms window of anchor-relative time and
    run ray-midpoint triangulation. Requires intrinsics + homography on both
    cameras."""
    if a.intrinsics is None or a.homography is None:
        raise ValueError("camera A missing calibration (run Calibrate in iPhone app)")
    if b.intrinsics is None or b.homography is None:
        raise ValueError("camera B missing calibration (run Calibrate in iPhone app)")

    K_a, R_a, _, C_a = _camera_pose(a.intrinsics, a.homography)
    K_b, R_b, _, C_b = _camera_pose(b.intrinsics, b.homography)

    items_a = _frame_items(a)
    items_b = _frame_items(b)
    if not items_a or not items_b:
        return []

    b_times = np.array([x[0] for x in items_b])
    max_dt = 1.0 / 120.0  # 8 ms tolerance at 240 fps

    dist_a = a.intrinsics.distortion
    dist_b = b.intrinsics.distortion

    results: list[TriangulatedPoint] = []
    for t_rel, tx_a, tz_a, px_a, py_a in items_a:
        idx = int(np.argmin(np.abs(b_times - t_rel)))
        if abs(b_times[idx] - t_rel) > max_dt:
            continue
        _, tx_b, tz_b, px_b, py_b = items_b[idx]

        d_a_cam = _ray_for_frame(tx_a, tz_a, px_a, py_a, K_a, dist_a)
        d_b_cam = _ray_for_frame(tx_b, tz_b, px_b, py_b, K_b, dist_b)
        d_a_world = R_a.T @ d_a_cam
        d_b_world = R_b.T @ d_b_cam

        P, gap = triangulate_rays(C_a, d_a_world, C_b, d_b_world)
        results.append(
            TriangulatedPoint(
                t_rel_s=t_rel,
                x_m=float(P[0]),
                y_m=float(P[1]),
                z_m=float(P[2]),
                residual_m=gap,
            )
        )
    return results


_DEFAULT_DATA_DIR = Path(os.environ.get("BALL_TRACKER_DATA_DIR", "data"))

# Seconds a heartbeat remains fresh. A phone beating at 1 Hz drops off the
# "online" list after missing ~3 beats — conservative enough to tolerate a
# stalled wifi roam without flapping.
_DEVICE_STALE_S = 3.0

# When a session ends, server keeps advertising `disarm` on /status for a
# brief window so the phone that didn't fire the cycle still gets the signal
# on its next poll. Long enough to cover any sensible poll cadence.
_DISARM_ECHO_S = 5.0

# Session auto-timeout if no cycle arrives. Covers "dashboard armed but
# nobody threw anything" — otherwise /status would keep dispatching arm
# forever.
_DEFAULT_SESSION_TIMEOUT_S = 60.0


@dataclass
class Device:
    """Most recent heartbeat from a single iPhone. `last_seen_at` is a wall
    clock unix timestamp so `now - last_seen_at` compares cleanly even
    across server restarts (the dict is memory-only, so restart implies no
    device is online yet)."""
    camera_id: str
    last_seen_at: float


@dataclass
class Session:
    """One dashboard "Arm" action → at most one session at a time. The
    session id is the server-minted pairing key for A/B uploads — iPhones
    stamp this id onto every PitchPayload they send during the armed
    window, so reconstruction is always keyed by session, never by
    device-local counters."""
    id: str
    started_at: float
    max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S
    ended_at: float | None = None
    end_reason: str | None = None   # "cycle_uploaded" | "cancelled" | "timeout"
    # Camera ids that have successfully uploaded while this session was
    # the current one. Dashboard reads this to render "session s_abc →
    # A, B".
    uploads_received: list[str] = field(default_factory=list)

    @property
    def armed(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "armed": self.armed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "max_duration_s": self.max_duration_s,
            "uploads_received": list(self.uploads_received),
        }


def _new_session_id() -> str:
    # 8 hex chars ≈ 4 bytes of entropy — plenty for a personal-LAN tool
    # where sessions are seconds apart, not microseconds.
    return "s_" + secrets.token_hex(4)


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
        self._pitch_dir.mkdir(parents=True, exist_ok=True)
        self._result_dir.mkdir(parents=True, exist_ok=True)
        self._video_dir.mkdir(parents=True, exist_ok=True)
        # Dashboard-control state. All in-memory — devices re-heartbeat on
        # connection, sessions don't survive restart.
        self._devices: dict[str, Device] = {}
        self._current_session: Session | None = None
        self._last_ended_session: Session | None = None
        # Injectable clock so timeout and staleness tests don't need sleeps.
        self._time_fn = time_fn
        self._load_from_disk()

    @property
    def video_dir(self) -> Path:
        return self._video_dir

    def save_clip(
        self, camera_id: str, session_id: str, data: bytes, ext: str = "mov"
    ) -> Path:
        """Persist a session's H.264 clip to disk. Writes atomically so a
        partial transfer cannot leave a corrupt file visible to downstream
        tools. Overwrites any existing clip for (camera_id, session_id)."""
        safe_ext = (ext or "mov").lstrip(".").lower()
        if not safe_ext or "/" in safe_ext or "\\" in safe_ext:
            safe_ext = "mov"
        path = self._video_dir / f"session_{session_id}_{camera_id}.{safe_ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
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
                pitch = PitchPayload.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt pitch file %s: %s", path.name, e)
                continue
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch

        seen_sessions = {sid for _, sid in self.pitches.keys()}
        for sid in sorted(seen_sessions):
            a = self.pitches.get(("A", sid))
            b = self.pitches.get(("B", sid))
            result = SessionResult(
                session_id=sid,
                camera_a_received=a is not None,
                camera_b_received=b is not None,
            )
            if a is not None and b is not None:
                try:
                    result.points = triangulate_cycle(a, b)
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
            self.results[sid] = result

        if self.pitches:
            logger.info(
                "restored %d pitch payloads across %d sessions from %s",
                len(self.pitches),
                len(seen_sessions),
                self._data_dir,
            )

    def _atomic_write(self, path: Path, payload: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    def record(self, pitch: PitchPayload) -> SessionResult:
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch
            self._atomic_write(
                self._pitch_path(pitch.camera_id, pitch.session_id),
                pitch.model_dump_json(),
            )

            # Drive the session state machine forward — any upload arriving
            # while armed disarms the session (one-shot pattern). The other
            # camera, if it was also recording, gets "disarm" on its next
            # /status poll and cleans up.
            self._register_upload_in_session_locked(pitch)

            a = self.pitches.get(("A", pitch.session_id))
            b = self.pitches.get(("B", pitch.session_id))
            result = SessionResult(
                session_id=pitch.session_id,
                camera_a_received=a is not None,
                camera_b_received=b is not None,
            )
            if a is not None and b is not None:
                try:
                    result.points = triangulate_cycle(a, b)
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
            self.results[pitch.session_id] = result
            self._atomic_write(
                self._result_path(pitch.session_id),
                result.model_dump_json(),
            )
            return result

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

    def heartbeat(self, camera_id: str) -> None:
        """Record one liveness ping. Overwrites the previous entry for this
        camera so `last_seen_at` always reflects the latest beat."""
        now = self._time_fn()
        with self._lock:
            self._devices[camera_id] = Device(
                camera_id=camera_id, last_seen_at=now
            )

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

    def _check_session_timeout_locked(self, now: float) -> None:
        """If the current session has exceeded its max_duration_s, transition
        it to ended(reason=timeout). Assumes the caller holds `self._lock`."""
        s = self._current_session
        if s is None or s.ended_at is not None:
            return
        if now - s.started_at > s.max_duration_s:
            s.ended_at = now
            s.end_reason = "timeout"
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
        self, max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S
    ) -> Session:
        """Begin a new armed session. If one is already armed, return it
        unchanged (idempotent so dashboard double-clicks don't double-arm)."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            if self._current_session is not None:
                return self._current_session
            session = Session(
                id=_new_session_id(),
                started_at=now,
                max_duration_s=max_duration_s,
            )
            self._current_session = session
            return session

    def cancel_session(self, reason: str = "cancelled") -> Session | None:
        """Force-end the current armed session. Returns the ended session,
        or None if nothing was armed."""
        now = self._time_fn()
        with self._lock:
            s = self._current_session
            if s is None or s.ended_at is not None:
                return None
            s.ended_at = now
            s.end_reason = reason
            self._last_ended_session = s
            self._current_session = None
            return s

    def _register_upload_in_session_locked(self, pitch: PitchPayload) -> None:
        """Called from `record()` while the state lock is held. If the pitch
        carries the current armed session's id, record the upload and
        auto-end the session (one-shot arm → first upload disarms). The
        other camera, if any, receives `disarm` via commands_for_devices
        during the echo window."""
        s = self._current_session
        if s is None or s.ended_at is not None:
            return
        if pitch.session_id != s.id:
            # Upload belongs to a different session (previous armed window
            # the phone is only now flushing). Don't touch the current one.
            return
        if pitch.camera_id not in s.uploads_received:
            s.uploads_received.append(pitch.camera_id)
        # One-shot: any upload for the current session ends it.
        s.ended_at = self._time_fn()
        s.end_reason = "cycle_uploaded"
        self._last_ended_session = s
        self._current_session = None

    def session_snapshot(self) -> Session | None:
        """Return the session most relevant to a status caller: the current
        armed session if any, otherwise the most recently ended one (so the
        dashboard can display "IDLE (last: cycle_uploaded)" and the iPhone
        sees session.armed == False during the disarm echo window)."""
        current = self.current_session()
        if current is not None:
            return current
        with self._lock:
            return self._last_ended_session

    def commands_for_devices(self) -> dict[str, str]:
        """Derive per-device commands from the current session state. The
        iPhone reads `commands[self.camera_id]` on each /status poll:
          - "arm"    if a session is currently armed
          - "disarm" if a session ended within _DISARM_ECHO_S ago
          - absent   otherwise (steady state, no action required)"""
        now = self._time_fn()
        current = self.current_session()  # applies timeout
        online_ids = [d.camera_id for d in self.online_devices()]
        with self._lock:
            last_ended = self._last_ended_session
        cmds: dict[str, str] = {}
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
        """
        with self._lock:
            sessions = sorted({sid for _, sid in self.pitches.keys()})
            events: list[dict[str, Any]] = []
            for sid in sessions:
                cams_present = sorted(
                    cam for (cam, s) in self.pitches.keys() if s == sid
                )
                cam_frame_counts: dict[str, int] = {}
                latest_mtime: float | None = None
                for cam in cams_present:
                    pitch = self.pitches[(cam, sid)]
                    cam_frame_counts[cam] = sum(
                        1 for f in pitch.frames if f.ball_detected
                    )
                    path = self._pitch_path(cam, sid)
                    try:
                        mtime = path.stat().st_mtime
                    except FileNotFoundError:
                        mtime = None
                    if mtime is not None and (
                        latest_mtime is None or mtime > latest_mtime
                    ):
                        latest_mtime = mtime

                result = self.results.get(sid)
                n_triangulated = len(result.points) if result is not None else 0
                error = result.error if result is not None else None

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
                if result is not None and result.points:
                    zs = [p.z_m for p in result.points]
                    peak_z = float(max(zs))
                    mean_res = float(
                        sum(p.residual_m for p in result.points)
                        / len(result.points)
                    )
                    ts = [p.t_rel_s for p in result.points]
                    duration = float(ts[-1] - ts[0])

                events.append(
                    {
                        "session_id": sid,
                        "cameras": cams_present,
                        "status": status,
                        "received_at": latest_mtime,
                        "n_ball_frames": cam_frame_counts,
                        "n_triangulated": n_triangulated,
                        "peak_z_m": peak_z,
                        "mean_residual_m": mean_res,
                        "duration_s": duration,
                        "error": error,
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
    yield


app = FastAPI(title="ball_tracker server", lifespan=lifespan)
state = State()


def _build_status_response() -> dict[str, Any]:
    """Shared shape for GET /status and POST /heartbeat responses. Anything
    an iPhone needs to decide whether to arm / disarm is in here — the
    phone just polls this and reacts to `commands[self.camera_id]`."""
    summary = state.summary()
    session = state.session_snapshot()
    return {
        **summary,
        "devices": [
            {"camera_id": d.camera_id, "last_seen_at": d.last_seen_at}
            for d in state.online_devices()
        ],
        "session": session.to_dict() if session is not None else None,
        "commands": state.commands_for_devices(),
    }


@app.get("/status")
def status() -> dict[str, Any]:
    return _build_status_response()


class HeartbeatBody(BaseModel):
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")


@app.post("/heartbeat")
def heartbeat(body: HeartbeatBody) -> dict[str, Any]:
    """iPhone liveness ping. Responds with the same payload as /status so
    the phone can decide arm/disarm from the reply without a second call."""
    state.heartbeat(body.camera_id)
    return _build_status_response()


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
    session = state.arm_session(max_duration_s=max_duration_s)
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "session": session.to_dict()}


@app.post("/sessions/cancel")
async def sessions_cancel(request: Request):
    """Force-disarm. Returns 409 to API callers when nothing was armed;
    HTML callers always get a 303 redirect back to the dashboard so the
    button never looks broken."""
    ended = state.cancel_session(reason="cancelled")
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    if ended is None:
        raise HTTPException(status_code=409, detail="no armed session")
    return {"ok": True, "session": ended.to_dict()}


def _summarize_result(result: SessionResult) -> dict[str, Any]:
    paired = result.camera_a_received and result.camera_b_received
    summary: dict[str, Any] = {
        "session_id": result.session_id,
        "paired": paired,
        "triangulated_points": len(result.points),
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
    payload: str = Form(...),
    video: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest one armed-session upload as multipart/form-data.

    Required form field `payload`: JSON-encoded `PitchPayload` (carries the
    server-minted `session_id` — the sole pairing key for A/B).
    Optional form field `video`:   MOV/MP4 clip of the recording. Stored
                                    under `data/videos/session_{id}_{cam}.{ext}`
                                    and not yet consumed by triangulation —
                                    Phase-1 raw-video staging.
    """
    try:
        payload_obj = PitchPayload.model_validate_json(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    clip_info: dict[str, Any] | None = None
    if video is not None:
        data = await video.read()
        if data:
            ext = "mov"
            if video.filename:
                suffix = Path(video.filename).suffix.lstrip(".").lower()
                if suffix:
                    ext = suffix
            clip_path = state.save_clip(
                payload_obj.camera_id, payload_obj.session_id, data, ext
            )
            clip_info = {"filename": clip_path.name, "bytes": len(data)}

    result = state.record(payload_obj)
    ball_frames = sum(1 for f in payload_obj.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s session=%s frames=%d ball=%d triangulated=%d%s%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        len(payload_obj.frames),
        ball_frames,
        len(result.points),
        f" clip={clip_info['bytes']}B" if clip_info else "",
        f" err={result.error}" if result.error else "",
    )
    if result.points:
        zs = [p.z_m for p in result.points]
        logger.info(
            "  session %s → %d pts, duration %.2fs, peak z = %.2fm",
            result.session_id,
            len(result.points),
            result.points[-1].t_rel_s - result.points[0].t_rel_s,
            max(zs),
        )
    response: dict[str, Any] = {"ok": True, **_summarize_result(result)}
    if clip_info is not None:
        response["clip"] = clip_info
    return response


@app.get("/chirp.wav")
def chirp_wav() -> Response:
    """Reference sync chirp for the 時間校正 step.

    Users download this on any device (browser) and play it near the two
    iPhones. Each phone's AudioChirpDetector runs matched filtering and
    pins the session-clock PTS of the peak as the per-cycle anchor.

    Signal: linear sweep 2 → 8 kHz, 100 ms, Hann-windowed, surrounded by
    0.5 s of silence either side so the phones can catch it mid-stream.
    """
    sr = 44100
    f0 = 2000.0
    f1 = 8000.0
    duration = 0.1
    n = int(sr * duration)
    t = np.arange(n) / sr
    phase = 2.0 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2.0 * duration))
    window = 0.5 * (1.0 - np.cos(2.0 * np.pi * np.arange(n) / (n - 1)))
    chirp = np.sin(phase) * window

    silence = np.zeros(int(sr * 0.5), dtype=np.float64)
    full = np.concatenate([silence, chirp, silence])
    pcm = np.clip(full * 0.8, -1.0, 1.0)
    pcm_int = (pcm * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_int.tobytes())
    return Response(
        content=buf.getvalue(),
        media_type="audio/wav",
        headers={"Content-Disposition": 'inline; filename="chirp.wav"'},
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


def _scene_for_session(session_id: str):
    """Shared fetch+build for the two scene endpoints. Raises 404 when no
    pitches have been received for this session yet."""
    # Local imports so the FastAPI app still boots when plotly is missing
    # (the JSON endpoint doesn't need it; the HTML one will surface a 500).
    from reconstruct import build_scene

    pitches = state.pitches_for_session(session_id)
    if not pitches:
        raise HTTPException(404, f"session {session_id} has no pitches")
    result = state.get(session_id)
    triangulated = result.points if result is not None else []
    return build_scene(session_id, pitches, triangulated)


@app.get("/reconstruction/{session_id}")
def reconstruction(session_id: str) -> dict[str, Any]:
    scene = _scene_for_session(session_id)
    return scene.to_dict()


@app.get("/viewer/{session_id}", response_class=HTMLResponse)
def viewer(session_id: str) -> HTMLResponse:
    from viewer import render_scene_html

    scene = _scene_for_session(session_id)
    return HTMLResponse(render_scene_html(scene))


@app.get("/events")
def events() -> list[dict[str, Any]]:
    return state.events()


@app.get("/", response_class=HTMLResponse)
def events_index() -> HTMLResponse:
    from viewer import render_events_index_html

    session = state.session_snapshot()
    return HTMLResponse(
        render_events_index_html(
            events=state.events(),
            devices=[
                {"camera_id": d.camera_id, "last_seen_at": d.last_seen_at}
                for d in state.online_devices()
            ],
            session=session.to_dict() if session is not None else None,
        )
    )


@app.post("/reset")
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
