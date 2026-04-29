"""Single source of truth for detection presets.

A preset bundles the **full detection-config triple** — HSVRange +
ShapeGate + CandidateSelectorTuning — so switching presets is a single
atomic operation that produces a reproducible config, and a frozen
pitch can be tagged with the preset that generated it. Earlier the
preset only carried HSV; shape_gate / selector were silently inherited
from current state during a `source=preset:NAME` reprocess, which
defeated the whole point of "rerun s_xxx with blue_ball" being
disk-independent.
"""
from __future__ import annotations

from dataclasses import dataclass

from candidate_selector import CandidateSelectorTuning
from detection import HSVRange, ShapeGate


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    hsv: HSVRange
    shape_gate: ShapeGate
    selector: CandidateSelectorTuning


PRESETS: dict[str, Preset] = {
    "tennis": Preset(
        name="tennis",
        label="Tennis",
        # All three bound to module defaults so retuning a default
        # auto-propagates to the preset and there's no drift to chase.
        hsv=HSVRange.default(),
        shape_gate=ShapeGate.default(),
        selector=CandidateSelectorTuning.default(),
    ),
    "blue_ball": Preset(
        name="blue_ball",
        label="Blue ball",
        # Project ball — deep-blue hardball. h tightened to 105-112 on
        # 2026-04-29 to filter background blue; v_min ≥ 40 required
        # because the ball's shaded underside drops to V~80 and lifting
        # v_min carves the mask into a crescent that fails aspect.
        hsv=HSVRange(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255),
        # Tighter aspect (0.75 vs default 0.70): the project ball is
        # rounder than a tennis ball — minimal motion blur ellipsing on
        # the rig — so we can afford a stricter circularity floor that
        # rejects more clutter (0.63-0.70 fill range observed at p50;
        # see CLAUDE.md tuning baselines).
        shape_gate=ShapeGate(aspect_min=0.75, fill_min=0.55),
        # Same selector weights as default — there's no rationale to
        # weight aspect/fill differently for the blue ball; the cost
        # function operates on already-normalized residuals.
        selector=CandidateSelectorTuning.default(),
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
