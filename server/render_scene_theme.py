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
