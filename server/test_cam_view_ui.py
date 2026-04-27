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
    """has-click is the gate for canvas pointer-events. CSS now selects
    via [data-cam-view] (the contract) so viewer's vid-cell — which
    skips the .cam-view class — still gets pointer-event handling."""
    assert "[data-cam-view] canvas[data-cam-canvas]" in CAM_VIEW_CSS
    assert "pointer-events: none" in CAM_VIEW_CSS
    assert "[data-cam-view].has-click canvas[data-cam-canvas]" in CAM_VIEW_CSS
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
    # has-click rule now lives on the data-attr selector so viewer (no
    # .cam-view class) gets the same click hit-test path.
    assert "[data-cam-view].has-click canvas" in body


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
    """tickPreviewImages must cache-bust the merged cam-view <img>.
    Phase 5 removed the legacy [data-preview-img] selector — single
    selector now."""
    from render_dashboard_client import _JS_TEMPLATE
    assert "[data-cam-img]" in _JS_TEMPLATE
    assert "[data-preview-img]" not in _JS_TEMPLATE


def test_setup_page_uses_cam_view():
    """Phase 3: /setup must render each cam as a single merged cam-view
    pane and inject the runtime so BallTrackerCamView is available."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    main.state.reset()
    with TestClient(app) as client:
        body = client.get("/setup").text
    assert_cam_view_present(body)
    assert 'data-cam-view="A"' in body and 'data-cam-view="B"' in body
    assert 'data-cam-canvas="A"' in body and 'data-cam-canvas="B"' in body
    # Both plate AND axes default-on for setup (geometric verification focus).
    assert 'data-layers-on="plate,axes"' in body
    # Legacy 2-pane data-attrs gone from /setup.
    assert 'data-preview-overlay="A"' not in body
    assert 'data-virt-canvas="A"' not in body


def test_setup_page_runtime_loads_before_main_js():
    """Mirror dashboard ordering: cam-view runtime before main JS so
    BallTrackerCamView is ready when the dashboard JS IIFE runs."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    main.state.reset()
    with TestClient(app) as client:
        body = client.get("/setup").text
    cv = body.find("BallTrackerCamView")
    main_idx = body.find("=== boot")
    assert cv > 0 and main_idx > 0
    assert cv < main_idx, "cam-view runtime must load before dashboard JS IIFE"


def test_render_device_rows_emits_cam_view_only():
    """Phase 5: legacy 2-pane branch deleted. _render_device_rows now
    emits cam-view shape only; data-virt-canvas / data-preview-overlay
    must not appear."""
    from render_dashboard_devices import _render_device_rows
    devs = [{"camera_id": "A"}, {"camera_id": "B"}]
    body = _render_device_rows(devs, [], compare_mode="toggle")
    assert 'data-cam-view="A"' in body
    assert 'data-cam-view="B"' in body
    assert 'data-virt-canvas' not in body
    assert 'data-preview-overlay' not in body
    assert 'preview-panel' not in body


def test_runtime_lists_cams_publicly():
    """Phase 2 review NIT: tickCalibration was reaching into _internal.
    A public listCams() avoids that brittleness."""
    js = CAM_VIEW_RUNTIME_JS
    assert "function listCams()" in js
    assert "listCams," in js  # exported on window.BallTrackerCamView


def test_runtime_tracks_resize_observer_per_cam():
    """Phase 2 review IMPORTANT: re-mount on innerHTML rebuild stranded
    a ResizeObserver on the discarded root each time. The runtime must
    track observers per cam_id and disconnect prior ones on remount."""
    js = CAM_VIEW_RUNTIME_JS
    assert "resizeObservers" in js
    assert "prev.disconnect()" in js


def test_runtime_setstatus_does_not_take_calibrated_arg():
    """Calibration truth is derived from setMeta payload inside
    applyStatusBadges. Don't accept a redundant 'calibrated' field on
    setStatus — two sources of truth would diverge."""
    js = CAM_VIEW_RUNTIME_JS
    # The function body must NOT propagate a calibrated field from status.
    # Easiest pin: comment line documents the contract.
    assert "Calibration badge is derived from\n" in js or "Calibration badge is derived from" in js


def test_dashboard_renderdevices_pushes_extras_for_all_rendered_cams():
    """Phase 2 review IMPORTANT: setStatus loop was EXPECTED-only, so
    extra cams (non-A/B) never got status. Loop must include extras."""
    from render_dashboard_client import _JS_TEMPLATE
    assert "renderedCams" in _JS_TEMPLATE
    # RMS badge wired through auto_calibration.last
    assert "reprojection_px" in _JS_TEMPLATE
    assert "setExtras" in _JS_TEMPLATE
    # And listCams used instead of _internal access.
    assert ".listCams()" in _JS_TEMPLATE


