"""Wire-schema validators that catch operator typos at the boundary
before they silently degrade detection.

`HSVRangePayload` enforces `*_min <= *_max` per axis: inverted bounds
(e.g. `h_min=120, h_max=50` from a copy-paste typo on POST /presets)
would otherwise collapse `cv2.inRange` to an all-zero mask and yield
0 candidates without any signal that the config was wrong.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas import HSVRangePayload


def test_hsv_range_payload_rejects_inverted_h_bounds():
    with pytest.raises(ValidationError, match=r"h_min .* > h_max"):
        HSVRangePayload(h_min=120, h_max=50, s_min=0, s_max=255,
                        v_min=0, v_max=255)


def test_hsv_range_payload_rejects_inverted_s_bounds():
    with pytest.raises(ValidationError, match=r"s_min .* > s_max"):
        HSVRangePayload(h_min=0, h_max=179, s_min=200, s_max=100,
                        v_min=0, v_max=255)


def test_hsv_range_payload_rejects_inverted_v_bounds():
    with pytest.raises(ValidationError, match=r"v_min .* > v_max"):
        HSVRangePayload(h_min=0, h_max=179, s_min=0, s_max=255,
                        v_min=200, v_max=100)


def test_hsv_range_payload_accepts_equal_bounds():
    # Degenerate but legal: a single-value cube. cv2.inRange still
    # produces a meaningful (if narrow) mask.
    p = HSVRangePayload(h_min=100, h_max=100, s_min=128, s_max=128,
                        v_min=64, v_max=64)
    assert p.h_min == p.h_max == 100
