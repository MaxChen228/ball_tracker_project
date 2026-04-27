"""Shared design tokens and geometry constants for scene/viewer rendering."""
from __future__ import annotations

_BG = "#F8F7F4"
_SURFACE = "#FCFBFA"
_INK = "#2A2520"
_INK_40 = "rgba(42, 37, 32, 0.4)"
_SUB = "#7A756C"
_BORDER_BASE = "#DBD6CD"
_BORDER_L = "#E8E4DB"
_CONTRA = "#4A6B8C"
_DUAL = "#D35400"
_DEV = "#C0392B"
_ACCENT = "#E6B300"
_OK = "#3D7B5F"
_PENDING = "#D49A1F"

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

_GROUND_HALF_EXTENT_M = 1.5
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
