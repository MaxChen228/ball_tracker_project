"""Single source of truth for HSV detection presets.

Historically two independent `_HSV_PRESETS` dicts existed (one in
`routes/settings.py` typed as `HSVRange`, another in
`render_dashboard_session.py` as plain dicts). They drifted in style
and risked drifting in values. This module is the canonical registry
— both consumers (apply-preset endpoint, dashboard preset buttons)
import from here.

A "preset" today is HSV-only. ShapeGate / CandidateSelectorTuning are
independent runtime knobs without preset semantics; if that changes,
extend `Preset` to carry the full triple.
"""
from __future__ import annotations

from dataclasses import dataclass

from detection import HSVRange


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    hsv: HSVRange


PRESETS: dict[str, Preset] = {
    "tennis": Preset(
        name="tennis",
        label="Tennis",
        # Bound to `HSVRange.default()` rather than redeclared so the two
        # cannot drift if the default is ever retuned. (`HSVRange.default`
        # is the headless-boot fallback when `data/hsv_range.json` is
        # absent; "tennis preset" and "default" are conceptually the
        # same thing — yellow-green tennis ball.)
        hsv=HSVRange.default(),
    ),
    "blue_ball": Preset(
        name="blue_ball",
        label="Blue ball",
        # Project ball — deep-blue hardball. h tightened to 105-112 on
        # 2026-04-29 to filter background blue; v_min ≥ 40 required
        # because the ball's shaded underside drops to V~80 and lifting
        # v_min carves the mask into a crescent that fails aspect.
        hsv=HSVRange(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
    ),
}


def get_preset(name: str) -> Preset:
    """Return preset by name. Raises `KeyError` if unknown — callers at
    the API boundary translate to HTTP 400."""
    return PRESETS[name]


def hsv_as_dict(preset: Preset) -> dict[str, int]:
    """Wire/render shape: dict of the 6 HSV ints. Used by the dashboard
    HSV card buttons (rendered as `data-*` attributes for the JS slider
    sync) and any JSON-emitting endpoint."""
    return {
        "h_min": preset.hsv.h_min,
        "h_max": preset.hsv.h_max,
        "s_min": preset.hsv.s_min,
        "s_max": preset.hsv.s_max,
        "v_min": preset.hsv.v_min,
        "v_max": preset.hsv.v_max,
    }
