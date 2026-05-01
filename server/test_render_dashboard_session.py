from __future__ import annotations

import pytest

from presets import Preset
from render_dashboard_session import _render_hsv_body


def _preset(name: str = "tennis") -> Preset:
    from detection import HSVRange, ShapeGate

    return Preset(
        name=name,
        label="Tennis",
        hsv=HSVRange(h_min=25, h_max=55, s_min=90, s_max=255, v_min=90, v_max=255),
        shape_gate=ShapeGate(aspect_min=0.70, fill_min=0.55),
        algorithm_id="hsv_cc_shape_v1",
    )


def test_render_hsv_body_none_uses_boot_defaults():
    html = _render_hsv_body(None, [_preset()])
    assert 'value="25"' in html
    assert 'value="55"' in html


def test_render_hsv_body_rejects_partial_config():
    with pytest.raises(KeyError, match="missing required"):
        _render_hsv_body(
            {
                "hsv": {
                    "h_min": 25,
                    "h_max": 55,
                    "s_min": 90,
                    "s_max": 255,
                    "v_min": 90,
                    # missing v_max
                },
                "shape_gate": {
                    "aspect_min": 0.70,
                    "fill_min": 0.55,
                },
            },
            [_preset()],
        )


def test_render_hsv_body_rejects_missing_identity_fields():
    cfg = {
        "hsv": {
            "h_min": 25,
            "h_max": 55,
            "s_min": 90,
            "s_max": 255,
            "v_min": 90,
            "v_max": 255,
        },
        "shape_gate": {
            "aspect_min": 0.70,
            "fill_min": 0.55,
        },
        "preset": "tennis",
    }
    with pytest.raises(KeyError, match="modified_fields"):
        _render_hsv_body(cfg, [_preset()])


def test_render_hsv_body_rejects_non_list_modified_fields():
    cfg = {
        "hsv": {
            "h_min": 25,
            "h_max": 55,
            "s_min": 90,
            "s_max": 255,
            "v_min": 90,
            "v_max": 255,
        },
        "shape_gate": {
            "aspect_min": 0.70,
            "fill_min": 0.55,
        },
        "preset": "tennis",
        "modified_fields": "hsv.h_min",
    }
    with pytest.raises(TypeError, match="modified_fields"):
        _render_hsv_body(cfg, [_preset()])
