"""Extended ArUco marker registry.

Phase 5 of the iOS-decoupling refactor. The operator tapes additional
DICT_4X4_50 markers (IDs 6-49) on the same plane as home plate — floor,
taped-down board, whatever — and uses the dashboard's "Register markers
from this camera" button to auto-recover their world coordinates:

  1. Grab a preview frame where the operator can see both the plate
     markers (IDs 0-5) AND the extended markers in the shot.
  2. Solve the plate homography `H_plate` from the 6 reserved markers.
  3. For each extra marker's image centroid, apply `H_plate⁻¹` to
     project it back to world (x, y) on the plate plane (z = 0).
  4. Persist `{id -> (wx, wy)}` to `data/extended_markers.json`.

Downstream, `/calibration/auto` merges plate ∪ extended into one
`world_map` and calls `solve_homography_from_world_map`. Cameras that
can't see all of the plate (e.g. a long third-base angle where markers
2 + 4 are occluded) can then still solve a usable homography off a mix
of plate + extended markers — the whole point of this feature.

Thread-safety: internal `threading.Lock` covers every read and write.
Persistence is atomic (tmp + replace) so a concurrent process can't see
a half-written file.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from threading import Lock

import numpy as np

from calibration_solver import (
    PLATE_MARKER_WORLD,
    detect_all_markers_in_dict,
    solve_homography,
)


# Plate-reserved IDs. IDs 0-5 are baked into `PLATE_MARKER_WORLD` with
# ground-truth measured world coordinates; attempting to register one as
# an extended marker would conflict with that contract.
_PLATE_RESERVED_IDS: frozenset[int] = frozenset(PLATE_MARKER_WORLD.keys())


class ExtendedMarkersDB:
    """Persisted `{marker_id: (wx, wy)}` map of operator-taped markers.

    File format (JSON): `{"markers": [{"id": 7, "wx": 0.8, "wy": -0.3}, ...]}`.
    Sorted by id on disk so version-control diffs stay readable if the
    user ever checks the registry in."""

    def __init__(self, data_dir: Path) -> None:
        self._lock = Lock()
        self._path = Path(data_dir) / "extended_markers.json"
        self._markers: dict[int, tuple[float, float]] = {}
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            obj = json.loads(self._path.read_text())
        except Exception:
            return
        for row in obj.get("markers", []):
            try:
                mid = int(row["id"])
                wx = float(row["wx"])
                wy = float(row["wy"])
            except (KeyError, TypeError, ValueError):
                continue
            if mid in _PLATE_RESERVED_IDS:
                continue
            self._markers[mid] = (wx, wy)

    def _atomic_write_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "markers": [
                {"id": mid, "wx": wx, "wy": wy}
                for mid, (wx, wy) in sorted(self._markers.items())
            ]
        }
        # Unique tmp so concurrent writers can't clobber each other.
        tmp = self._path.with_suffix(self._path.suffix + f".{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._path)

    # ---------- CRUD ----------

    def register(self, marker_id: int, world_xy: tuple[float, float]) -> None:
        if marker_id in _PLATE_RESERVED_IDS:
            raise ValueError(
                f"marker id {marker_id} is reserved for plate landmarks (0-5)"
            )
        if not (0 <= marker_id <= 49):
            raise ValueError(f"marker id {marker_id} outside DICT_4X4_50 range 0-49")
        wx, wy = float(world_xy[0]), float(world_xy[1])
        with self._lock:
            self._markers[int(marker_id)] = (wx, wy)
            self._atomic_write_locked()

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

    def all(self) -> dict[int, tuple[float, float]]:
        with self._lock:
            return dict(self._markers)

    # ---------- auto-registration from an image ----------

    def register_from_image(
        self,
        bgr_image: np.ndarray,
    ) -> dict[int, tuple[float, float]]:
        """Detect every DICT_4X4_50 marker in `bgr_image`, solve the plate
        homography from the 6 reserved IDs, then inverse-project every
        non-plate marker's centroid to world (x, y) on the plate plane.

        Returns the NEW `{id: (wx, wy)}` mapping for every marker that
        resolved — does NOT auto-persist. Caller decides whether to commit
        (dashboard flow: commit immediately; future CLI flow: show a diff
        first). Raises `ValueError` if the plate homography can't be
        solved (fewer than 5 plate markers detected in the image)."""
        all_detected = detect_all_markers_in_dict(bgr_image)
        plate_detected = [m for m in all_detected if m.id in PLATE_MARKER_WORLD]
        h_img = bgr_image.shape[0]
        w_img = bgr_image.shape[1]
        result = solve_homography(plate_detected, image_size=(w_img, h_img))
        if result is None:
            raise ValueError(
                "cannot register extended markers: need ≥5 plate markers "
                "(IDs 0-5) visible in the same image"
            )
        H = np.asarray(result.homography_row_major, dtype=np.float64).reshape(3, 3)
        try:
            H_inv = np.linalg.inv(H)
        except np.linalg.LinAlgError as e:
            raise ValueError(f"plate homography not invertible: {e}") from e

        registered: dict[int, tuple[float, float]] = {}
        for m in all_detected:
            if m.id in _PLATE_RESERVED_IDS:
                continue
            # Image-space centroid → world (x, y) on z = 0.
            cx, cy = m.corners.mean(axis=0)
            u = np.array([cx, cy, 1.0], dtype=np.float64)
            w = H_inv @ u
            if abs(w[2]) < 1e-12:
                continue
            wx = float(w[0] / w[2])
            wy = float(w[1] / w[2])
            registered[int(m.id)] = (wx, wy)
        return registered
