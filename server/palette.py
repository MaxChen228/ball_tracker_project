"""PHYSICS_LAB palette — single source of truth for the warm-neutral hex
constants shared across `render_shared.py`, `render_scene_theme.py`, and
the various per-page renderers. Values match render_shared.py prior to
the M3 dedup; render_scene_theme.py duplicated the same hexes and now
re-imports from here."""
from __future__ import annotations


# Warm-neutral surface palette (PHYSICS_LAB design tokens).
_BG = "#F8F7F4"
_SURFACE = "#FCFBFA"
_SURFACE_HOVER = "#F3F0EA"

# Ink + sub-text.
_INK = "#2A2520"
_SUB = "#7A756C"
_INK_LIGHT = "#5A5550"

# Borders.
_BORDER_BASE = "#DBD6CD"
_BORDER_L = "#E8E4DB"

# Semantic accents.
_DEV = "#C0392B"
_CONTRA = "#4A6B8C"
_DUAL = "#D35400"
_ACCENT = "#E6B300"

# Status hues used by the scene/viewer renderers; the dashboard CSS uses
# the var(--passed)/var(--warn) wash variants instead, but the raw hexes
# still live here so the Plotly layer can reach them without depending
# on the dashboard stylesheet.
_OK = "#3D7B5F"
_PENDING = "#D49A1F"
