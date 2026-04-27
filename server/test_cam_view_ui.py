"""Smoke tests for the shared cam-view runtime + render helper.

The runtime mirrors `overlays_ui.py` — every page that wants the merged
real+virtual single-pane camera view must inject CAM_VIEW_RUNTIME_JS
ahead of its page-local script. These tests pin the contract so a
regression that drops the runtime / reorders script tags / breaks the
HTML data-attr shape gets caught at unit-test time, not after a UI
change goes silently stale on one of three pages.
"""
from __future__ import annotations

import re

from cam_view_ui import (
    CAM_VIEW_CSS,
    CAM_VIEW_RUNTIME_JS,
    assert_cam_view_present,
    cam_view_runtime_self_check,
    render_cam_view,
)


def test_runtime_self_check_passes():
    cam_view_runtime_self_check()


def test_runtime_exposes_required_api():
    js = CAM_VIEW_RUNTIME_JS
    for needle in (
        "window.BallTrackerCamView",
        "function mount(",
        "function setMeta(",
        "function setLayer(",
        "function setOpacity(",
        "function registerLayer(",
        "layerRenderers.set('plate'",
        "layerRenderers.set('axes'",
        "drawVirtualBase",
        "applyStatusBadges",
    ):
        assert needle in js, f"runtime missing {needle!r}"


def test_runtime_reuses_existing_projection_helpers():
    """Don't re-implement projection — must reuse render_compare's strings."""
    from render_compare import PLATE_WORLD_JS, PROJECTION_JS, DRAW_VIRTUAL_BASE_JS
    assert PLATE_WORLD_JS.strip() in CAM_VIEW_RUNTIME_JS
    assert PROJECTION_JS.strip() in CAM_VIEW_RUNTIME_JS
    assert DRAW_VIRTUAL_BASE_JS.strip() in CAM_VIEW_RUNTIME_JS


def test_render_cam_view_basic_shape():
    body = render_cam_view(
        "A",
        preview_src="/camera/A/preview?t=0",
        layers=["plate", "axes"],
        default_opacity=65,
    )
    # Container with cam id + layer config encoded as data-attrs.
    assert 'data-cam-view="A"' in body
    assert 'data-layers="plate,axes"' in body
    assert 'data-layers-on="plate,axes"' in body
    assert 'data-default-opacity="65"' in body
    # Both layers under <img> + canvas.
    assert 'data-cam-img="A"' in body and 'src="/camera/A/preview?t=0"' in body
    assert 'data-cam-canvas="A"' in body
    # Layer pills exist for both layers and start "on".
    assert re.search(r'class="cv-layer on" data-layer="plate"', body)
    assert re.search(r'class="cv-layer on" data-layer="axes"', body)
    # Opacity slider exists.
    assert 'type="range"' in body and 'value="65"' in body


def test_render_cam_view_layers_on_subset():
    body = render_cam_view(
        "B",
        preview_src="/camera/B/preview?t=0",
        layers=["plate", "axes", "marker_footprints"],
        layers_on=["plate"],  # axes and markers default off
    )
    assert 'data-layers-on="plate"' in body
    assert 'class="cv-layer on" data-layer="plate"' in body
    # axes / markers pills present but NOT on
    assert re.search(r'class="cv-layer" data-layer="axes"', body)
    assert re.search(r'class="cv-layer" data-layer="marker_footprints"', body)


def test_render_cam_view_can_hide_opacity_slider():
    body = render_cam_view(
        "A", preview_src="/x", layers=["plate"], show_opacity=False
    )
    assert 'type="range"' not in body


def test_render_cam_view_extra_slot():
    body = render_cam_view(
        "A", preview_src="/x", layers=["plate"],
        extra_html='<button type="button" data-test="x">x</button>',
    )
    assert 'data-test="x"' in body
    # Extra slot lives in cam-view-extra container.
    assert '<div class="cam-view-extra">' in body


def test_assert_cam_view_present_helper():
    """assert_cam_view_present should pass on runtime+render combo, fail on bare."""
    page = (
        "<html><head><style>" + CAM_VIEW_CSS + "</style></head><body>"
        + render_cam_view("A", preview_src="/x", layers=["plate"])
        + "<script>" + CAM_VIEW_RUNTIME_JS + "</script>"
        + "</body></html>"
    )
    assert_cam_view_present(page)
    try:
        assert_cam_view_present("<html><body>nothing</body></html>")
    except AssertionError:
        pass
    else:
        raise AssertionError("expected assert_cam_view_present to reject empty page")


def test_built_in_layer_renderers_registered():
    """plate + axes must be available out of the box — every page uses them."""
    js = CAM_VIEW_RUNTIME_JS
    assert "layerRenderers.set('plate'" in js
    assert "layerRenderers.set('axes', drawAxesLayer)" in js


