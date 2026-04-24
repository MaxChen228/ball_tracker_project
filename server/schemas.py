"""Wire-contract + in-memory domain model for the ball-tracker server.

This module holds the Pydantic models that define the HTTP payload shape
(`POST /pitch`, triangulation results, WS message envelopes) as well as
the lightweight dataclasses backing the in-memory device registry and
armed session machine. Split out of `main.py` so the request handlers, state
container, and persistence layer can import the types without pulling in
FastAPI app plumbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class CaptureMode(str, Enum):
    """Legacy preset vocabulary. Only `camera_only` remains; newer code
    should read `Session.paths` directly.
    """
    camera_only = "camera_only"


_DEFAULT_CAPTURE_MODE = CaptureMode.camera_only


class DetectionPath(str, Enum):
    """Orthogonal detection pipelines that can be enabled together."""

    live = "live"
    server_post = "server_post"


_DEFAULT_PATHS = frozenset({DetectionPath.live})


_MODE_TO_PATHS: dict[CaptureMode, frozenset[DetectionPath]] = {
    CaptureMode.camera_only: frozenset({DetectionPath.live}),
}


def paths_for_mode(mode: CaptureMode) -> set[DetectionPath]:
    return set(_MODE_TO_PATHS.get(mode, _DEFAULT_PATHS))


def mode_for_paths(paths: set[DetectionPath] | frozenset[DetectionPath]) -> CaptureMode:
    """Best-effort legacy preset representing `paths`. Only `camera_only`
    exists now — the authoritative detail lives in `paths` itself."""

    return CaptureMode.camera_only


class TrackingExposureCapMode(str, Enum):
    """Server-owned tracking exposure policy pushed to iOS via WS settings.

    This only affects the high-speed tracking path. Standby / sync windows
    stay capped at the frame duration so the rig's idle behaviour doesn't
    unexpectedly darken."""
    frame_duration = "frame_duration"
    shutter_500 = "shutter_500"
    shutter_1000 = "shutter_1000"


_DEFAULT_TRACKING_EXPOSURE_CAP_MODE = TrackingExposureCapMode.frame_duration


# Shared sync-run identifier used by both the mutual two-way chirp flow
# and the legacy third-device chirp-anchor flow. Distinct `sy_` prefix so
# logs can visually separate sync runs from pitch/session ids.
_SYNC_ID_PATTERN = r"^sy_[0-9a-f]{4,32}$"


class IntrinsicsPayload(BaseModel):
    # Legacy `fz` was a naming collision — iOS stored the image-vertical
    # focal length (OpenCV's fy) under a key called "fz". The rename is
    # mechanical now that iOS no longer echoes intrinsics on uploads. We
    # still accept the old `fz` wire key when loading historical
    # calibration JSON or old test fixtures via AliasChoices; population
    # by name keeps new code using `fy=...` constructors.
    model_config = ConfigDict(populate_by_name=True)

    fx: float
    fy: float = Field(..., validation_alias=AliasChoices("fy", "fz"))
    cx: float
    cy: float
    # OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Optional so
    # payloads without distortion still validate; server detection still
    # runs, just without lens distortion correction in triangulation.
    distortion: list[float] | None = None


class FramePayload(BaseModel):
    """Internal shape produced by server-side detection. NOT part of the wire
    contract any more — the iPhone uploads only the MOV + metadata; server
    synthesises one `FramePayload` per decoded video frame. `theta_x_rad`
    / `theta_z_rad` are always None now (the phone doesn't do angle
    projection); px/py come from server detection, and triangulation
    always uses the pixel+distortion path."""
    frame_index: int
    timestamp_s: float
    theta_x_rad: float | None = None
    theta_z_rad: float | None = None
    px: float | None = None
    py: float | None = None
    ball_detected: bool
    # Post-detection chain filter verdict. None = not yet scored (raw
    # upload / live frame pre-finalization). "kept" survives all gates;
    # "rejected_flicker" = chain was too short (min_run_len); "rejected_jump"
    # = chain broke because the ray direction jumped past max_jump_px. Set
    # only on frames where ball_detected is True — non-detections stay None.
    filter_status: Literal["kept", "rejected_flicker", "rejected_jump"] | None = None


class CaptureTelemetryPayload(BaseModel):
    """Actual capture conditions observed on-device for one uploaded pitch.

    This is intentionally "applied telemetry", not policy. Dashboard controls
    the desired mode/exposure; iOS reports what format/exposure path the
    hardware actually ended up using so post-mortems can answer "what really
    happened on this take?"."""
    width_px: int
    height_px: int
    target_fps: float
    applied_fps: float | None = None
    format_fov_deg: float | None = None
    format_index: int | None = None
    is_video_binned: bool | None = None
    tracking_exposure_cap: TrackingExposureCapMode | None = None
    applied_max_exposure_s: float | None = None


