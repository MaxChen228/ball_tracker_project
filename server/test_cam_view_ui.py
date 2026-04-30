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
    CAM_VIEW_BOX_CSS,
    CAM_VIEW_CONTENT_CSS,
    CAM_VIEW_FULL_CSS,
    CAM_VIEW_RUNTIME_JS,
    assert_cam_view_present,
    cam_view_runtime_self_check,
    render_cam_view,
)


def test_runtime_self_check_passes():
    cam_view_runtime_self_check()


def test_css_buckets_split_along_class_vs_attr_selector():
    """Phase 3: every rule starting with `.cam-view` belongs in the BOX
    bucket; every rule starting with `[data-cam-view]` belongs in the
    CONTENT bucket. Viewer pulls only CONTENT (it has no .cam-view
    class), so a leak in either direction silently breaks one of the
    two consumer shapes."""
    # BOX bucket: only selectors that REQUIRE `.cam-view` (either bare or
    # chained with [data-cam-view]). The aspect-ratio frame, absolute-
    # positioned toolbar, and dark-theme palette overrides all live here.
    assert ".cam-view {" in CAM_VIEW_BOX_CSS
    assert "aspect-ratio: 16 / 9" in CAM_VIEW_BOX_CSS
    # Dark-theme palette lives in BOX as `.cam-view[data-cam-view]` chains
    # so the specificity (0,3,0) clears CONTENT's bare `[data-cam-view]`
    # base (0,2,0). Bare-attr selectors (without `.cam-view`) still belong
    # in CONTENT exclusively.
    # Bare-attr selectors live in CONTENT only — guard against a copy-
    # paste that drops `[data-cam-view] .cv-layer` into BOX without the
    # `.cam-view` chain (which would tie specificity with CONTENT and
    # silently lose to source-order).
    assert "\n[data-cam-view]" not in CAM_VIEW_BOX_CSS  # no line-leading bare-attr
    assert ".cam-view[data-cam-view] .cv-layer" in CAM_VIEW_BOX_CSS  # chained palette override
    # CONTENT bucket: only bare `[data-cam-view] ...` selectors. Pill /
    # slider / badge styling lives here so viewer's vid-cell inherits
    # without eating box rules.
    assert "[data-cam-view] .cam-view-toolbar" in CAM_VIEW_CONTENT_CSS
    assert "[data-cam-view] .cam-view-badge" in CAM_VIEW_CONTENT_CSS
    # CONTENT must hold no rules whose selector starts with `.cam-view`
    # (regardless of trailing space, dot, brace, attr, etc.) — catches
    # both the bare `.cam-view {` rule and the dark-theme `.cam-view ...`
    # palette overrides if they ever leak the wrong way.
    assert not re.search(
        r'^\.cam-view(?:[\s\.\{\[]|$)',
        CAM_VIEW_CONTENT_CSS,
        re.MULTILINE,
    )
    # FULL = BOX + CONTENT, by construction.
    assert CAM_VIEW_FULL_CSS == CAM_VIEW_BOX_CSS + CAM_VIEW_CONTENT_CSS


def test_dark_theme_palette_beats_content_base():
    """Phase 3 review: BOX-bucket dark-theme overrides for `.cv-layer`,
    `.cv-layer.on`, `.cv-opacity`, and `.cv-opacity input` must use a
    selector with strictly higher specificity than the matching bare
    `[data-cam-view] ...` rules in CONTENT, otherwise FULL_CSS = BOX +
    CONTENT silently flips dashboard / setup / markers back to the light
    palette via source-order tiebreak."""
    for selector in (
        ".cam-view[data-cam-view] .cv-layer {",
        ".cam-view[data-cam-view] .cv-layer.on {",
        ".cam-view[data-cam-view] .cv-opacity {",
        ".cam-view[data-cam-view] .cv-opacity input[type=range] {",
    ):
        assert selector in CAM_VIEW_BOX_CSS, f"missing dark-theme override: {selector!r}"


def test_viewer_imports_only_content_bucket():
    """Viewer imports CAM_VIEW_CONTENT_CSS by itself — pulling the BOX
    bucket too would dump .cam-view aspect-ratio rules onto the page,
    a footgun if anyone in viewer ever adds a .cam-view class."""
    import ast
    import inspect
    import viewer_page
    tree = ast.parse(inspect.getsource(viewer_page))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "cam_view_ui":
            imported.update(alias.name for alias in node.names)
    # Viewer must take the CONTENT bucket and nothing else CSS-shaped.
    assert "CAM_VIEW_CONTENT_CSS" in imported
    assert "CAM_VIEW_FULL_CSS" not in imported
    assert "CAM_VIEW_BOX_CSS" not in imported


