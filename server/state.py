from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from schemas import (
    CalibrationSnapshot,
    DetectionPath,
    Device,
    FramePayload,
    PitchPayload,
    Session,
    SessionResult,
    SyncLogEntry,
    SyncReport,
    SyncResult,
    SyncRun,
    TrackingExposureCapMode,
    TriangulatedPoint,
    _DEFAULT_SESSION_TIMEOUT_S,
    _DEFAULT_PATHS,
)
from detection import HSVRange
from preview import PreviewBuffer
from marker_registry import MarkerRegistryDB
from sync_solver import compute_mutual_sync
from live_pairing import LivePairingSession
from reconstruct import Ray, ray_for_frame
from state_runtime import RuntimeSettingsStore, SyncParams
from state_calibration import (
    AutoCalibrationRun as _AutoCalibrationRun,
    AutoCalibrationRunStore,
    CalibrationFrameBuffer,
    CalibrationStore,
    CALIBRATION_FRAME_TTL_S as _CALIBRATION_FRAME_TTL_S,
    validate_calibration_snapshot as _validate_calibration_snapshot,
)
from state_devices import DeviceRegistry
from state_events import build_events
from state_processing import SessionProcessingState
import session_results

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

# Window after a sync ends (solved OR aborted) during which late aborted
# reports can still merge traces into the run's SyncResult. The side that
# never heard both bands typically POSTs its abort report right around
# the server-side timeout, and without this grace window the trace data
# (our main post-mortem signal) gets silently dropped as "no_sync".
_SYNC_LATE_REPORT_GRACE_S = 5.0

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


