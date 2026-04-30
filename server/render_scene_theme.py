"""Shared design tokens and geometry constants for scene/viewer rendering."""
from __future__ import annotations

from palette import (
    _ACCENT,
    _BG,
    _BORDER_BASE,
    _BORDER_L,
    _CONTRA,
    _DEV,
    _DUAL,
    _INK,
    _OK,
    _PENDING,
    _SUB,
    _SURFACE,
)

# Module-local: dim ink for the up-axis on camera triads + world Z axis.
# Was previously `rgba(42,37,32,0.4)` but THREE.Color drops alpha (warning
# in console, render was opaque anyway). Kept as hex; if a true translucent
# axis is wanted, set `transparent:true, opacity:X` on the line material.
_INK_40 = _INK

# Chain-filter ghost-mode colors. Rejected detections stay drawn but in
# these distinct hues so operators can see what the filter removed and
# decide if thresholds are right. Opacity is applied at render time.
_GHOST_FLICKER = "#F59E0B"  # amber — "appeared & disappeared" 1-2 frame noise
_GHOST_JUMP = "#EF4444"     # red   — "ray direction jumped" past max_jump_px
_GHOST_OPACITY = 0.25
_GHOST_LINE_WIDTH = 1.5

_CAMERA_COLORS = {
    "A": _CONTRA,
    "B": _DUAL,
}
_FALLBACK_CAMERA_COLOR = _SUB

_GROUND_HALF_EXTENT_M = 0.6
_WORLD_AXIS_LEN_M = 0.3
_CAMERA_AXIS_LEN_M = 0.25
_CAMERA_FORWARD_ARROW_M = 0.5

_PLATE_WIDTH_M = 0.432
_PLATE_SHOULDER_Y_M = 0.216
_PLATE_TIP_Y_M = 0.432
_PLATE_X = [
    -_PLATE_WIDTH_M / 2,
    +_PLATE_WIDTH_M / 2,
    +_PLATE_WIDTH_M / 2,
    0.0,
    -_PLATE_WIDTH_M / 2,
]
_PLATE_Y = [
    0.0,
    0.0,
    _PLATE_SHOULDER_Y_M,
    _PLATE_TIP_Y_M,
    _PLATE_SHOULDER_Y_M,
]

# Strike zone: a rectangular prism directly above the plate. Width and
# depth match the plate footprint (X half = plate half width = 0.216 m,
# Y from front edge 0.0 to back tip 0.432 m). Vertical bounds default
# to MLB's adult average — knee hollow ~0.46 m to midpoint between
# shoulder and pants top ~1.06 m. Hard-coded for now; surface later as a
# settings toggle if per-batter heights start mattering.
_STRIKE_ZONE_X_HALF_M = _PLATE_WIDTH_M / 2
_STRIKE_ZONE_Y_FRONT_M = 0.0
_STRIKE_ZONE_Y_BACK_M = _PLATE_TIP_Y_M
_STRIKE_ZONE_Z_BOTTOM_M = 0.46
_STRIKE_ZONE_Z_TOP_M = 1.06
_STRIKE_ZONE_COLOR = _ACCENT
_STRIKE_ZONE_LINE_WIDTH = 3
_STRIKE_ZONE_FILL_OPACITY = 0.06
