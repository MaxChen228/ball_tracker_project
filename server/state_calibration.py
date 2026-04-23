from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from schemas import CalibrationSnapshot

logger = logging.getLogger("ball_tracker")

CALIBRATION_FRAME_TTL_S = 10.0


def validate_calibration_snapshot(snap: CalibrationSnapshot) -> None:
    """Gatekeep CalibrationSnapshot writes before poses are trusted."""
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


class CalibrationStore:
    """Persistent per-camera calibration snapshots."""

    def __init__(
        self,
        directory: Path,
        *,
        atomic_write: Callable[[Path, str], None],
    ) -> None:
        self._directory = directory
        self._atomic_write = atomic_write
        self._items: dict[str, CalibrationSnapshot] = {}
        self._directory.mkdir(parents=True, exist_ok=True)
        self.load()

    def path(self, camera_id: str) -> Path:
        return self._directory / f"{camera_id}.json"

    def load(self) -> None:
        for path in sorted(self._directory.glob("*.json")):
            try:
                obj = json.loads(path.read_text())
                snap = CalibrationSnapshot.model_validate(obj)
            except Exception as e:
                logger.warning("skip corrupt calibration file %s: %s", path.name, e)
                continue
            try:
                validate_calibration_snapshot(snap)
            except ValueError as e:
                logger.warning(
                    "skip inconsistent calibration %s: %s — "
                    "delete the file and re-run Auto Calibrate",
                    path.name, e,
                )
                continue
            self._items[snap.camera_id] = snap
        if self._items:
            logger.info(
                "restored %d camera calibration(s) from %s",
                len(self._items),
                self._directory,
            )

    def set(self, snapshot: CalibrationSnapshot) -> None:
        validate_calibration_snapshot(snapshot)
        self._items[snapshot.camera_id] = snapshot
        self._atomic_write(self.path(snapshot.camera_id), snapshot.model_dump_json(indent=2))

    def snapshot(self) -> dict[str, CalibrationSnapshot]:
        return dict(self._items)

    def get(self, camera_id: str) -> CalibrationSnapshot | None:
        return self._items.get(camera_id)


class CalibrationFrameBuffer:
    """One-shot high-resolution calibration frames pushed by iOS."""

    def __init__(self, *, time_fn: Callable[[], float]) -> None:
        self._time_fn = time_fn
        self._frames: dict[str, tuple[bytes, float]] = {}
        self._requested: dict[str, float] = {}

    def request(self, camera_id: str) -> None:
        now = self._time_fn()
        self._requested[camera_id] = now + CALIBRATION_FRAME_TTL_S

    def is_requested(self, camera_id: str) -> bool:
        now = self._time_fn()
        exp = self._requested.get(camera_id)
        if exp is None:
            return False
        if now >= exp:
            self._requested.pop(camera_id, None)
            return False
        return True

    def requested_ids(self) -> list[str]:
        return [cam for cam in list(self._requested.keys()) if self.is_requested(cam)]

    def store(self, camera_id: str, jpeg_bytes: bytes) -> None:
        now = self._time_fn()
        self._frames[camera_id] = (jpeg_bytes, now)
        self._requested.pop(camera_id, None)

    def consume(
        self,
        camera_id: str,
        *,
        max_age_s: float = CALIBRATION_FRAME_TTL_S,
    ) -> tuple[bytes, float] | None:
        now = self._time_fn()
        got = self._frames.pop(camera_id, None)
        if got is None:
            return None
        _, ts = got
        if now - ts > max_age_s:
            return None
        return got


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
    events: list[dict[str, Any]] = field(default_factory=list)

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
            "events": [dict(ev) for ev in (self.events or [])],
        }


class AutoCalibrationRunStore:
    """In-memory auto-calibration run status for dashboard polling."""

    def __init__(self, *, time_fn: Callable[[], float]) -> None:
        self._time_fn = time_fn
        self._active: dict[str, AutoCalibrationRun] = {}
        self._last: dict[str, AutoCalibrationRun] = {}

    def start(self, camera_id: str) -> AutoCalibrationRun:
        now = self._time_fn()
        current = self._active.get(camera_id)
        if current is not None and current.status not in {"completed", "failed"}:
            raise ValueError(f"auto calibration already running for camera {camera_id}")
        run = AutoCalibrationRun(
            id=f"acr_{secrets.token_hex(4)}",
            camera_id=camera_id,
            status="searching",
            started_at=now,
            updated_at=now,
            summary="Requesting full-res frame",
        )
        self._active[camera_id] = run
        return AutoCalibrationRun(**run.to_dict())

    def update(self, camera_id: str, **updates: Any) -> AutoCalibrationRun | None:
        now = self._time_fn()
        run = self._active.get(camera_id)
        if run is None:
            return None
        for key, value in updates.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.updated_at = now
        return AutoCalibrationRun(**run.to_dict())

    def append_event(
        self,
        camera_id: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        now = self._time_fn()
        run = self._active.get(camera_id)
        if run is None:
            return
        ev: dict[str, Any] = {
            "t": round(now - run.started_at, 3),
            "level": level,
            "msg": message,
        }
        if data:
            ev["data"] = data
        run.events.append(ev)
        run.updated_at = now

    def finish(
        self,
        camera_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        summary: str | None = None,
        detail: str | None = None,
        applied: bool | None = None,
    ) -> AutoCalibrationRun | None:
        now = self._time_fn()
        run = self._active.get(camera_id)
        if run is None:
            return None
        run.status = status
        run.updated_at = now
        run.result = result
        if summary is not None:
            run.summary = summary
        if detail is not None:
            run.detail = detail
        if applied is not None:
            run.applied = applied
        run.events.append({
            "t": round(now - run.started_at, 3),
            "level": "error" if status == "failed" else "info",
            "msg": f"finish status={status}",
            "data": {"detail": detail, "summary": summary, "applied": applied},
        })
        snap = AutoCalibrationRun(**run.to_dict())
        self._last[camera_id] = snap
        if status in {"completed", "failed"}:
            self._active.pop(camera_id, None)
        return snap

    def status(self) -> dict[str, Any]:
        active = {cam: run.to_dict() for cam, run in self._active.items()}
        last = {cam: run.to_dict() for cam, run in self._last.items()}
        return {"active": active, "last": last}