def test_runtime_exposes_required_api():
    js = CAM_VIEW_RUNTIME_JS
    for needle in (
        "window.BallTrackerCamView",
        "function mount(",
        "function setMeta(",
        "function setLayer(",
        "function setOpacity(",
        "function registerLayer(",
        "function forgetCam(",
        "function startPreviewPolling(",
        "function startCalibrationPolling(",
        "layerRenderers.set('plate'",
        "layerRenderers.set('axes'",
        "drawVirtualBase",
        "applyStatusBadges",
    ):
        assert needle in js, f"runtime missing {needle!r}"
    # Phase 2 lifecycle + polling helpers must be exported on the public
    # surface — without this, callers fall back to inlining the loop and
    # the offline-gate / cleanup divergence resurfaces (see preview poll
    # divergence between dashboard and markers prior to Phase 2).
    assert "forgetCam, startPreviewPolling, startCalibrationPolling" in js


def test_forgetcam_clears_all_cam_keyed_state():
    """forgetCam is the hard 'cam is gone' signal — every Map keyed by
    camId, the ResizeObserver, and any preview pollers must drop.
    setMeta(null) is the softer 'decalibrated but still present' path
    and intentionally leaves layer toggles + opacity sliders alone."""
    js = CAM_VIEW_RUNTIME_JS
    for clear_call in (
        "camMeta.delete(camId)",
        "camExtras.delete(camId)",
        "camStatus.delete(camId)",
        "layerState.delete(camId)",
        "opacityState.delete(camId)",
        "clickHandlers.delete(camId)",
        "resizeObservers.delete(camId)",
        "previewPollers.delete(camId)",
    ):
        assert clear_call in js, f"forgetCam missing cleanup: {clear_call!r}"


def test_start_calibration_polling_setmeta_diff_drops_absent_cams():
    """startCalibrationPolling must call setMeta(cam, null) for cams that
    appear in listCams() but not in the latest /calibration/state — that's
    how the runtime flips the badge to 'uncalibrated' the moment the
    server stops reporting a cam, without waiting for a page reload."""
    js = CAM_VIEW_RUNTIME_JS
    assert "for (const cam of listCams())" in js
    assert "setMeta(cam, null)" in js


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
        "<html><head><style>" + CAM_VIEW_FULL_CSS + "</style></head><body>"
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
    # `axes` is registered via a wrapper that forwards to `drawAxesLayer`
    # so the chip popover sliders (opacity / line width) take effect; the
    # legacy direct `layerRenderers.set('axes', drawAxesLayer)` form is
    # gone post-PR.
    assert "layerRenderers.set('axes'" in js
    assert "drawAxesLayer(ctx, sx, sy, cam" in js


def test_drawvirtualbase_does_not_paint_builtin_plate():
    """drawVirtualBase must only set up the canvas surface — plate +
    principal-point are owned by the cam-view runtime's toggleable
    layers. The legacy built-in painting branch was removed; verify it
    stays gone so we don't double-paint."""
    from render_compare import DRAW_VIRTUAL_BASE_JS
    assert "PLATE_WORLD.map" not in DRAW_VIRTUAL_BASE_JS
    assert "skipBuiltins" not in DRAW_VIRTUAL_BASE_JS


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
    assert "[data-cam-view] canvas[data-cam-canvas]" in CAM_VIEW_FULL_CSS
    assert "pointer-events: none" in CAM_VIEW_FULL_CSS
    assert "[data-cam-view].has-click canvas[data-cam-canvas]" in CAM_VIEW_FULL_CSS
    assert "pointer-events: auto" in CAM_VIEW_FULL_CSS


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


def test_dashboard_preview_poll_uses_runtime_api():
    """Phase 2: dashboard delegates preview polling to the cam-view
    runtime via startPreviewPolling. The inline cache-bust loop moved
    into cam_view_ui.py so /setup and /markers share the same
    offline gate + per-cam abort handle."""
    from render_dashboard_client import _JS_TEMPLATE
    assert "BallTrackerCamView.startPreviewPolling" in _JS_TEMPLATE
    # Legacy inline tickPreviewImages + selector removed.
    assert "setInterval(tickPreviewImages" not in _JS_TEMPLATE
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


def test_runtime_warns_loud_when_badge_container_missing():
    """Phase 4: applyStatusBadges used to silently return when the
    cam-view root had no `.cam-view-badges` child. That's a contract
    violation between the runtime and the page's HTML — make it loud
    once per cam (not every tick) so the operator notices."""
    js = CAM_VIEW_RUNTIME_JS
    assert "_warnedBadgesMissing" in js
    assert "cam-view-badges container missing" in js


def test_runtime_treats_data_no_badges_as_explicit_optout():
    """Phase 4 review: viewer's vid-cell carries data-cam-view but no
    cam-view-badges container — viewer surfaces those signals via its
    own vid-head label. The runtime must read data-no-badges as an
    explicit opt-out and skip silently, otherwise every viewer page
    open noisily warns about a contract the page isn't trying to
    fulfill."""
    js = CAM_VIEW_RUNTIME_JS
    assert "data-no-badges" in js
    assert "hasAttribute('data-no-badges')" in js


def test_viewer_fragments_emits_data_no_badges_optout():
    """Pin the partner side: viewer's video_cell_html must stamp
    data-no-badges in the cam-view attrs block so the runtime's
    opt-out path actually triggers — checked at the source level
    because the function's branching makes a single fixture call
    awkward."""
    import inspect
    import viewer_fragments
    src = inspect.getsource(viewer_fragments.video_cell_html)
    # data-no-badges must appear inside the cam_view_attrs assembly so
    # every entry-present render emits it.
    assert "data-no-badges" in src
    assert "data-cam-view" in src


