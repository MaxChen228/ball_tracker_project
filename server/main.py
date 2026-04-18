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

import json
import logging
import os
import re
import secrets
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Callable

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

# Re-exports so `from main import PitchPayload, ...` keeps working for the
# existing test suite and any downstream tooling. New callers should import
# from the split modules directly (schemas / pairing / chirp / render_*).
from schemas import (
    CalibrationSnapshot,
    Device,
    FramePayload,
    HeartbeatBody,
    IntrinsicsPayload,
    PitchPayload,
    Session,
    SessionResult,
    TriangulatedPoint,
    _DEFAULT_SESSION_TIMEOUT_S,
)
from pairing import triangulate_cycle
from pipeline import detect_pitch
from chirp import chirp_wav_bytes
from cleanup_old_sessions import cleanup_expired_sessions

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
        # Per-camera calibration snapshots. Written by POST /calibration,
        # read by the dashboard canvas so the 3D preview shows where each
        # phone "thinks it is" relative to the plate, independent of any
        # session. Persisted as one JSON per camera so a server restart
        # keeps whatever calibrations were live.
        self._calibrations: dict[str, CalibrationSnapshot] = {}
        # Injectable clock so timeout and staleness tests don't need sleeps.
        self._time_fn = time_fn
        self._load_from_disk()
        self._load_calibrations_from_disk()

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
            self._calibrations[snap.camera_id] = snap
        if self._calibrations:
            logger.info(
                "restored %d camera calibration(s) from %s",
                len(self._calibrations),
                self._calibration_dir,
            )

    def set_calibration(self, snapshot: CalibrationSnapshot) -> None:
        """Record (or overwrite) one camera's calibration and persist it
        atomically so the dashboard survives a restart. Last write wins —
        the phone re-POSTs every time the user completes a Calibration
        screen save, so there's no attempt to version older snapshots."""
        payload = snapshot.model_dump_json(indent=2)
        with self._lock:
            self._calibrations[snapshot.camera_id] = snapshot
            self._atomic_write(self._calibration_path(snapshot.camera_id), payload)

    def calibrations(self) -> dict[str, CalibrationSnapshot]:
        with self._lock:
            return dict(self._calibrations)

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
            a = self.pitches.get(("A", pitch.session_id))
            b = self.pitches.get(("B", pitch.session_id))

        # --- Outside the lock: write pitch JSON. Filename is unique per
        # (camera, session) and each pitch uses its own tmp file, so two
        # concurrent calls here cannot collide. ---
        self._atomic_write(pitch_path, pitch.model_dump_json())

        # --- Outside the lock: build the result + triangulate if paired. ---
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

        # --- Outside the lock: persist the result JSON. ---
        self._atomic_write(
            self._result_path(pitch.session_id),
            result.model_dump_json(),
        )

        # --- Critical section 2: publish the result into the in-memory map. ---
        with self._lock:
            self.results[pitch.session_id] = result
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

    def heartbeat(self, camera_id: str, time_synced: bool = False) -> None:
        """Record one liveness ping. Overwrites the previous entry for this
        camera so `last_seen_at` and `time_synced` always reflect the latest
        beat — the phone is the authoritative source for both. Prunes any
        entry older than `_DEVICE_GC_AFTER_S` and enforces a hard size cap
        (evicts the oldest by `last_seen_at`) so a misbehaving client can't
        grow the registry without bound."""
        now = self._time_fn()
        with self._lock:
            self._devices[camera_id] = Device(
                camera_id=camera_id,
                last_seen_at=now,
                time_synced=time_synced,
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


def _build_status_response() -> dict[str, Any]:
    """Shared shape for GET /status and POST /heartbeat responses. Anything
    an iPhone needs to decide whether to arm / disarm is in here — the
    phone just polls this and reacts to `commands[self.camera_id]`."""
    summary = state.summary()
    session = state.session_snapshot()
    return {
        **summary,
        "devices": [
            {
                "camera_id": d.camera_id,
                "last_seen_at": d.last_seen_at,
                "time_synced": d.time_synced,
            }
            for d in state.online_devices()
        ],
        "session": session.to_dict() if session is not None else None,
        "commands": state.commands_for_devices(),
    }


@app.get("/status")
def status() -> dict[str, Any]:
    return _build_status_response()


@app.post("/heartbeat")
def heartbeat(body: HeartbeatBody) -> dict[str, Any]:
    """iPhone liveness ping. Responds with the same payload as /status so
    the phone can decide arm/disarm from the reply without a second call."""
    state.heartbeat(body.camera_id, time_synced=body.time_synced)
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
    request: Request,
    payload: str = Form(...),
    video: UploadFile = File(...),
) -> dict[str, Any]:
    """Ingest one armed-session upload as multipart/form-data.

    Required form fields:
      - `payload` — JSON-encoded `PitchPayload` (camera_id, session_id,
        sync_anchor_timestamp_s, video_start_pts_s, video_fps, intrinsics,
        homography, image_{width,height}_px). No per-frame data.
      - `video`   — H.264 MOV/MP4 of the cycle. Server decodes it, runs
        HSV ball detection per frame, then triangulates with the partner
        upload for this session.

    Flow:
      1. 413 guard on declared Content-Length
      2. Validate JSON payload
      3. Persist the clip bytes atomically
      4. Decode the clip and synthesise per-frame detection results
      5. `state.record()` stores the enriched payload + triangulates if B
         (or A) is already on file
      6. Return the session summary — triangulation stats for the dashboard.

    Uploads without a time-sync anchor (`sync_anchor_timestamp_s is None`)
    skip detection + triangulation and surface `error="no time sync"` on
    the session. Operator's cue to re-run 時間校正.
    """
    # Fail fast on oversize bodies when the client advertises Content-Length
    # (Starlette has already buffered by the time we get here, but returning
    # 413 early is still cheaper than processing the multipart parts).
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

    data = await video.read()
    # Defence in depth: re-check the actual read size in case the declared
    # Content-Length was missing or spoofed.
    if len(data) > _MAX_PITCH_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="video too large")
    if not data:
        raise HTTPException(status_code=422, detail="video attachment is empty")
    ext = "mov"
    if video.filename:
        suffix = Path(video.filename).suffix.lstrip(".").lower()
        if suffix:
            ext = suffix
    clip_path = state.save_clip(
        payload_obj.camera_id, payload_obj.session_id, data, ext
    )
    clip_info = {"filename": clip_path.name, "bytes": len(data)}

    # Run ball detection against the persisted MOV OUTSIDE any state lock
    # (detection is the single heavy step per upload, up to a few seconds
    # for a multi-second clip — state.record() grabs the lock for
    # millisecond-scale work only).
    if payload_obj.sync_anchor_timestamp_s is None:
        # No time anchor → pairing is impossible; skip detection so we
        # don't waste CPU on data that will never triangulate.
        payload_obj.frames = []
        detection_ran = False
    else:
        payload_obj.frames = detect_pitch(
            clip_path, payload_obj.video_start_pts_s
        )
        detection_ran = True

    result = state.record(payload_obj)
    if payload_obj.sync_anchor_timestamp_s is None and result.error is None:
        # Persist the "no time sync" diagnostic so the session reads
        # consistently from /events / /results without re-running record().
        result.error = "no time sync"
    ball_frames = sum(1 for f in payload_obj.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s session=%s clip=%dB frames=%d ball=%d detected=%s triangulated=%d%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        clip_info["bytes"],
        len(payload_obj.frames),
        ball_frames,
        detection_ran,
        len(result.points),
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
    return Response(
        content=chirp_wav_bytes(),
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
    from render_scene import render_viewer_html

    scene = _scene_for_session(session_id)
    videos = _videos_for_session(session_id)
    return HTMLResponse(render_viewer_html(scene, videos))


# Only the exact filename shape `/pitch` writes is allowed through the
# /videos route, to keep the handler from serving arbitrary files out of
# `data/videos/` if something unexpected ever lands there.
_VIDEO_FILENAME_RE = re.compile(
    r"^session_s_[0-9a-f]{4,32}_[A-Za-z0-9_-]{1,16}\.(mov|mp4|m4v)$"
)


def _videos_for_session(session_id: str) -> list[tuple[str, str]]:
    """Return `[(camera_id, "/videos/<filename>"), ...]` sorted by camera_id
    for every MOV/MP4 clip that landed on disk for this session. Empty
    list when no clips exist (e.g. a session the server saved frames for
    but no video — shouldn't happen post-pivot, but keep the helper
    permissive so the viewer just hides the video area)."""
    prefix = f"session_{session_id}_"
    out: list[tuple[str, str]] = []
    for path in sorted(state.video_dir.glob(f"{prefix}*")):
        name = path.name
        if not _VIDEO_FILENAME_RE.match(name):
            continue
        # Extract camera_id: strip "session_{sid}_" prefix and extension.
        cam = name[len(prefix):].rsplit(".", 1)[0]
        out.append((cam, f"/videos/{name}"))
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
def events() -> list[dict[str, Any]]:
    return state.events()


@app.post("/calibration")
def post_calibration(snapshot: CalibrationSnapshot) -> dict[str, Any]:
    """iPhone pushes its freshly-solved calibration (intrinsics + homography)
    so the dashboard canvas can show where the camera is positioned in world
    space, even before the first pitch is ever recorded. Idempotent overwrite:
    each camera only keeps its latest snapshot."""
    state.set_calibration(snapshot)
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
    fig_json = json.loads(fig.to_json())
    return {
        "calibrations": [
            {
                "camera_id": cam_id,
                "image_width_px": snap.image_width_px,
                "image_height_px": snap.image_height_px,
            }
            for cam_id, snap in sorted(cals.items())
        ],
        "scene": scene.to_dict(),
        "plot": {
            "data": fig_json.get("data", []),
            "layout": fig_json.get("layout", {}),
        },
    }


@app.get("/", response_class=HTMLResponse)
def events_index() -> HTMLResponse:
    from render_dashboard import render_events_index_html

    session = state.session_snapshot()
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
        )
    )


@app.post("/reset")
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
