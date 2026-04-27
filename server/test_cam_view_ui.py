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
