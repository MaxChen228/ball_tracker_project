"""Persistent device_uuid → camera_id assignments (Phase 0 foundation).

Multi-camera rig support replaces hardcoded `cameraRole` in iOS UserDefaults
with operator-driven assignment from the dashboard. This store holds the
authoritative `device_uuid (identifierForVendor) → camera_id` mapping that
survives server restart.

In PR1 this store is purely additive: REST endpoints in `routes/devices.py`
let the operator pre-create assignments. PR2 will wire the WS handshake to
consult this store and gate cam-id resolution on it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_DEVICE_UUID_MAX_LEN = 64
_DEVICE_MODEL_MAX_LEN = 32


class AssignmentError(ValueError):
    """Raised when an assignment violates an invariant (bad cam_id format,
    cam_id collision with a different device_uuid, etc.)."""


@dataclass(frozen=True)
class DeviceAssignment:
    device_uuid: str
    camera_id: str
    device_model: str | None
    assigned_at: float

    def to_json(self) -> dict:
        return {
            "device_uuid": self.device_uuid,
            "camera_id": self.camera_id,
            "device_model": self.device_model,
            "assigned_at": self.assigned_at,
        }

    @classmethod
    def from_json(cls, obj: dict) -> "DeviceAssignment":
        return cls(
            device_uuid=str(obj["device_uuid"]),
            camera_id=str(obj["camera_id"]),
            device_model=(
                str(obj["device_model"]) if obj.get("device_model") else None
            ),
            assigned_at=float(obj["assigned_at"]),
        )


class DeviceAssignmentStore:
    """One JSON file (`data/device_assignments.json`) holds the full map.

    Single file rather than per-device JSON because the dashboard pool view
    needs the full list on every poll, and the map is small (N cameras,
    realistically <= 8). Atomic write on every mutation; in-memory cache
    keeps reads cheap.

    Invariants enforced by `assign()`:
      - camera_id matches the wire regex `^[A-Za-z0-9_-]{1,16}$`
      - one device_uuid maps to at most one camera_id
      - one camera_id is taken by at most one device_uuid
      - re-assigning the same device_uuid to a new camera_id releases the
        old camera_id atomically (single mutation)
    """

    def __init__(
        self,
        path: Path,
        *,
        atomic_write: Callable[[Path, str], None],
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._atomic_write = atomic_write
        self._time_fn = time_fn
        # Keyed by device_uuid (primary). camera_id index built from it.
        self._by_uuid: dict[str, DeviceAssignment] = {}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            obj = json.loads(self._path.read_text())
        except json.JSONDecodeError as e:
            raise ValueError(f"{self._path} is not valid JSON: {e}") from e
        items = obj.get("assignments")
        if not isinstance(items, list):
            raise ValueError(
                f"{self._path} missing 'assignments' list — delete the file "
                f"to start over (no migration in experimental phase)"
            )
        loaded: dict[str, DeviceAssignment] = {}
        seen_cams: set[str] = set()
        for entry in items:
            rec = DeviceAssignment.from_json(entry)
            if rec.device_uuid in loaded:
                raise ValueError(
                    f"{self._path} has duplicate device_uuid {rec.device_uuid!r}"
                )
            if rec.camera_id in seen_cams:
                raise ValueError(
                    f"{self._path} has duplicate camera_id {rec.camera_id!r}"
                )
            loaded[rec.device_uuid] = rec
            seen_cams.add(rec.camera_id)
        self._by_uuid = loaded
        if self._by_uuid:
            logger.info(
                "restored %d device assignment(s) from %s",
                len(self._by_uuid),
                self._path,
            )

    def _persist(self) -> None:
        payload = {
            "assignments": [
                rec.to_json()
                for rec in sorted(
                    self._by_uuid.values(), key=lambda r: r.camera_id
                )
            ]
        }
        self._atomic_write(self._path, json.dumps(payload, indent=2))

    def assign(
        self,
        *,
        device_uuid: str,
        camera_id: str,
        device_model: str | None = None,
    ) -> DeviceAssignment:
        """Create or update an assignment.

        Raises AssignmentError if camera_id is invalid format or already
        held by a different device_uuid. If the same device_uuid is
        re-assigned to a new camera_id, the old camera_id is released as
        part of the same write.
        """
        if not isinstance(device_uuid, str) or not device_uuid:
            raise AssignmentError("device_uuid must be a non-empty string")
        device_uuid = device_uuid.strip()
        if not device_uuid or len(device_uuid) > _DEVICE_UUID_MAX_LEN:
            raise AssignmentError(
                f"device_uuid length must be 1..{_DEVICE_UUID_MAX_LEN}"
            )
        if not isinstance(camera_id, str) or not _CAMERA_ID_RE.match(camera_id):
            raise AssignmentError(
                f"camera_id {camera_id!r} fails regex {_CAMERA_ID_RE.pattern}"
            )
        if device_model is not None:
            if not isinstance(device_model, str):
                raise AssignmentError("device_model must be a string or null")
            device_model = device_model.strip()
            if not device_model:
                device_model = None
            elif len(device_model) > _DEVICE_MODEL_MAX_LEN:
                raise AssignmentError(
                    f"device_model length must be <= {_DEVICE_MODEL_MAX_LEN}"
                )

        # Collision check: cam_id held by a *different* device_uuid?
        existing_cam_holder = self.get_by_camera(camera_id)
        if existing_cam_holder is not None and existing_cam_holder.device_uuid != device_uuid:
            raise AssignmentError(
                f"camera_id {camera_id!r} already assigned to device "
                f"{existing_cam_holder.device_uuid!r} — unassign first"
            )

        rec = DeviceAssignment(
            device_uuid=device_uuid,
            camera_id=camera_id,
            device_model=device_model,
            assigned_at=self._time_fn(),
        )
        # Stage the new map BEFORE persisting so we can roll back if disk
        # write fails (otherwise an OSError would leave the in-memory state
        # ahead of disk — restart would silently revert the assignment).
        prior = self._by_uuid.get(device_uuid)
        self._by_uuid[device_uuid] = rec
        try:
            self._persist()
        except OSError:
            if prior is None:
                self._by_uuid.pop(device_uuid, None)
            else:
                self._by_uuid[device_uuid] = prior
            raise
        return rec

    def unassign_by_camera(self, camera_id: str) -> bool:
        """Remove the assignment whose camera_id matches. Returns True if
        an entry existed and was removed."""
        rec = self.get_by_camera(camera_id)
        if rec is None:
            return False
        prior = self._by_uuid.pop(rec.device_uuid)
        try:
            self._persist()
        except OSError:
            self._by_uuid[prior.device_uuid] = prior
            raise
        return True

    def unassign_by_device(self, device_uuid: str) -> bool:
        rec = self._by_uuid.get(device_uuid)
        if rec is None:
            return False
        self._by_uuid.pop(device_uuid)
        try:
            self._persist()
        except OSError:
            self._by_uuid[rec.device_uuid] = rec
            raise
        return True

    def get_by_device(self, device_uuid: str) -> DeviceAssignment | None:
        return self._by_uuid.get(device_uuid)

    def get_by_camera(self, camera_id: str) -> DeviceAssignment | None:
        for rec in self._by_uuid.values():
            if rec.camera_id == camera_id:
                return rec
        return None

    def snapshot(self) -> list[DeviceAssignment]:
        """All assignments, sorted by camera_id for stable dashboard order."""
        return sorted(self._by_uuid.values(), key=lambda r: r.camera_id)

    def clear(self) -> None:
        """Wipe all assignments (used by tests / operator reset). Persists
        an empty list rather than deleting the file so the on-disk format
        stays observable."""
        self._by_uuid.clear()
        self._persist()