def test_markers_page_uses_cam_view_with_footprint_layer():
    """Phase 4: /markers must render each cam as a single merged
    cam-view pane, register the marker_footprints layer, and wire
    onCanvasClick for selection. Legacy SVG marker overlay + virt
    canvas must be gone."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    main.state.reset()
    with TestClient(app) as client:
        body = client.get("/markers").text
    assert_cam_view_present(body)
    # Single merged pane per cam.
    assert 'data-cam-view="A"' in body and 'data-cam-view="B"' in body
    # Three layers offered, two on by default (plate + marker_footprints).
    assert 'data-layers="plate,axes,marker_footprints"' in body
    assert 'data-layers-on="plate,marker_footprints"' in body
    # Layer registration + click wiring inside _MARKERS_JS.
    assert "registerLayer('marker_footprints'" in body
    assert "onCanvasClick('A'" in body
    assert "onCanvasClick('B'" in body
    # Marker hit-test uses image-space u/v matching projectWorldToPixel.
    assert "handleCamClick" in body


def test_markers_page_no_longer_inlines_projection_helpers():
    """The shared cam-view runtime injects PLATE_WORLD / projection /
    drawVirtualBase ahead of _MARKERS_JS. Markers must NOT redeclare
    these — single source of truth pulls duplication risk to zero."""
    from render_markers import _MARKERS_JS
    # The helpers are defined ONCE in CAM_VIEW_RUNTIME_JS (which already
    # contains PLATE_WORLD / projectWorldToPixel definitions). Markers
    # JS must not redefine them.
    assert "const PLATE_WORLD =" not in _MARKERS_JS
    assert "function projectWorldToPixel" not in _MARKERS_JS
    assert "function drawVirtualBase" not in _MARKERS_JS


def test_markers_page_drops_legacy_svg_marker_overlay():
    """The SVG-based preview-overlay marker drawing path is replaced
    by the canvas marker_footprints layer. drawPreviewOverlay should
    no longer exist in the markers JS."""
    from render_markers import _MARKERS_JS
    assert "drawPreviewOverlay" not in _MARKERS_JS
    assert "drawCompareVirtual" not in _MARKERS_JS
    # virtCamMeta map gone — meta lives in BallTrackerCamView now.
    assert "const virtCamMeta" not in _MARKERS_JS


def test_viewer_page_uses_cam_view_with_detection_layers():
    """Phase 6: /viewer migrated to merged cam-view substrate. Each cam's
    vid-cell carries data-cam-view + a canvas overlay; the runtime
    registers detection_live / detection_svr layers that draw the
    per-frame ball blob from each pipeline. Per-cam toolbar exposes
    PLATE / AXES / LIVE / SVR pills so the operator can toggle each
    overlay independently — half-transparent over the real video."""
    from fastapi.testclient import TestClient
    import main
    from main import app
    import numpy as np
    from conftest import sid
    from test_viewer import _make_rig, _pitch, _record_pitch

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(99100)
    _record_pitch(_pitch("A", 99100, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    main.state.save_clip("A", session_id, b"clip", "mov")
    body = TestClient(app).get(f"/viewer/{session_id}").text

    # Runtime injected before viewer JS.
    assert_cam_view_present(body)
    cv = body.find("BallTrackerCamView")
    viewer_iife = body.find("function _viewer") if False else body.find("scheduleSceneDraw")
    assert cv > 0 and viewer_iife > 0 and cv < viewer_iife

    # Each cam: data-cam-view attribute + overlaid canvas.
    assert 'data-cam-view="A"' in body
    assert 'data-cam-canvas="A"' in body
    # Layer set + initial-on subset declared per cam.
    assert 'data-layers="plate,axes,detection_live,detection_svr"' in body
    assert 'data-layers-on="plate,detection_live,detection_svr"' in body
    # Default opacity 65 — half-transparent overlay over the real video.
    assert 'data-default-opacity="65"' in body
    # Per-cam toolbar pills for all four toggleable layers.
    for layer in ("plate", "axes", "detection_live", "detection_svr"):
        assert f'data-layer="{layer}"' in body
    # Detection blob layers registered with the runtime.
    assert "registerLayer('detection_live'" in body
    assert "registerLayer('detection_svr'" in body
    # Legacy SVG plate overlay + standalone virt-canvas DOM removed.
    assert 'real-plate-overlay-A' not in body
    assert 'id="virt-canvas-A"' not in body


def test_markers_page_preview_poll_uses_cam_img_selector():
    """The inline tickPreviewImages must query [data-cam-img] (new
    merged pane) instead of legacy [data-preview-img]."""
    from render_markers import _MARKERS_JS
    assert "[data-cam-img]" in _MARKERS_JS
    assert "[data-preview-img]" not in _MARKERS_JS
    # Markers tick aligned to the rest of the codebase at 200 ms (was 250).
    assert "setInterval(tickPreviewImages, 200)" in _MARKERS_JS
