from __future__ import annotations

import json
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from schemas import (
    CalibrationSnapshot,
    DetectionConfigSnapshotPayload,
    DetectionPath,
    Device,
    DeviceIntrinsics,
    FramePayload,
    IOS_CAPTURE_TIME_ALGORITHM_ID,
    PitchPayload,
    Session,
    SessionResult,
    SyncRun,
    TrackingExposureCapMode,
    TriangulatedPoint,
    _DEFAULT_SESSION_TIMEOUT_S,
    persist_pitch_json,
    persist_result_json,
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
from reconstruct import Ray
from state_runtime import RuntimeSettingsStore, SyncParams
from strike_zone import StrikeZoneGeometry, strike_zone_geometry_for_height
from state_calibration import (
    AutoCalibrationRun as _AutoCalibrationRun,
    AutoCalibrationRunStore,
    CalibrationFrameBuffer,
    LastSolveStore,
    CalibrationStore,
    CALIBRATION_FRAME_TTL_S as _CALIBRATION_FRAME_TTL_S,
    DeviceIntrinsicsStore,
)
from state_devices import DeviceRegistry
from state_events import build_events
from state_processing import SessionProcessingState
from state_sync import (
    SyncCoordinator,
    _SYNC_COMMAND_TTL_S,
    _SYNC_COOLDOWN_S,
    _SYNC_LATE_REPORT_GRACE_S,
    _SYNC_TIMEOUT_S,
    _TIME_SYNC_INTENT_WINDOW_S,
    _TIME_SYNC_MAX_AGE_S,
)
import session_results
import state_detection
import status_view
import ws_messages
from ws_messages import _DISARM_ECHO_S  # noqa: F401  re-exported for main.py (server/main.py:91)

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

# `_DISARM_ECHO_S` now lives in `ws_messages.py` alongside the
# `commands_for_devices` derivation that consumes it. Re-exported below
# (see import block) for back-compat with `main.py` / tests that read
# it via `state._DISARM_ECHO_S` or `from main import _DISARM_ECHO_S`.

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


def _summarise_load_error(e: Exception) -> str:
    """Compact one-line summary of a pitch-file load failure for
    `_load_from_disk`. Pydantic's `ValidationError.__str__` emits four
    lines per offending field (path / message / docs URL / blank), so a
    single legacy file with N missing fields drowns out the next file's
    error and the post-loop "N file(s) failed" summary. We collapse to
    "<count> validation error(s); first fields: a.b.c, d.e.f, …" — the
    operator can re-run `PitchPayload.model_validate(json.loads(...))`
    on any flagged path to recover full detail."""
    errors = getattr(e, "errors", None)
    if callable(errors):
        try:
            entries = errors()
        except Exception:
            entries = None
        if entries:
            fields = ", ".join(
                ".".join(str(part) for part in entry.get("loc", ()))
                for entry in entries[:3]
            )
            more = "" if len(entries) <= 3 else f" (+{len(entries) - 3} more)"
            return f"{len(entries)} validation error(s); first fields: {fields}{more}"
    # Non-pydantic exception (JSONDecodeError, OSError) — first line only.
    return str(e).splitlines()[0] if str(e) else type(e).__name__


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
        # Mtime of the latest atomic_write to each pitch JSON, keyed by
        # (camera_id, session_id). state_events.build_events reads this
        # to avoid a per-row disk stat() on the dashboard's 5 s tick
        # (~40k stat()/min at 100-session scale). Source of truth is still
        # the on-disk file; the cache is a write-through summary.
        # Invalidated by record() (write) and delete_session() / reset()
        # (purge). Cold misses fall through to stat() in
        # state_events._latest_pitch_mtime.
        self._pitch_mtime_cache: dict[tuple[str, str], float] = {}
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
        # Tombstones for sessions explicitly deleted via `delete_session`.
        # `record()` checks this before publishing a pitch so a stale
        # upload (e.g., iOS retry from PayloadUploadQueue after operator
        # deleted the session on the dashboard) cannot resurrect the
        # session on disk + in memory. Bounded — older tombstones age
        # out once 256 deletions have happened, well beyond any realistic
        # in-flight upload retry horizon. Tombstones are NOT persisted
        # across restart: server restart already invalidates iOS retry
        # queues by the heartbeat reconnect handshake, so the in-memory
        # bound is sufficient.
        #
        # deque(maxlen=256) — covers ~one week of sessions for personal LAN
        # tool; older deletions age out and could in principle resurrect on
        # retry-after-delete-after-256-other-deletions; non-persisted across
        # server restart (relies on iOS WS reconnect to invalidate stale
        # retry attempts).
        self._deleted_session_tombstones: deque[str] = deque(maxlen=256)
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
        # Phase-3 dual active preset: `_detection_config.preset` drives
        # the live (iOS) path and is v11_hsv_cc-only because iOS does
        # the detection itself. `_active_server_post_preset_name` drives
        # the server-side post-pass and accepts any registered algorithm
        # (v11 or hybrid). Boot default = same as live preset (operator
        # can re-bind to a hybrid preset via dashboard); persisted so a
        # restart doesn't silently drop the operator's last server_post
        # choice. Read by the dashboard render path + the `Run server`
        # event button; orthogonal to live config and WS settings push
        # (iOS never sees the server_post selection — it only ever runs
        # the live algorithm).
        self._active_server_post_preset_name: str = (
            self._load_active_server_post_preset_or_default()
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
        # Calibrations first — stale/missing result-cache entries are rebuilt
        # during _load_from_disk(), and triangulation needs the calibration
        # snapshot to decide the intrinsic-scale factor (MOV dims vs.
        # calibration dims).
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

    @property
    def sync(self) -> SyncCoordinator:
        """Public accessor for the sync coordinator (chirp + mutual sync)."""
        return self._sync

    @property
    def preview(self) -> PreviewBuffer:
        """Public accessor for the live preview JPEG buffer."""
        return self._preview

    @property
    def markers(self) -> MarkerRegistryDB:
        """Public accessor for the marker registry DB."""
        return self._marker_registry

    def calibration_path(self, camera_id: str) -> Path:
        """Public accessor for the on-disk calibration JSON path of a camera."""
        return self._calibration_path(camera_id)

    def pitch_path(self, camera_id: str, session_id: str) -> Path:
        """Public accessor for the on-disk pitch JSON path."""
        return self._pitch_path(camera_id, session_id)

    def session_paths_for(self, session_id: str) -> set[DetectionPath] | None:
        """Return the detection paths frozen on a session at arm time, or
        None if the session is unknown. Holds `_lock` during lookup."""
        with self._lock:
            sess = self._lookup_session_locked(session_id)
            return set(sess.paths) if sess is not None else None

    def default_detection_paths(self) -> set[DetectionPath]:
        """Return a copy of the operator's currently-selected default
        detection paths. Holds `_lock` during read."""
        with self._lock:
            return set(self._runtime_settings.default_paths)

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

    def _load_cached_result_for_session(self, session_id: str, pitch_paths: list[Path]) -> tuple[SessionResult | None, str]:
        path = self._result_path(session_id)
        if not path.exists():
            return None, "missing"
        try:
            result_mtime_ns = path.stat().st_mtime_ns
        except OSError as e:
            return None, f"stat_error:{e}"
        latest_pitch_mtime_ns = 0
        for pitch_path in pitch_paths:
            try:
                latest_pitch_mtime_ns = max(latest_pitch_mtime_ns, pitch_path.stat().st_mtime_ns)
            except OSError as e:
                return None, f"pitch_stat_error:{e}"
        if latest_pitch_mtime_ns > result_mtime_ns:
            return None, "stale"
        try:
            result = SessionResult.model_validate(json.loads(path.read_text()))
        except Exception as e:
            return None, f"invalid:{str(e)[:120]}"
        if result.session_id != session_id:
            return None, f"session_id_mismatch:{result.session_id}"
        return result, "cached"

    def _load_from_disk(self) -> None:
        # Corrupt / schema-incompatible pitch JSONs get logged at ERROR
        # (was WARNING — masqueraded as benign) so the operator notices
        # them in stdout. Server still boots so a single bad file doesn't
        # block startup, but the failure count is reported below to make
        # silent disappearance of sessions impossible to miss.
        backfill: list[tuple[Path, tuple[str, str]]] = []
        load_failures: list[tuple[str, str]] = []
        pitch_paths_by_session: dict[str, list[Path]] = {}
        for path in sorted(self._pitch_dir.glob("session_*.json")):
            try:
                obj = json.loads(path.read_text())
                pitch = PitchPayload.model_validate(obj)
            except Exception as e:
                # pydantic ValidationError.__str__ dumps every offending
                # field across multiple lines, burying the final
                # "N file(s) failed" summary and every other startup
                # INFO line under hundreds of lines per legacy file.
                # Compress to count + first three field paths; full
                # detail is reproducible by re-running model_validate
                # manually on the offending file.
                summary = _summarise_load_error(e)
                logger.error("skip corrupt pitch file %s: %s", path.name, summary)
                load_failures.append((path.name, summary[:200]))
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
            pitch_paths_by_session.setdefault(pitch.session_id, []).append(path)

        for path, key in backfill:
            pitch = self.pitches.get(key)
            if pitch is None:
                continue
            try:
                self._atomic_write(path, persist_pitch_json(pitch))
            except OSError as e:
                logger.warning("created_at backfill write failed %s: %s", path, e)

        seen_sessions = set(pitch_paths_by_session)
        cached_count = 0
        rebuilt_by_reason: dict[str, int] = {}
        for sid in sorted(seen_sessions):
            cached, reason = self._load_cached_result_for_session(sid, pitch_paths_by_session[sid])
            if cached is not None:
                self.results[sid] = cached
                cached_count += 1
                continue
            rebuilt_by_reason[reason] = rebuilt_by_reason.get(reason, 0) + 1
            result = session_results.rebuild_result_for_session(self, sid)
            self.results[sid] = result
            self._atomic_write(self._result_path(sid), persist_result_json(result))

        if self.pitches:
            logger.info(
                "restored %d pitch payloads across %d sessions from %s; result cache cached=%d rebuilt=%d reasons=%s",
                len(self.pitches),
                len(seen_sessions),
                self._data_dir,
                cached_count,
                sum(rebuilt_by_reason.values()),
                rebuilt_by_reason,
            )
        if load_failures:
            logger.error(
                "%d pitch file(s) failed schema validation and were skipped — "
                "their sessions will not appear in the dashboard event list",
                len(load_failures),
            )

    def _load_session_meta_from_disk(self) -> None:
        """Strict loader: a present-but-corrupt session_meta file raises
        rather than silently dropping trashed/starred state. Research-mode
        invariant — silently restoring "no trash, no stars" on a parse
        failure would un-hide sessions the operator deliberately trashed
        and contaminate the events list."""
        path = self._session_meta_path
        if not path.exists():
            return
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{path} must be a JSON object")
        trashed = obj.get("trashed_sessions")
        if trashed is not None:
            if not isinstance(trashed, dict):
                raise ValueError(
                    f"{path} 'trashed_sessions' must be an object, got {type(trashed).__name__}"
                )
            parsed: dict[str, float] = {}
            for sid, ts in trashed.items():
                if not isinstance(sid, str):
                    raise ValueError(
                        f"{path} 'trashed_sessions' key must be a string, got {type(sid).__name__}"
                    )
                if not isinstance(ts, (int, float)):
                    raise ValueError(
                        f"{path} 'trashed_sessions[{sid}]' must be numeric, got {type(ts).__name__}"
                    )
                parsed[sid] = float(ts)
            self._processing.load_trashed(parsed)
        starred = obj.get("starred_sessions")
        if starred is not None:
            if not isinstance(starred, dict):
                raise ValueError(
                    f"{path} 'starred_sessions' must be an object, got {type(starred).__name__}"
                )
            parsed_s: dict[str, float] = {}
            for sid, ts in starred.items():
                if not isinstance(sid, str):
                    raise ValueError(
                        f"{path} 'starred_sessions' key must be a string, got {type(sid).__name__}"
                    )
                if not isinstance(ts, (int, float)):
                    raise ValueError(
                        f"{path} 'starred_sessions[{sid}]' must be numeric, got {type(ts).__name__}"
                    )
                parsed_s[sid] = float(ts)
            self._processing.load_starred(parsed_s)

    def _persist_session_meta_locked(self) -> None:
        payload = json.dumps(
            {
                "trashed_sessions": self._processing.trashed_sessions,
                "starred_sessions": self._processing.starred_sessions,
            },
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

    @property
    def _active_server_post_preset_path(self) -> Path:
        return self._data_dir / "active_server_post_preset.json"

    def _load_active_server_post_preset_or_default(self) -> str:
        """Boot loader for the server_post active preset slot. On
        first boot (no sidecar file), explicitly initialises the slot
        to the live preset name AND writes the sidecar so the choice
        becomes auditable on disk — no silent in-memory fallback that
        an operator would have to re-pick after a restart. Strict on
        a present-but-corrupt file: wraps `json.loads` so malformed
        bytes surface as a typed message naming the path.

        If the live `_detection_config.preset` is None (e.g. an operator
        nudged HSV from the dashboard which clears the preset binding),
        first-boot must NOT write `{"name": null}` to the sidecar —
        that would make the *next* boot raise ValueError on the
        non-empty-string check and the server would never come up.
        Fall back to the `tennis` builtin (seed_builtins guarantees it
        on disk by the time this method runs) so the slot always has
        a real preset on first write."""
        path = self._active_server_post_preset_path
        if not path.exists():
            initial = self._detection_config.preset
            if not isinstance(initial, str) or not initial:
                # Live config has no preset binding right now; pick a
                # builtin so the sidecar is always non-null.
                initial = "tennis"
                if not _presets.preset_exists(self._data_dir, initial):
                    raise RuntimeError(
                        f"builtin preset {initial!r} missing on disk — "
                        f"seed_builtins must run before _load_active_server_post_preset_or_default"
                    )
            self._atomic_write(path, json.dumps({"name": initial}))
            return initial
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{path} is not valid JSON: {e}"
            ) from e
        if not isinstance(obj, dict):
            raise ValueError(
                f"{path} must be a JSON object with 'name'"
            )
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"{path} missing required 'name' field"
            )
        return name

    def _persist_active_server_post_preset_locked(self) -> None:
        """Caller owns `self._lock`. Sidecar file separate from
        `detection_config.json` because that file is v11-shaped (carries
        flat hsv + shape_gate) and the server_post slot is opaque
        (just a preset name → preset library is the source of truth
        for params)."""
        self._atomic_write(
            self._active_server_post_preset_path,
            json.dumps({"name": self._active_server_post_preset_name}),
        )

    def active_server_post_preset_name(self) -> str:
        """Slug of the preset that drives the server-side post-pass.
        Always non-empty (boot default = live preset). Orthogonal to
        `detection_config().preset` — the live path is v11-only, the
        server_post path accepts any registered algorithm."""
        with self._lock:
            return self._active_server_post_preset_name

    def set_active_server_post_preset(self, name: str) -> str:
        """Switch the server_post active preset. Validates the preset
        exists AND parses cleanly (algorithm_id registered, params
        schema-valid) before binding; raises `KeyError` if missing,
        `ValueError` if corrupt. Full round-trip via `load_preset` not
        bare `preset_exists` so a corrupt preset can't be activated and
        then crash detection on the next /run_server_post.
        Persists immediately so a restart doesn't drop the choice.
        Does NOT touch live `_detection_config` and does NOT broadcast
        WS settings — operators of the live (iOS) path see no change.
        """
        # Validate-then-bind under the lock: keeps the on-disk sidecar
        # consistent with the in-memory pointer even if a concurrent
        # set_active_server_post_preset / delete_preset arrives mid-call.
        with self._lock:
            # load_preset raises KeyError if missing; the migration
            # path + algorithm_id registration check happens inside it.
            _presets.load_preset(
                self._data_dir, name, atomic_write=self._atomic_write
            )
            # Write-then-mutate: persist sidecar first, then update the
            # in-memory pointer. On disk failure the operator's previous
            # choice survives instead of in-memory drift vs disk.
            self._atomic_write(
                self._active_server_post_preset_path,
                json.dumps({"name": name}),
            )
            self._active_server_post_preset_name = name
            return self._active_server_post_preset_name

    def _load_pairing_tuning_from_disk(self) -> PairingTuning:
        """Strict loader: a present-but-corrupt pairing_tuning file raises
        rather than silently reverting to `PairingTuning.default()`.
        Research-mode invariant — silently restoring defaults would
        contaminate comparisons across pairing parameter sweeps."""
        path = self._pairing_tuning_path
        if not path.exists():
            return PairingTuning.default()
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise ValueError(f"{path} must be a JSON object")
        raw = obj.get("gap_threshold_m")
        if raw is None:
            raise ValueError(f"{path} missing required 'gap_threshold_m'")
        if not isinstance(raw, (int, float)):
            raise ValueError(
                f"{path} 'gap_threshold_m' must be numeric, got {type(raw).__name__}"
            )
        return PairingTuning(gap_threshold_m=float(raw))

    def _persist_pairing_tuning_locked(self) -> None:
        t = self._pairing_tuning
        payload = json.dumps(
            {"gap_threshold_m": t.gap_threshold_m},
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
        return state_detection.ingest_live_frame(self, camera_id, session_id, frame)

    def live_rays_for_frame(
        self,
        camera_id: str,
        session_id: str,
        frame: FramePayload,
    ) -> list[Ray]:
        return state_detection.live_rays_for_frame(self, camera_id, session_id, frame)

    def mark_live_path_ended(self, camera_id: str, session_id: str, reason: str | None = None) -> None:
        state_detection.mark_live_path_ended(self, camera_id, session_id, reason)

    def persist_live_frames(self, camera_id: str, session_id: str) -> SessionResult | None:
        return state_detection.persist_live_frames(self, camera_id, session_id)

    def flush_live_frames_for_session(self, session_id: str) -> None:
        state_detection.flush_live_frames_for_session(self, session_id)

    def _atomic_write(self, path: Path, payload: str) -> None:
        # Unique tmp filename per call so concurrent writers targeting the
        # same `path` (e.g. two simultaneous /pitch POSTs producing the same
        # result file) can't clobber each other's in-flight tmp before the
        # rename. Each caller writes its own tmp then atomically replaces
        # `path`; last writer wins on `path` (deterministic content).
        #
        # On any write failure (disk full, permissions, KeyboardInterrupt
        # mid-write, etc.) the tmp file must be unlinked or it accumulates
        # forever as `.<token>.tmp` siblings — eats inodes long-term and
        # confuses `delete_session`'s `*.tmp` glob which assumes tmps are
        # in-flight not abandoned.
        tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(payload)
            tmp.replace(path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as e:
                # Best-effort cleanup; surface the original failure, but
                # log the cleanup failure so inode accumulation is auditable.
                logger.warning("failed to unlink leftover tmp %s: %s", tmp, e)
            raise

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

        Race note 1b — concurrent same-(cam, sid) record(): two
        simultaneous uploads for the same (cam, sid) pair (e.g. iOS
        PayloadUploadQueue retry racing the original) both read
        existing=None in CS0, build identical merged pitches in
        parallel, and the later CS1 publish overwrites the earlier
        without union. For a personal LAN tool with bounded iOS retry
        this collapses to last-writer-wins (deterministic on disk via
        atomic_write). If frame-set union semantics matter in future,
        re-read existing in CS1 and merge.

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

        # --- CS0 (read-only snapshot): tombstone guard + build merged.
        # Pre-write existence guard. `delete_session` drops a tombstone
        # for the deleted sid; a stale upload (iOS retry from
        # `PayloadUploadQueue` after operator deleted the session, or
        # an in-flight `_run_server_detection` that finished after
        # delete) must not silently resurrect the session on disk + in
        # memory. Refuse — return a synthetic SessionResult with
        # `error="session_deleted_during_record"` so callers don't
        # silently shadow a real result with a shell.
        #
        # NOT used as guard: "is sid known to pitches/results/sessions?"
        # — that would block first-record of a session that's been
        # armed-then-recorded via the normal path. Only the tombstone
        # set proves a session was explicitly deleted.
        sid = pitch.session_id
        cam = pitch.camera_id
        with self._lock:
            if sid in self._deleted_session_tombstones:
                logger.warning(
                    "record: session %s was deleted before record() — "
                    "refusing to resurrect (cam=%s)",
                    sid, cam,
                )
                return SessionResult(
                    session_id=sid,
                    cameras_received={"A": False, "B": False},
                    error="session_deleted_during_record",
                )
            existing = self.pitches.get((cam, sid))
            live_frames = session_results.live_frames_for_camera_locked(
                self, sid, cam,
            )
            existing_snapshot = (
                existing.model_copy(deep=True) if existing is not None else None
            )

        # --- Outside the lock: build the merged pitch. Pure CPU; no
        # state mutation. ---
        merged = pitch.model_copy(deep=True)
        if existing_snapshot is not None:
            # Dict-level merge: existing buckets that the incoming
            # pitch lacks must survive. Running v11→v12 would
            # otherwise lose v11's accumulated frames (incoming
            # writer typically rebuilds the pitch from current
            # snapshot only). Incoming wins on key collision
            # (latest write); missing keys carry over from existing.
            # Same union logic for config_used_by_algorithm.
            for alg_id, frames in existing_snapshot.frames_by_algorithm.items():
                if alg_id not in merged.frames_by_algorithm:
                    # Deep-copy each frame so any future in-place
                    # mutation on `merged` cannot bleed back into
                    # the cached `existing` (FramePayload is not
                    # frozen).
                    merged.frames_by_algorithm[alg_id] = [
                        f.model_copy(deep=True) for f in frames
                    ]
            for alg_id, snap in existing_snapshot.config_used_by_algorithm.items():
                if alg_id not in merged.config_used_by_algorithm:
                    merged.config_used_by_algorithm[alg_id] = snap.model_copy(deep=True)
            # Preserve the active server_post pointer when incoming
            # didn't carry one (e.g. live-frames merge after a
            # server_post run had already stamped the pointer).
            if (
                merged.active_server_post_algorithm_id is None
                and existing_snapshot.active_server_post_algorithm_id is not None
            ):
                merged.active_server_post_algorithm_id = (
                    existing_snapshot.active_server_post_algorithm_id
                )
            # Preserve the previous run's wall-clock when the
            # incoming pitch doesn't carry one (e.g., live-frames
            # merge after server_post had already completed).
            if merged.server_post_ran_at is None and existing_snapshot.server_post_ran_at is not None:
                merged.server_post_ran_at = existing_snapshot.server_post_ran_at
            # Preserve the original creation stamp across re-records
            # (server_post backfill, live merge). If the existing record
            # lacked one (legacy / synthetic before this field shipped),
            # fall through and stamp now.
            if existing_snapshot.created_at is not None:
                merged.created_at = existing_snapshot.created_at
        if merged.created_at is None:
            merged.created_at = self._time_fn()
        if not merged.frames_live and live_frames:
            merged.frames_by_algorithm[IOS_CAPTURE_TIME_ALGORITHM_ID] = list(live_frames)
        pitch = merged

        # --- Outside the lock: write pitch JSON FIRST, before mutating
        # the in-memory map. Filename is unique per (camera, session) and
        # each pitch uses its own tmp file, so two concurrent calls here
        # cannot collide. If `_atomic_write` raises (disk full, perm,
        # etc.) the in-memory `self.pitches` stays consistent with disk.
        # ---
        self._atomic_write(pitch_path, persist_pitch_json(pitch))

        # --- CS1 (publish): tombstone re-check + mutate pitches map +
        # drive session FSM. A concurrent `delete_session` could have
        # raced between CS0 and now, dropping a tombstone while we
        # wrote disk. If so: unlink the just-written pitch JSON
        # (delete_session already glob-purged but we re-wrote
        # afterwards) and bail without resurrecting `self.pitches`.
        with self._lock:
            sid = pitch.session_id
            cam = pitch.camera_id
            tombstoned = sid in self._deleted_session_tombstones
            if tombstoned:
                logger.warning(
                    "record: session %s deleted between CS0 and CS1 — "
                    "discarding pitch publish (cam=%s)",
                    sid, cam,
                )
        if tombstoned:
            try:
                pitch_path.unlink(missing_ok=True)
            except OSError:
                pass
            return SessionResult(
                session_id=sid,
                cameras_received={"A": False, "B": False},
                error="session_deleted_during_record",
            )
        with self._lock:
            self.pitches[(pitch.camera_id, pitch.session_id)] = pitch
            # Refresh the mtime cache so state_events.build_events can
            # skip the per-row stat(). Using _time_fn() (write moment)
            # instead of path.stat().st_mtime avoids an extra syscall
            # and matches the ordering need — the value is only ever
            # compared against other cached values within build_events.
            self._pitch_mtime_cache[(pitch.camera_id, pitch.session_id)] = self._time_fn()
            # Drive the session state machine forward — any upload arriving
            # while armed disarms the session (one-shot pattern). The other
            # camera, if it was also recording, gets "disarm" on the next
            # WS settings push and cleans up.
            self._register_upload_in_session_locked(pitch)

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
            persist_result_json(result),
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
        return status_view.summary(self)

    def latest(self) -> SessionResult | None:
        return status_view.latest(self)

    def session_known(self, session_id: str) -> bool:
        """Existence check matching `store_result`'s own guard: a session
        is 'alive' iff it has a pitch entry, a result entry, or a live
        pairing buffer. Live-only WS sessions before `persist_live_frames`
        flush live only in `_live_pairings`, so checking `pitches` alone
        would 404 a still-active live session.

        Public accessor so route handlers don't have to poke `state._lock`
        + `state._live_pairings` directly (PR #93 / state.py refactor
        keeps internal locks internal).
        """
        with self._lock:
            return (
                any(s == session_id for _, s in self.pitches)
                or session_id in self.results
                or session_id in self._live_pairings
            )

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
        self._atomic_write(self._result_path(sid), persist_result_json(result))
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

    def stamp_server_post_config(
        self,
        session_id: str,
        snapshot: "DetectionConfigSnapshotPayload",
    ) -> SessionResult | None:
        return state_detection.stamp_server_post_config(self, session_id, snapshot)

    def set_active_server_post_algorithm(
        self,
        session_id: str,
        algorithm_id: str,
    ) -> SessionResult | None:
        return state_detection.set_active_server_post_algorithm(
            self, session_id, algorithm_id,
        )

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
        return status_view.calibration_last_solve_summary(self, camera_id)

    def all_calibration_last_solves(self) -> dict[str, dict[str, Any]]:
        return status_view.all_calibration_last_solves(self)

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
            del marker_count  # marker registry cleared; not surfaced
            return {
                "calibrations_removed": cal_count,
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
        return status_view.auto_cal_status(self)

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
        return state_detection.live_missing_calibration_for(self, session_id)

    def live_session_summary(self) -> dict[str, Any] | None:
        return status_view.live_session_summary(self)

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

    def touch_device_last_seen(self, camera_id: str) -> None:
        """Freshen `Device.last_seen_at` WITHOUT touching time-sync state.

        Called from the WS connect handler. Using `heartbeat()` here used
        to clear `time_synced` / `time_sync_id` / `sync_anchor_timestamp_s`
        because the connect handler doesn't yet know what the phone will
        report on its first `hello` — every reconnect blip therefore wiped
        a freshly-established time-sync until the next hello arrived. The
        no-arg heartbeat() default `time_synced=False` is intentional for
        the explicit-clear sites; this method is the surgical alternative
        when liveness is the only thing we want to refresh.
        """
        with self._lock:
            self._device_registry.touch_last_seen(camera_id)

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
                # Distinguish "caller passed None (defer to runtime)" from
                # "caller passed an explicit set". Empty set is rejected —
                # arming a session with zero detection paths is nonsensical
                # (CLAUDE.md no-silent-fallback: prefer raise over a hidden
                # `_DEFAULT_PATHS` default that masks the misuse).
                if paths is None:
                    chosen_paths = set(self._runtime_settings.default_paths)
                else:
                    chosen_paths = set(paths)
                if not chosen_paths:
                    raise RuntimeError(
                        "arm_session: no detection paths chosen. Caller "
                        "passed an empty set OR runtime_settings."
                        "default_paths is empty (which is unreachable "
                        "under normal init — runtime_settings file is "
                        "likely corrupt)."
                    )
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
                # below has a `live.live_config_used is None` guard so the
                # test-bypass-arm path (build LivePairingSession inline,
                # call ingest directly) still gets stamped on first frame.
                live = LivePairingSession(session.id)
                live.pairing_tuning = self._pairing_tuning
                live.live_config_used = (
                    DetectionConfigSnapshotPayload.from_detection_config(
                        self._detection_config
                    )
                )
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

        If `cfg.preset` is non-None, validates the preset still exists
        on disk WITHIN the lock. The `routes/presets.py` Apply path
        does its own `load_preset` outside the lock before calling this
        — a concurrent `DELETE /presets/{name}` between that load and
        the bind here would have wired the live config to a deleted
        preset name. Raising KeyError here (route translates to 409)
        keeps the in-memory `_detection_config.preset` pointing only at
        presets that still exist on disk.
        """
        import algorithms
        algorithms.validate_runnable_id(cfg.algorithm_id)
        with self._lock:
            if cfg.preset is not None:
                if not _presets.preset_exists(self._data_dir, cfg.preset):
                    raise KeyError(cfg.preset)
            # Write-then-mutate: build the new value, persist it
            # atomically, and only on disk-success update in-memory
            # state. On disk failure (full disk, perm error) the in-
            # memory `_detection_config` stays in sync with disk.
            new_cfg = cfg.with_(last_applied_at=self._time_fn())
            _detection_config_persist(
                new_cfg,
                self._data_dir,
                atomic_write=self._atomic_write,
            )
            self._detection_config = new_cfg
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
            # Write-then-mutate: see `set_detection_config` for rationale.
            new_cfg = self._detection_config.with_(
                hsv=hsv_range,
                preset=None,
                last_applied_at=self._time_fn(),
            )
            _detection_config_persist(
                new_cfg,
                self._data_dir,
                atomic_write=self._atomic_write,
            )
            self._detection_config = new_cfg
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
        dashboard render path is the dominant caller; ms-scale).

        Threads `_atomic_write` through so any preset file pre-dating
        the `algorithm_id` field gets rewritten in canonical shape on
        first read post-upgrade."""
        return _presets.list_presets(
            self._data_dir, atomic_write=self._atomic_write
        )

    def load_preset(self, name: str):
        """Single preset by slug. Raises `KeyError(name)` if the file
        is missing on disk — endpoint handlers translate to 404; the
        dashboard renderer's `identity-deleted` branch handles the
        dangling-reference case before this would be called.

        Threads `_atomic_write` through so a file pre-dating the
        `algorithm_id` field gets rewritten in canonical shape on
        first read post-upgrade."""
        return _presets.load_preset(
            self._data_dir, name, atomic_write=self._atomic_write
        )

    def preset_exists(self, name: str) -> bool:
        return _presets.preset_exists(self._data_dir, name)

    def save_preset(self, preset) -> None:
        _presets.save_preset(
            self._data_dir, preset, atomic_write=self._atomic_write
        )

    def delete_preset(self, name: str) -> None:
        """Unlink the preset file. Raises `KeyError(name)` if absent;
        raises `RuntimeError` if `name` is currently bound to either
        the live or the server_post active slot — operator must
        re-bind the slot first.

        The active-slot check + filesystem unlink run under
        `self._lock` so a concurrent `set_active_server_post_preset`
        can't race past `preset_exists` while we're mid-delete and
        leave the sidecar pointing at a deleted file. Live
        `detection_config.preset` is also checked here as a defensive
        layer — `routes/presets.py` DELETE handler also rejects with
        409 before reaching this method, but that route check is
        outside the lock and the in-memory `_detection_config` could
        change between the route's check and this method.
        """
        with self._lock:
            if self._detection_config.preset == name:
                raise RuntimeError(
                    f"preset {name!r} is the active live preset"
                )
            if self._active_server_post_preset_name == name:
                raise RuntimeError(
                    f"preset {name!r} is the active server_post preset"
                )
            _presets.delete_preset(self._data_dir, name)

    def modified_fields_for(self, cfg: DetectionConfig) -> list[str]:
        return _detection_config_modified_fields(
            cfg,
            data_dir=self._data_dir,
            atomic_write=self._atomic_write,
        )

    def set_pairing_tuning(self, tuning: PairingTuning) -> PairingTuning:
        with self._lock:
            # Write-then-mutate: persist to disk first, then update
            # in-memory state. On `_atomic_write` failure the in-memory
            # `_pairing_tuning` stays consistent with disk.
            payload = json.dumps(
                {"gap_threshold_m": tuning.gap_threshold_m},
                indent=2,
            )
            self._atomic_write(self._pairing_tuning_path, payload)
            self._pairing_tuning = tuning
            return self._pairing_tuning

    def live_session_frozen_config(
        self, session_id: str
    ) -> tuple[HSVRange, ShapeGate] | None:
        return state_detection.live_session_frozen_config(self, session_id)

    def set_shape_gate(self, shape_gate: ShapeGate) -> ShapeGate:
        with self._lock:
            # Write-then-mutate: see `set_detection_config` for rationale.
            new_cfg = self._detection_config.with_(
                shape_gate=shape_gate,
                preset=None,
                last_applied_at=self._time_fn(),
            )
            _detection_config_persist(
                new_cfg,
                self._data_dir,
                atomic_write=self._atomic_write,
            )
            self._detection_config = new_cfg
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

    def set_sync_params(self, params: SyncParams) -> SyncParams:
        with self._lock:
            return self._runtime_settings.set_sync_params(params)

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

    def batter_height_cm(self) -> int:
        with self._lock:
            return self._runtime_settings.batter_height_cm

    def set_batter_height_cm(self, value: int) -> int:
        with self._lock:
            return self._runtime_settings.set_batter_height_cm(value)

    def strike_zone(self) -> StrikeZoneGeometry:
        with self._lock:
            return strike_zone_geometry_for_height(self._runtime_settings.batter_height_cm)

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

    def star_session(self, session_id: str) -> bool:
        now = self._time_fn()
        with self._lock:
            known = any(sid == session_id for _, sid in self.pitches) or session_id in self.results
            if not known:
                return False
            self._processing.star(session_id, at=now)
            self._persist_session_meta_locked()
            return True

    def unstar_session(self, session_id: str) -> bool:
        with self._lock:
            if not self._processing.unstar(session_id):
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
        the next arm replaces it)."""
        current = self.current_session()
        if current is not None:
            return current
        with self._lock:
            return self._most_recent_ended_session_locked()

    def commands_for_devices(self) -> dict[str, str]:
        return ws_messages.commands_for_devices(self)

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
            # Drop any cached pitch mtimes for this session — both cams,
            # whether or not pitches actually held them (e.g. server_post
            # rerun without a fresh pitch upload may have cached without
            # a pitches entry, defensive).
            for cam in ("A", "B"):
                self._pitch_mtime_cache.pop((cam, session_id), None)
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
            # Drop a tombstone so a late `record()` (stale iOS retry,
            # in-flight server_post job that hadn't finished writing)
            # cannot silently resurrect this session on disk + in memory.
            if session_id not in self._deleted_session_tombstones:
                self._deleted_session_tombstones.append(session_id)
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
            self._pitch_mtime_cache.clear()
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
