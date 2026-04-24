"""Shape-gate unit tests for `detection.detect_ball`.

Synthesizes BGR frames with controlled HSV + geometry so we can assert
the shape gate rejects elongated / sparse clutter and accepts filled
circles — no external fixtures.

NOTE on lock-step: iOS `BallDetector.mm` must carry the same
`kMinAspect` / `kMinFill` constants. These tests guard the Python end
only; when thresholds move here, sync the Obj-C++ side (Agent E /
manual) and re-verify.
"""
from __future__ import annotations

import cv2
import numpy as np

from detection import HSVRange, detect_ball


def _yellow_green_bgr() -> tuple[int, int, int]:
    """A BGR triple that sits inside HSVRange.default() (tennis)."""
    hsv = np.uint8([[[40, 200, 200]]])  # H≈40, lime-yellow
    bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0]
    return int(bgr[0]), int(bgr[1]), int(bgr[2])


def _blank(h: int = 480, w: int = 640) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_detects_clean_circle():
    """Filled yellow-green circle well inside area bounds is accepted."""
    img = _blank()
    cv2.circle(img, (320, 240), 24, _yellow_green_bgr(), thickness=-1)
    result = detect_ball(img, HSVRange.default())
    assert result is not None
    cx, cy = result
    assert abs(cx - 320) < 2.0
    assert abs(cy - 240) < 2.0


def test_rejects_thin_horizontal_strip():
    """200×10 elongated blob — aspect 0.05 — must be rejected by the
    aspect gate regardless of how bright it is."""
    img = _blank()
    cv2.rectangle(img, (200, 235), (400, 245), _yellow_green_bgr(), thickness=-1)
    result = detect_ball(img, HSVRange.default())
    assert result is None


def test_rejects_sparse_checkerboard():
    """Blob whose bbox is square-ish but fill ratio << 0.55. A 40x40
    bbox with only ~30% yellow-green pixels (random mask) should be
    dropped by the fill gate."""
    img = _blank()
    rng = np.random.default_rng(seed=42)
    yg = _yellow_green_bgr()
    for yy in range(220, 260):
        for xx in range(300, 340):
            if rng.random() < 0.30:
                img[yy, xx] = yg
    result = detect_ball(img, HSVRange.default())
    assert result is None


def test_accepts_slightly_elliptical_ball():
    """min-aspect at 0.70 should accept a mildly-squished ball (28x20
    ellipse → aspect ≈ 0.71) — this was rejected under the previous
    0.75 gate and is exactly the kind of motion-blur edge case the
    loosening targets."""
    img = _blank()
    cv2.ellipse(
        img,
        center=(320, 240),
        axes=(28, 20),  # semi-axes → bbox 56x40, aspect ≈ 0.714
        angle=0,
        startAngle=0,
        endAngle=360,
        color=_yellow_green_bgr(),
        thickness=-1,
    )
    result = detect_ball(img, HSVRange.default())
    assert result is not None


def test_accepts_fill_0p60_ring():
    """A filled circle with a small hole — bbox fill drops toward 0.60
    (inside the new 0.55 gate, would have been borderline before). The
    p50=0.68 empirical distribution says real balls often look like
    this after HSV∧fg_mask edge carving."""
    img = _blank()
    cv2.circle(img, (320, 240), 30, _yellow_green_bgr(), thickness=-1)
    # carve an off-center hole so bbox/area ratio falls but aspect stays ~1.
    cv2.circle(img, (325, 235), 14, (0, 0, 0), thickness=-1)
    result = detect_ball(img, HSVRange.default())
    assert result is not None
