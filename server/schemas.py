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

from pydantic import BaseModel, Field


class CaptureMode(str, Enum):
    """Which side of the split does ball detection on the recording.

    - `camera_only`: iPhone uploads the MOV, server runs detection + triangulation.
      Bandwidth heavy, but the server sees the raw pixels so the detection
      pipeline (HSV + shape gate + MOG2) is fully authoritative.
    - `on_device`: iPhone runs the same detection pipeline locally and uploads
      only the per-frame results as JSON. Bandwidth is a few KB per session.
      Triangulation still happens on the server using the uploaded frames.
    - `dual`: both of the above in one shot — MOV is uploaded AND the
      iPhone's on-device detection result is attached as `frames_on_device`.
      Server runs its own detection on the MOV into `frames`, then
      triangulates each source independently. Used for ground-truth
      comparison during HSV / shape-gate tuning — viewer overlays the two
      point clouds so you can see where iOS and server disagree.

    The dashboard toggles the mode; every armed session snapshots the
    current global mode at arm time so a late dashboard toggle doesn't
    disturb an in-flight recording.
    """
    camera_only = "camera_only"
    on_device = "on_device"
    dual = "dual"


_DEFAULT_CAPTURE_MODE = CaptureMode.camera_only


class DetectionPath(str, Enum):
    """Orthogonal detection pipelines that can be enabled together.

    `CaptureMode` stays around as a backwards-compat preset vocabulary for
    older iOS / dashboard code, but new code should snapshot a set of
    `DetectionPath`s onto each armed session."""

    live = "live"
    ios_post = "ios_post"
    server_post = "server_post"


_DEFAULT_PATHS = frozenset({DetectionPath.server_post})


_MODE_TO_PATHS: dict[CaptureMode, frozenset[DetectionPath]] = {
    CaptureMode.camera_only: frozenset({DetectionPath.server_post}),
    CaptureMode.on_device: frozenset({DetectionPath.ios_post}),
    CaptureMode.dual: frozenset({DetectionPath.ios_post, DetectionPath.server_post}),
}


def paths_for_mode(mode: CaptureMode) -> set[DetectionPath]:
    return set(_MODE_TO_PATHS.get(mode, _DEFAULT_PATHS))


def mode_for_paths(paths: set[DetectionPath] | frozenset[DetectionPath]) -> CaptureMode:
    """Best-effort legacy preset representing `paths`.

    `CaptureMode` cannot express `live`-only or `live+server_post`. We map
    those to the nearest older preset purely for backward-compat clients; the
    authoritative detail lives in `paths`."""

    norm = set(paths)
    if norm == {DetectionPath.ios_post}:
        return CaptureMode.on_device
    if norm == {DetectionPath.ios_post, DetectionPath.server_post}:
        return CaptureMode.dual
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
    fx: float
    fz: float
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
    # For `camera_only` / `dual` modes the iPhone omits this on the wire and
    # server detection fills it before triangulation. For `on_device` mode
    # the iPhone populates it directly and server detection is skipped.
    frames: list[FramePayload] = Field(default_factory=list)
    # Live-streamed frame detections captured over WebSocket during the
    # active session. Persisted for forensics / future viewer switching.
    frames_live: list[FramePayload] = Field(default_factory=list)
    # Finalized iOS-side post-pass results over the local MOV.
    frames_ios_post: list[FramePayload] = Field(default_factory=list)
    # Finalized server-side post-pass results decoded from the uploaded MOV.
    frames_server_post: list[FramePayload] = Field(default_factory=list)
    # Parallel detection stream shipped by the iPhone when the session was
    # armed in `dual` mode. Lets the server keep both iOS-end and server-end
    # detection results for side-by-side comparison. Empty list otherwise.
    frames_on_device: list[FramePayload] = Field(default_factory=list)
    intrinsics: IntrinsicsPayload | None = None
    homography: list[float] | None = None
    image_width_px: int | None = None
    image_height_px: int | None = None
    capture_telemetry: CaptureTelemetryPayload | None = None


class PitchAnalysisPayload(BaseModel):
    """Late-arriving on-device post-pass analysis keyed to an existing pitch.

    Used by the PR61 analysis plane: the raw MOV (or mode-one payload) lands
    first, then the iPhone decodes its finalized local MOV and uploads the
    authoritative on-device frame list later."""
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    session_id: str = Field(..., pattern=r"^s_[0-9a-f]{4,32}$")
    frames_on_device: list[FramePayload] = Field(default_factory=list)
    capture_telemetry: CaptureTelemetryPayload | None = None


class TriangulatedPoint(BaseModel):
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


class TrajectoryFit(BaseModel):
    """RANSAC-fitted 3D quadratic `p(t) = ½·a·t² + v₀·t + p₀` over the
    triangulated points. Each axis is stored highest-degree-first (numpy's
    convention): `coeffs_x[0]` is the quadratic term, `coeffs_x[2]` is the
    constant. Index arrays reference positions in the source `points`
    list so the viewer can cross-reference raw points to fit labels.

    `release_*` captures the earliest inlier position on the fitted curve.
    `plate_*` is the fit evaluated at Y = 0 (the plate plane in world
    frame); None when no real-valued crossing exists in the observed
    window + 0.5 s slack."""
    coeffs_x: list[float] = Field(..., min_length=3, max_length=3)
    coeffs_y: list[float] = Field(..., min_length=3, max_length=3)
    coeffs_z: list[float] = Field(..., min_length=3, max_length=3)
    t_min_s: float
    t_max_s: float
    inlier_indices: list[int]
    outlier_indices: list[int]
    rms_m: float
    threshold_m: float
    release_xyz_m: list[float] = Field(..., min_length=3, max_length=3)
    release_t_s: float
    plate_xyz_m: list[float] | None = None
    plate_t_s: float | None = None


class SessionResult(BaseModel):
    """One armed-session's triangulation result. Replaces the old
    `CycleResult` now that "cycle" is a per-device recording-window concept
    and the pitch unit is server-level "session".

    Dual mode surfaces two parallel point clouds — `points` is the default
    (server detection) stream that existing code keys off, `points_on_device`
    is the iOS-end stream when the session armed in `dual` mode. Mono-mode
    sessions (camera_only / on_device) leave `points_on_device` empty and
    `points` is the single authoritative result.

    `fit` / `fit_on_device` are the RANSAC quadratic fits applied to the
    respective point clouds. LIVE + REPLAY UIs render the fit; raw points
    are only shown in FORENSIC. Optional so restored results from older
    server builds (pre-fit) keep loading without migration."""
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
    points_on_device: list[TriangulatedPoint] = []
    error_on_device: str | None = None
    fit: TrajectoryFit | None = None
    fit_on_device: TrajectoryFit | None = None





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
    marker_id: int = Field(..., ge=6, le=49)
    x_m: float
    y_m: float
    z_m: float
    label: str | None = None
    on_plate_plane: bool = False
    residual_m: float | None = None
    source_camera_ids: list[str] = Field(default_factory=list)


class MarkerDraft(BaseModel):
    marker_id: int = Field(..., ge=6, le=49)
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
