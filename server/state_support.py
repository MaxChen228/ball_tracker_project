from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from schemas import CalibrationSnapshot


def new_session_id() -> str:
    return "s_" + secrets.token_hex(4)


def new_sync_id() -> str:
    return "sy_" + secrets.token_hex(4)


def validate_calibration_snapshot(snap: CalibrationSnapshot) -> None:
    w, h = snap.image_width_px, snap.image_height_px
    if w <= 0 or h <= 0:
        raise ValueError(f"invalid image dims {w}x{h}")
    k = snap.intrinsics
    if k.fx <= 0 or k.fz <= 0:
        raise ValueError(f"non-positive focal length fx={k.fx} fy={k.fz}")
    if max(k.fx, k.fz) / min(k.fx, k.fz) > 2.0:
        raise ValueError(f"fx/fy ratio out of bounds: fx={k.fx} fy={k.fz}")
    if not (-0.05 * w <= k.cx <= 1.05 * w):
        raise ValueError(
            f"cx={k.cx} outside image width {w} — K likely from a "
            f"different resolution than image_dims claim"
        )
    if not (-0.05 * h <= k.cy <= 1.05 * h):
        raise ValueError(
            f"cy={k.cy} outside image height {h} — K likely from a "
            f"different resolution than image_dims claim"
        )
    h_flat = snap.homography
    if len(h_flat) != 9 or abs(h_flat[8]) < 1e-9:
        raise ValueError(
            f"degenerate homography: h33={h_flat[8] if len(h_flat) == 9 else 'wrong length'}"
        )


@dataclass
class LegacyTimeSyncIntent:
    id: str
    started_at: float
    expires_at: float


@dataclass
class AutoCalibrationRun:
    id: str
    camera_id: str
    status: str
    started_at: float
    updated_at: float
    frames_seen: int = 0
    good_frames: int = 0
    stable_frames: int = 0
    markers_visible: int = 0
    solver: str | None = None
    reprojection_px: float | None = None
    position_jitter_cm: float | None = None
    angle_jitter_deg: float | None = None
    applied: bool = False
    summary: str | None = None
    detail: str | None = None
    detected_ids: list[int] | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "frames_seen": self.frames_seen,
            "good_frames": self.good_frames,
            "stable_frames": self.stable_frames,
            "markers_visible": self.markers_visible,
            "solver": self.solver,
            "reprojection_px": self.reprojection_px,
            "position_jitter_cm": self.position_jitter_cm,
            "angle_jitter_deg": self.angle_jitter_deg,
            "applied": self.applied,
            "summary": self.summary,
            "detail": self.detail,
            "detected_ids": list(self.detected_ids or []),
            "result": dict(self.result or {}),
        }