class PitchPayload(BaseModel):
    """Wire + in-memory shape. The iPhone posts the wire subset (no `frames`);
    server detection populates `frames` before triangulation and re-saves
    the enriched record to disk, so reloads across restarts skip re-
    detection."""
    # Constrained so we can safely interpolate into filenames (clips,
    # pitch json). Matches the iOS-side values ("A" / "B") with slack for
    # future role additions but blocks path-traversal attempts.
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    # Server-assigned session identifier (from `POST /sessions/arm`). This
    # is the sole pairing key for A/B uploads — iPhones no longer generate
    # their own counters. Pattern matches `_new_session_id()`; also safe to
    # interpolate into filenames.
    session_id: str = Field(..., pattern=r"^s_[0-9a-f]{4,32}$")
    # Shared sync-run identifier for the legacy third-device chirp flow.
    # When present, both A and B MUST match for the session to be
    # triangulatable; mismatches indicate the phones latched different chirp
    # runs and their `sync_anchor_timestamp_s` values are incomparable.
    sync_id: str | None = Field(default=None, pattern=_SYNC_ID_PATTERN)
    # Shared time anchor for A/B pairing, recovered from an audio-chirp
    # matched-filter hit on the 時間校正 step. Nil when the operator armed
    # without running a fresh time sync → server marks the session as
    # `error="no time sync"` and skips triangulation.
    sync_anchor_timestamp_s: float | None = None
    # Absolute session-clock PTS (seconds) of the first video sample. Server
    # adds this to each container-relative frame PTS so `FramePayload.timestamp_s`
    # lives on the same iOS master clock as `sync_anchor_timestamp_s`.
    video_start_pts_s: float
    # Nominal capture rate of the MOV. Sanity-check + detection log only —
    # optional since the iPhone no longer echoes it on every upload (it was
    # constant per build and the server had no authoritative use for the
    # value beyond the render-scene URL builder, which now defaults to
    # 240 fps when absent).
    video_fps: float | None = None
    # Optional device-local recording counter. Not used for pairing; kept
    # purely for operator debugging.
    local_recording_index: int | None = None
    # Snapshot of the session's requested detection paths. Optional so
    # older clients that only know `mode` keep validating.
    paths: list[str] | None = None
    # Server-side synthesised per-frame data (populated after detection).
    # Server fills this from the uploaded MOV before triangulation.
    frames: list[FramePayload] = Field(default_factory=list)
    # Live-streamed frame detections captured over WebSocket during the
    # active session. Persisted for forensics / future viewer switching.
    frames_live: list[FramePayload] = Field(default_factory=list)
    # Finalized server-side post-pass results decoded from the uploaded MOV.
    frames_server_post: list[FramePayload] = Field(default_factory=list)
    intrinsics: IntrinsicsPayload | None = None
    homography: list[float] | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None
    capture_telemetry: CaptureTelemetryPayload | None = None


class TriangulatedPoint(BaseModel):
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


class BallisticSummary(BaseModel):
    """Per-path ballistic RANSAC fit summary. Populated by
    `session_results.rebuild_result_for_session` when a path's
    triangulated set has >= `min_inliers` points and consensus is
    reached; otherwise absent (no silent defaults)."""

    release_point_m: tuple[float, float, float]
    release_velocity_mps: tuple[float, float, float]
    speed_mps: float
    speed_mph: float
    g_fit: float
    g_mode: str  # "free" | "pinned"
    n_inliers: int
    n_total: int
    rmse_m: float
    t0_s: float
    inlier_indices: list[int] = Field(default_factory=list)


class SessionResult(BaseModel):
    """One armed-session's triangulation result. Replaces the old
    `CycleResult` now that "cycle" is a per-device recording-window concept
    and the pitch unit is server-level "session"."""
    session_id: str
    camera_a_received: bool
    camera_b_received: bool
    solved_at: float | None = None
    triangulated: list[TriangulatedPoint] = []
    triangulated_by_path: dict[str, list[TriangulatedPoint]] = Field(default_factory=dict)
    frame_counts_by_path: dict[str, dict[str, int]] = Field(default_factory=dict)
    paths_completed: set[str] = Field(default_factory=set)
    aborted: bool = False
    abort_reasons: dict[str, str] = Field(default_factory=dict)
    points: list[TriangulatedPoint] = []
    error: str | None = None
    # Per-path ballistic RANSAC fit summaries. Absent when a path has
    # fewer than min_inliers points or consensus failed — explicit
    # absence, no defaults.
    ballistic_by_path: dict[str, BallisticSummary] = Field(default_factory=dict)
    ballistic_live: BallisticSummary | None = None
    ballistic_server_post: BallisticSummary | None = None





