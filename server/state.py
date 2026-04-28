from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from schemas import (
    CalibrationSnapshot,
    DetectionPath,
    Device,
    DeviceIntrinsics,
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
from candidate_selector import CandidateSelectorTuning
from chain_filter import ChainFilterParams, annotate as chain_filter_annotate
from detection import HSVRange, ShapeGate
from preview import PreviewBuffer
from marker_registry import MarkerRegistryDB
from live_pairing import LivePairingSession
from reconstruct import Ray, ray_for_frame
from state_runtime import RuntimeSettingsStore, SyncParams
from state_calibration import (
    AutoCalibrationRun as _AutoCalibrationRun,
    AutoCalibrationRunStore,
    CalibrationFrameBuffer,
    CalibrationStore,
    CALIBRATION_FRAME_TTL_S as _CALIBRATION_FRAME_TTL_S,
    DeviceIntrinsicsStore,
    scale_intrinsics_to,
    validate_calibration_snapshot as _validate_calibration_snapshot,
)
from state_devices import DeviceRegistry
from state_events import build_events
from state_processing import SessionProcessingState
from state_sync import (
    SyncCoordinator,
    _LegacyTimeSyncIntent,
    _new_sync_id,
    _SYNC_COMMAND_TTL_S,
    _SYNC_COOLDOWN_S,
    _SYNC_LATE_REPORT_GRACE_S,
    _SYNC_TIMEOUT_S,
    _TIME_SYNC_INTENT_WINDOW_S,
    _TIME_SYNC_MAX_AGE_S,
)
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

# Sync / chirp subsystem constants live in state_sync.py. `_TIME_SYNC_MAX_AGE_S`
# is re-exported here because `_common_time_sync_id_locked` (device-registry
# aware, kept on State) still consults it.

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
    """Server-wide in-memory state, protected by a single `_lock` (see
    invariant below).

    **Lock model — read before refactoring**

    There is exactly one mutex (`self._lock`, a non-reentrant
    `threading.Lock`) and it serialises every read/write to ANY of the
    underlying stores: pitches, results, sessions, calibration stores,
    device registry, sync coordinator, runtime settings, live pairings.
    The stores themselves (`RuntimeSettingsStore`, `CalibrationStore`,
    `DeviceIntrinsicsStore`, etc.) are deliberately NOT thread-safe in
    isolation — `state_runtime.py` even spells this out: "State owns
    synchronization; this class owns validation, defaults, and the JSON
    shape on disk."

    This is why the ~22 settings facades and ~18 calibration facades on
    this class look like one-line pass-through (`with self._lock: return
    self._runtime_settings.X`). They are not vestigial abstraction — the
    `with self._lock` IS the work. Deleting them and pointing callers
    directly at `state._runtime_settings.X` would silently lose the
    synchronization. Don't.

    The single-lock-everything model is intentional for now (personal LAN
    rig, low contention) but it does mean a long-running operation under
    `_lock` blocks every other touchpoint. If contention ever shows up as
    a real symptom, the next move is to push locks INTO each store and
    then collapse the facades — both at once, atomically. Don't try to
    do half of it.

    Methods whose name ends in `_locked` MUST be called with `self._lock`
    already held. Methods that take the lock themselves must not be
    called from another `_lock`-holding context — `Lock` is non-reentrant
    and would deadlock; if you need that pattern, switch the lock to
    `RLock` first."""

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
        self._shape_gate_path = data_dir / "shape_gate.json"
        self._candidate_selector_tuning_path = data_dir / "candidate_selector_tuning.json"
        self._chain_filter_params_path = data_dir / "chain_filter_params.json"
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
        # Recently-ended sessions ring (most recent first). Kept >1 deep so
        # that an iOS phone draining its detection backlog after disarm can
        # still locate its session even if the operator has armed (and
        # ended) another session in the meantime — the prior single-slot
        # `_last_ended_session` would silently lose the original reference,
        # causing late frames to fall through `ingest_live_frame` lookup.
        # `maxlen` covers four overlapping drain/arm cycles which is well
        # above any realistic operator cadence; older sessions still live
        # on disk via `pitches` / `results` for any consumer that needs
        # historical reconstruction.
        self._recently_ended_sessions: deque[Session] = deque(maxlen=4)
        # Per-camera calibration snapshots. Written by POST /calibration,
        # read by the dashboard canvas so the 3D preview shows where each
        # phone "thinks it is" relative to the plate, independent of any
        # session. Persisted as one JSON per camera so a server restart
        # keeps whatever calibrations were live.
        self._calibration_store = CalibrationStore(
            self._calibration_dir,
            atomic_write=self._atomic_write,
        )
        # Per-device ChArUco intrinsics, keyed by identifierForVendor. Read
        # by auto-cal as the preferred intrinsics source; written by the
        # dashboard upload endpoint. `data/intrinsics/` lives next to
        # `data/calibrations/` so operators see them side-by-side.
        self._device_intrinsics_dir = data_dir / "intrinsics"
        self._device_intrinsics = DeviceIntrinsicsStore(
            self._device_intrinsics_dir,
            atomic_write=self._atomic_write,
        )
        self._hsv_range = self._load_hsv_range_from_disk()
        self._shape_gate = self._load_shape_gate_from_disk()
        self._candidate_selector_tuning = self._load_candidate_selector_tuning_from_disk()
        self._chain_filter_params = self._load_chain_filter_params_from_disk()
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
        # Sync / chirp subsystem: mutual-chirp run lifecycle, legacy
        # single-listener command dispatch, diagnostic log, per-cam
        # telemetry. Shares the State lock — all `*_locked` coordinator
        # helpers assume the caller already holds it.
        self._sync = SyncCoordinator(
            self._lock,
            self._time_fn,
            self._runtime_settings,
        )
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
        # Cams observed to be missing calibration while live frames were
        # arriving, keyed by session id. Surfaced on live_session_summary
        # / /events so the operator sees a reason for a silent live path
        # instead of having to tail the server log.
        self._live_missing_cal: dict[str, set[str]] = {}
        # Dedupe key for the warn-once-per-cam-session log below.
        self._live_missing_cal_logged: set[tuple[str, str]] = set()
        # Sessions whose live frame buffer needs to be flushed to disk.
        # Populated by `_check_session_timeout_locked` / `stop_session` and
        # drained by the next `current_session()` / `arm_session()` call,
        # which runs the actual flush outside the lock. Covers the case
        # where iOS dies mid-session without sending `cycle_end`, so the
        # in-memory live frames would otherwise be lost.
        self._pending_live_flush_sessions: set[str] = set()
        # Session-level trash + server_post-processing job coordinator.
        # Owns job state, per-(session, cam) error strings, and the
        # candidate-finder used by /events + /sessions/{sid}/run_server_post.
        # Trash is persisted through State's session-meta JSON; everything
        # else is in-memory orchestration. attach() wires up the State +
        # lock refs so the coordinator can read pitches / video dir.
        self._processing = SessionProcessingState()
        self._processing.attach(self, self._lock)
        # Calibrations first — _load_from_disk re-triangulates every cached
        # pitch, and triangulation needs the calibration snapshot to decide
        # the intrinsic-scale factor (MOV dims vs. calibration dims).
        self._load_session_meta_from_disk()
        self._load_from_disk()

    # ---- Sync coordinator passthroughs --------------------------------
    # Back-compat shims so external callers (older tests, old diagnostic
    # hooks) that poked at `state._current_sync` / `state._last_sync_result`
    # / `state._sync_cooldown_until` keep working after the sync subsystem
    # moved into `SyncCoordinator`.
    @property
    def _current_sync(self) -> SyncRun | None:
        return self._sync._current_sync

    @_current_sync.setter
    def _current_sync(self, value: SyncRun | None) -> None:
        self._sync._current_sync = value

    @property
    def _last_sync_result(self) -> SyncResult | None:
        return self._sync._last_sync_result

    @_last_sync_result.setter
    def _last_sync_result(self, value: SyncResult | None) -> None:
        self._sync._last_sync_result = value

    @property
    def _sync_cooldown_until(self) -> float:
        return self._sync._sync_cooldown_until

    @_sync_cooldown_until.setter
    def _sync_cooldown_until(self, value: float) -> None:
        self._sync._sync_cooldown_until = value

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
            # Annotate pre-filter-era pitches (filter_status=None everywhere)
            # so the viewer can render ghost-mode on historical sessions
            # without a reprocess_sessions run.
            chain_filter_annotate(pitch.frames_live, self._chain_filter_params)
            chain_filter_annotate(pitch.frames_server_post, self._chain_filter_params)
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

    def _load_shape_gate_from_disk(self) -> ShapeGate:
        path = self._shape_gate_path
        if not path.exists():
            return ShapeGate.default()
        try:
            obj = json.loads(path.read_text())
            return ShapeGate(
                aspect_min=float(obj["aspect_min"]),
                fill_min=float(obj["fill_min"]),
            )
        except Exception as e:
            logger.warning("skip corrupt shape_gate %s: %s", path, e)
            return ShapeGate.default()

    def _persist_shape_gate_locked(self) -> None:
        payload = json.dumps(
            {
                "aspect_min": self._shape_gate.aspect_min,
                "fill_min": self._shape_gate.fill_min,
            },
            indent=2,
        )
        self._atomic_write(self._shape_gate_path, payload)

    def _load_candidate_selector_tuning_from_disk(self) -> CandidateSelectorTuning:
        path = self._candidate_selector_tuning_path
        if not path.exists():
            return CandidateSelectorTuning.default()
        try:
            obj = json.loads(path.read_text())
            return CandidateSelectorTuning(
                r_px_expected=float(obj["r_px_expected"]),
                w_area=float(obj["w_area"]),
                w_dist=float(obj["w_dist"]),
                dist_cost_sat_radii=float(obj["dist_cost_sat_radii"]),
            )
        except Exception as e:
            logger.warning("skip corrupt candidate_selector_tuning %s: %s", path, e)
            return CandidateSelectorTuning.default()

    def _persist_candidate_selector_tuning_locked(self) -> None:
        t = self._candidate_selector_tuning
        payload = json.dumps(
            {
                "r_px_expected": t.r_px_expected,
                "w_area": t.w_area,
                "w_dist": t.w_dist,
                "dist_cost_sat_radii": t.dist_cost_sat_radii,
            },
            indent=2,
        )
        self._atomic_write(self._candidate_selector_tuning_path, payload)

    def _load_chain_filter_params_from_disk(self) -> ChainFilterParams:
        path = self._chain_filter_params_path
        if not path.exists():
            return ChainFilterParams()
        try:
            obj = json.loads(path.read_text())
            return ChainFilterParams(
                max_frame_gap=int(obj["max_frame_gap"]),
                max_jump_px=float(obj["max_jump_px"]),
                min_run_len=int(obj["min_run_len"]),
            )
        except Exception as e:
            logger.warning("skip corrupt chain_filter_params %s: %s", path, e)
            return ChainFilterParams()

    def _persist_chain_filter_params_locked(self) -> None:
        p = self._chain_filter_params
        payload = json.dumps(
            {
                "max_frame_gap": p.max_frame_gap,
                "max_jump_px": p.max_jump_px,
                "min_run_len": p.min_run_len,
            },
            indent=2,
        )
        self._atomic_write(self._chain_filter_params_path, payload)

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

    # -------- Device-keyed ChArUco intrinsics --------------------------
    # These are DECOUPLED from CalibrationSnapshot on purpose: intrinsics
    # are a per-sensor hardware property (stable across sessions, rig moves,
    # A/B swaps) while CalibrationSnapshot.homography is a per-role extrinsic
    # that changes every time someone bumps the tripod. Storing them
    # together meant running auto-cal overwrote the good ChArUco K with
    # whatever FOV-approximation K the auto-cal derived that moment.

    def device_intrinsics(self) -> dict[str, DeviceIntrinsics]:
        with self._lock:
            return self._device_intrinsics.snapshot()

    def get_device_intrinsics(self, device_id: str) -> DeviceIntrinsics | None:
        with self._lock:
            return self._device_intrinsics.get(device_id)

    def set_device_intrinsics(self, rec: DeviceIntrinsics) -> None:
        with self._lock:
            self._device_intrinsics.set(rec)

    def delete_device_intrinsics(self, device_id: str) -> bool:
        with self._lock:
            return self._device_intrinsics.delete(device_id)

    def device_intrinsics_for_camera(self, camera_id: str) -> DeviceIntrinsics | None:
        """Lookup the ChArUco intrinsics currently wired to a role via the
        role→device_id mapping last heartbeated by that phone. Returns None
        when either the phone hasn't reported its identifierForVendor yet
        (pre-WS-upgrade client) or when no ChArUco record exists for that
        hardware."""
        with self._lock:
            dev = self._device_registry.get(camera_id)
            if dev is None or dev.device_id is None:
                return None
            return self._device_intrinsics.get(dev.device_id)

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
    ) -> tuple[list[TriangulatedPoint], dict[str, int], FramePayload]:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            # Refresh selector tuning every ingest so dashboard slider
            # changes apply on the next frame without a session reset.
            live.tuning = self._candidate_selector_tuning
            cal_a = self._calibration_store.get("A")
            cal_b = self._calibration_store.get("B")
            dev_a = self._device_registry.get("A")
            dev_b = self._device_registry.get("B")
            session_obj = self._lookup_session_locked(session_id)

        # Each iPhone's `frame.timestamp_s` is its own mach-absolute clock
        # (seconds since device boot), so the two cameras' raw timestamps
        # can be tens of thousands of seconds apart. Hand each device's
        # anchor to `LivePairingSession.ingest` so its 8 ms cross-cam
        # comparison happens on anchor-relative time, while persisted
        # frames keep raw timestamps for downstream consumers.
        anchors = {
            "A": dev_a.sync_anchor_timestamp_s if dev_a is not None else None,
            "B": dev_b.sync_anchor_timestamp_s if dev_b is not None else None,
        }

        # Populate / refresh the per-cam cached pose on the live session
        # so triangulate_live can skip the PitchPayload/scale path and go
        # straight to the ray math. Intrinsics come from the same
        # snapshot the scale path would have seen, so scale factor is
        # unity — no loss of accuracy. Done once per cam per session;
        # re-keyed if the snapshot rotates (dims change).
        from live_pairing import CameraPose as _CameraPose
        from pairing import _camera_pose as _build_pose

        for cam, cal in (("A", cal_a), ("B", cal_b)):
            if cal is None:
                live.camera_poses.pop(cam, None)
                continue
            dims = (cal.image_width_px, cal.image_height_px)
            existing = live.camera_poses.get(cam)
            if existing is not None and existing.image_wh == dims:
                continue
            K, R, _t, C = _build_pose(cal.intrinsics, list(cal.homography))
            live.camera_poses[cam] = _CameraPose(
                K=K, R=R, C=C,
                dist=cal.intrinsics.distortion,
                image_wh=dims,
            )

        def triangulate_live(cam: str, first: FramePayload, second: FramePayload) -> TriangulatedPoint | None:
            left_frame, right_frame = (first, second) if cam == "A" else (second, first)
            pose_a = live.camera_poses.get("A")
            pose_b = live.camera_poses.get("B")
            if pose_a is None or pose_b is None:
                return None
            if dev_a is None or dev_b is None:
                return None
            if dev_a.sync_anchor_timestamp_s is None or dev_b.sync_anchor_timestamp_s is None:
                return None
            from pairing import triangulate_live_pair
            return triangulate_live_pair(
                pose_a, pose_b,
                left_frame, right_frame,
                anchor_a=dev_a.sync_anchor_timestamp_s,
                anchor_b=dev_b.sync_anchor_timestamp_s,
            )

        created = live.ingest(camera_id, frame, triangulate_live, anchors=anchors)
        # The frame stored by live.ingest is the candidate-resolved one
        # (px/py picked by the temporal-prior selector); hand it back so
        # callers (WS handler → live_ray_for_frame) work off the resolved
        # version, not the raw inbound.
        resolved = live.frames_by_cam.get(camera_id, [])[-1] if live.frames_by_cam.get(camera_id) else frame
        return created, dict(live.frame_counts), resolved

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
                self._live_missing_cal.setdefault(session_id, set()).add(camera_id)
                log_key = (session_id, camera_id)
                should_log = log_key not in self._live_missing_cal_logged
                if should_log:
                    self._live_missing_cal_logged.add(log_key)
        if cal is None:
            if should_log:
                logger.warning(
                    "live_ray_for_frame: cam=%s session=%s has no calibration on "
                    "file — live rays dropped until /calibration or /calibration/auto runs",
                    camera_id,
                    session_id,
                )
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
        chain_filter_annotate(merged.frames_live, self._chain_filter_params)
        return self.record(merged)

    def flush_live_frames_for_session(self, session_id: str) -> None:
        """Persist any buffered live frames to disk pitch JSONs for the
        given session. Called at session-end (timeout / Stop) so frames
        survive even if iOS never sent `cycle_end` (WS death, app crash,
        force-kill, network partition).

        For cams that already uploaded /pitch, this is a no-op redirect to
        `persist_live_frames` (which merges live frames into the existing
        pitch JSON). For cams that died before /pitch arrived, synthesise
        a minimal pitch carrying just the live bucket — without it the
        in-memory frames would silently vanish on restart, violating the
        no-silent-fallback rule.

        Idempotent: safe to call repeatedly per session id."""
        with self._lock:
            live = self._live_pairings.get(session_id)
            cam_ids = sorted(live.frames_by_cam.keys()) if live is not None else []
        if not cam_ids:
            return
        for cam_id in cam_ids:
            with self._lock:
                buffered = bool(live.frames_by_cam.get(cam_id))
                existing = self.pitches.get((cam_id, session_id))
            if not buffered:
                continue
            if existing is not None:
                self.persist_live_frames(cam_id, session_id)
                continue
            with self._lock:
                dev = self._device_registry.get(cam_id)
                cal_snap = self._calibration_store.get(cam_id)
            anchor = dev.sync_anchor_timestamp_s if dev is not None else None
            sync_id = dev.time_sync_id if dev is not None else None
            # Mirror the /pitch handler: pitches that hit `record()` MUST
            # carry calibration + sync_id, otherwise the viewer reads back
            # a row with intrinsics=None and renders the misleading
            # "Cam X missing calibration" error even though the operator
            # set everything up correctly. /pitch fills these from
            # state.calibrations() before record(); the synthesise path
            # used to skip that step, leaving a permanently-broken pitch
            # JSON on disk for any cam whose MOV upload didn't land.
            synthetic = PitchPayload(
                camera_id=cam_id,
                session_id=session_id,
                sync_id=sync_id,
                sync_anchor_timestamp_s=anchor,
                video_start_pts_s=anchor if anchor is not None else 0.0,
                paths=[DetectionPath.live.value],
                intrinsics=cal_snap.intrinsics if cal_snap is not None else None,
                homography=list(cal_snap.homography) if cal_snap is not None else None,
                image_width_px=cal_snap.image_width_px if cal_snap is not None else None,
                image_height_px=cal_snap.image_height_px if cal_snap is not None else None,
            )
            logger.info(
                "flush_live_frames: synthesising live-only pitch session=%s cam=%s "
                "anchor=%s sync_id=%s calibrated=%s",
                session_id, cam_id, anchor, sync_id, cal_snap is not None,
            )
            self.record(synthetic)

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
            # Annotate whichever buckets we just touched. Safe to re-run:
            # annotate sorts + rewrites filter_status from scratch each time,
            # so late-arriving frames get a fresh verdict alongside the old.
            chain_filter_annotate(merged.frames_live, self._chain_filter_params)
            chain_filter_annotate(merged.frames_server_post, self._chain_filter_params)
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

    def append_auto_cal_event(
        self,
        camera_id: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._auto_cal_runs.append_event(
                camera_id, message, level=level, data=data
            )

    def live_missing_calibration_for(self, session_id: str) -> list[str]:
        """Cams whose live frames were dropped for missing calibration in this
        session, sorted. Empty list if none / unknown session."""
        with self._lock:
            return sorted(self._live_missing_cal.get(session_id, set()))

    def live_session_summary(self) -> dict[str, Any] | None:
        session = self.session_snapshot()
        if session is None:
            return None
        with self._lock:
            live = self._live_pairings.get(session.id)
            result = self.results.get(session.id)
            missing_cal = sorted(self._live_missing_cal.get(session.id, set()))
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
                "live_missing_calibration": missing_cal,
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
            "live_missing_calibration": missing_cal,
        }

    def claim_time_sync_intent(self) -> _LegacyTimeSyncIntent:
        """Return the currently-live legacy chirp sync run id, minting a
        fresh one when the prior listening window expired."""
        return self._sync.claim_time_sync_intent()

    def heartbeat(
        self,
        camera_id: str,
        time_synced: bool = False,
        time_sync_id: str | None = None,
        sync_anchor_timestamp_s: float | None = None,
        battery_level: float | None = None,
        battery_state: str | None = None,
        device_id: str | None = None,
        device_model: str | None = None,
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
                battery_level=battery_level,
                battery_state=battery_state,
                device_id=device_id,
                device_model=device_model,
            )

    def record_sync_telemetry(self, camera_id: str, telem: dict[str, Any]) -> None:
        self._sync.record_sync_telemetry(camera_id, telem)

    def clear_last_sync_result(self) -> None:
        self._sync.clear_last_sync_result()

    def reset_sync_telemetry_peaks(self, camera_ids: list[str] | None = None) -> None:
        self._sync.reset_sync_telemetry_peaks(camera_ids)

    def sync_telemetry_snapshot(self) -> dict[str, dict[str, Any]]:
        return self._sync.sync_telemetry_snapshot()

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

    def device_id_for(self, camera_id: str) -> str | None:
        """Current role→hardware mapping. Returns the `identifierForVendor`
        the phone most recently reported on hello/heartbeat. Used by the
        ChArUco intrinsics lookup — swapping which phone plays role A vs B
        must not carry the wrong sensor's K + distortion over."""
        with self._lock:
            dev = self._device_registry.get(camera_id)
            return dev.device_id if dev is not None else None

    def _check_session_timeout_locked(self, now: float) -> None:
        """If the current session has exceeded its max_duration_s, transition
        it to ended. Assumes the caller holds `self._lock`."""
        s = self._current_session
        if s is None or s.ended_at is not None:
            return
        if now - s.started_at > s.max_duration_s:
            s.ended_at = now
            self._recently_ended_sessions.appendleft(s)
            self._current_session = None
            self._pending_live_flush_sessions.add(s.id)

    def _lookup_session_locked(self, session_id: str) -> Session | None:
        """Find a Session by id across the current and recently-ended ring.
        Returns None when the id is unknown — caller must hold `self._lock`.
        Searches current first, then recent-most-first across the ring; the
        ring is short so the linear scan is trivial."""
        cur = self._current_session
        if cur is not None and cur.id == session_id:
            return cur
        for ended in self._recently_ended_sessions:
            if ended.id == session_id:
                return ended
        return None

    def _most_recent_ended_session_locked(self) -> Session | None:
        """Most recent ended session, or None. Caller holds `self._lock`."""
        if not self._recently_ended_sessions:
            return None
        return self._recently_ended_sessions[0]

    def _drain_pending_live_flushes_locked(self) -> set[str]:
        """Pop the pending-flush set under the caller's lock. Caller must
        run `flush_live_frames_for_session` on each id OUTSIDE the lock."""
        pending = self._pending_live_flush_sessions
        self._pending_live_flush_sessions = set()
        return pending

    def _run_pending_live_flushes(self, pending: set[str]) -> None:
        for sid in pending:
            try:
                self.flush_live_frames_for_session(sid)
            except Exception:
                logger.exception("flush_live_frames_for_session failed sid=%s", sid)

    def current_session(self) -> Session | None:
        """Current armed session (None if idle). Side-effect: lazily applies
        the timeout so polling callers (status, commands) drive the state
        machine forward without a background task. Also drains any pending
        live-frame flushes triggered by a just-expired timeout."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            pending = self._drain_pending_live_flushes_locked()
            cur = self._current_session
        self._run_pending_live_flushes(pending)
        return cur

    def arm_session(
        self,
        max_duration_s: float = _DEFAULT_SESSION_TIMEOUT_S,
        paths: set[DetectionPath] | None = None,
    ) -> Session:
        """Begin a new armed session. If one is already armed, return it
        unchanged (idempotent so dashboard double-clicks don't double-arm).
        Snapshots the current global path-set + tracking exposure cap so a
        late dashboard toggle can't disturb the in-flight recording."""
        now = self._time_fn()
        with self._lock:
            self._check_session_timeout_locked(now)
            pending = self._drain_pending_live_flushes_locked()
            if self._current_session is not None:
                cur = self._current_session
            else:
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
                self._sync.clear_time_sync_intent_locked()
                cur = session
        self._run_pending_live_flushes(pending)
        return cur

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

    def shape_gate(self) -> ShapeGate:
        with self._lock:
            return self._shape_gate

    def candidate_selector_tuning(self) -> CandidateSelectorTuning:
        with self._lock:
            return self._candidate_selector_tuning

    def set_candidate_selector_tuning(
        self, tuning: CandidateSelectorTuning
    ) -> CandidateSelectorTuning:
        with self._lock:
            self._candidate_selector_tuning = tuning
            self._persist_candidate_selector_tuning_locked()
            return self._candidate_selector_tuning

    def chain_filter_params(self) -> ChainFilterParams:
        with self._lock:
            return self._chain_filter_params

    def set_chain_filter_params(self, params: ChainFilterParams) -> ChainFilterParams:
        with self._lock:
            self._chain_filter_params = params
            self._persist_chain_filter_params_locked()
            return self._chain_filter_params

    def set_shape_gate(self, shape_gate: ShapeGate) -> ShapeGate:
        with self._lock:
            self._shape_gate = shape_gate
            self._persist_shape_gate_locked()
            return self._shape_gate

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

    def detection_bg_subtraction_enabled(self) -> bool:
        with self._lock:
            return self._runtime_settings.detection_bg_subtraction_enabled

    def set_detection_bg_subtraction_enabled(self, enabled: bool) -> bool:
        with self._lock:
            return self._runtime_settings.set_detection_bg_subtraction_enabled(enabled)

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
        normal lifecycle event, not an abort. Triggers a live-frames flush
        so any in-memory buffer reaches disk even if iOS never sent
        cycle_end (e.g. app crash, force-kill)."""
        now = self._time_fn()
        with self._lock:
            s = self._current_session
            if s is None or s.ended_at is not None:
                return None
            s.ended_at = now
            self._recently_ended_sessions.appendleft(s)
            self._current_session = None
            self._pending_live_flush_sessions.add(s.id)
            pending = self._drain_pending_live_flushes_locked()
        self._run_pending_live_flushes(pending)
        return s

    def log_sync_event(
        self, source: str, event: str, detail: dict[str, Any] | None = None
    ) -> None:
        self._sync.log_sync_event(source, event, detail)

    def sync_logs(self, limit: int = 200) -> list[SyncLogEntry]:
        return self._sync.sync_logs(limit)

    def _check_sync_timeout_locked(self, now: float) -> None:
        """Delegate to the sync coordinator. Kept on State as a back-compat
        entry point for code paths that acquire the shared lock and then
        want to advance the sync-run state machine (e.g. `commands_for_devices`,
        `trigger_sync_command`). Caller must hold `self._lock`."""
        self._sync._check_sync_timeout_locked(now)

    def current_sync(self) -> SyncRun | None:
        """Snapshot of the in-progress sync run (None when idle). Lazily
        applies the timeout on read, mirroring `current_session()`."""
        return self._sync.current_sync()

    def last_sync_result(self) -> SyncResult | None:
        """Most recently solved sync result, or None if no sync has ever
        succeeded on this server instance."""
        return self._sync.last_sync_result()

    def sync_cooldown_remaining_s(self) -> float:
        """Seconds remaining on the post-sync cooldown. 0 when ready."""
        return self._sync.sync_cooldown_remaining_s()

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
            dispatched = self._sync.dispatch_sync_commands_locked(now, targets)
            # Force-drop any stale anchor on the targeted cams. Without
            # this step the phone keeps reporting its previous successful
            # anchor on every heartbeat until (and unless) the new chirp
            # is detected, so a cam that misses the chirp silently passes
            # readiness with an old id while its peer locked onto the new
            # one. Cleared cams re-flip to time_synced=True only after
            # iOS heartbeats with the new id (which `_gated_time_synced`
            # then matches against the freshly-set expected id).
            for cam in dispatched:
                self._device_registry.clear_time_sync(cam)
            return dispatched

    def consume_sync_command(self, camera_id: str) -> tuple[str | None, str | None]:
        """Atomically pop + return a pending time-sync command for the
        named camera, or `(None, None)` when there's nothing queued."""
        return self._sync.consume_sync_command(camera_id)

    def pending_sync_commands(self) -> dict[str, str]:
        return self._sync.pending_sync_commands()

    def set_expected_sync_id(self, camera_ids: list[str], sync_id: str) -> None:
        self._sync.set_expected_sync_id(camera_ids, sync_id)

    def expected_sync_id_snapshot(self) -> dict[str, str]:
        return self._sync.expected_sync_id_snapshot()

    def pending_sync_command_ids(self) -> dict[str, str]:
        return self._sync.pending_sync_command_ids()

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

    # server_post lifecycle moved to SessionProcessingState — call
    # `state._processing.{mark_server_post_queued, start_server_post_job,
    # should_cancel_server_post_job, finish_server_post_job, record_error,
    # clear_error, errors_for, cancel_processing, run_server_post,
    # session_summary, session_candidates, find_video_for}` directly from
    # routes/. State no longer proxies these.

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
            return self._sync.start_sync_locked(
                now,
                online_ids,
                session_armed=current is not None,
            )

    def record_sync_report(
        self, report: SyncReport
    ) -> tuple[SyncRun | None, SyncResult | None, str | None]:
        """Attach a phone's matched-filter report to the current run."""
        return self._sync.record_sync_report(report)

    def clear_last_ended_session(self) -> bool:
        """Drop the recently-ended sessions ring so the dashboard's
        session card goes blank again. No-op (returns False) when a
        session is currently armed or there's nothing to clear — the
        ring is strictly a dashboard-idle-state concern."""
        with self._lock:
            if self._current_session is not None and self._current_session.ended_at is None:
                return False
            if not self._recently_ended_sessions:
                return False
            self._recently_ended_sessions.clear()
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
            return self._most_recent_ended_session_locked()

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
            self._sync._check_sync_timeout_locked(now)
            sync_run = self._sync._current_sync
            last_ended = self._most_recent_ended_session_locked()
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
        the live and any `.tmp` siblings), and clears the entry from
        `_pitches`, `_results`, and the `_recently_ended_sessions` ring if
        it matches."""
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
            self._live_missing_cal.pop(session_id, None)
            self._live_missing_cal_logged = {
                key for key in self._live_missing_cal_logged if key[0] != session_id
            }
            if any(s.id == session_id for s in self._recently_ended_sessions):
                # `deque` has no `remove_if`; rebuild preserving order/maxlen.
                kept = [s for s in self._recently_ended_sessions if s.id != session_id]
                self._recently_ended_sessions.clear()
                self._recently_ended_sessions.extend(kept)
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
            # Includes raw + any in-flight `.tmp` sibling.
            path.unlink(missing_ok=True)
            removed_any = True
        return removed_any

    def reset(self, purge_disk: bool = False) -> None:
        with self._lock:
            self.pitches.clear()
            self.results.clear()
            self._device_registry.clear()
            self._current_session = None
            self._recently_ended_sessions.clear()
            self._processing.clear()
            self._live_missing_cal.clear()
            self._live_missing_cal_logged.clear()
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
