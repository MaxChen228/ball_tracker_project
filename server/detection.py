"""Server-side ball detection.

The iPhone uploads a raw H.264 MOV per cycle; this module runs an HSV
threshold → connected-components → largest-blob pipeline on each decoded
frame to recover `(px, py)` in image space. The result feeds `pipeline.py`
which synthesises `FramePayload`s for the existing `triangulate_cycle` path.

HSV defaults target the deep-blue baseball the physical rig was built for.
Override via the `BALL_TRACKER_HSV_RANGE` env var (comma-separated
`hMin,hMax,sMin,sMax,vMin,vMax`) if you change the ball.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HSVRange:
    h_min: int
    h_max: int
    s_min: int
    s_max: int
    v_min: int
    v_max: int

    @classmethod
    def default(cls) -> "HSVRange":
        # Matches the historical Swift-side defaults: deep-blue baseball.
        return cls(h_min=100, h_max=130, s_min=140, s_max=255, v_min=40, v_max=255)

    @classmethod
    def from_env(cls) -> "HSVRange":
        raw = os.environ.get("BALL_TRACKER_HSV_RANGE", "").strip()
        if not raw:
            return cls.default()
        try:
            parts = [int(x) for x in raw.split(",")]
            if len(parts) != 6:
                raise ValueError(f"expected 6 ints, got {len(parts)}")
            return cls(*parts)
        except Exception as e:
            logger.warning(
                "BALL_TRACKER_HSV_RANGE=%r parse failed (%s) — using default deep-blue",
                raw, e,
            )
            return cls.default()

    def lo(self) -> np.ndarray:
        return np.array([self.h_min, self.s_min, self.v_min], dtype=np.uint8)

    def hi(self) -> np.ndarray:
        return np.array([self.h_max, self.s_max, self.v_max], dtype=np.uint8)


# Minimum / maximum area (in pixels) a candidate blob must have to be
# considered a ball. Same bounds as the Swift implementation used.
_MIN_AREA_PX = 20
_MAX_AREA_PX = 150_000


def detect_ball(
    frame_bgr: np.ndarray,
    hsv_range: HSVRange,
) -> tuple[float, float] | None:
    """Find the largest HSV-masked blob whose area is within
    `[MIN_AREA_PX, MAX_AREA_PX]` and return its centroid as
    `(px, py)` in **pixel** coordinates (column, row). Returns `None` if no
    blob satisfies the filter.

    Simple-minded on purpose: no morphological ops, no temporal smoothing.
    The triangulator already tolerates a handful of false positives via the
    per-frame drop log; anything more aggressive belongs in a follow-up
    ML-based detector.
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return None
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, hsv_range.lo(), hsv_range.hi())

    # Connected components with stats (label 0 is the background).
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return None

    best_idx = -1
    best_area = -1
    for idx in range(1, num_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < _MIN_AREA_PX or area > _MAX_AREA_PX:
            continue
        if area > best_area:
            best_area = area
            best_idx = idx

    if best_idx < 0:
        return None

    cx, cy = centroids[best_idx]
    return float(cx), float(cy)
