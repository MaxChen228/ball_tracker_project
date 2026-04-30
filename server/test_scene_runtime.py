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
    """three.module.min.js + OrbitControls.js + scene_runtime.js must
    exist. Without them the boot-time invariant in main.py raises and
    the server fails to start — this test catches the same regression
    in CI before deploy."""
    assert (_VENDOR_DIR / "three.module.min.js").exists()
    assert (_VENDOR_DIR / "OrbitControls.js").exists()
    assert (_RUNTIME_DIR / "scene_runtime.js").exists()
    assert vendor_files_present()


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
    assert theme["axes"]["world_len_m"] > 0


def test_scene_runtime_html_contains_importmap_and_payload():
    """The fragment that pages embed must wire (a) importmap so `three`
    resolves to the vendored ESM bundle, (b) a JSON payload script,
    and (c) the module-type boot script that mounts the scene."""
    html = scene_runtime_html(container_id="scene")
    assert '<script type="importmap">' in html
    assert '"three":"/static/threejs/vendor/three.module.min.js"' in html
    assert '"three/addons/controls/OrbitControls.js":"/static/threejs/vendor/OrbitControls.js"' in html
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
