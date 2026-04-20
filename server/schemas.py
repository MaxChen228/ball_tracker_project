"""Wire-contract + in-memory domain model for the ball-tracker server.

This module holds the Pydantic models that define the HTTP payload shape
(`POST /pitch`, `POST /heartbeat`, triangulation results) as well as the
lightweight dataclasses backing the in-memory device registry and armed
session machine. Split out of `main.py` so the request handlers, state
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
    # Shared time anchor for A/B pairing, recovered from an audio-chirp
    # matched-filter hit on the 時間校正 step. Nil when the operator armed
    # without running a fresh time sync → server marks the session as
    # `error="no time sync"` and skips triangulation.
    sync_anchor_timestamp_s: float | None = None
    # Absolute session-clock PTS (seconds) of the first video sample. Server
    # adds this to each container-relative frame PTS so `FramePayload.timestamp_s`
    # lives on the same iOS master clock as `sync_anchor_timestamp_s`.
    video_start_pts_s: float
    # Nominal capture rate of the MOV. Sanity-check + detection log.
    video_fps: float
    # Optional device-local recording counter. Not used for pairing; kept
    # purely for operator debugging.
    local_recording_index: int | None = None
    # Server-side synthesised per-frame data (populated after detection).
    # For `camera_only` / `dual` modes the iPhone omits this on the wire and
    # server detection fills it before triangulation. For `on_device` mode
    # the iPhone populates it directly and server detection is skipped.
    frames: list[FramePayload] = Field(default_factory=list)
    # Parallel detection stream shipped by the iPhone when the session was
    # armed in `dual` mode. Lets the server keep both iOS-end and server-end
    # detection results for side-by-side comparison. Empty list otherwise.
    frames_on_device: list[FramePayload] = Field(default_factory=list)
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
    points: list[TriangulatedPoint] = []
    error: str | None = None
    points_on_device: list[TriangulatedPoint] = []
    error_on_device: str | None = None
    fit: TrajectoryFit | None = None
    fit_on_device: TrajectoryFit | None = None


class HeartbeatBody(BaseModel):
    camera_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,16}$")
    # Whether this phone currently has a valid audio-chirp sync anchor
    # cached locally. Surfaced on `/status` so the dashboard can show a
    # per-device "time sync ✓/✗" badge without waiting for a pitch
    # upload. Optional + defaults False so older iOS builds that don't
    # send the field still validate.
    time_synced: bool = False


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

_SYNC_ID_PATTERN = r"^sy_[0-9a-f]{4,32}$"


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
    # mic PTS when this phone heard its own chirp (own-band matched filter)
    t_self_s: float
    # mic PTS when this phone heard the other phone's chirp (other-band filter)
    t_from_other_s: float
    # Which frequency band this phone actually emitted — cross-checked
    # against role at the server to catch role-config drift on the rig.
    emitted_band: Literal["A", "B"]
    # Optional rolling matched-filter traces (own-band + other-band) for
    # the sync debug plot. Old iOS builds that don't collect traces simply
    # omit these fields and the Pydantic default keeps validation passing.
    trace_self: list[SyncTraceSample] | None = None
    trace_other: list[SyncTraceSample] | None = None


class SyncResult(BaseModel):
    """Solved outcome of one mutual-sync run. `delta_s` is **A clock
    minus B clock** (a positive value means A is ahead of B). Apply it
    as `t_on_A = t_on_B + delta_s` when re-timing B's events into A's
    timeline."""
    id: str
    delta_s: float
    distance_m: float
    solved_at: float
    # Raw timestamps preserved for post-hoc debugging / viewer overlays.
    t_a_self_s: float
    t_a_from_b_s: float
    t_b_self_s: float
    t_b_from_a_s: float
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
    device is online yet). `time_synced` is the latest value the phone
    asserted on its heartbeat — the phone is authoritative because only
    it owns the chirp-detector state, so every heartbeat simply overwrites
    the cached flag."""
    camera_id: str
    last_seen_at: float
    time_synced: bool = False


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
    # Snapshot of the dashboard's `capture_mode` at arm time. Once armed
    # the session's mode is immutable — a late dashboard toggle only
    # affects the next session.
    mode: CaptureMode = _DEFAULT_CAPTURE_MODE

    @property
    def armed(self) -> bool:
        return self.ended_at is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "armed": self.armed,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "max_duration_s": self.max_duration_s,
            "uploads_received": list(self.uploads_received),
            "mode": self.mode.value,
        }