@dataclass
class _LegacyTimeSyncIntent:
    id: str
    started_at: float
    expires_at: float


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
        self._hsv_path = data_dir / "hsv_range.json"
        self._session_meta_path = data_dir / "session_meta.json"
        self._pitch_dir.mkdir(parents=True, exist_ok=True)
        self._result_dir.mkdir(parents=True, exist_ok=True)
        self._video_dir.mkdir(parents=True, exist_ok=True)
        self._calibration_dir.mkdir(parents=True, exist_ok=True)
        # Dashboard-control state. All in-memory — devices re-heartbeat on
        # connection, sessions don't survive restart.
        self._device_registry = DeviceRegistry(
            time_fn=time_fn,
            stale_after_s=_DEVICE_STALE_S,
            gc_after_s=_DEVICE_GC_AFTER_S,
            cap=_DEVICE_REGISTRY_CAP,
        )
        # Back-compat for tests and old diagnostics that inspected the raw
        # registry; all writes go through DeviceRegistry.
        self._devices = self._device_registry.devices
        self._current_session: Session | None = None
        self._last_ended_session: Session | None = None
        # Per-camera calibration snapshots. Written by POST /calibration,
        # read by the dashboard canvas so the 3D preview shows where each
        # phone "thinks it is" relative to the plate, independent of any
        # session. Persisted as one JSON per camera so a server restart
        # keeps whatever calibrations were live.
        self._calibration_store = CalibrationStore(
            self._calibration_dir,
            atomic_write=self._atomic_write,
        )
        self._hsv_range = self._load_hsv_range_from_disk()
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
        # Per-cam "the id we EXPECT this cam to report back with after the
        # current attempt succeeds". Set on every /sync/trigger (quick)
        # and /sync/start (mutual); a heartbeat's `time_sync_id` is
        # treated as synced-for-UI only when it matches. Without this, a
        # phone that successfully synced a previous attempt keeps its
        # LED green forever, so the operator can't see the current
        # attempt's progress.
        self._expected_sync_id_per_cam: dict[str, str] = {}
        # Injectable clock so timeout and staleness tests don't need sleeps.
        self._time_fn = time_fn
        # Runtime tunables pushed from the dashboard, hot-applied on the
        # iPhone via WS `settings` messages. Persisted so a server restart
        # doesn't silently drop the operator's last-chosen values.
        # Two independent thresholds — quick-chirp (third-device up+down
        # sweep, typically strong signal 0.2-0.9 on a clean recording)
        # vs mutual-sync (two-phone cross-detection; the far phone's
        # chirp can land much quieter, 0.06-0.3 in practice). Sharing
        # one gate forced operators to tune for the weaker modality and
        # lose false-positive margin on the stronger one.
        self._runtime_settings = RuntimeSettingsStore(
            data_dir / "runtime_settings.json",
            atomic_write=self._atomic_write,
        )
        # Live-preview buffer (Phase 4a). Keeps one latest JPEG per camera
        # in memory, gated by a per-camera "dashboard is watching" flag
        # with a 5 s TTL. Shares the State-level `_time_fn` so clock-drift
        # tests apply here too without a parallel shim.
        self._preview = PreviewBuffer(time_fn=time_fn)
        # Per-cam live quick-chirp telemetry (input RMS, peak, matched-
        # filter peaks, CFAR floors). Populated by WS heartbeat messages
        # when the phone is in .timeSyncWaiting; drained by /sync page.
        self._sync_telemetry: dict[str, dict[str, Any]] = {}
        # One-shot high-resolution calibration frame per camera. Separate
        # buffer from preview (preview is 480p, advisory; calibration
        # frames are native capture res, accuracy-critical). `_cal_frames`
        # holds (jpeg_bytes, ts) keyed by camera_id; `_cal_frame_requested`
        # flags pending requests that iOS drains on its next captureOutput.
        self._calibration_frames = CalibrationFrameBuffer(time_fn=time_fn)
        # Operator-managed marker registry. Stores 3D world coords plus a
        # "on plate plane" flag so the current planar auto-calibration path
        # can keep consuming only the eligible subset.
        self._marker_registry = MarkerRegistryDB(data_dir)
        # Per-camera auto-calibration run state. Long-running server-side
        # observation window updates this so `/setup` can show
        # searching/stabilizing/solving/verified instead of a blind spinner.
        self._auto_cal_runs = AutoCalibrationRunStore(time_fn=time_fn)
        # Live streaming state keyed by session id.
        self._live_pairings: dict[str, LivePairingSession] = {}
        # Session-level trash + processing-control metadata. Trash is
        # persisted; processing state is in-memory orchestration around
        # server-side post-processing jobs.
        self._processing = SessionProcessingState()
        # Calibrations first — _load_from_disk re-triangulates every cached
        # pitch, and triangulation needs the calibration snapshot to decide
        # the intrinsic-scale factor (MOV dims vs. calibration dims).
        self._load_session_meta_from_disk()
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
                # Legacy `frames` key maps to `frames_server_post` via the
                # PitchPayload AliasChoices; no manual pre-processing needed.
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

    def _load_session_meta_from_disk(self) -> None:
        path = self._session_meta_path
        if not path.exists():
            return
        try:
            obj = json.loads(path.read_text())
        except Exception as e:
            logger.warning("skip corrupt session_meta %s: %s", path, e)
            return
        trashed = obj.get("trashed_sessions")
        if isinstance(trashed, dict):
            parsed: dict[str, float] = {}
            for sid, ts in trashed.items():
                if isinstance(sid, str) and isinstance(ts, (int, float)):
                    parsed[sid] = float(ts)
            self._processing.load_trashed(parsed)

    def _persist_session_meta_locked(self) -> None:
        payload = json.dumps(
            {"trashed_sessions": self._processing.trashed_sessions},
            indent=2,
        )
        self._atomic_write(self._session_meta_path, payload)

    def _load_hsv_range_from_disk(self) -> HSVRange:
        path = self._hsv_path
        if not path.exists():
            return HSVRange.default()
        try:
            obj = json.loads(path.read_text())
            return HSVRange(
                h_min=int(obj["h_min"]),
                h_max=int(obj["h_max"]),
                s_min=int(obj["s_min"]),
                s_max=int(obj["s_max"]),
                v_min=int(obj["v_min"]),
                v_max=int(obj["v_max"]),
            )
        except Exception as e:
            logger.warning("skip corrupt hsv_range %s: %s", path, e)
            return HSVRange.default()

    def _persist_hsv_range_locked(self) -> None:
        payload = json.dumps(
            {
                "h_min": self._hsv_range.h_min,
                "h_max": self._hsv_range.h_max,
                "s_min": self._hsv_range.s_min,
                "s_max": self._hsv_range.s_max,
                "v_min": self._hsv_range.v_min,
                "v_max": self._hsv_range.v_max,
            },
            indent=2,
        )
        self._atomic_write(self._hsv_path, payload)

    def _calibration_path(self, camera_id: str) -> Path:
        return self._calibration_store.path(camera_id)

    def set_calibration(self, snapshot: CalibrationSnapshot) -> None:
        """Record (or overwrite) one camera's calibration and persist it
        atomically so the dashboard survives a restart. Last write wins.

        Validates K/H/dims self-consistency before storing — an earlier bug
        mixed 1080p intrinsics with 480p homography which silently produced
        garbage extrinsics downstream. Catching it at the boundary saves
        hours of "why is Cam A at Z=0.66m" debugging."""
        with self._lock:
            self._calibration_store.set(snapshot)

    def calibrations(self) -> dict[str, CalibrationSnapshot]:
        with self._lock:
            return self._calibration_store.snapshot()

    # The following three wrappers are kept because external callers
    # (main.py, routes/sessions.py, routes/settings.py, routes/pitch.py)
    # reach for them via `state._foo`. The module-level implementations
    # live in session_results / detection_paths; call those directly from
    # new code.

    @staticmethod
    def _normalize_paths(
        raw_paths: list[str] | set[DetectionPath] | None,
    ) -> set[DetectionPath]:
        return session_results.normalize_paths(raw_paths)

    def _paths_for_pitch(self, pitch: PitchPayload) -> set[DetectionPath]:
        return session_results.paths_for_pitch(self, pitch)

    def _rebuild_result_for_session(self, session_id: str) -> SessionResult:
        return session_results.rebuild_result_for_session(self, session_id)

    def ingest_live_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> tuple[list[TriangulatedPoint], dict[str, int]]:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            cal_a = self._calibration_store.get("A")
            cal_b = self._calibration_store.get("B")
            dev_a = self._device_registry.get("A")
            dev_b = self._device_registry.get("B")
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
            pts = session_results.triangulate_pair(self, pa, pb, source="server")
            return pts[0] if pts else None

        created = live.ingest(camera_id, frame, triangulate_live)
        return created, dict(live.frame_counts)

    def live_ray_for_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> Ray | None:
        """Project one live detection into world space for dashboard rays.

        Stereo live points still require A/B pairing and a shared time anchor.
        A monocular ray only needs that camera's calibration; if the phone has
        no sync anchor, use the frame index as an approximate relative clock so
        hover/color values stay small and readable.
        """
        with self._lock:
            cal = self._calibration_store.get(camera_id)
            dev = self._device_registry.get(camera_id)
        if cal is None:
            return None
        anchor = (
            dev.sync_anchor_timestamp_s
            if dev is not None and dev.sync_anchor_timestamp_s is not None
            else frame.timestamp_s - (float(frame.frame_index) / 240.0)
        )
        return ray_for_frame(
            camera_id=camera_id,
            frame=frame,
            intrinsics=cal.intrinsics,
            homography=list(cal.homography),
            anchor_timestamp_s=anchor,
            source="live",
        )

    def mark_live_path_ended(self, camera_id: str, session_id: str, reason: str | None = None) -> None:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            live.mark_completed(camera_id)
            if reason and reason != "disarmed":
                live.mark_aborted(camera_id, reason)

    def _drop_live_pairing_if_persisted_locked(self, session_id: str) -> bool:
        """Drop `_live_pairings[session_id]` when the live path has fully
        drained — both cams have reported cycle_end AND each cam's live
        detections have been persisted onto its pitch JSON (i.e. the
        `frames_live` buckets on both pitches match the live buffer).
        Returns True when the entry was dropped.

        Idempotent — safe to invoke repeatedly; the method only removes
        an entry when the twin-persisted precondition is met, so a later
        call after a successful drop is a no-op. Caller must hold
        `self._lock`.

        Scope: this only covers the normal end-of-session pairing flow.
        `delete_session` / `reset` / `cancel_session` have their own
        explicit pops because those paths discard data rather than drain
        it, and must not be gated on persistence."""
        live = self._live_pairings.get(session_id)
        if live is None:
            return False
        if not {"A", "B"}.issubset(live.completed_cameras):
            return False
        pa = self.pitches.get(("A", session_id))
        pb = self.pitches.get(("B", session_id))
        if pa is None or pb is None:
            return False
        # Sanity — ensure each cam's live detections already made it onto
        # the pitch. We only need to confirm the frame counts landed, not
        # do a byte-for-byte compare; the pitch-JSON write that happened
        # inside `persist_live_frames` is the authoritative archive.
        a_count = live.frame_counts.get("A", 0)
        b_count = live.frame_counts.get("B", 0)
        if a_count and len(pa.frames_live) < a_count:
            return False
        if b_count and len(pb.frames_live) < b_count:
            return False
        self._live_pairings.pop(session_id, None)
        return True

    def _drop_live_pairing(self, session_id: str) -> None:
        """Unconditionally pop a `LivePairingSession` from `_live_pairings`
        if present. Idempotent — callers may invoke this from multiple
        lifecycle hooks (cancel, delete, reset) without having to
        remember whether a prior hook already fired."""
        with self._lock:
            self._live_pairings.pop(session_id, None)

    def persist_live_frames(self, camera_id: str, session_id: str) -> SessionResult | None:
        with self._lock:
            existing = self.pitches.get((camera_id, session_id))
            live_frames = session_results.live_frames_for_camera_locked(
                self, session_id, camera_id,
            )
        if existing is None or not live_frames:
            return None
        if existing.frames_live == live_frames:
            return self.get(session_id)
        merged = existing.model_copy(deep=True)
        merged.frames_live = list(live_frames)
        return self.record(merged)

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
            existing = self.pitches.get((pitch.camera_id, pitch.session_id))
            live_frames = session_results.live_frames_for_camera_locked(
                self, pitch.session_id, pitch.camera_id,
            )
            merged = pitch.model_copy(deep=True)
            if existing is not None:
                if not merged.frames_live and existing.frames_live:
                    merged.frames_live = list(existing.frames_live)
                if not merged.frames_server_post and existing.frames_server_post:
                    merged.frames_server_post = list(existing.frames_server_post)
            if not merged.frames_live and live_frames:
                merged.frames_live = list(live_frames)
            pitch = merged
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
            # Once both cams' pitches carry their live frames and both
            # cams have reported cycle_end, the rolling `_live_pairings`
            # entry is dead weight — its contents are fully archived on
            # the pitch JSONs and replayable from disk. Drop it so a
            # long-running server doesn't accumulate one bounded-but-
            # nonzero session entry per pairing forever.
            self._drop_live_pairing_if_persisted_locked(pitch.session_id)
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
        with self._lock:
            self._calibration_frames.request(camera_id)

    def is_calibration_frame_requested(self, camera_id: str) -> bool:
        """True if the flag is pending and within TTL. Lazy-sweeps stale."""
        with self._lock:
            return self._calibration_frames.is_requested(camera_id)

    def requested_calibration_frame_ids(self) -> list[str]:
        with self._lock:
            return self._calibration_frames.requested_ids()

    def store_calibration_frame(self, camera_id: str, jpeg_bytes: bytes) -> None:
        """Phone pushed a calibration frame; stash it and clear the flag."""
        with self._lock:
            self._calibration_frames.store(camera_id, jpeg_bytes)

    def consume_calibration_frame(
        self, camera_id: str, max_age_s: float = _CALIBRATION_FRAME_TTL_S,
    ) -> tuple[bytes, float] | None:
        """Atomic pop-if-fresh. Returns None if no frame cached or stale."""
        with self._lock:
            return self._calibration_frames.consume(camera_id, max_age_s=max_age_s)

    # --- Auto-calibration runs -------------------------------------------

    def start_auto_cal_run(self, camera_id: str) -> _AutoCalibrationRun:
        with self._lock:
            return self._auto_cal_runs.start(camera_id)

    def update_auto_cal_run(self, camera_id: str, **updates: Any) -> _AutoCalibrationRun | None:
        with self._lock:
            return self._auto_cal_runs.update(camera_id, **updates)

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
        with self._lock:
            return self._auto_cal_runs.finish(
                camera_id,
                status=status,
                result=result,
                summary=summary,
                detail=detail,
                applied=applied,
            )

    def auto_cal_status(self) -> dict[str, Any]:
        with self._lock:
            return self._auto_cal_runs.status()

    def live_session_summary(self) -> dict[str, Any] | None:
        session = self.session_snapshot()
        if session is None:
            return None
        with self._lock:
            live = self._live_pairings.get(session.id)
            result = self.results.get(session.id)
        paths_completed = sorted(result.paths_completed) if result is not None else []
        if live is None:
            return {
                "session_id": session.id,
                "armed": session.armed,
                "paths": sorted(p.value for p in session.paths),
                "frame_counts": {},
                "point_count": 0,
                "paths_completed": paths_completed,
                "abort_reasons": {},
            }
        return {
            "session_id": session.id,
            "armed": session.armed,
            "paths": sorted(p.value for p in session.paths),
            "frame_counts": dict(live.frame_counts),
            "point_count": len(live.triangulated),
            "paths_completed": paths_completed,
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

    def _claim_time_sync_intent_locked(
        self, now: float, *, force_new: bool = False,
    ) -> _LegacyTimeSyncIntent:
        """`force_new=True` always mints a fresh id. Used by the
        dashboard-remote trigger where each button click is a distinct
        attempt — otherwise a second click inside the intent window
        would hand back the same id, and cams already synced from the
        prior attempt wouldn't flip their LED red (the id_match stayed
        true). The per-phone POST /sync/claim path still gets the dedup
        behavior so two phones claiming within a few seconds of each
        other converge on one id."""
        if not force_new:
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
        with self._lock:
            self._device_registry.heartbeat(
                camera_id,
                time_synced=time_synced,
                time_sync_id=time_sync_id,
                sync_anchor_timestamp_s=sync_anchor_timestamp_s,
            )

    def record_sync_telemetry(self, camera_id: str, telem: dict[str, Any]) -> None:
        """Stash the latest quick-chirp live telemetry for a cam AND roll
        a per-cam peak-observed window so the operator can read maxima
        achieved during a Quick chirp attempt even AFTER the phone stops
        listening. `reset_sync_telemetry_peaks` clears the window at the
        start of each /sync/trigger so the numbers reflect this attempt,
        not all-time."""
        now = self._time_fn()
        with self._lock:
            prior = self._sync_telemetry.get(camera_id, {})

            def roll_max(key: str) -> float | None:
                new_raw = telem.get(key)
                try:
                    new_v = None if new_raw is None else float(new_raw)
                except (TypeError, ValueError):
                    new_v = None
                old_raw = prior.get(f"peak_{key}")
                try:
                    old_v = None if old_raw is None else float(old_raw)
                except (TypeError, ValueError):
                    old_v = None
                if new_v is None:
                    return old_v
                if old_v is None:
                    return new_v
                return max(old_v, new_v)

            rolled = {
                f"peak_{k}": roll_max(k)
                for k in ("input_rms", "input_peak", "up_peak", "down_peak")
            }
            self._sync_telemetry[camera_id] = {
                "ts": now,
                **{k: telem.get(k) for k in (
                    "mode", "armed", "input_rms", "input_peak",
                    "up_peak", "down_peak", "cfar_up_floor",
                    "cfar_down_floor", "threshold", "pending_up",
                )},
                **rolled,
            }

    def clear_last_sync_result(self) -> None:
        """Drop the latched `last_sync_result` so a fresh listen window
        doesn't render stale ABORTED text in the Sync Control card."""
        with self._lock:
            self._last_sync_result = None

    def reset_sync_telemetry_peaks(self, camera_ids: list[str] | None = None) -> None:
        """Zero the rolling peak columns for the named cams (or every cam
        currently in the registry). Called at the start of each
        /sync/trigger so the operator sees peaks for THIS attempt only."""
        with self._lock:
            targets = camera_ids if camera_ids is not None else list(self._sync_telemetry.keys())
            for cam in targets:
                rec = self._sync_telemetry.get(cam)
                if rec is None:
                    continue
                for k in ("input_rms", "input_peak", "up_peak", "down_peak"):
                    rec.pop(f"peak_{k}", None)

    def sync_telemetry_snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-cam telemetry. No staleness sweep — the operator wants to
        read the last observed values AFTER the phone stops listening
        for post-hoc analysis. `age_s` is attached so the UI can decorate
        stale entries (fade / "last seen N s ago") without inventing a
        client clock."""
        now = self._time_fn()
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for cam, rec in self._sync_telemetry.items():
                r = dict(rec)
                r["age_s"] = max(0.0, now - float(rec.get("ts", now)))
                out[cam] = r
        return out

    def mark_device_offline(self, camera_id: str) -> None:
        """Age out `last_seen_at` so the device shows offline on the very
        next `/status` poll instead of waiting for the stale window to
        close. Called from the WS disconnect `finally` — the phone has
        explicitly dropped its control channel (e.g. screen lock, app
        backgrounded), so the 3 s grace that a dropped heartbeat would
        earn is no longer appropriate."""
        with self._lock:
            self._device_registry.mark_offline(camera_id)

    def _common_time_sync_id_locked(self, now: float) -> str | None:
        fresh = self._device_registry.online()
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
        with self._lock:
            return self._device_registry.online(stale_after_s)

    def known_camera_ids(self) -> list[str]:
        """All camera ids that have ever heartbeated this run — used by WS
        broadcast targets that want to notify siblings regardless of current
        liveness (e.g. calibration updates, which the other cam might care
        about even if its heartbeat lapsed briefly)."""
        with self._lock:
            return self._device_registry.known_camera_ids()

    def device_snapshot(self, camera_id: str) -> Device | None:
        with self._lock:
            return self._device_registry.snapshot(camera_id)

    def _check_session_timeout_locked(self, now: float) -> None:
        """If the current session has exceeded its max_duration_s, transition
        it to ended. Assumes the caller holds `self._lock`.

        Live pairing buffers are NOT dropped here — a cam that has been
        streaming frames up to the timeout still needs its buffered
        detections to flow through `persist_live_frames` once the WS
        `cycle_end` arrives. The bounded-deque design in `LivePairingSession`
        means the entry is size-capped either way; `mark_live_path_ended`
        or `delete_session` will drop the `_live_pairings` entry for real."""
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
        Snapshots the current default detection paths so a late dashboard
        toggle can't disturb the in-flight recording."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            if self._current_session is not None:
                return self._current_session
            chosen_paths = set(paths or self._runtime_settings.default_paths or _DEFAULT_PATHS)
            session = Session(
                id=_new_session_id(),
                started_at=now,
                max_duration_s=max_duration_s,
                paths=chosen_paths,
                tracking_exposure_cap=self._runtime_settings.tracking_exposure_cap,
                sync_id=self._common_time_sync_id_locked(now),
            )
            self._live_pairings[session.id] = LivePairingSession(session.id)
            self._current_session = session
            self._current_time_sync_intent = None
            return session

    def default_paths(self) -> set[DetectionPath]:
        with self._lock:
            return set(self._runtime_settings.default_paths)

    def hsv_range(self) -> HSVRange:
        with self._lock:
            return self._hsv_range

    def set_hsv_range(self, hsv_range: HSVRange) -> HSVRange:
        with self._lock:
            self._hsv_range = hsv_range
            self._persist_hsv_range_locked()
            return self._hsv_range

    def set_default_paths(self, paths: set[DetectionPath]) -> set[DetectionPath]:
        with self._lock:
            return self._runtime_settings.set_default_paths(paths)

    def capture_height_px(self) -> int:
        with self._lock:
            return self._runtime_settings.capture_height_px

    def set_capture_height_px(self, value: int) -> int:
        with self._lock:
            return self._runtime_settings.set_capture_height_px(value)

    def chirp_detect_threshold(self) -> float:
        with self._lock:
            return self._runtime_settings.chirp_detect_threshold

    def set_chirp_detect_threshold(self, value: float) -> float:
        with self._lock:
            return self._runtime_settings.set_chirp_detect_threshold(value)

    def mutual_sync_threshold(self) -> float:
        with self._lock:
            return self._runtime_settings.mutual_sync_threshold

    def set_mutual_sync_threshold(self, value: float) -> float:
        with self._lock:
            return self._runtime_settings.set_mutual_sync_threshold(value)

    def sync_params(self) -> SyncParams:
        with self._lock:
            return self._runtime_settings.sync_params

    def set_sync_params(self, params: SyncParams) -> None:
        with self._lock:
            self._runtime_settings.sync_params = params

    def heartbeat_interval_s(self) -> float:
        with self._lock:
            return self._runtime_settings.heartbeat_interval_s

    def set_heartbeat_interval_s(self, value: float) -> float:
        with self._lock:
            return self._runtime_settings.set_heartbeat_interval_s(value)

    def tracking_exposure_cap(self) -> TrackingExposureCapMode:
        with self._lock:
            return self._runtime_settings.tracking_exposure_cap

    def set_tracking_exposure_cap(self, mode: TrackingExposureCapMode) -> TrackingExposureCapMode:
        with self._lock:
            return self._runtime_settings.set_tracking_exposure_cap(mode)

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

    def _merge_late_abort_report_locked(
        self, report: SyncReport, now: float,
    ) -> None:
        """Merge a post-timeout abort report's traces into the already-
        latched `_last_sync_result`. Keeps the run's diagnostic picture
        intact even when one phone's abort POST races the server-side
        timeout. Logs a post-mortem line for the merged streams so the
        sync log still carries the quantitative context. Caller must
        hold `self._lock`."""
        result = self._last_sync_result
        if result is None:
            return
        updates: dict[str, Any] = {}
        reasons = dict(result.abort_reasons)
        if report.abort_reason:
            reasons[report.role] = report.abort_reason
        else:
            reasons.setdefault(report.role, "aborted_late")
        updates["abort_reasons"] = reasons
        updates["aborted"] = True
        if report.role == "A":
            if report.trace_self is not None:
                updates["trace_a_self"] = report.trace_self
            if report.trace_other is not None:
                updates["trace_a_other"] = report.trace_other
            if report.t_self_s is not None:
                updates["t_a_self_s"] = report.t_self_s
            if report.t_from_other_s is not None:
                updates["t_a_from_b_s"] = report.t_from_other_s
        else:
            if report.trace_self is not None:
                updates["trace_b_self"] = report.trace_self
            if report.trace_other is not None:
                updates["trace_b_other"] = report.trace_other
            if report.t_self_s is not None:
                updates["t_b_self_s"] = report.t_self_s
            if report.t_from_other_s is not None:
                updates["t_b_from_a_s"] = report.t_from_other_s
        self._last_sync_result = result.model_copy(update=updates)
        self._sync_log.append(SyncLogEntry(
            ts=now, source="server", event="report_late_merged",
            detail={
                "id": report.sync_id,
                "role": report.role,
                "reason": report.abort_reason,
                "had_traces": {
                    "self": report.trace_self is not None,
                    "other": report.trace_other is not None,
                },
            },
        ))
        logger.info(
            "sync report_late_merged id=%s role=%s reason=%s",
            report.sync_id, report.role, report.abort_reason,
        )
        # Fire post-mortem on the newly-merged streams so the sync log
        # has a self-contained quantitative line per late-arriving abort.
        # Mutual-sync uses its own threshold; quick-chirp's is independent.
        thr = self._runtime_settings.mutual_sync_threshold
        if report.role == "A":
            self._log_trace_post_mortem_locked(
                report.sync_id, "A.self", report.trace_self, thr)
            self._log_trace_post_mortem_locked(
                report.sync_id, "A.other", report.trace_other, thr)
        else:
            self._log_trace_post_mortem_locked(
                report.sync_id, "B.self", report.trace_self, thr)
            self._log_trace_post_mortem_locked(
                report.sync_id, "B.other", report.trace_other, thr)

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
        thr = self._runtime_settings.chirp_detect_threshold
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
            # Dashboard trigger = explicit new attempt. Force a fresh id
            # so previously-synced cams' LEDs flip red (id_match breaks)
            # instead of silently reusing the previous intent's id.
            intent = (
                self._claim_time_sync_intent_locked(now, force_new=True)
                if targets else None
            )
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

    def set_expected_sync_id(self, camera_ids: list[str], sync_id: str) -> None:
        """Record `sync_id` as the id we expect the listed cams to report
        after the current listen window succeeds. Heartbeats reporting
        anything else are rendered as not-synced on the dashboard,
        which gives the operator a per-cam "listening" → "synced"
        transition instead of a stuck-green LED from a previous
        attempt."""
        with self._lock:
            for cam in camera_ids:
                self._expected_sync_id_per_cam[cam] = sync_id

    def expected_sync_id_snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._expected_sync_id_per_cam)

    def pending_sync_command_ids(self) -> dict[str, str]:
        """Per-cam mapping from cam → the actual legacy-sync intent id,
        for WS push to iOS. The public `pending_sync_commands()` returns
        the literal "start" as its value because it feeds a dashboard
        chip that doesn't need the id — but the WS push MUST send the
        real `_LegacyTimeSyncIntent.id` so iOS can echo it back in its
        heartbeat's `time_sync_id`. Earlier the WS push re-used the
        dashboard-chip map and accidentally shipped "start" as the id,
        which then appeared verbatim on the dashboard as
        `sync_id start`."""
        now = self._time_fn()
        with self._lock:
            return {
                cam: pending.id
                for cam, pending in self._sync_command_pending.items()
                if pending.expires_at > now
            }

    def _session_is_trashed_locked(self, session_id: str) -> bool:
        return self._processing.is_trashed(session_id)

    def trash_session(self, session_id: str) -> bool:
        now = self._time_fn()
        with self._lock:
            current = self._current_session
            if (
                current is not None
                and current.ended_at is None
                and current.id == session_id
            ):
                raise RuntimeError(
                    f"cannot trash armed session {session_id}; stop it first"
                )
            known = any(sid == session_id for _, sid in self.pitches) or session_id in self.results
            if not known:
                return False
            self._processing.trash(session_id, at=now)
            self._persist_session_meta_locked()
            return True

    def restore_session(self, session_id: str) -> bool:
        with self._lock:
            if not self._processing.restore(session_id):
                return False
            self._persist_session_meta_locked()
            return True

    def trash_count(self) -> int:
        with self._lock:
            return self._processing.trash_count()

    def _session_server_post_candidates(self, session_id: str) -> list[tuple[str, PitchPayload, Path]]:
        with self._lock:
            pitches = [
                (cam, pitch)
                for (cam, sid), pitch in self.pitches.items()
                if sid == session_id
            ]
        candidates: list[tuple[str, PitchPayload, Path]] = []
        for cam, pitch in pitches:
            if self._session_is_trashed(session_id):
                continue
            if DetectionPath.server_post not in self._paths_for_pitch(pitch):
                continue
            if pitch.frames_server_post:
                continue
            clip_path = self._find_video_for_session_camera(session_id, cam)
            if clip_path is None:
                continue
            candidates.append((cam, pitch, clip_path))
        return candidates

    def _find_video_for_session_camera(self, session_id: str, camera_id: str) -> Path | None:
        matches = sorted(self._video_dir.glob(f"session_{session_id}_{camera_id}.*"))
        for path in matches:
            if path.name.endswith(".tmp"):
                continue
            if "_annotated." in path.name:
                continue
            return path
        return None

    def _session_is_trashed(self, session_id: str) -> bool:
        with self._lock:
            return self._session_is_trashed_locked(session_id)

    def mark_server_post_queued(self, session_id: str, camera_id: str) -> None:
        with self._lock:
            self._processing.mark_queued((camera_id, session_id))

    def start_server_post_job(self, session_id: str, camera_id: str) -> bool:
        with self._lock:
            return self._processing.start_job((camera_id, session_id))

    def should_cancel_server_post_job(self, session_id: str, camera_id: str) -> bool:
        with self._lock:
            return self._processing.should_cancel((camera_id, session_id))

    def finish_server_post_job(self, session_id: str, camera_id: str, *, canceled: bool) -> None:
        with self._lock:
            self._processing.finish_job((camera_id, session_id), canceled=canceled)

    def record_server_post_abort(
        self, session_id: str, camera_id: str, reason: str,
    ) -> SessionResult | None:
        """Persist a server-side post-processing failure so the dashboard
        can render it as a red pill on `/events`. Writes `reason` into
        `SessionResult.abort_reasons[server_post]` (keyed by pipeline, not
        by cam, because the events view pills at pipeline granularity)
        and re-persists the result JSON. Returns the updated result, or
        None if no result record exists yet (e.g. the pitch JSON was
        already deleted).

        Idempotent — calling twice with the same reason produces the
        same on-disk state. A second call with a different reason
        replaces the first; `_run_server_detection`'s lifecycle is
        one-shot per (session, cam) so the collision case is only
        possible via operator resume, in which case the fresher reason
        is the useful one."""
        with self._lock:
            result = self.results.get(session_id)
            if result is None:
                return None
            reasons = dict(result.abort_reasons)
            reasons[DetectionPath.server_post.value] = reason
            # Also key by camera so diagnostics can see which cam failed
            # when only one side errored. State_events collapses the
            # cam-keyed entries into the same pipeline pill via the
            # `server_post:` prefix check in `_path_status_pills`, so the
            # extra detail doesn't produce spurious pills.
            reasons[f"{DetectionPath.server_post.value}:{camera_id}"] = reason
            updated = result.model_copy(
                update={
                    "abort_reasons": reasons,
                    "aborted": True,
                }
            )
            self.results[session_id] = updated
        self._atomic_write(self._result_path(session_id), updated.model_dump_json())
        return updated

    def cancel_processing(self, session_id: str) -> bool:
        keys = [(cam, session_id) for cam, _pitch, _clip in self._session_server_post_candidates(session_id)]
        with self._lock:
            return self._processing.cancel(keys)

    def resume_processing(self, session_id: str) -> list[tuple[Path, PitchPayload]]:
        candidates = self._session_server_post_candidates(session_id)
        by_key = {
            (cam, session_id): (clip_path, pitch)
            for cam, pitch, clip_path in candidates
        }
        with self._lock:
            queued_keys = self._processing.resume(by_key.keys())
        return [
            (by_key[key][0], by_key[key][1].model_copy(deep=True))
            for key in queued_keys
        ]

    def session_processing_summary(self, session_id: str) -> tuple[str | None, bool]:
        candidates = self._session_server_post_candidates(session_id)
        pending_keys = {(cam, session_id) for cam, _pitch, _clip in candidates}
        with self._lock:
            completed = any(
                sid == session_id and bool(pitch.frames_server_post)
                for (_cam, sid), pitch in self.pitches.items()
            )
            return self._processing.summary(
                pending_keys=pending_keys,
                completed=completed,
                has_candidates=bool(candidates),
            )

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
            # Fresh listen window → drop prior run's result so the "Last"
            # chip doesn't show stale ABORTED / timing from a previous
            # attempt. Telemetry peaks reset independently via
            # `reset_sync_telemetry_peaks`.
            self._last_sync_result = None
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
                # Late abort reports arrive right after the server-side
                # timeout fired and cleared `_current_sync`. Without this
                # grace path we lose the trace data from the side that
                # never produced a full report (typically the failed cam),
                # which is exactly the diagnostic we need most.
                if (
                    report.aborted
                    and self._last_sync_result is not None
                    and self._last_sync_result.id == report.sync_id
                    and now - self._last_sync_result.solved_at <= _SYNC_LATE_REPORT_GRACE_S
                ):
                    self._merge_late_abort_report_locked(report, now)
                    return None, None, None
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
            # Nullable on aborted reports (phone heard its own band but
            # not the other's, or timed out before either) — can't format
            # None with %f, so render them as strings first.
            fmt_ts = lambda v: "None" if v is None else f"{float(v):.6f}"
            logger.info(
                "sync report received id=%s role=%s t_self=%s t_from_other=%s aborted=%s",
                run.id, report.role,
                fmt_ts(report.t_self_s), fmt_ts(report.t_from_other_s),
                bool(report.aborted),
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

    def events(self, *, bucket: str = "active") -> list[dict[str, Any]]:
        return build_events(self, bucket=bucket)

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
            self._processing.remove_session(session_id)
            # Also drop the rolling live buffer — if the session hadn't
            # yet had both cams emit cycle_end, `_live_pairings` would
            # otherwise leak its bounded-but-nonzero footprint forever.
            self._live_pairings.pop(session_id, None)
            if (
                self._last_ended_session is not None
                and self._last_ended_session.id == session_id
            ):
                self._last_ended_session = None
            self._persist_session_meta_locked()

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
            self._device_registry.clear()
            self._current_session = None
            self._last_ended_session = None
            self._processing.clear()
            self._live_pairings.clear()
            self._persist_session_meta_locked()
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
                self._session_meta_path.unlink(missing_ok=True)
