"""Phase 1 of the Plotly → Three.js 3D migration.

These tests guard the *infrastructure* — they don't touch the
dashboard / viewer renderers yet (those land in phase 2 / 3). What
they DO check:

  - the vendored Three.js + OrbitControls files are on disk
  - the shared `scene_runtime.js` module is on disk
  - the FastAPI /static mount serves them with the right MIME type
  - `scene_theme()` returns a JSON-safe payload covering every
    constant the JS runtime reads
  - `scene_runtime_html()` produces an importmap + boot fragment
    that mentions the runtime URL + the JSON theme element id
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from scene_runtime import (
    assert_scene_runtime_present,
    scene_runtime_html,
    scene_theme,
    scene_theme_json,
    vendor_files_present,
)


_VENDOR_DIR = Path(main.__file__).parent / "static" / "threejs" / "vendor"
_RUNTIME_DIR = Path(main.__file__).parent / "static" / "threejs"


def test_vendor_files_on_disk():
    """three.module.min.js + OrbitControls.js + scene_runtime.js + the
    five Line2 fat-line vendor files must exist. Without them the
    boot-time invariant in main.py raises and the server fails to
    start — this test catches the same regression in CI before deploy."""
    assert (_VENDOR_DIR / "three.module.min.js").exists()
    assert (_VENDOR_DIR / "OrbitControls.js").exists()
    assert (_RUNTIME_DIR / "scene_runtime.js").exists()
    lines_dir = _VENDOR_DIR / "lines"
    for name in (
        "Line2.js",
        "LineSegments2.js",
        "LineGeometry.js",
        "LineSegmentsGeometry.js",
        "LineMaterial.js",
    ):
        assert (lines_dir / name).exists(), f"missing vendor/lines/{name}"
    assert vendor_files_present()


def test_static_mount_serves_line2_vendor():
    """Line2 + its dependencies must be reachable via the importmap
    rewrites in `scene_runtime_html`. The fat-line module tree is what
    powers the operator-tunable fit-curve linewidth + dashed extension."""
    client = TestClient(main.app)
    for name in (
        "Line2.js",
        "LineSegments2.js",
        "LineGeometry.js",
        "LineSegmentsGeometry.js",
        "LineMaterial.js",
    ):
        r = client.get(f"/static/threejs/vendor/lines/{name}")
        assert r.status_code == 200, f"vendor/lines/{name} not served"
        assert "javascript" in r.headers["content-type"].lower()


def test_static_mount_serves_three_module():
    """The /static mount has to serve the ESM bundle as JavaScript so
    the browser's importmap-driven module loader accepts it. FastAPI
    StaticFiles infers MIME type from extension."""
    client = TestClient(main.app)
    r = client.get("/static/threejs/vendor/three.module.min.js")
    assert r.status_code == 200
    # Content-type prefix is enough — exact type can vary
    # (text/javascript vs application/javascript) per server config.
    ctype = r.headers["content-type"].lower()
    assert "javascript" in ctype, ctype
    assert "Three.js" in r.text  # sanity: the actual bundle's banner


def test_static_mount_serves_orbit_controls():
    client = TestClient(main.app)
    r = client.get("/static/threejs/vendor/OrbitControls.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    assert "OrbitControls" in r.text


def test_static_mount_serves_scene_runtime():
    """The shared runtime module must be reachable at the URL the
    importmap will rewrite specifiers to."""
    client = TestClient(main.app)
    r = client.get("/static/threejs/scene_runtime.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"].lower()
    # Surface check — runtime exports + class name.
    text = r.text
    assert "class BallTrackerScene" in text
    assert "export function mountScene" in text
    assert 'import * as THREE from "three"' in text
    assert "setStrikeZone(next)" in text


def test_viewer_layers_use_role_based_not_path_based_colors():
    """Viewer 3D should visually align with dashboard: PATH chooses the
    geometry source, while hue encodes scene role. Guard against
    restoring the old per-path colour table in viewer_layers.js."""
    text = (_RUNTIME_DIR / "viewer_layers.js").read_text()
    assert "const PATH_COLORS =" not in text
    assert "function colorForCamPath" not in text
    assert "function colorForCamera" in text
    assert "const FIT_ACCENT = 0xC0392B;" in text
    assert 'group.name = "viewer_traj";' in text
    assert 'viewer_traj_live' not in text
    assert 'viewer_traj_svr' not in text
    assert "this._currentTrajectory()" in text
    assert "this._currentSegments()" in text
    assert "this.SEGMENTS_BY_PATH = opts.SEGMENTS_BY_PATH || {};" in text


def test_scene_theme_is_json_safe():
    """`scene_theme()` is consumed via JSON.parse(textContent) on a
    `<script type=application/json>` block. Every value must be
    JSON-serialisable. `json.dumps` is the canonical guard."""
    theme = scene_theme()
    payload = json.dumps(theme)
    # Round-trip — ensures no NaN/Infinity/numpy snuck in.
    assert json.loads(payload) == theme


def test_scene_theme_covers_runtime_consumers():
    """The JS runtime reads specific keys to build the static layers.
    Drop or rename any of these and the strike zone / plate / ground
    silently disappear in the rendered scene — assert presence."""
    theme = scene_theme()
    assert "colors" in theme
    for k in ("ink", "ink_40", "sub", "border_l", "strike_zone", "dev", "contra"):
        assert k in theme["colors"], f"colors.{k} missing"
    assert theme["ground"]["half_extent_m"] > 0
    assert len(theme["plate"]["x"]) == 5
    assert len(theme["plate"]["y"]) == 5
    sz = theme["strike_zone"]
    assert sz["x_half_m"] > 0
    assert sz["y_back_m"] > sz["y_front_m"]
    assert sz["z_top_m"] > sz["z_bottom_m"]
    assert 0 < sz["fill_opacity"] < 1
    assert len(sz["front_face"]) == 4
    assert len(sz["back_face"]) == 4
    assert len(sz["connectors"]) == 4
    assert len(sz["front_grid"]) == 4
    assert theme["axes"]["world_len_m"] > 0


def test_scene_runtime_html_contains_importmap_and_payload():
    """The fragment that pages embed must wire (a) importmap so `three`
    + the five Line2 fat-line specifiers resolve to the vendored ESM
    bundles, (b) a JSON payload script, and (c) the module-type boot
    script that mounts the scene."""
    html = scene_runtime_html(container_id="scene")
    assert '<script type="importmap">' in html
    assert '"three": "/static/threejs/vendor/three.module.min.js"' in html
    assert '"three/addons/controls/OrbitControls.js": "/static/threejs/vendor/OrbitControls.js"' in html
    for spec, path in (
        ("three/addons/lines/Line2.js", "/static/threejs/vendor/lines/Line2.js"),
        ("three/addons/lines/LineSegments2.js", "/static/threejs/vendor/lines/LineSegments2.js"),
        ("three/addons/lines/LineGeometry.js", "/static/threejs/vendor/lines/LineGeometry.js"),
        ("three/addons/lines/LineSegmentsGeometry.js", "/static/threejs/vendor/lines/LineSegmentsGeometry.js"),
        ("three/addons/lines/LineMaterial.js", "/static/threejs/vendor/lines/LineMaterial.js"),
    ):
        assert f'"{spec}": "{path}"' in html, f"importmap missing {spec}"
    assert '<script type="application/json" id="bt-scene-theme">' in html
    assert 'mountScene("scene")' in html
    assert '/static/threejs/scene_runtime.js' in html


def test_assert_scene_runtime_present_catches_missing_injection():
    """Sanity helper used by future dashboard / viewer integration
    tests. Mirrors `assert_overlays_present` semantics."""
    bad = "<html><body>no runtime</body></html>"
    with pytest.raises(AssertionError):
        assert_scene_runtime_present(bad)
    good = scene_runtime_html()
    assert_scene_runtime_present(good)  # should not raise


def test_theme_json_escape_is_safe_for_inline_script():
    """The JSON payload is embedded between `<script>` and `</script>`
    — any literal `</` inside the JSON would prematurely terminate
    the script block. The helper escapes `</` to `<\\/` per the
    standard inline-JSON pattern."""
    html = scene_runtime_html()
    # Pull out the JSON payload between the open and close tags.
    open_tag = '<script type="application/json" id="bt-scene-theme">'
    close_tag = '</script>'
    start = html.index(open_tag) + len(open_tag)
    end = html.index(close_tag, start)
    payload = html[start:end]
    # Re-escape `<\\/` back to `</` for parsing.
    parsable = payload.replace("<\\/", "</")
    assert json.loads(parsable) == scene_theme()
    # And the literal string `</script>` must NOT appear in the payload
    # (would break the surrounding script element parsing).
    assert "</script>" not in payload


def test_vendor_present_helper_detects_missing_files(tmp_path, monkeypatch):
    """Unit-level guard for the helper that backs the boot-time
    invariant. The actual `main.py` import-time check that *raises*
    on missing vendor files runs once per process at module load —
    can't be re-run inside a test without `importlib.reload(main)`,
    which would tear down the running app fixture. We test the helper
    in isolation here; the boot raise is exercised by deployment
    smoke-testing whenever main.py imports."""
    import scene_runtime as sr
    fake_vendor = tmp_path / "vendor"
    fake_vendor.mkdir()
    fake_runtime = tmp_path  # missing scene_runtime.js
    monkeypatch.setattr(sr, "_VENDOR_DIR", fake_vendor)
    monkeypatch.setattr(sr, "_RUNTIME_DIR", fake_runtime)
    assert sr.vendor_files_present() is False
    # Also confirm the helper returns True under the real paths
    # (sanity: monkeypatch unwinds and we check the actual on-disk
    # state matches what the boot check would have seen).
    monkeypatch.undo()
    assert sr.vendor_files_present() is True


def test_boot_invariant_raises_on_missing_vendor(tmp_path, monkeypatch):
    """Reload `main` with a patched `vendor_files_present` to actually
    exercise the boot-time raise path. This is the test that catches
    a regression where someone swaps the check to a silent log."""
    import importlib
    import scene_runtime as sr
    monkeypatch.setattr(sr, "vendor_files_present", lambda: False)
    # Re-importing main runs the body again, including the invariant.
    # Use a fresh import to avoid mutating the module other tests share.
    import main as _main_orig  # noqa: F401  (ensure original is loaded so undo works)
    with pytest.raises(RuntimeError, match="scene_runtime vendor files missing"):
        importlib.reload(_main_orig)
    # Restore: undo the monkeypatch so the next reload succeeds, then
    # reload once more to put the real `app` back in place for any
    # subsequent test that imports main.
    monkeypatch.undo()
    importlib.reload(_main_orig)


def test_point_size_slider_html_renders_required_hooks():
    """`point_size_slider_html()` must ship the data-* attrs the page-
    specific JS click handlers grep for. Hooks: range input with
    data-point-size-slider, span with data-point-size-readout, and an
    outer container id that lets the boot script scope its lookup."""
    from scene_runtime import point_size_slider_html
    html = point_size_slider_html(slot_id="x-test")
    assert 'id="x-test"' in html
    assert 'data-point-size-slider' in html
    assert 'data-point-size-readout' in html
    assert 'type="range"' in html


def test_fit_line_width_slider_html_renders_required_hooks():
    """`fit_line_width_slider_html()` ships the LW slider used in the
    Fit chip popover. Hooks: data-fit-line-width-slider /
    data-fit-line-width-readout."""
    from scene_runtime import fit_line_width_slider_html
    html = fit_line_width_slider_html(slot_id="x-fit-lw")
    assert 'id="x-fit-lw"' in html
    assert 'data-fit-line-width-slider' in html
    assert 'data-fit-line-width-readout' in html
    assert 'type="range"' in html


def test_fit_extension_seconds_slider_html_renders_required_hooks():
    """`fit_extension_seconds_slider_html()` ships the EXT slider used
    in the Fit chip popover. Hooks: data-fit-extension-slider /
    data-fit-extension-readout."""
    from scene_runtime import fit_extension_seconds_slider_html
    html = fit_extension_seconds_slider_html(slot_id="x-fit-ext")
    assert 'id="x-fit-ext"' in html
    assert 'data-fit-extension-slider' in html
    assert 'data-fit-extension-readout' in html
    assert 'type="range"' in html


def test_layer_chip_with_popover_html_includes_toggle_and_panel():
    """`layer_chip_with_popover_html()` is the wrapper used by both
    dashboard + viewer to render an expandable layer chip. Must ship a
    chevron toggle with data-popover-target pointing at the popover
    div, the popover div with matching id + data-popover, and the
    inner content (caller-supplied)."""
    from scene_runtime import layer_chip_with_popover_html
    html = layer_chip_with_popover_html(
        group_key="traj",
        label="Traj",
        layer_data_attr="traj",
        checked=True,
        popover_id="x-traj-pop",
        popover_inner_html='<span class="x-inner">INNER</span>',
    )
    assert 'data-layer-group="traj"' in html
    assert 'data-layer="traj"' in html
    assert 'data-popover-target="x-traj-pop"' in html
    assert 'id="x-traj-pop"' in html
    assert 'data-popover' in html
    assert 'aria-expanded="false"' in html
    assert 'class="x-inner"' in html


def test_layer_chip_with_popover_supports_checkbox_less_chip():
    """Dashboard's Fit chip is checkbox-less (fit visibility isn't
    operator-toggleable on the dashboard). Helper must render a chip
    with no `<input type=checkbox>` when neither checkbox_id nor
    layer_data_attr is supplied."""
    from scene_runtime import layer_chip_with_popover_html
    html = layer_chip_with_popover_html(
        group_key="fit",
        label="Fit",
        popover_id="x-fit-pop",
        popover_inner_html="",
    )
    assert 'type="checkbox"' not in html
    assert 'layer-name-only' in html
    assert 'data-popover-target="x-fit-pop"' in html


def test_fit_constants_match_fit_curves_layer_js():
    """Python helpers render <input min/max/step/value> and the JS
    fit_curves_layer.js exports parallel constants used to clamp the
    persisted values on construction. Two sources of truth would
    silently desync after a tuning change — this test catches that."""
    import re
    import scene_runtime as sr
    js_text = (
        Path(main.__file__).parent / "static" / "threejs" / "fit_curves_layer.js"
    ).read_text()
    for name, py_value in [
        ("FIT_LINE_WIDTH_PX_MIN", sr.FIT_LINE_WIDTH_PX_MIN),
        ("FIT_LINE_WIDTH_PX_MAX", sr.FIT_LINE_WIDTH_PX_MAX),
        ("FIT_LINE_WIDTH_PX_STEP", sr.FIT_LINE_WIDTH_PX_STEP),
        ("FIT_LINE_WIDTH_PX_DEFAULT", sr.FIT_LINE_WIDTH_PX_DEFAULT),
        ("FIT_EXTENSION_SEC_MIN", sr.FIT_EXTENSION_SEC_MIN),
        ("FIT_EXTENSION_SEC_MAX", sr.FIT_EXTENSION_SEC_MAX),
        ("FIT_EXTENSION_SEC_STEP", sr.FIT_EXTENSION_SEC_STEP),
        ("FIT_EXTENSION_SEC_DEFAULT", sr.FIT_EXTENSION_SEC_DEFAULT),
    ]:
        m = re.search(rf"export const {name}\s*=\s*([0-9.]+)\s*;", js_text)
        assert m, (
            f"{name}: no `export const {name} = N;` line in fit_curves_layer.js. "
            f"Update both `scene_runtime.py` and `static/threejs/fit_curves_layer.js`."
        )
        js_value = float(m.group(1))
        assert js_value == py_value, (
            f"{name} desync: Python={py_value!r}, JS={js_value!r}. "
            f"Update both `scene_runtime.py` and `static/threejs/fit_curves_layer.js`."
        )


def test_point_size_constants_match_points_layer_js():
    """The Python helper renders <input min/max/step/value> and the JS
    layer (static/threejs/points_layer.js) reads the same numbers when
    deciding how to clamp the persisted size on construction. Two
    sources of truth would silently desync after a tuning change —
    this test catches that."""
    from pathlib import Path
    import scene_runtime as sr
    js_text = (Path(main.__file__).parent / "static" / "threejs" / "points_layer.js").read_text()
    # Each Python const has a matching JS export. Source-of-truth check:
    # the JS literal text must contain the Python value (formatted to a
    # plain float repr — the JS file uses 0.005, 0.040, etc.).
    # Parse the JS literal numerically — string match would either false-
    # match (0.04 substring of 0.0401) or false-fail (Python's repr drops
    # trailing zeros so 0.040 → "0.04" while the JS source keeps 0.040;).
    import re
    for name, py_value in [
        ("POINT_SIZE_M_MIN", sr.POINT_SIZE_M_MIN),
        ("POINT_SIZE_M_MAX", sr.POINT_SIZE_M_MAX),
        ("POINT_SIZE_M_STEP", sr.POINT_SIZE_M_STEP),
        ("POINT_SIZE_M_DEFAULT", sr.POINT_SIZE_M_DEFAULT),
    ]:
        m = re.search(rf"export const {name}\s*=\s*([0-9.]+)\s*;", js_text)
        assert m, (
            f"{name}: no `export const {name} = N;` line in points_layer.js. "
            f"Update both `scene_runtime.py` and `static/threejs/points_layer.js`."
        )
        js_value = float(m.group(1))
        assert js_value == py_value, (
            f"{name} desync: Python={py_value!r}, JS={js_value!r}. "
            f"Update both `scene_runtime.py` and `static/threejs/points_layer.js`."
        )