def test_start_calibration_polling_logs_errors_not_silent():
    """Phase 2 review: original `catch (_) { /* silent retry */ }` and
    `try { onPayload(...) } catch (_) {}` violated the project's no-
    silent-fallback rule. Network errors throttled to once a minute
    (avoid hosing console on full outage); onPayload errors logged
    every time (caller bugs, not network bugs)."""
    js = CAM_VIEW_RUNTIME_JS
    # No bare silent catch in startCalibrationPolling.
    assert "/* silent retry */" not in js
    # onPayload errors get a meaningful log.
    assert "onPayload threw" in js
    # Network errors throttled, not muted.
    assert "NET_WARN_COOLDOWN_MS" in js


def test_runtime_does_not_expose_internal_state_handle():
    """Phase 4: _internal: { camMeta, ... } was kept around as a debug
    escape hatch but the previous round of review already showed
    callers used it (tickCalibration reached into camMeta directly)
    and had to be migrated to listCams(). Drop the hatch — public
    API only on window.BallTrackerCamView."""
    js = CAM_VIEW_RUNTIME_JS
    assert "_internal:" not in js
    assert "_internal," not in js


def test_runtime_click_handlers_dedupe_same_fn():
    """Phase 4: onCanvasClick was push, so the same fn registered twice
    fired twice. Move the handlers Map value to a Set so reference
    equality dedupes — defensive against re-init paths that forget to
    deregister, and harmless when only one handler is registered."""
    js = CAM_VIEW_RUNTIME_JS
    assert "clickHandlers.set(camId, new Set())" in js
    assert "clickHandlers.get(camId).add(fn)" in js
    # Comment block of the previous shape (push to array) must be gone.
    assert "clickHandlers.get(camId).push" not in js


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
    """v6: /viewer migrated to merged cam-view substrate. Each cam's
    vid-cell carries data-cam-view + a canvas overlay; the runtime
    registers a single BLOBS layer (`detection_blobs`) whose data path
    follows the global PATH selector on the 3D toolbar."""
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
    # Layer set: PLATE + AXES calibration overlays + one BLOBS layer per
    # detection path. Default-on: PLATE + LIVE BLOBS. SVR off (legacy /
    # live-only sessions have no svr data; opt-in via Run server).
    assert 'data-layers="plate,axes,detection_blobs"' in body
    assert 'data-layers-on="plate,detection_blobs"' in body
    # Default opacity 65 — half-transparent overlay over the real video.
    assert 'data-default-opacity="65"' in body
    # Toolbar pills for the three toggleable shared layers.
    for layer in ("plate", "axes", "detection_blobs"):
        assert f'data-layer="{layer}"' in body
    # Winner-dot layers gone post fan-out.
    assert 'data-layer="detection_live"' not in body
    assert 'data-layer="detection_svr"' not in body
    # v5 split-path BLOBS markup must be gone — single BLOBS chip now,
    # data path follows global PATH selector on the 3D toolbar.
    assert 'data-layer="detection_blobs_live"' not in body
    assert 'data-layer="detection_blobs_svr"' not in body
    assert 'data-blobs-group' not in body
    # Single BLOBS layer registered now.
    assert "registerLayer('detection_blobs'" in body
    assert "registerLayer('detection_blobs_live'" not in body
    assert "registerLayer('detection_blobs_svr'" not in body
    assert "registerLayer('detection_live'" not in body
    assert "registerLayer('detection_svr'" not in body
    # K slider replaced by session-header cost_threshold slider — the
    # CAND layer's filtering is now driven by `window._setCostThreshold`.
    assert 'class="cv-blobs-k"' not in body
    assert 'window._setCandTopK' not in body
    assert 'window._setCostThreshold' in body
    # Legacy SVG plate overlay + standalone virt-canvas DOM removed.
    assert 'real-plate-overlay-A' not in body
    assert 'id="virt-canvas-A"' not in body


def test_markers_page_preview_poll_uses_runtime_api():
    """Phase 2: markers init delegates the preview cache-bust loop to
    the cam-view runtime (offline gate + per-cam abort handle now
    shared across all four pages) and kicks startCalibrationPolling
    so a cross-tab auto-cal updates marker footprints in-place
    without a manual reload."""
    from render_markers import _MARKERS_JS
    assert "BallTrackerCamView.startPreviewPolling('A')" in _MARKERS_JS
    assert "BallTrackerCamView.startPreviewPolling('B')" in _MARKERS_JS
    assert "BallTrackerCamView.startCalibrationPolling" in _MARKERS_JS
    # Legacy inline tickPreviewImages + setInterval removed.
    assert "function tickPreviewImages" not in _MARKERS_JS
    assert "setInterval(tickPreviewImages" not in _MARKERS_JS
    assert "[data-preview-img]" not in _MARKERS_JS