def test_runtime_passes_skip_builtins_to_drawvirtualbase():
    """Reviewer flagged: drawVirtualBase has built-in plate+cross painting
    that double-paints when the cam-view runtime owns plate as a layer.
    Runtime must opt out via skipBuiltins."""
    assert "skipBuiltins: true" in CAM_VIEW_RUNTIME_JS


def test_drawvirtualbase_supports_skip_builtins():
    """The opt-out mechanism must actually exist on the helper side too."""
    from render_compare import DRAW_VIRTUAL_BASE_JS
    assert "opts.skipBuiltins" in DRAW_VIRTUAL_BASE_JS


def test_runtime_exposes_click_api_for_phase4():
    """Phase 4 markers page needs canvas click hit-testing. The API shape
    must be settled before Phase 2 ships so the data-attr contract is
    forward-compatible."""
    js = CAM_VIEW_RUNTIME_JS
    assert "onCanvasClick" in js
    assert "has-click" in js  # CSS toggle that re-enables pointer events
    # Click info must include image-space u/v (not just css pixels).
    assert "image_width_px" in js and "image_height_px" in js
    assert "_emitCanvasClick" in js


def test_css_enables_pointer_events_when_click_handler_attached():
    """has-click class must be the gate for canvas pointer-events."""
    assert ".cam-view canvas[data-cam-canvas]" in CAM_VIEW_CSS
    assert "pointer-events: none" in CAM_VIEW_CSS
    assert ".cam-view.has-click canvas[data-cam-canvas]" in CAM_VIEW_CSS
    assert "pointer-events: auto" in CAM_VIEW_CSS


def test_remount_preserves_layer_state():
    """tickCalibration rebuilds devicesBox.innerHTML, which destroys
    every cam-view DOM element. The layerState Map (keyed by camId)
    must persist user-toggled state across re-mounts — otherwise every
    /calibration/state tick silently undoes operator toggles."""
    js = CAM_VIEW_RUNTIME_JS
    # Mount must seed defaults ONLY for keys not already in layerState.
    assert "if (!(k in ls))" in js


def test_resize_observer_attached():
    """window resize alone misses container-level reflow (sidebar
    collapse). ResizeObserver per cam-view root catches it."""
    assert "ResizeObserver" in CAM_VIEW_RUNTIME_JS


def test_dashboard_injects_cam_view_runtime():
    """Phase 2: dashboard / page must include CAM_VIEW_RUNTIME_JS before
    the page-local JS, mirroring the OVERLAYS_RUNTIME_JS pattern."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    main.state.reset()
    with TestClient(app) as client:
        body = client.get("/").text
    assert_cam_view_present(body)
    cam_view_idx = body.find("BallTrackerCamView")
    main_idx = body.find("=== boot")
    assert cam_view_idx > 0 and main_idx > 0
    assert cam_view_idx < main_idx, "cam-view runtime must load before dashboard JS IIFE"


def test_dashboard_css_includes_cam_view_styles():
    """Dashboard CSS bundle must carry .cam-view rules so the merged
    real+virtual pane styles apply on /."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    main.state.reset()
    with TestClient(app) as client:
        body = client.get("/").text
    assert ".cam-view" in body
    assert ".cam-view.has-click canvas" in body


def test_dashboard_js_renders_cam_view_in_device_row():
    """The JS-side device row builder (50_renderers.js) must emit the
    new cam-view shape — data-cam-view + data-cam-img + data-cam-canvas
    + a layer toggle bar — instead of the legacy 2-pane preview-panel
    + virt-cell pair."""
    from render_dashboard_client import _JS_TEMPLATE
    assert 'data-cam-view="' in _JS_TEMPLATE
    assert 'data-cam-img="' in _JS_TEMPLATE
    assert 'data-cam-canvas="' in _JS_TEMPLATE
    assert 'data-layers="plate,axes"' in _JS_TEMPLATE
    # Old 2-pane shape must be gone from the dashboard's row builder.
    assert 'data-preview-panel="${esc(cam)}"' not in _JS_TEMPLATE
    assert 'data-virt-cell="${esc(cam)}"' not in _JS_TEMPLATE


def test_dashboard_tick_pushes_cam_view_meta():
    """tickCalibration must forward scene.cameras to BallTrackerCamView."""
    from render_dashboard_client import _JS_TEMPLATE
    assert "BallTrackerCamView.setMeta" in _JS_TEMPLATE
    assert "BallTrackerCamView.mountAll" in _JS_TEMPLATE


def test_dashboard_preview_poll_handles_cam_view_imgs():
    """tickPreviewImages must cache-bust both legacy [data-preview-img]
    and new [data-cam-img] so dashboard's merged pane stays fresh."""
    from render_dashboard_client import _JS_TEMPLATE
    assert "[data-cam-img]" in _JS_TEMPLATE
    # Legacy selector still present for setup/markers until those phases.
    assert "[data-preview-img]" in _JS_TEMPLATE
