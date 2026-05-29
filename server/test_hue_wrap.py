"""Regression: HSV hue wrap-around (h_min > h_max) must not silently
yield an all-zero mask / zero detections.

Red/orange balls straddle the OpenCV hue 179→0 boundary, so their range
is expressed as e.g. h_min=170, h_max=10. A naive single `cv2.inRange`
requires (h>=h_min AND h<=h_max) per pixel with h_min>h_max → never true
→ all-zero mask → silent zero-detection (no candidate, no error). This
violates the project's no-silent-fallback rule. `_run_hsv_emit_pipeline`
must split into [h_min,179] ∪ [0,h_max] and OR the masks.

Lock-step: `ball_tracker/BallDetector.mm` carries the same split.
See docs/reference/hue-and-color.md.
"""
from __future__ import annotations

import cv2
import numpy as np

from detection import HSVRange, detect_ball


def _bgr_for_hue(h: int) -> tuple[int, int, int]:
    """A saturated/bright BGR triple at the given OpenCV hue."""
    hsv = np.uint8([[[h, 220, 220]]])
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def _blank(h: int = 480, w: int = 640) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


# Red ball straddling the wrap: accepts hues in [170,179] and [0,10].
_RED_WRAP = HSVRange(h_min=170, h_max=10, s_min=90, s_max=255, v_min=90, v_max=255)


def test_wrap_detects_high_segment_hue():
    """A circle at hue 175 (inside [170,179]) must be detected."""
    img = _blank()
    cv2.circle(img, (320, 240), 24, _bgr_for_hue(175), thickness=-1)
    result = detect_ball(img, _RED_WRAP)
    assert result is not None, "wrap range missed the [h_min,179] segment"
    cx, cy = result
    assert abs(cx - 320) < 2.0 and abs(cy - 240) < 2.0


def test_wrap_detects_low_segment_hue():
    """A circle at hue 5 (inside [0,10]) must be detected."""
    img = _blank()
    cv2.circle(img, (200, 150), 24, _bgr_for_hue(5), thickness=-1)
    result = detect_ball(img, _RED_WRAP)
    assert result is not None, "wrap range missed the [0,h_max] segment"
    cx, cy = result
    assert abs(cx - 200) < 2.0 and abs(cy - 150) < 2.0


def test_wrap_rejects_out_of_band_hue():
    """A green circle (hue ~60, well outside the wrap band) stays rejected
    — the OR must not widen the gate to everything."""
    img = _blank()
    cv2.circle(img, (320, 240), 24, _bgr_for_hue(60), thickness=-1)
    result = detect_ball(img, _RED_WRAP)
    assert result is None, "wrap OR leaked an out-of-band hue"


def test_nonwrap_unchanged():
    """Sanity: a normal (h_min<=h_max) range still detects in-band hue,
    confirming the wrap branch did not alter the single-segment path."""
    img = _blank()
    cv2.circle(img, (320, 240), 24, _bgr_for_hue(40), thickness=-1)
    result = detect_ball(img, HSVRange.default())  # tennis 25-55
    assert result is not None
