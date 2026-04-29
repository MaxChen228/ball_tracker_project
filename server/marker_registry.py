"""Persistent 3D ArUco marker registry.

This supersedes the old plate-plane-only extended-marker map. Markers are
stored as 3D world points with metadata, while the current planar auto-cal
path can still query only the subset explicitly marked as lying on the plate
plane.
"""
from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from threading import Lock

from schemas import MarkerRecord

logger = logging.getLogger(__name__)


class MarkerRegistryDB:
    """Thread-safe persisted `{marker_id -> MarkerRecord}` store."""

    def __init__(self, data_dir: Path) -> None:
        self._lock = Lock()
        self._path = Path(data_dir) / "markers.json"
        self._legacy_path = Path(data_dir) / "extended_markers.json"
        self._markers: dict[int, MarkerRecord] = {}
        self._load()

    def _load(self) -> None:
        raw_obj: dict | None = None
        if self._path.exists():
            try:
                raw_obj = json.loads(self._path.read_text())
            except Exception:
                logger.exception(
                    "MarkerRegistryDB: failed to parse %s — starting empty",
                    self._path,
                )
                raw_obj = None
        elif self._legacy_path.exists():
            try:
                raw_obj = json.loads(self._legacy_path.read_text())
            except Exception:
                logger.exception(
                    "MarkerRegistryDB: failed to parse legacy %s — starting empty",
                    self._legacy_path,
                )
                raw_obj = None
        if raw_obj is None:
            return
        for row in raw_obj.get("markers", []):
            try:
                if "marker_id" in row or "x_m" in row:
                    rec = MarkerRecord.model_validate(row)
                else:
                    # Back-compat with old `{id, wx, wy}` shape.
                    rec = MarkerRecord(
                        marker_id=int(row["id"]),
                        x_m=float(row["wx"]),
                        y_m=float(row["wy"]),
                        z_m=0.0,
                        on_plate_plane=True,
                        source_camera_ids=[],
                    )
            except Exception:
                continue
            self._markers[rec.marker_id] = rec

    def _atomic_write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "markers": [
                rec.model_dump()
                for _, rec in sorted(self._markers.items())
            ]
        }
        tmp = self._path.with_suffix(self._path.suffix + f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    def upsert(self, record: MarkerRecord) -> MarkerRecord:
        with self._lock:
            self._markers[record.marker_id] = record
            self._atomic_write_locked()
            return record

    def remove(self, marker_id: int) -> bool:
        with self._lock:
            existed = self._markers.pop(int(marker_id), None) is not None
            if existed:
                self._atomic_write_locked()
            return existed

    def clear(self) -> int:
        with self._lock:
            n = len(self._markers)
            self._markers.clear()
            self._atomic_write_locked()
            return n

    def get(self, marker_id: int) -> MarkerRecord | None:
        with self._lock:
            rec = self._markers.get(int(marker_id))
            return rec.model_copy(deep=True) if rec is not None else None

    def all_records(self) -> list[MarkerRecord]:
        with self._lock:
            return [
                rec.model_copy(deep=True)
                for _, rec in sorted(self._markers.items())
            ]

    def planar_world_map(self) -> dict[int, tuple[float, float]]:
        with self._lock:
            return {
                rec.marker_id: (rec.x_m, rec.y_m)
                for rec in self._markers.values()
                if rec.on_plate_plane
            }
