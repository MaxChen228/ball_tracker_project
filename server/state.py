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
    SyncRun,
    TrackingExposureCapMode,
    TriangulatedPoint,
    _DEFAULT_SESSION_TIMEOUT_S,
    _DEFAULT_PATHS,
)
from detection import HSVRange, ShapeGate
from detection_config import (
    DetectionConfig,
    load_or_migrate as _detection_config_load_or_migrate,
    modified_fields as _detection_config_modified_fields,
    persist as _detection_config_persist,
)
from pairing_tuning import PairingTuning
import presets as _presets
from preview import PreviewBuffer
from marker_registry import MarkerRegistryDB
from live_pairing import LivePairingSession
from reconstruct import Ray, rays_for_frame
from state_runtime import RuntimeSettingsStore, SyncParams
from state_calibration import (
    AutoCalibrationRun as _AutoCalibrationRun,
    AutoCalibrationRunStore,
    CalibrationFrameBuffer,
    LastSolveStore,
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
    TimeSyncIntent,
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

# Entries in the device registry older than this get pruned on every
# heartbeat write. Legitimate phones beat at 1 Hz so anything beyond 60 s
# is not coming back; pruning on write is what keeps a malformed/spoofed
# client from ballooning the registry forever without needing a
# background task.
_DEVICE_GC_AFTER_S = 60.0

# Hard cap on the device registry size. Even with GC-on-write, a burst
# of distinct camera_ids within the GC window could push memory up. Cap
# at 64 — more than enough for any plausible rig (we run 2-phone stereo)
# while still bounding adversarial input.
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
    `RLock` first.

    Exceptions: `_marker_registry` and `_preview` carry their own internal
    locks and are NOT serialised by `self._lock`. `LivePairingSession` (in
    `_live_pairings`) likewise owns its own mutex covering buffers/frame
    counts/triangulated/etc.; lookups against `_live_pairings` itself still
    take `self._lock`, but mutations on the returned session go through
    its own thread-safe accessors."""

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
        # Phase 2 of unified-config redesign: the detection pair
        # (HSV + shape gate) lives in a single `detection_config.json`.
        # Boot reads/migrates from the legacy three-file layout
        # (hsv_range.json + shape_gate.json + candidate_selector_tuning.json)
        # on first start, then deletes the legacy files. The legacy paths
        # are no longer kept as instance attrs — anything that needs them
        # goes through `_detection_config` in memory or the unified file
        # on disk. Selector cost weights were retired post-PR93 and are
        # now `_W_ASPECT` / `_W_FILL` module constants in
        # `candidate_selector` rather than a runtime tunable.
        self._detection_config_path = data_dir / "detection_config.json"
        self._pairing_tuning_path = data_dir / "pairing_tuning.json"
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
        # Preset library is disk-backed (`data/presets/<name>.json`).
        # Seed built-in tennis / blue_ball files if missing — must run
        # before `_detection_config_load_or_migrate` because that loader
        # falls through to `tennis` as the boot default and reads it
        # from disk.
        _presets.seed_builtins(data_dir, atomic_write=self._atomic_write)
        self._detection_config: DetectionConfig = _detection_config_load_or_migrate(
            data_dir,
            atomic_write=self._atomic_write,
        )
        self._pairing_tuning = self._load_pairing_tuning_from_disk()
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
        # Per-cam last-successful-solve record (reproj / markers / FOV /
        # delta-pose). Single-shot calibration: each [Recalibrate] press
        # is one frame; success overwrites. Reset only by reset_rig().
        self._last_solves = LastSolveStore()
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
        # Per (session_id, camera_id) dedupe set for the missing-sync-anchor
        # info log. Mirrors `_live_missing_cal_logged` so each cam/session
        # logs exactly once until the next reset/clear.
        self._live_missing_sync_logged: set[tuple[str, str]] = set()
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

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def video_dir(self) -> Path:
        return self._video_dir

    @property
    def processing(self) -> "SessionProcessingState":
        """Public accessor for the session-processing coordinator.

        Exposed as a `@property` (not a method) because it returns the
        SessionProcessingState facade — a stable reference whose own
        threadsafety/locking lives inside the coordinator, so callers
        treat it as a fixed object: `state.processing.foo(...)`.

        Routes call this directly for server_post job lifecycle, error
        recording, and trash queries — no proxy methods on State for
        those, keep this read-only attribute access. Compatible with the
        existing facade: SessionProcessingState owns its own internal
        bookkeeping; state-lock interactions still go through the
        coordinator's `attach`-bound reference, so concurrent calls
        respect the same `_lock` invariants the legacy `_processing`
        path relied on."""
        return self._processing

    def now(self) -> float:
        """Public accessor for the injectable wall-clock used across
        State (`_time_fn`).

        Exposed as a **method** (not a `@property`) because each call
        must return a *fresh* float — wall-clock advances every
        invocation. Always call as `state.now()`; writing
        `if state.now > x:` would compare a bound-method object to a
        float and silently always be truthy. Routes / helpers should
        call this rather than poking `state._time_fn` directly so test
        fixtures that override the clock keep flowing through one entry
        point."""
        return self._time_fn()

    def save_clip(
        self, camera_id: str, session_id: str, data: bytes, ext: str = "mov"
    ) -> Path:
        """Persist a session's H.264 clip to disk. Writes atomically so a
        partial transfer cannot leave a corrupt file visible to downstream
        tools. Overwrites any existing clip for (camera_id, session_id).

        Concurrency: `secrets.token_hex(4)` gives each writer a unique tmp
        filename, so the bytes write + rename can run outside `self._lock`.
        Holding the lock across a 50-100 MB write would stall heartbeats
        for the duration of the disk I/O."""
        safe_ext = (ext or "mov").lstrip(".").lower()
        if not safe_ext or "/" in safe_ext or "\\" in safe_ext:
            safe_ext = "mov"
        path = self._video_dir / f"session_{session_id}_{camera_id}.{safe_ext}"
        tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    def _pitch_path(self, camera_id: str, session_id: str) -> Path:
        return self._pitch_dir / f"session_{session_id}_{camera_id}.json"

    def _result_path(self, session_id: str) -> Path:
        return self._result_dir / f"session_{session_id}.json"

    def _load_from_disk(self) -> None:
        backfill: list[tuple[Path, tuple[str, str]]] = []
        for path in sorted(self._pitch_dir.glob("session_*.json")):
            try:
                obj = json.loads(path.read_text())
                pitch = PitchPayload.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt pitch file %s: %s", path.name, e)
                continue
            # Backfill `created_at` for legacy pitches written before the
            # field shipped: prefer the file's mtime (real upload moment) so
            # historical sessions group under the day they actually happened
            # instead of being smashed into "today" on first restart.
            if pitch.created_at is None:
                try:
                    pitch.created_at = path.stat().st_mtime
                except OSError:
                    pitch.created_at = self._time_fn()
                backfill.append((path, (pitch.camera_id, pitch.session_id)))
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch

        for path, key in backfill:
            pitch = self.pitches.get(key)
            if pitch is None:
                continue
            try:
                self._atomic_write(path, pitch.model_dump_json())
            except OSError as e:
                logger.warning("created_at backfill write failed %s: %s", path, e)

        seen_sessions = {sid for _, sid in self.pitches.keys()}
        for sid in sorted(seen_sessions):
            self.results[sid] = session_results.rebuild_result_for_session(self, sid)

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

    def _persist_detection_config_locked(self) -> None:
        """Caller owns `self._lock`. Single-file atomic write replacing
        the previous three independent persisters; ensures the triple
        cannot half-update on a partial-failure path."""
        _detection_config_persist(
            self._detection_config,
            self._data_dir,
            atomic_write=self._atomic_write,
        )

    def _load_pairing_tuning_from_disk(self) -> PairingTuning:
        path = self._pairing_tuning_path
        if not path.exists():
            return PairingTuning.default()
        try:
            obj = json.loads(path.read_text())
            d = PairingTuning.default()
            return PairingTuning(
                cost_threshold=float(obj.get("cost_threshold", d.cost_threshold)),
                gap_threshold_m=float(obj.get("gap_threshold_m", d.gap_threshold_m)),
            )
        except Exception as e:
            logger.warning("skip corrupt pairing_tuning %s: %s", path, e)
            return PairingTuning.default()

    def _persist_pairing_tuning_locked(self) -> None:
        t = self._pairing_tuning
        payload = json.dumps(
            {
                "cost_threshold": t.cost_threshold,
                "gap_threshold_m": t.gap_threshold_m,
            },
            indent=2,
        )
        self._atomic_write(self._pairing_tuning_path, payload)

    def _calibration_path(self, camera_id: str) -> Path:
        return self._calibration_store.path(camera_id)

    def set_calibration(self, snapshot: CalibrationSnapshot) -> None:
        """Record (or overwrite) one camera's calibration and persist it
        atomically so the dashboard survives a restart. Last write wins.

        Validates K/H/dims self-consistency before storing — an earlier bug
        mixed 1080p intrinsics with 480p homography which silently produced
        garbage extrinsics downstream. Catching it at the boundary saves
        hours of "why is Cam A at Z=0.66m" debugging.

        Invalidates any cached `CameraPose` on active live sessions so the
        next `ingest_live_frame` rebuilds K/R/C from the new snapshot. The
        cached pose's `image_wh` would otherwise still match (canonical
        1920×1080 doesn't change across recals) and the live triangulation
        would silently keep using the stale calibration."""
        with self._lock:
            self._calibration_store.set(snapshot)
            for live in self._live_pairings.values():
                live.update_camera_pose(snapshot.camera_id, None)

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

    def ingest_live_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> tuple[list[TriangulatedPoint], dict[str, int], FramePayload]:
        with self._lock:
            live = self._live_pairings.setdefault(session_id, LivePairingSession(session_id))
            # Freeze pairing tuning + hsv/shape on the FIRST real frame
            # seen by this LivePairingSession. Mirrors the cd87995
            # PairingTuning-on-SessionResult contract: a session's cost
            # basis is decided at arm time and cannot shift mid-cycle.
            # Dashboard slider edits during an active session land on
            # the NEXT session.
            #
            # Idempotent: arm_session pre-creates LivePairingSession (so a
            # `session_id not in dict` check would never fire on the dashboard
            # path), and tests that bypass arm hit the setdefault above. The
            # `hsv_range_used is None` freshness check covers both: stamp
            # exactly once on first ingest regardless of who created the
            # LivePairingSession. Subsequent ingests see the fields already
            # set and skip the block.
            if live.hsv_range_used is None:
                live.pairing_tuning = self._pairing_tuning
                live.hsv_range_used = self._detection_config.hsv
                live.shape_gate_used = self._detection_config.shape_gate
            cal_a = self._calibration_store.get("A")
            cal_b = self._calibration_store.get("B")
            dev_a = self._device_registry.get("A")
            dev_b = self._device_registry.get("B")
            session_obj = self._lookup_session_locked(session_id)
            # Snapshot runtime capture height under the same lock that
            # protects every other runtime-settings read in this class
            # (the State docstring at L128-135 names this invariant).
            # Used below to scale snap K + H to actual live frame dims.
            live_h = self._runtime_settings.capture_height_px

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
        # straight to the ray math.
        #
        # The snapshot is stored at canonical 1920×1080 by auto-cal, but
        # the iPhone may stream live BGRA frames at either 1080p or 720p
        # depending on operator's `capture_height_px` setting. Pre-scale
        # K + H to the actual live frame grid here so triangulate_live_pair
        # consumes a K/H pair that matches frame.px/frame.py basis.
        # Without this, 720p standby silently produces 1.5× off live rays.
        from live_pairing import CameraPose as _CameraPose
        from pairing import _camera_pose as _build_pose, _scale_homography, _scale_intrinsics

        live_w = (live_h * 16) // 9
        live_dims = (live_w, live_h)

        for cam, cal in (("A", cal_a), ("B", cal_b)):
            if cal is None:
                live.update_camera_pose(cam, None)
                continue
            existing = live.camera_pose(cam)
            if existing is not None and existing.image_wh == live_dims:
                continue
            snap_dims = (cal.image_width_px, cal.image_height_px)
            if snap_dims != live_dims:
                sx = live_dims[0] / snap_dims[0]
                sy = live_dims[1] / snap_dims[1]
                live_intr = _scale_intrinsics(cal.intrinsics, sx, sy)
                live_h_flat = _scale_homography(list(cal.homography), sx, sy)
            else:
                live_intr = cal.intrinsics
                live_h_flat = list(cal.homography)
            K, R, _t, C = _build_pose(live_intr, live_h_flat)
            live.update_camera_pose(cam, _CameraPose(
                K=K, R=R, C=C,
                dist=live_intr.distortion,
                image_wh=live_dims,
            ))

        def triangulate_live(frame_a: FramePayload, frame_b: FramePayload) -> list[TriangulatedPoint]:
            # frame_a / frame_b are pre-canonicalized A-first by ingest();
            # the closure name + argument order is the contract. No
            # cam-direction flipping needed here.
            pose_a = live.camera_pose("A")
            pose_b = live.camera_pose("B")
            if pose_a is None or pose_b is None:
                return []
            if dev_a is None or dev_b is None:
                return []
            if dev_a.sync_anchor_timestamp_s is None or dev_b.sync_anchor_timestamp_s is None:
                return []
            from pairing import triangulate_live_pair
            return triangulate_live_pair(
                pose_a, pose_b,
                frame_a, frame_b,
                anchor_a=dev_a.sync_anchor_timestamp_s,
                anchor_b=dev_b.sync_anchor_timestamp_s,
                tuning=live.pairing_tuning,
            )

        created = live.ingest(camera_id, frame, triangulate_live, anchors=anchors)
        # The frame stored by live.ingest is the candidate-resolved one
        # (px/py picked by the shape-prior selector); hand it back so
        # callers (WS handler → live_rays_for_frame) work off the resolved
        # version, not the raw inbound.
        resolved = live.latest_frame_for(camera_id)
        if resolved is None:
            raise RuntimeError(
                f"ingest_live_frame: live buffer empty after ingest cam={camera_id} sid={session_id}"
            )
        return created, live.frame_counts_snapshot(), resolved

    def live_rays_for_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> list[Ray]:
        """Project this frame's candidates into world space for dashboard rays.

        Returns one ray per shape-gate-passing candidate (fan-out parity
        with the post-pitch viewer scene). Empty list when no calibration
        on file, no anchor reachable, or `frame.ball_detected` is False.

        Stereo live points still require A/B pairing and a shared time
        anchor. A monocular ray only needs that camera's calibration;
        if the phone has no sync anchor, returns [] (mirrors the
        no-calibration path; emits a one-time info log per cam/session
        for operator visibility).
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
            sync_log_key = (session_id, camera_id)
            should_log_sync = (
                cal is not None
                and (dev is None or dev.sync_anchor_timestamp_s is None)
                and sync_log_key not in self._live_missing_sync_logged
            )
            if should_log_sync:
                self._live_missing_sync_logged.add(sync_log_key)
        if cal is None:
            if should_log:
                logger.warning(
                    "live_rays_for_frame: cam=%s session=%s has no calibration on "
                    "file — live rays dropped until /calibration or /calibration/auto runs",
                    camera_id,
                    session_id,
                )
            return []
        # Silent fallback removed: the previous code synthesised an
        # anchor from `frame.timestamp_s - frame_index/240` when the
        # device had no sync anchor on file. That produced rays whose
        # `t_rel_s` looked plausible but was actually decoupled from
        # mutual-sync clock — they would rendr in the dashboard 3D scene
        # alongside genuinely time-aligned rays and the operator had no
        # way to tell. Mirror the no-calibration path: drop silently
        # (one-time info log per cam/session) instead of fabricating a clock.
        if dev is None or dev.sync_anchor_timestamp_s is None:
            if should_log_sync:
                logger.info(
                    "live_rays_for_frame: cam=%s session=%s has no sync anchor — "
                    "live rays dropped until chirp/mutual sync completes",
                    camera_id,
                    session_id,
                )
            return []
        anchor = dev.sync_anchor_timestamp_s
        return rays_for_frame(
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
        if live is None:
            return
        cam_ids = live.cameras_with_frames()
        if not cam_ids:
            return
        for cam_id in sorted(cam_ids):
            with self._lock:
                existing = self.pitches.get((cam_id, session_id))
            if existing is not None:
                self.persist_live_frames(cam_id, session_id)
                continue
            with self._lock:
                dev = self._device_registry.get(cam_id)
                cal_snap = self._calibration_store.get(cam_id)
            anchor = dev.sync_anchor_timestamp_s if dev is not None else None
            sync_id = dev.time_sync_id if dev is not None else None
            if anchor is None:
                # No sync anchor → synthesising a pitch with video_start_pts_s=0
                # would peg t_rel_s onto an absolute mach-clock that downstream
                # reconstruct.py / viewer expect to start at 0. Drop the buffer
                # rather than write a forever-broken JSON.
                logger.warning(
                    "flush_live_frames: skipping synthesise session=%s cam=%s — "
                    "no sync anchor (live frames discarded)",
                    session_id, cam_id,
                )
                continue
            # Mirror the /pitch handler: pitches that hit `record()` MUST
            # carry calibration + sync_id, otherwise the viewer reads back
            # a row with intrinsics=None and renders the misleading
            # "Cam X missing calibration" error even though the operator
            # set everything up correctly.
            synthetic = PitchPayload(
                camera_id=cam_id,
                session_id=session_id,
                sync_id=sync_id,
                sync_anchor_timestamp_s=anchor,
                video_start_pts_s=anchor,
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
        NumPy triangulation happen OUTSIDE `self._lock` so WS heartbeats
        and dashboard `/status` reads don't block on a slow disk or a
        millisecond-scale triangulation run.

        Race note 1 — concurrent A+B record(): two simultaneous A+B
        uploads for the same session can each observe the other inside
        their own critical section and both trigger triangulation.
        That's redundant CPU but not incorrect — both computations take
        the same (a, b) snapshot and deterministically yield the same
        points; last-writer-wins on `self.results[sid]` and on the
        result JSON file (both atomic).

        Race note 2 — record vs delete_session: CS1 and CS2 are
        deliberately separated so disk I/O + triangulation don't block
        WS heartbeats. A concurrent `delete_session` squeezed between
        CS1 and CS2 would otherwise resurrect the just-deleted session
        via the result publish (both the `_atomic_write` of the result
        JSON and the CS2 republish). The guards below re-check
        `(camera_id, session_id) in self.pitches` before each write,
        collapsing the resurrection window from "duration of
        triangulation + disk I/O" (milliseconds) to "between read and
        write" (microseconds). Fully eliminating it would need a
        generation counter on `(camera_id, session_id)`; not worth the
        complexity for a personal LAN tool."""
        pitch_path = self._pitch_path(pitch.camera_id, pitch.session_id)
        normalized_paths = session_results.normalize_paths(pitch.paths)
        if not normalized_paths:
            normalized_paths = session_results.paths_for_pitch(self, pitch)
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
                # Preserve the previous run's wall-clock when the
                # incoming pitch doesn't carry one (e.g., live-frames
                # merge after server_post had already completed).
                if merged.server_post_ran_at is None and existing.server_post_ran_at is not None:
                    merged.server_post_ran_at = existing.server_post_ran_at
                # Preserve the original creation stamp across re-records
                # (server_post backfill, live merge). If the existing record
                # lacked one (legacy / synthetic before this field shipped),
                # fall through and stamp now.
                if existing.created_at is not None:
                    merged.created_at = existing.created_at
            if merged.created_at is None:
                merged.created_at = self._time_fn()
            if not merged.frames_live and live_frames:
                merged.frames_live = list(live_frames)
            pitch = merged
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch
            # Drive the session state machine forward — any upload arriving
            # while armed disarms the session (one-shot pattern). The other
            # camera, if it was also recording, gets "disarm" on the next
            # WS settings push and cleans up.
            self._register_upload_in_session_locked(pitch)

        # --- Outside the lock: write pitch JSON. Filename is unique per
        # (camera, session) and each pitch uses its own tmp file, so two
        # concurrent calls here cannot collide. ---
        self._atomic_write(pitch_path, pitch.model_dump_json())

        # --- Outside the lock: build the result + triangulate if paired. ---
        result = session_results.rebuild_result_for_session(self, pitch.session_id)

        # --- Guard: bail before disk write if delete_session ran between
        # CS1 and now. See "Race note 2" in the docstring. ---
        with self._lock:
            still_present = (pitch.camera_id, pitch.session_id) in self.pitches
        if not still_present:
            logger.info(
                "record: session %s deleted during write — discarding result publish",
                pitch.session_id,
            )
            return result

        # --- Outside the lock: persist the result JSON. ---
        self._atomic_write(
            self._result_path(pitch.session_id),
            result.model_dump_json(),
        )

        # --- Critical section 2: publish the result into the in-memory map.
        # Re-check once more — delete_session could have raced into the
        # window between the guard above and this line. ---
        with self._lock:
            if (pitch.camera_id, pitch.session_id) not in self.pitches:
                logger.info(
                    "record: session %s deleted between disk write and CS2 — discarding republish",
                    pitch.session_id,
                )
                return result
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
            cached = self.results.get(session_id)
            has_live = session_id in self._live_pairings
        if cached is not None:
            return cached
        # Per-frame rebuild was retired in the WS frame loop (paid 50-200×
        # disk writes per pitch for no UI gain). The viewer / dashboard
        # still expects a partial result mid-stream; build one on demand
        # against the current live snapshot. Returns None for unknown
        # sessions exactly like the old behaviour.
        if not has_live:
            return None
        return session_results.rebuild_result_for_session(self, session_id)

    def store_result(self, result: SessionResult) -> None:
        # Same delete-during-write race as record() (see "Race note 2"
        # there). Bail if the session has been purged from EVERY
        # in-memory home it could live in: pitches, results, and the
        # live pairing map. Live-only WS sessions never enter `pitches`
        # until persist_live_frames flushes, so we must include
        # `_live_pairings` — otherwise the very first publish from the
        # WS frame loop would incorrectly trip the guard. (The
        # `_live_pairings` lookup here is just a key existence check
        # under self._lock; mutating the LivePairingSession itself
        # still goes through its own internal mutex, see the State
        # docstring.) `delete_session` purges all four in the same
        # critical section, so a True read here means the session was
        # alive at the moment we checked.
        sid = result.session_id
        with self._lock:
            known = (
                any(s == sid for _, s in self.pitches)
                or sid in self.results
                or sid in self._live_pairings
            )
        if not known:
            logger.info("store_result: skipping unknown session %s (deleted?)", sid)
            return
        self._atomic_write(self._result_path(sid), result.model_dump_json())
        with self._lock:
            still_known = (
                any(s == sid for _, s in self.pitches)
                or sid in self.results
                or sid in self._live_pairings
            )
            if not still_known:
                logger.info(
                    "store_result: session %s deleted between disk write and republish",
                    sid,
                )
                return
            self.results[sid] = result

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

    def store_calibration_frame(
        self,
        camera_id: str,
        jpeg_bytes: bytes,
        *,
        photo_fov_deg: float | None = None,
        video_fov_deg: float | None = None,
    ) -> None:
        """Phone pushed a calibration frame; stash it and clear the flag.
        FOVs (when iOS reports them) carry the photo format's basis +
        the live video format's basis so the auto-cal route can solve
        in one and store in the other."""
        with self._lock:
            self._calibration_frames.store(
                camera_id, jpeg_bytes,
                photo_fov_deg=photo_fov_deg,
                video_fov_deg=video_fov_deg,
            )

    def consume_calibration_frame(
        self, camera_id: str, max_age_s: float = _CALIBRATION_FRAME_TTL_S,
    ):
        """Atomic pop-if-fresh. Returns the CalibrationFramePayload (jpeg
        + received_at + FOV pair) or None if no frame cached or stale."""
        with self._lock:
            return self._calibration_frames.consume(camera_id, max_age_s=max_age_s)

    # --- Last-solve record -----------------------------------------------

    def record_calibration_last_solve(self, camera_id: str, record) -> None:
        """Persist the metadata of a successful solve so the dashboard
        can surface "last calibrated N min ago" continuously."""
        with self._lock:
            self._last_solves.record(camera_id, record)

    def calibration_last_solve_summary(self, camera_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._last_solves.summary(camera_id)

    def all_calibration_last_solves(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._last_solves.all_summaries()

    def reset_rig(self) -> dict[str, int]:
        """Wipe all calibrations + extended marker registry + last-solve
        records + invalidate cached live camera poses. Returns counts
        of what was removed. Used by dashboard 'Reset rig' for full rig
        re-setup (board moved, cams reseated).

        Intentionally preserved: per-device ChArUco intrinsics
        (sensor-physical, survive rig moves) and in-memory
        AutoCalibrationRunStore history (harmless, restart-volatile).
        Operator deletes individual ChArUco entries via the existing
        /calibration/intrinsics/{device_id} DELETE route if needed."""
        with self._lock:
            cal_count = self._calibration_store.clear()
            marker_count = self._marker_registry.clear()
            ls_count = self._last_solves.clear_all()
            for live in self._live_pairings.values():
                for cam in ("A", "B"):
                    live.update_camera_pose(cam, None)
            return {
                "calibrations_removed": cal_count,
                "extended_markers_removed": marker_count,
                "last_solves_cleared": ls_count,
            }

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
            "frame_counts": live.frame_counts_snapshot(),
            "point_count": live.triangulated_count(),
            "paths_completed": paths_completed,
            "completed_cameras": live.completed_cameras_snapshot(),
            "abort_reasons": live.abort_reasons_snapshot(),
            "live_missing_calibration": missing_cal,
        }

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
                # Pre-create LivePairingSession AND stamp the frozen
                # detection config NOW, at arm time — not at first ingest.
                # The R7-fixed contract: a slider drag between arm and
                # first WS frame must NOT poison the session. ingest_live_frame
                # below has a `live.hsv_range_used is None` guard so the
                # test-bypass-arm path (build LivePairingSession inline,
                # call ingest directly) still gets stamped on first frame.
                live = LivePairingSession(session.id)
                live.pairing_tuning = self._pairing_tuning
                live.hsv_range_used = self._detection_config.hsv
                live.shape_gate_used = self._detection_config.shape_gate
                self._live_pairings[session.id] = live
                self._current_session = session
                self._sync.clear_time_sync_intent_locked()
                cur = session
        self._run_pending_live_flushes(pending)
        return cur

    def default_paths(self) -> set[DetectionPath]:
        with self._lock:
            return set(self._runtime_settings.default_paths)

    def detection_config(self) -> DetectionConfig:
        """Atomic snapshot of the full detection-config pair + preset
        identity. Phase 2 of the unified-config redesign — earlier
        callers reached for hsv_range() / shape_gate() independently
        and risked seeing values from different write epochs if the
        setters were called interleaved by another thread."""
        with self._lock:
            return self._detection_config

    def set_detection_config(
        self,
        cfg: DetectionConfig,
    ) -> DetectionConfig:
        """Atomic single-write replacement for the legacy three setters.

        Stamps `last_applied_at` from `self._time_fn()` under the lock —
        callers must NOT pre-fill it (any value supplied is overwritten),
        so the persisted `last_applied_at` always equals the actual lock-
        held write epoch. This keeps cross-module callers (`routes/`)
        out of `state._time_fn`, and means a "now()" stamp can never
        drift from when the disk row was actually written.
        """
        with self._lock:
            self._detection_config = cfg.with_(last_applied_at=self._time_fn())
            self._persist_detection_config_locked()
            return self._detection_config

    def hsv_range(self) -> HSVRange:
        with self._lock:
            return self._detection_config.hsv

    def set_hsv_range(self, hsv_range: HSVRange) -> HSVRange:
        """Single-section convenience setter — used by tests and any
        legacy caller that wants to mutate just HSV. Editing one sub-
        knob clears preset binding (the resulting config no longer
        matches any named preset by definition). HTTP exposure of this
        retired in phase 3; the dashboard goes through unified
        `POST /detection/config` only."""
        with self._lock:
            self._detection_config = self._detection_config.with_(
                hsv=hsv_range,
                preset=None,
                last_applied_at=self._time_fn(),
            )
            self._persist_detection_config_locked()
            return self._detection_config.hsv

    def shape_gate(self) -> ShapeGate:
        with self._lock:
            return self._detection_config.shape_gate

    def pairing_tuning(self) -> PairingTuning:
        with self._lock:
            return self._pairing_tuning

    # ---- preset library accessors --------------------------------------
    # Thin pass-throughs to the disk-backed `presets` module. Endpoint
    # handlers go through these so callers don't reach into `_data_dir`
    # directly. No locking — the preset filesystem is independent of
    # the `_lock`-guarded in-memory state, and `_atomic_write` is
    # collision-safe by itself (unique tmp suffix per call).

    def list_presets(self):
        """All presets sorted by slug. Reads disk on every call (the
        dashboard render path is the dominant caller; ms-scale)."""
        return _presets.list_presets(self._data_dir)

    def load_preset(self, name: str):
        """Single preset by slug. Raises `KeyError(name)` if the file
        is missing on disk — endpoint handlers translate to 404; the
        dashboard renderer's `identity-deleted` branch handles the
        dangling-reference case before this would be called."""
        return _presets.load_preset(self._data_dir, name)

    def preset_exists(self, name: str) -> bool:
        return _presets.preset_exists(self._data_dir, name)

    def save_preset(self, preset) -> None:
        _presets.save_preset(
            self._data_dir, preset, atomic_write=self._atomic_write
        )

    def delete_preset(self, name: str) -> None:
        """Unlink the preset file. Raises `KeyError(name)` if absent.
        No cascade on the live `detection_config.preset` reference —
        the dashboard renderer surfaces the dangling state explicitly
        and the next `set_detection_config` clears it."""
        _presets.delete_preset(self._data_dir, name)

    def modified_fields_for(self, cfg: DetectionConfig) -> list[str]:
        return _detection_config_modified_fields(cfg, data_dir=self._data_dir)

    def set_pairing_tuning(self, tuning: PairingTuning) -> PairingTuning:
        with self._lock:
            self._pairing_tuning = tuning
            self._persist_pairing_tuning_locked()
            return self._pairing_tuning

    def live_session_frozen_config(
        self, session_id: str
    ) -> tuple[HSVRange, ShapeGate] | None:
        """Public accessor for the (hsv_range, shape_gate) pair frozen
        onto a LivePairingSession at first ingest_live_frame.

        Returns None when:
          - no LivePairingSession exists for `session_id` (test fixture
            / replay path that POSTed /pitch without arming + ingesting), OR
          - the LivePairingSession was pre-created by arm_session but no
            live frame has flowed yet (e.g. server_post-only flow where
            iOS never streams live frames between arm and /pitch upload).

        Returns the frozen pair when first-ingest stamping has run; this
        is the dashboard-armed live-streaming production path. Callers must
        treat None as "no frozen snapshot — fall back to current state",
        not as an invariant violation: server_post-only is a real flow.
        """
        with self._lock:
            live = self._live_pairings.get(session_id)
            if live is None:
                return None
            if live.hsv_range_used is None or live.shape_gate_used is None:
                return None
            return (
                live.hsv_range_used,
                live.shape_gate_used,
            )

    def set_shape_gate(self, shape_gate: ShapeGate) -> ShapeGate:
        with self._lock:
            self._detection_config = self._detection_config.with_(
                shape_gate=shape_gate,
                preset=None,
                last_applied_at=self._time_fn(),
            )
            self._persist_detection_config_locked()
            return self._detection_config.shape_gate

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
    # `state.processing.{mark_server_post_queued, start_server_post_job,
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
        iPhone receives its slot via WS push (`/ws/device/{cam}` on hello
        and on every settings broadcast); `/status` mirrors the same map
        for dashboard observability:
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
            removed_any = (
                bool(keys_to_drop)
                or session_id in self.results
                or session_id in self._live_pairings
            )
            for key in keys_to_drop:
                self.pitches.pop(key, None)
            self.results.pop(session_id, None)
            # Purge the live-pairing entry too. Without this the
            # store_result race guard (which treats "session in
            # _live_pairings" as proof the session is still alive)
            # would never trigger for a live-only WS session that gets
            # deleted mid-stream.
            self._live_pairings.pop(session_id, None)
            self._processing.remove_session(session_id)
            self._live_missing_cal.pop(session_id, None)
            self._live_missing_cal_logged = {
                key for key in self._live_missing_cal_logged if key[0] != session_id
            }
            self._live_missing_sync_logged = {
                key for key in self._live_missing_sync_logged if key[0] != session_id
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
            self._live_missing_sync_logged.clear()
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
