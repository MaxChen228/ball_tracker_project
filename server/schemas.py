"""Wire-contract + in-memory domain model for the ball-tracker server.

This module holds the Pydantic models that define the HTTP payload shape
(`POST /pitch`, `POST /heartbeat`, triangulation results) as well as the
lightweight dataclasses backing the in-memory device registry and armed
session machine. Split out of `main.py` so the request handlers, state
container, and persistence layer can import the types without pulling in
FastAPI app plumbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field


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
    # Optional on the wire: the iPhone always omits it; server writes it
    # back to disk before triangulation.
    frames: list[FramePayload] = Field(default_factory=list)
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
        }
