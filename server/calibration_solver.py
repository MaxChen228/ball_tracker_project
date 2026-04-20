"""Server-side Auto-ArUco calibration solver.

Pure-function helpers that Phase 5's `/calibration/auto` endpoint wires
up. Ported from the iOS `BTArucoDetector` + `CalibrationShared` pair so
server and phone stay bit-compatible if we ever need to cross-check.

World frame matches the rest of the codebase: X = plate left/right,
Y = plate depth (front→back), Z = up. The home-plate plane is Z=0, so
the 6 markers are 2D points.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

# Home-plate dimensions (metres) — mirrored from
# `ball_tracker/CalibrationShared.swift`. Do NOT change without updating
# the iOS constants too (or the two solvers produce divergent homographies
# that can't be compared side-by-side).
_PLATE_WIDTH_M = 0.432       # 17" front edge
_PLATE_SHOULDER_Y_M = 0.216  # 8.5" back to shoulder
_PLATE_TIP_Y_M = 0.432       # 17" back to back tip

# DICT_4X4_50, IDs 0-5 on the 6 plate landmarks. Mirrors
# `CalibrationShared.markerWorldPoints`. Units: metres.
PLATE_MARKER_WORLD: dict[int, tuple[float, float]] = {
    0: (-_PLATE_WIDTH_M / 2.0, 0.0),                 # FL
    1: ( _PLATE_WIDTH_M / 2.0, 0.0),                 # FR
    2: ( _PLATE_WIDTH_M / 2.0, _PLATE_SHOULDER_Y_M), # RS
    3: (-_PLATE_WIDTH_M / 2.0, _PLATE_SHOULDER_Y_M), # LS
    4: ( 0.0, _PLATE_TIP_Y_M),                       # BT (back tip)
    5: ( 0.0, 0.0),                                  # MF (mid-front)
}
_ALL_MARKER_IDS = tuple(sorted(PLATE_MARKER_WORLD.keys()))

# Minimum marker count for a usable solve. Allow one missing out of 6
# (matches iOS AutoCalibrationViewController behaviour) — RANSAC still has
# 5 correspondences plus outlier rejection slack.
_MIN_MARKERS_FOR_SOLVE = 5


@dataclass(frozen=True)
class DetectedMarker:
    id: int
    corners: np.ndarray  # shape (4, 2), image pixels


@dataclass(frozen=True)
class CalibrationSolveResult:
    homography_row_major: list[float]  # 9 floats, h33 normalized to 1
    detected_ids: list[int]
    missing_ids: list[int]
    image_width_px: int
    image_height_px: int


def detect_plate_markers(bgr_image: np.ndarray) -> list[DetectedMarker]:
    """Run ArUco (DICT_4X4_50) detection on a BGR image and return only
    markers with IDs in the plate-landmark set (0-5). Extra / unknown IDs
    are silently dropped so a stray marker in the background can't poison
    the homography solve."""
    return [m for m in detect_all_markers_in_dict(bgr_image)
            if m.id in PLATE_MARKER_WORLD]


def detect_all_markers_in_dict(bgr_image: np.ndarray) -> list[DetectedMarker]:
    """Run ArUco (DICT_4X4_50) detection and return every detected marker
    (IDs 0-49). Used by Phase 5's extended-markers registration + the
    generalised `solve_homography_from_world_map` so operators can tape
    additional markers on the plate plane as landmarks beyond IDs 0-5."""
    if bgr_image.ndim != 3 or bgr_image.shape[2] != 3:
        raise ValueError(f"expected BGR image, got shape {bgr_image.shape}")
    gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(aruco_dict, params)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None:
        return []
    results: list[DetectedMarker] = []
    for corner_set, marker_id in zip(corners, ids.flatten().tolist()):
        # corner_set shape (1, 4, 2) → (4, 2)
        pts = np.asarray(corner_set, dtype=np.float64).reshape(4, 2)
        results.append(DetectedMarker(id=int(marker_id), corners=pts))
    return results


def solve_homography(
    detected: Iterable[DetectedMarker],
    image_size: tuple[int, int],
) -> CalibrationSolveResult | None:
    """Solve plate→image homography from detected markers using RANSAC.

    Each marker contributes its centroid (mean of 4 corners) as a single
    point correspondence — mirrors the iOS path where 4 corners collapse
    to one centroid per marker. Returns None when fewer than
    `_MIN_MARKERS_FOR_SOLVE` plate markers were detected.

    `image_size` is `(width_px, height_px)` of the source image, cached
    into the result so callers can stamp `CalibrationSnapshot.image_*_px`
    without re-measuring.
    """
    markers_by_id = {m.id: m for m in detected}
    detected_ids = sorted(markers_by_id.keys())
    missing_ids = [i for i in _ALL_MARKER_IDS if i not in markers_by_id]
    if len(detected_ids) < _MIN_MARKERS_FOR_SOLVE:
        return None

    world_pts = np.array(
        [PLATE_MARKER_WORLD[i] for i in detected_ids],
        dtype=np.float64,
    )
    image_pts = np.array(
        [markers_by_id[i].corners.mean(axis=0) for i in detected_ids],
        dtype=np.float64,
    )

    # cv2.findHomography with RANSAC. Threshold 3 px is conservative —
    # the synthetic-projection tests hit sub-pixel RMS, but real field
    # conditions (motion blur, mild defocus) easily spread centroids 1-2 px.
    H, mask = cv2.findHomography(world_pts, image_pts, cv2.RANSAC, 3.0)
    if H is None:
        return None
    # Normalize h33 to 1 so downstream consumers (server pairing, dashboard
    # preview canvas) can assume a canonical representation.
    if abs(H[2, 2]) < 1e-12:
        return None
    H = H / H[2, 2]
    return CalibrationSolveResult(
        homography_row_major=H.flatten().tolist(),
        detected_ids=detected_ids,
        missing_ids=missing_ids,
        image_width_px=int(image_size[0]),
        image_height_px=int(image_size[1]),
    )


def solve_homography_from_world_map(
    detected: Iterable[DetectedMarker],
    world_map: dict[int, tuple[float, float]],
    image_size: tuple[int, int],
) -> CalibrationSolveResult | None:
    """Generalised homography solve — like `solve_homography` but accepts an
    arbitrary `world_map` (plate markers ∪ extended markers). Needs ≥5 of
    the detected markers to appear as keys in `world_map`; returns None
    otherwise. Detected markers whose IDs are absent from `world_map` are
    silently dropped (same policy as `detect_plate_markers` → unknown IDs
    can't poison the solve).

    `missing_ids` in the result is always relative to the plate-landmark
    set (IDs 0-5) so dashboards keep a stable "which plate marker did we
    miss?" signal regardless of how many extended markers were in play."""
    markers_by_id = {m.id: m for m in detected if m.id in world_map}
    detected_ids = sorted(markers_by_id.keys())
    missing_ids = [i for i in _ALL_MARKER_IDS if i not in markers_by_id]
    if len(detected_ids) < _MIN_MARKERS_FOR_SOLVE:
        return None

    world_pts = np.array(
        [world_map[i] for i in detected_ids],
        dtype=np.float64,
    )
    image_pts = np.array(
        [markers_by_id[i].corners.mean(axis=0) for i in detected_ids],
        dtype=np.float64,
    )
    H, _mask = cv2.findHomography(world_pts, image_pts, cv2.RANSAC, 3.0)
    if H is None:
        return None
    if abs(H[2, 2]) < 1e-12:
        return None
    H = H / H[2, 2]
    return CalibrationSolveResult(
        homography_row_major=H.flatten().tolist(),
        detected_ids=detected_ids,
        missing_ids=missing_ids,
        image_width_px=int(image_size[0]),
        image_height_px=int(image_size[1]),
    )


def derive_fov_intrinsics(
    image_width_px: int,
    image_height_px: int,
    horizontal_fov_rad: float,
) -> tuple[float, float, float, float]:
    """Pinhole FOV-approximation intrinsics. Mirrors
    `CalibrationShared.persistFovIntrinsicsIfPossible` on iOS so server-
    derived intrinsics match what the phone used to persist.

    Returns `(fx, fy, cx, cy)`. Note the server uses OpenCV naming (`fy`),
    while iOS persists the same value under the key `intrinsic_fz` — this
    is the vertical focal length in both cases.

    Caller should use this only when no ChArUco-measured intrinsics are
    on file for the camera; the ChArUco path has ~10x smaller reprojection
    error and shouldn't be overwritten.
    """
    if image_width_px <= 0 or image_height_px <= 0:
        raise ValueError("image dimensions must be positive")
    if horizontal_fov_rad <= 0 or horizontal_fov_rad >= np.pi:
        raise ValueError("horizontal_fov_rad out of (0, pi)")
    fx = (image_width_px / 2.0) / np.tan(horizontal_fov_rad / 2.0)
    vertical_fov_rad = 2.0 * np.arctan(
        np.tan(horizontal_fov_rad / 2.0)
        * (image_height_px / image_width_px)
    )
    fy = (image_height_px / 2.0) / np.tan(vertical_fov_rad / 2.0)
    cx = image_width_px / 2.0
    cy = image_height_px / 2.0
    return float(fx), float(fy), float(cx), float(cy)
