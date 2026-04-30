"""Server-side bridge for the Three.js 3D scene runtime.

`server/static/threejs/scene_runtime.js` is shared between dashboard
and viewer; it builds the static layers (ground / plate / strike zone /
world axes) directly from a JSON payload that this module emits. Doing
this in Python rather than hardcoding the constants in JS keeps a
single source of truth (`render_scene_theme.py`) — the same values
that drive trace colours, strike zone bounds, etc.
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
from strike_zone import (
    DEFAULT_BATTER_HEIGHT_CM,
    strike_zone_geometry_for_height,
)


_VENDOR_DIR = Path(__file__).parent / "static" / "threejs" / "vendor"
_RUNTIME_DIR = Path(__file__).parent / "static" / "threejs"


def scene_theme(strike_zone: dict | None = None) -> dict:
    """Constants the JS scene needs to render the static layers.

    Strike zone, plate, ground extent, axis lengths, colours. JSON-safe
    primitives only — JS reads via `JSON.parse(textContent)` on a
    `<script type="application/json">` block injected by the page
    renderer."""
    sz = strike_zone or strike_zone_geometry_for_height(DEFAULT_BATTER_HEIGHT_CM).to_dict()
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
            "batter_height_cm": int(sz["batter_height_cm"]),
            "x_half_m": float(sz["x_half_m"]),
            "y_front_m": float(sz["y_front_m"]),
            "y_back_m": float(sz["y_back_m"]),
            "z_bottom_m": float(sz["z_bottom_m"]),
            "z_top_m": float(sz["z_top_m"]),
            "z_height_m": float(sz["z_height_m"]),
            "front_face": sz["front_face"],
            "back_face": sz["back_face"],
            "connectors": sz["connectors"],
            "front_grid": sz["front_grid"],
            "fill_opacity": _STRIKE_ZONE_FILL_OPACITY,
            "line_width": _STRIKE_ZONE_LINE_WIDTH,
            "front_opacity": 0.95,
            "back_opacity": 0.40,
            "connector_opacity": 0.30,
            "grid_opacity": 0.72,
        },
        "axes": {
            "world_len_m": _WORLD_AXIS_LEN_M,
            "camera_axis_len_m": _CAMERA_AXIS_LEN_M,
            "camera_forward_len_m": _CAMERA_FORWARD_ARROW_M,
        },
    }


def scene_theme_json(strike_zone: dict | None = None) -> str:
    """Serialise `scene_theme()` for embedding in a `<script>` block."""
    return _json.dumps(scene_theme(strike_zone))


def scene_runtime_html(*, container_id: str = "scene", strike_zone: dict | None = None) -> str:
    """Return the HTML fragment that boots the Three.js scene runtime.

    Includes:
      - import map pinning ``three`` to the vendored ESM bundle
      - `<script type="application/json" id="bt-scene-theme">` payload
      - module-type `<script>` that imports
        ``/static/threejs/scene_runtime.js`` and constructs the scene

    The scene mounts itself onto an element with ``id=container_id``;
    the caller is responsible for placing that element in the page DOM.
    """
    theme = scene_theme_json(strike_zone).replace("</", "<\\/")
    importmap = _json.dumps({
        "imports": {
            "three": "/static/threejs/vendor/three.module.min.js",
            "three/addons/controls/OrbitControls.js": "/static/threejs/vendor/OrbitControls.js",
            "three/addons/lines/Line2.js": "/static/threejs/vendor/lines/Line2.js",
            "three/addons/lines/LineSegments2.js": "/static/threejs/vendor/lines/LineSegments2.js",
            "three/addons/lines/LineGeometry.js": "/static/threejs/vendor/lines/LineGeometry.js",
            "three/addons/lines/LineSegmentsGeometry.js": "/static/threejs/vendor/lines/LineSegmentsGeometry.js",
            "three/addons/lines/LineMaterial.js": "/static/threejs/vendor/lines/LineMaterial.js",
        },
    })
    return (
        f'<script type="importmap">{importmap}</script>'
        f'<script type="application/json" id="bt-scene-theme">{theme}</script>'
        '<script type="module">'
        'import { mountScene } from "/static/threejs/scene_runtime.js";'
        f'mountScene({_json.dumps(container_id)});'
        '</script>'
    )


def view_presets_toolbar_html(*, default_view: str = "iso") -> str:
    """5-button toolbar for ISO / CATCH / SIDE / TOP / PITCHER.

    Markup only; the Three.js scene runtime (`BallTrackerScene.bindViewToolbar`)
    wires click → `setView(name)` and clears the active pill on the
    first user-drag via OrbitControls' `start` event. Default view
    has the `.active` class so the SSR HTML matches the freshly-mounted
    scene's preset (ISO).
    """
    buttons = [
        ("iso", "ISO", "Isometric overview (default)"),
        ("catch", "CATCH", "Catcher's view — strike zone front-on (X/Z plane)"),
        ("side", "SIDE", "1B-side view — trajectory arc (Y/Z plane)"),
        ("top", "TOP", "Top-down — horizontal break (X/Y plane)"),
        ("pitcher", "PITCHER", "Pitcher's view — looking back at catcher"),
    ]
    parts = ['<div class="scene-views" role="toolbar" aria-label="Camera presets">']
    for key, label, title in buttons:
        cls = "view-preset" + (" active" if key == default_view else "")
        parts.append(
            f'<button class="{cls}" type="button" data-view="{key}" '
            f'title="{title}">{label}</button>'
        )
    parts.append("</div>")
    return "".join(parts)


# Slider bounds — keep in lockstep with `static/threejs/points_layer.js`
# constants (POINT_SIZE_M_MIN/MAX/STEP/DEFAULT). Duplicated here because
# the server renders the <input> element and the client mutates the
# material; two sources of truth would silently desync after a tuning
# change. Test asserts they match.
POINT_SIZE_M_MIN = 0.005
POINT_SIZE_M_MAX = 0.150
POINT_SIZE_M_STEP = 0.001
POINT_SIZE_M_DEFAULT = 0.018


def point_size_slider_html(*, slot_id: str = "scene-point-size") -> str:
    """Range slider for trajectory point world-space size; layer module's
    setupX() binds it (push, not pull, to avoid the classic-vs-module
    boot race). localStorage key shared across dashboard + viewer."""
    return (
        f'<span class="mini-slider point-size-slider" id="{slot_id}" '
        f'title="Point size — world-space metres (sphere stays this size '
        f'in 3D regardless of camera distance)">'
        f'<span class="ms-name ps-name">PT</span>'
        f'<input type="range" '
        f'min="{POINT_SIZE_M_MIN:.3f}" '
        f'max="{POINT_SIZE_M_MAX:.3f}" '
        f'step="{POINT_SIZE_M_STEP:.3f}" '
        f'value="{POINT_SIZE_M_DEFAULT:.3f}" '
        f'data-point-size-slider>'
        f'<span class="ms-readout ps-readout" data-point-size-readout>'
        f'{int(round(POINT_SIZE_M_DEFAULT * 1000))} mm</span>'
        f'</span>'
    )


# Fit-line width slider — Line2 LineMaterial.linewidth is in screen-space
# pixels, not world-metres. Defaults wide enough that the active highlight
# (×1.6) reads as a clear hierarchy, narrow enough that 8 overlapping
# segments don't melt into one slab.
FIT_LINE_WIDTH_PX_MIN = 1.0
FIT_LINE_WIDTH_PX_MAX = 8.0
FIT_LINE_WIDTH_PX_STEP = 0.5
FIT_LINE_WIDTH_PX_DEFAULT = 2.0

# Fit-extension slider — seconds of pre/post padding on each segment's
# parabola sample. 0 hides extensions entirely. 0.5 s covers about half
# a typical pitch flight, plenty for visual extrapolation.
FIT_EXTENSION_SEC_MIN = 0.0
FIT_EXTENSION_SEC_MAX = 0.5
FIT_EXTENSION_SEC_STEP = 0.02
FIT_EXTENSION_SEC_DEFAULT = 0.10


def fit_line_width_slider_html(*, slot_id: str) -> str:
    """Range slider for the Line2-based fit-curve linewidth (screen-px)."""
    return (
        f'<span class="mini-slider fit-line-width-slider" id="{slot_id}" '
        f'title="Fit curve line width — screen-space pixels">'
        f'<span class="ms-name">LW</span>'
        f'<input type="range" '
        f'min="{FIT_LINE_WIDTH_PX_MIN:.1f}" '
        f'max="{FIT_LINE_WIDTH_PX_MAX:.1f}" '
        f'step="{FIT_LINE_WIDTH_PX_STEP:.1f}" '
        f'value="{FIT_LINE_WIDTH_PX_DEFAULT:.1f}" '
        f'data-fit-line-width-slider>'
        f'<span class="ms-readout" data-fit-line-width-readout>'
        f'{FIT_LINE_WIDTH_PX_DEFAULT:.1f} px</span>'
        f'</span>'
    )


def fit_extension_seconds_slider_html(*, slot_id: str) -> str:
    """Range slider for the seconds of dashed extension on each fit segment."""
    return (
        f'<span class="mini-slider fit-extension-slider" id="{slot_id}" '
        f'title="Dashed extension on each fit segment, seconds before t_start and after t_end">'
        f'<span class="ms-name">EXT</span>'
        f'<input type="range" '
        f'min="{FIT_EXTENSION_SEC_MIN:.2f}" '
        f'max="{FIT_EXTENSION_SEC_MAX:.2f}" '
        f'step="{FIT_EXTENSION_SEC_STEP:.2f}" '
        f'value="{FIT_EXTENSION_SEC_DEFAULT:.2f}" '
        f'data-fit-extension-slider>'
        f'<span class="ms-readout" data-fit-extension-readout>'
        f'{int(round(FIT_EXTENSION_SEC_DEFAULT * 1000))} ms</span>'
        f'</span>'
    )


def layer_chip_with_popover_html(
    *,
    group_key: str,
    label: str,
    checkbox_id: str | None = None,
    layer_data_attr: str | None = None,
    checked: bool = True,
    popover_id: str,
    popover_inner_html: str,
    title: str | None = None,
) -> str:
    """A layer chip whose body opens a popover with config sliders.

    Renders ``<span class="layer-group has-popover">`` containing:
      - the original layer checkbox (preserved so existing wiring keeps
        firing change events on it),
      - a chevron `▾` toggle that opens/closes a sibling popover,
      - the popover element itself, hidden by default, holding caller-
        supplied controls (slider HTML built by `point_size_slider_html`,
        `fit_line_width_slider_html`, etc.).

    Open/close + outside-click logic is wired by `bindLayerPopovers()`
    in the JS layer modules so the markup here stays declarative and
    server-rendered.
    """
    title_attr = f' title="{title}"' if title else ""
    cb_id_attr = f' id="{checkbox_id}"' if checkbox_id else ""
    cb_layer_attr = f' data-layer="{layer_data_attr}"' if layer_data_attr else ""
    checked_attr = " checked" if checked else ""
    has_checkbox = bool(checkbox_id) or bool(layer_data_attr)
    if has_checkbox:
        head = (
            f'  <label class="layer-checkbox">'
            f'    <input type="checkbox" class="layer-checkbox"{cb_id_attr}{cb_layer_attr}{checked_attr}>'
            f'    <span class="layer-name">{label}</span>'
            f'  </label>'
        )
    else:
        head = f'  <span class="layer-name layer-name-only">{label}</span>'
    return (
        f'<span class="layer-group has-popover" data-layer-group="{group_key}"{title_attr}>'
        f'{head}'
        f'  <button type="button" class="layer-popover-toggle" '
        f'data-popover-target="{popover_id}" aria-expanded="false" '
        f'aria-label="{label} display settings">▾</button>'
        f'  <div class="layer-popover" id="{popover_id}" data-popover hidden role="dialog">'
        f'    {popover_inner_html}'
        f'  </div>'
        f'</span>'
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
    lines_dir = _VENDOR_DIR / "lines"
    return (
        (_VENDOR_DIR / "three.module.min.js").exists()
        and (_VENDOR_DIR / "OrbitControls.js").exists()
        and (_RUNTIME_DIR / "scene_runtime.js").exists()
        and all(
            (lines_dir / name).exists()
            for name in (
                "Line2.js",
                "LineSegments2.js",
                "LineGeometry.js",
                "LineSegmentsGeometry.js",
                "LineMaterial.js",
            )
        )
    )
