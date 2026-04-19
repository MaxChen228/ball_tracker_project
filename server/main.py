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
    CaptureMode,
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
from pairing import scale_pitch_to_video_dims, triangulate_cycle
from pipeline import annotate_video, detect_pitch
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
        # Global capture-mode toggle. Dashboard flips this; every subsequent
        # arm_session() snapshots the current value into the session. Default
        # `camera_only` preserves the pre-mode-split behaviour for anyone
        # upgrading a running server without touching the dashboard.
        self._current_mode: CaptureMode = CaptureMode.camera_only
        # Per-camera calibration snapshots. Written by POST /calibration,
        # read by the dashboard canvas so the 3D preview shows where each
        # phone "thinks it is" relative to the plate, independent of any
        # session. Persisted as one JSON per camera so a server restart
        # keeps whatever calibrations were live.
        self._calibrations: dict[str, CalibrationSnapshot] = {}
        # Injectable clock so timeout and staleness tests don't need sleeps.
        self._time_fn = time_fn
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
                    result.points = self._triangulate_pair(a, b, source="server")
                except Exception as e:
                    result.error = f"{type(e).__name__}: {e}"
                if self._has_on_device_frames(a) and self._has_on_device_frames(b):
                    try:
                        result.points_on_device = self._triangulate_pair(a, b, source="on_device")
                    except Exception as e:
                        result.error_on_device = f"{type(e).__name__}: {e}"
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
    def _has_on_device_frames(pitch: PitchPayload) -> bool:
        """Dual-mode detection: if any pitch carries `frames_on_device`,
        the session was armed dual and we owe the caller a second
        triangulation pass over the iOS detection stream."""
        return bool(pitch and pitch.frames_on_device)

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
                result.points = self._triangulate_pair(a, b, source="server")
            except Exception as e:
                result.error = f"{type(e).__name__}: {e}"
            if self._has_on_device_frames(a) and self._has_on_device_frames(b):
                try:
                    result.points_on_device = self._triangulate_pair(a, b, source="on_device")
                except Exception as e:
                    result.error_on_device = f"{type(e).__name__}: {e}"

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
        self, max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S
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
            session = Session(
                id=_new_session_id(),
                started_at=now,
                max_duration_s=max_duration_s,
                mode=self._current_mode,
            )
            self._current_session = session
            return session

    def current_mode(self) -> CaptureMode:
        """Dashboard-selected capture mode (global, not session-scoped).
        iPhones read this from status/heartbeat to render the HUD mode chip
        even while idle."""
        with self._lock:
            return self._current_mode

    def set_mode(self, mode: CaptureMode) -> CaptureMode:
        """Record the dashboard's mode choice. Only affects sessions armed
        after this call — in-flight sessions keep their snapshot mode."""
        with self._lock:
            self._current_mode = mode
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
        Disk `stat()` happens AFTER releasing `self._lock` so the
        dashboard's 5 s tick can't block heartbeats / /pitch handlers
        that need to mutate the state map.
        """
        # --- Critical section: snapshot only the in-memory data we need.
        with self._lock:
            sessions = sorted({sid for _, sid in self.pitches.keys()})
            snapshots: list[tuple[str, list[str], dict[str, int], dict[str, int], SessionResult | None]] = []
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
                snapshots.append(
                    (sid, cams_present, cam_frame_counts, cam_frame_counts_on_device,
                     has_any_on_device_frames, self.results.get(sid))
                )

        # --- Outside the lock: file stats + summary derivation.
        events: list[dict[str, Any]] = []
        for sid, cams_present, cam_frame_counts, cam_frame_counts_on_device, has_any_on_device_frames, result in snapshots:
            latest_mtime: float | None = None
            for cam in cams_present:
                try:
                    mtime = self._pitch_path(cam, sid).stat().st_mtime
                except FileNotFoundError:
                    continue
                if latest_mtime is None or mtime > latest_mtime:
                    latest_mtime = mtime

            n_triangulated = len(result.points) if result is not None else 0
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
            if result is not None and result.points:
                zs = [p.z_m for p in result.points]
                peak_z = float(max(zs))
                mean_res = float(
                    sum(p.residual_m for p in result.points)
                    / len(result.points)
                )
                ts = [p.t_rel_s for p in result.points]
                duration = float(ts[-1] - ts[0])

            # Infer the session's capture mode from payload shape:
            # - any pitch carries frames_on_device → dual OR on_device
            # - any MOV exists on disk → camera_only OR dual
            # Combine: on_device+MOV = dual; frames_on_device only = on_device;
            # MOV only = camera_only.
            has_any_video = any(
                self._video_dir.glob(f"session_{sid}_*")
            )
            if has_any_video and has_any_on_device_frames:
                mode = "dual"
            elif has_any_video:
                mode = "camera_only"
            else:
                mode = "on_device"

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
                    "error": error,
                    "error_on_device": error_on_device,
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
        # Global dashboard mode choice. iPhones show this on the HUD in idle
        # and fall back to it when there's no armed session; during an armed
        # session they read session.mode instead (it's the snapshot that
        # can't drift from under them).
        "capture_mode": state.current_mode().value,
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
    if _wants_html(request):
        return RedirectResponse("/", status_code=303)
    return {"ok": True, "capture_mode": applied.value}


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
    video: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Ingest one armed-session upload as multipart/form-data.

    Required form fields:
      - `payload` — JSON-encoded `PitchPayload`. In mode-one (`camera_only`)
        carries only session-level metadata; in mode-two (`on_device`) also
        carries the per-frame `frames: [FramePayload]` list produced by the
        iPhone's own HSV+MOG2 detector.

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

    has_video = video is not None and (video.filename or video.size)
    # Either stream counts as "data the server can work with": `frames`
    # from mode-two (iOS detection, authoritative for its session) or
    # `frames_on_device` from a degraded-dual upload (dual-mode cycle
    # where the MOV writer failed but the on-device detector still
    # produced a frame list). Both land in the triangulation pipeline.
    has_frames = bool(payload_obj.frames) or bool(payload_obj.frames_on_device)
    if not has_video and not has_frames:
        raise HTTPException(
            status_code=422,
            detail="must supply either `video` (mode-one / dual) or a "
                   "non-empty `frames` / `frames_on_device` list in payload",
        )

    clip_info: dict[str, Any] | None = None
    detection_ran = False

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
        # Hand the heavy steps (disk write, PyAV decode + cv2 detection,
        # annotate_video re-encode, atomic JSON writes + triangulation)
        # to the default thread executor.
        clip_path = await asyncio.to_thread(
            state.save_clip,
            payload_obj.camera_id, payload_obj.session_id, data, ext,
        )
        clip_info = {"filename": clip_path.name, "bytes": len(data)}

        if payload_obj.sync_anchor_timestamp_s is None:
            payload_obj.frames = []
        else:
            # Mode-one: server-side detection is authoritative. We
            # overwrite any `frames` the iPhone may have speculatively
            # included alongside the MOV.
            payload_obj.frames = await asyncio.to_thread(
                detect_pitch, clip_path, payload_obj.video_start_pts_s,
            )
            detection_ran = True
            annotated_path = clip_path.with_stem(clip_path.stem + "_annotated")
            try:
                await asyncio.to_thread(
                    annotate_video, clip_path, annotated_path, payload_obj.frames,
                )
            except Exception as exc:
                logger.warning(
                    "annotate_video failed session=%s cam=%s err=%s",
                    payload_obj.session_id, payload_obj.camera_id, exc,
                )
                if annotated_path.exists():
                    try:
                        annotated_path.unlink()
                    except OSError:
                        pass
    else:
        # Mode-two: iPhone already detected; we trust the frames list and
        # only run pairing + triangulation. No disk write, no annotated
        # clip — the viewer for this session will fall back to the
        # per-frame trace from the payload JSON.
        if payload_obj.sync_anchor_timestamp_s is None:
            # Anchor missing ⇒ the session can't pair no matter what the
            # frames say; drop them so downstream counts stay honest.
            payload_obj.frames = []
        else:
            detection_ran = True  # "detection" already ran on the device

    result = await asyncio.to_thread(state.record, payload_obj)
    if payload_obj.sync_anchor_timestamp_s is None and result.error is None:
        result.error = "no time sync"
    ball_frames = sum(1 for f in payload_obj.frames if f.ball_detected)
    logger.info(
        "pitch camera=%s session=%s clip=%s frames=%d ball=%d detected_on=%s triangulated=%d%s",
        payload_obj.camera_id,
        payload_obj.session_id,
        f"{clip_info['bytes']}B" if clip_info else "none",
        len(payload_obj.frames),
        ball_frames,
        "server" if has_video else ("device" if detection_ran else "skipped"),
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
    triangulated_on_device = result.points_on_device if result is not None else []
    return build_scene(
        session_id, pitches, triangulated,
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
            }
        else:
            cams[cam_id] = {
                "received": True,
                "calibrated": p.intrinsics is not None and p.homography is not None,
                "time_synced": p.sync_anchor_timestamp_s is not None,
                "n_frames": len(p.frames),
                "n_detected": sum(1 for f in p.frames if f.ball_detected),
            }

    duration_s: float | None = None
    timestamps = [
        f.timestamp_s for p in pitches.values() for f in p.frames
    ]
    if timestamps:
        duration_s = float(max(timestamps) - min(timestamps))

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
        fps = float(pitch.video_fps) if pitch is not None else 240.0
        anchor = pitch.sync_anchor_timestamp_s if pitch is not None else None
        if pitch is not None and anchor is not None:
            t_rel = [float(f.timestamp_s - anchor) for f in pitch.frames]
            detected = [bool(f.ball_detected) for f in pitch.frames]
            t_rel_od = [float(f.timestamp_s - anchor) for f in pitch.frames_on_device]
            detected_od = [bool(f.ball_detected) for f in pitch.frames_on_device]
        else:
            t_rel = []
            detected = []
            t_rel_od = []
            detected_od = []
        # Ship both detection streams so the viewer can render two
        # parallel density strips and overlay the dual-mode rays. Legacy
        # `t_rel_s`/`detected` keys preserved for backwards compatibility;
        # `on_device` sub-dict is empty for mono-mode sessions.
        frames_info = {
            "t_rel_s": t_rel,
            "detected": detected,
            "on_device": {"t_rel_s": t_rel_od, "detected": detected_od},
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
            capture_mode=state.current_mode().value,
        )
    )


@app.post("/reset")
def reset(purge: bool = False) -> dict[str, bool]:
    state.reset(purge_disk=purge)
    return {"ok": True, "purged": purge}