class DeviceIntrinsics(BaseModel):
    """Per-device ChArUco-measured camera intrinsics, keyed by stable
    hardware identity (`identifierForVendor`) rather than role. Holds K and
    the 5-coefficient distortion at the resolution the ChArUco shots were
    taken at; the auto-cal path scales fx/fy/cx/cy to whatever resolution
    the current capture frame actually delivers.

    `source_width_px` / `source_height_px` are the pixel grid the K was
    solved on — not necessarily 4032×3024; any constant grid works as long
    as the target preserves aspect ratio (4:3 ChArUco on a sensor that
    outputs 4:3 stills is fine). An AR mismatch at scale time is an
    operator error (tried to reuse 4:3 ChArUco K on a 16:9-cropped still),
    logged + rejected by the scale helper."""

    device_id: str = Field(..., pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    device_model: str | None = Field(default=None, max_length=32)
    source_width_px: int = Field(..., gt=0)
    source_height_px: int = Field(..., gt=0)
    intrinsics: IntrinsicsPayload
    rms_reprojection_px: float | None = Field(default=None, ge=0)
    n_images: int | None = Field(default=None, ge=1)
    calibrated_at: float | None = Field(default=None, ge=0)
    # Operator-supplied label, e.g. "charuco-a4-iphone15pro". Free-form,
    # no server-side semantics; shown in the dashboard's intrinsics card.
    source_label: str | None = Field(default=None, max_length=64)


class CalibrationSnapshot(BaseModel):
    """Standalone calibration upload. Sent by the phone whenever the user
    finishes Auto (ArUco) or Manual 5-handle Save, so the dashboard can draw
    the camera's pose in the 3D canvas without waiting for a pitch. Same
    `intrinsics + homography + image_{width,height}_px` shape PitchPayload
    already carries — the server can reuse `reconstruct`'s extrinsics math
    on it verbatim."""
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    intrinsics: IntrinsicsPayload
    homography: list[float] = Field(..., min_length=9, max_length=9)
    image_width_px: int
    image_height_px: int


class MarkerRecord(BaseModel):
    """Persisted operator-managed ArUco landmark.

    `on_plate_plane=True` means the marker is known to lie on the same
    physical plane as home plate and is therefore eligible for the current
    planar homography auto-calibration path. Free-space markers remain useful
    for registry / layout / future pose-solving work, but are excluded from
    today's planar auto-cal.
    """
    marker_id: int = Field(..., ge=9, le=49)
    x_m: float
    y_m: float
    z_m: float
    label: str | None = None
    on_plate_plane: bool = False
    residual_m: float | None = None
    source_camera_ids: list[str] = Field(default_factory=list)


class MarkerDraft(BaseModel):
    marker_id: int = Field(..., ge=9, le=49)
    x_m: float
    y_m: float
    z_m: float
    label: str | None = None
    on_plate_plane: bool = False
    snap_to_plate_plane: bool = False
    residual_m: float | None = None
    source_camera_ids: list[str] = Field(default_factory=list)


class MarkerBatchUpsertRequest(BaseModel):
    markers: list[MarkerDraft] = Field(default_factory=list)


class MarkerUpdateRequest(BaseModel):
    label: str | None = None
    x_m: float | None = None
    y_m: float | None = None
    z_m: float | None = None
    on_plate_plane: bool | None = None
    snap_to_plate_plane: bool = False
    # Optional: the grid the intrinsics were ORIGINALLY COMPUTED FROM (e.g.
    # 4032×3024 ChArUco stills subsequently scaled + cropped to the capture
    # grid). Knowing both lets the server detect 4:3→16:9 basis mismatches.
    source_width_px: int | None = None
    source_height_px: int | None = None


# nobody threw anything" — otherwise /status would keep dispatching arm
# forever.
_DEFAULT_SESSION_TIMEOUT_S = 60.0


# --- Mutual chirp sync -----------------------------------------------------
# Each phone emits a distinct audio chirp (two disjoint frequency bands),
# records its own self-hear + the peer's arrival via mic, and ships both
# timestamps to the server. The server solves a 2-variable linear system
# for Δ (A−B clock offset) and D (inter-phone distance). Pairing within
# the /pitch flow then uses Δ to align B's timeline onto A's — replacing
# the third-device chirp anchor that `sync_anchor_timestamp_s` carried.


# Mirror the defaults baked into `AudioSyncDetector.swift` (threshold=0.18,
# minPSR=1.5 at runtime but the debug plot reference line uses a looser 3.0
# to match the historical sync-health bar). Exposed here so the sync page
# can draw a reference line without plumbing them through render code.
SYNC_TRACE_THRESHOLD = 0.18
SYNC_TRACE_MIN_PSR = 3.0


class SyncTraceSample(BaseModel):
    """One matched-filter sample from a phone's dual-band detector. The
    trace buffer collects these at ~30 Hz during a sync run so the
    dashboard can plot sub-threshold peaks (the whole point of the debug
    view — long-distance failures manifest as peaks that never crossed
    0.18)."""
    # Run-relative seconds (subtract firstPTS from the sample's PTS).
    t: float
    # Normalized matched-filter peak in [0, 1] — same metric the gate uses.
    peak: float
    # Peak-to-sidelobe ratio (best / second-best outside exclusion window).
    psr: float


class SyncReport(BaseModel):
    """Wire payload for `POST /sync/report`. Each phone submits one of
    these once both the self-hear and cross-hear matched-filter peaks have
    fired on its mic stream. Both timestamps MUST live on the same mach
    host clock as video-frame PTS — iOS wires both `AVCaptureSession.masterClock`
    and `AVAudioTime.hostTime` to `CMClockGetHostTimeClock()`, so the
    capture-session path and the mutual-sync `AVAudioEngine` tap path
    produce interchangeable timestamps. The solver's algebra only depends
    on that single-clock invariant."""
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    sync_id: str = Field(..., pattern=_SYNC_ID_PATTERN)
    role: Literal["A", "B"]
    # mic PTS when this phone heard its own chirp. Null when aborted without
    # self-hear (speaker muted, silent switch on, etc.).
    t_self_s: float | None = None
    # mic PTS when this phone heard the other phone's chirp. Null when
    # aborted without cross-hear (peer silent, too far, band mismatch).
    t_from_other_s: float | None = None
    # Which frequency band this phone actually emitted — cross-checked
    # against role at the server to catch role-config drift on the rig.
    emitted_band: Literal["A", "B"]
    # Optional rolling matched-filter traces (own-band + other-band) for
    # the sync debug plot. Old iOS builds that don't collect traces simply
    # omit these fields and the Pydantic default keeps validation passing.
    trace_self: list[SyncTraceSample] | None = None
    trace_other: list[SyncTraceSample] | None = None
    # Failure-mode telemetry: when the phone gave up (timeout, dismissed,
    # disarmed) it still POSTs this report with whatever traces it has so
    # server-side post-mortem can surface sub-threshold peaks + noise floor.
    # `aborted=true` implies at least one of `t_self_s` / `t_from_other_s`
    # will typically be null — the whole point is shipping partial data.
    aborted: bool = False
    abort_reason: str | None = None


class SyncResult(BaseModel):
    """Outcome of one mutual-sync run — solved OR aborted. `delta_s` is
    **A clock minus B clock** (a positive value means A is ahead of B).
    Apply it as `t_on_A = t_on_B + delta_s` when re-timing B's events into
    A's timeline.

    When `aborted=True`, `delta_s` / `distance_m` / raw timestamps are
    None and the row is a diagnostic carrier: the traces + `abort_reasons`
    map still describe what each phone heard (and didn't), so a post-hoc
    dashboard / log reader can see sub-threshold peaks and noise floor."""
    id: str
    delta_s: float | None = None
    distance_m: float | None = None
    solved_at: float
    # Raw timestamps preserved for post-hoc debugging / viewer overlays.
    # Null on aborted runs where that phone never heard the corresponding
    # chirp.
    t_a_self_s: float | None = None
    t_a_from_b_s: float | None = None
    t_b_self_s: float | None = None
    t_b_from_a_s: float | None = None
    # Failure-mode fields. `aborted=True` when at least one of the two
    # phones couldn't produce a full timestamp pair. `abort_reasons` maps
    # role → reason string ("timeout", "dismissed", "disarmed", ...).
    aborted: bool = False
    abort_reasons: dict[str, str] = Field(default_factory=dict)
    # Per-role matched-filter traces copied off the incoming SyncReports so
    # the /sync page can render the full peak timeline post-hoc (page
    # reload, or inspecting a past run). Optional: old iOS builds ship
    # reports without traces.
    trace_a_self: list[SyncTraceSample] | None = None
    trace_a_other: list[SyncTraceSample] | None = None
    trace_b_self: list[SyncTraceSample] | None = None
    trace_b_other: list[SyncTraceSample] | None = None


class SyncLogEntry(BaseModel):
    """Single line in the dashboard's Time Sync diagnostic log. Both the
    server and each phone append entries — `source` is `"server"` or the
    originating `camera_id`. `detail` carries event-specific fields (e.g.
    `{"band": "A", "peak": 0.42}` for an iOS `band_fired` event); kept as
    free-form JSON so adding new events doesn't require a schema change."""
    ts: float
    source: str
    event: str = Field(..., max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class SyncLogBody(BaseModel):
    """Wire shape for `POST /sync/log`. Phones push one entry per major
    sync-flow event so the dashboard's diagnostic panel can display the
    full A/B/server timeline in one place."""
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    event: str = Field(..., max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


@dataclass
class SyncRun:
    """Transient in-memory state for an in-progress mutual-sync run. Lives
    on `State` alongside the armed-session slot. Keyed by role so a late
    repeat report from the same phone overwrites rather than ambiguates."""
    id: str
    started_at: float
    reports: dict[str, SyncReport] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return "A" in self.reports and "B" in self.reports

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "reports_received": sorted(self.reports.keys()),
        }


@dataclass
class Device:
    """Most recent heartbeat from a single iPhone. `last_seen_at` is a wall
    clock unix timestamp so `now - last_seen_at` compares cleanly even
    across server restarts (the dict is memory-only, so restart implies no
    device is online yet). `time_synced` is the latest boolean the phone
    asserted on its heartbeat; `time_sync_id` / `time_sync_at` identify
    which legacy chirp run produced the currently-held anchor and when the
    server most recently heard about it."""
    camera_id: str
    last_seen_at: float
    time_synced: bool = False
    time_sync_id: str | None = None
    time_sync_at: float | None = None
    sync_anchor_timestamp_s: float | None = None
    # Battery level 0..1 reported by UIDevice.batteryLevel; None when the
    # phone hasn't reported it yet or reports -1 (monitoring disabled).
    battery_level: float | None = None
    # UIDevice.batteryState as lowercase string: "unknown" | "unplugged"
    # | "charging" | "full". None when not reported.
    battery_state: str | None = None
    # Stable hardware identity: `UIDevice.identifierForVendor.uuidString`.
    # Survives app launches on the same device; reinstalling the app rotates
    # it. This is the key used to look up per-device ChArUco intrinsics in
    # `data/intrinsics/{device_id}.json` — the camera role ("A"/"B") only
    # identifies position, not the physical sensor.
    device_id: str | None = None
    # sysctl machine identifier (e.g. "iPhone15,3"). Operator-facing hint so
    # the dashboard can show a friendlier label alongside the UUID.
    device_model: str | None = None


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
    # Camera ids that have successfully uploaded while this session was
    # the current one. Dashboard reads this to render "session s_abc →
    # A, B".
    uploads_received: list[str] = field(default_factory=list)
    # Snapshot of the dashboard's path-set at arm time. New code should read
    # this. The legacy `mode` field below is derived from the same choice so
    # pre-path clients still see a familiar preset string.
    paths: set[DetectionPath] = field(default_factory=lambda: set(_DEFAULT_PATHS))
    # Snapshot of the dashboard's `capture_mode` at arm time. Once armed
    # the session's mode is immutable — a late dashboard toggle only
    # affects the next session.
    mode: CaptureMode = _DEFAULT_CAPTURE_MODE
    # Snapshot of the dashboard's tracking exposure-cap policy at arm time.
    # Once armed this is frozen for the whole session, matching `mode`.
    tracking_exposure_cap: TrackingExposureCapMode = _DEFAULT_TRACKING_EXPOSURE_CAP_MODE
    # Shared legacy chirp sync id observed across the online rig when this
    # session armed. Nil means the rig was not in a provably common synced
    # state (missing, stale, or mismatched ids) and any later triangulation
    # must rely on the payload pair validating itself.
    sync_id: str | None = None

    @property
    def armed(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        mode = mode_for_paths(self.paths)
        return {
            "id": self.id,
            "armed": self.armed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "max_duration_s": self.max_duration_s,
            "uploads_received": list(self.uploads_received),
            "mode": mode.value,
            "paths": sorted(p.value for p in self.paths),
            "tracking_exposure_cap": self.tracking_exposure_cap.value,
            "sync_id": self.sync_id,
        }


class StoredPitch(PitchPayload):
    """On-disk enriched payload shape.

    Exists mainly as a semantic marker so migration code can say "stored
    payload" while staying wire-compatible with `PitchPayload`."""

    pass
