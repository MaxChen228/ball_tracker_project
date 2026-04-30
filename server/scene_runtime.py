"""Server-side bridge for the Three.js 3D scene runtime.

`server/static/threejs/scene_runtime.js` is shared between dashboard
and viewer; it builds the static layers (ground / plate / strike zone /
world axes) directly from a JSON payload that this module emits. Doing
this in Python rather than hardcoding the constants in JS keeps a
single source of truth (`render_scene_theme.py`) — the same values
that drive trace colours, strike zone bounds, etc.

The Plotly-era 3D pipeline (`render_scene.py`, `render_scene_static.py`,
`render_scene_layout.py`) is being retired alongside this migration.
Until phase 4 cleanup ships, those modules still exist for the
calibration auto-cal preview path; new code should import from here.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

from render_scene_theme import (
    _ACCENT,
    _BG,
    _BORDER_BASE,
    _BORDER_L,
    _CAMERA_AXIS_LEN_M,
    _CAMERA_COLORS,
    _CAMERA_FORWARD_ARROW_M,
    _CONTRA,
    _DEV,
    _FALLBACK_CAMERA_COLOR,
    _GROUND_HALF_EXTENT_M,
    _INK,
    _INK_40,
    _PLATE_X,
    _PLATE_Y,
    _STRIKE_ZONE_COLOR,
    _STRIKE_ZONE_FILL_OPACITY,
    _STRIKE_ZONE_LINE_WIDTH,
    _STRIKE_ZONE_X_HALF_M,
    _STRIKE_ZONE_Y_BACK_M,
    _STRIKE_ZONE_Y_FRONT_M,
    _STRIKE_ZONE_Z_BOTTOM_M,
    _STRIKE_ZONE_Z_TOP_M,
    _SUB,
    _SURFACE,
    _WORLD_AXIS_LEN_M,
)


_VENDOR_DIR = Path(__file__).parent / "static" / "threejs" / "vendor"
_RUNTIME_DIR = Path(__file__).parent / "static" / "threejs"


def scene_theme() -> dict:
    """Constants the JS scene needs to render the static layers.

    Strike zone, plate, ground extent, axis lengths, colours. JSON-safe
    primitives only — JS reads via `JSON.parse(textContent)` on a
    `<script type="application/json">` block injected by the page
    renderer."""
    return {
        "colors": {
            "bg": _BG,
            "surface": _SURFACE,
            "ink": _INK,
            "ink_40": _INK_40,
            "sub": _SUB,
            "border_base": _BORDER_BASE,
            "border_l": _BORDER_L,
            "accent": _ACCENT,
            "dev": _DEV,
            "contra": _CONTRA,
            "strike_zone": _STRIKE_ZONE_COLOR,
            "fallback_camera": _FALLBACK_CAMERA_COLOR,
        },
        "camera_colors": dict(_CAMERA_COLORS),
        "ground": {
            "half_extent_m": _GROUND_HALF_EXTENT_M,
        },
        "plate": {
            "x": list(_PLATE_X),
            "y": list(_PLATE_Y),
        },
        "strike_zone": {
            "x_half_m": _STRIKE_ZONE_X_HALF_M,
            "y_front_m": _STRIKE_ZONE_Y_FRONT_M,
            "y_back_m": _STRIKE_ZONE_Y_BACK_M,
            "z_bottom_m": _STRIKE_ZONE_Z_BOTTOM_M,
            "z_top_m": _STRIKE_ZONE_Z_TOP_M,
            "fill_opacity": _STRIKE_ZONE_FILL_OPACITY,
            "line_width": _STRIKE_ZONE_LINE_WIDTH,
        },
        "axes": {
            "world_len_m": _WORLD_AXIS_LEN_M,
            "camera_axis_len_m": _CAMERA_AXIS_LEN_M,
            "camera_forward_len_m": _CAMERA_FORWARD_ARROW_M,
        },
    }


def scene_theme_json() -> str:
    """Serialise `scene_theme()` for embedding in a `<script>` block."""
    return _json.dumps(scene_theme())


def scene_runtime_html(*, container_id: str = "scene") -> str:
    """Return the HTML fragment that boots the Three.js scene runtime.

    Includes:
      - import map pinning ``three`` to the vendored ESM bundle
      - `<script type="application/json" id="bt-scene-theme">` payload
      - module-type `<script>` that imports
        ``/static/threejs/scene_runtime.js`` and constructs the scene

    The scene mounts itself onto an element with ``id=container_id``;
    the caller is responsible for placing that element in the page DOM.
    """
    theme = scene_theme_json().replace("</", "<\\/")
    return (
        '<script type="importmap">'
        '{"imports":{"three":"/static/threejs/vendor/three.module.min.js",'
        '"three/addons/controls/OrbitControls.js":"/static/threejs/vendor/OrbitControls.js"}}'
        "</script>"
        f'<script type="application/json" id="bt-scene-theme">{theme}</script>'
        '<script type="module">'
        'import { mountScene } from "/static/threejs/scene_runtime.js";'
        f'mountScene({_json.dumps(container_id)});'
        '</script>'
    )


def assert_scene_runtime_present(html: str) -> None:
    """Sanity-check that a rendered page actually injected the runtime.

    Mirrors `assert_overlays_present` for the older runtime — catches
    silent regressions where a refactor drops the kwarg / moves the
    script tag and the scene quietly fails to mount."""
    if "bt-scene-theme" not in html or "scene_runtime.js" not in html:
        raise AssertionError("rendered page missing Three.js scene runtime injection")


def vendor_files_present() -> bool:
    """True iff the vendored Three.js + OrbitControls are on disk.

    Used by the import-time check in `main.py`/equivalent so the
    server fails loudly at boot if the migration vendor drop wasn't
    committed alongside the Python changes."""
    return (
        (_VENDOR_DIR / "three.module.min.js").exists()
        and (_VENDOR_DIR / "OrbitControls.js").exists()
        and (_RUNTIME_DIR / "scene_runtime.js").exists()
    )
