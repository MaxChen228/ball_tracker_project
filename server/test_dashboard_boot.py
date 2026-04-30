from __future__ import annotations

from render_dashboard_html import render_dashboard_html


def test_dashboard_scene_hookup_rehydrates_selection_after_layer_mount():
    html = render_dashboard_html(
        css="",
        nav_html="",
        session_html="",
        hsv_html="",
        tuning_html="",
        strike_zone_html="",
        intrinsics_html="",
        events_html="",
        scene_div='<div id="scene-root"></div>',
        scene_runtime_html="",
        view_presets_toolbar_html="",
        overlays_js="",
        cam_view_js="",
        dashboard_js="",
        trash_count=0,
    )
    assert 'setupDashboardLayers(window.BallTrackerScene);' in html
    assert 'if (typeof window.BallTrackerDashboardRepaint === "function") {' in html
    assert 'window.BallTrackerDashboardRepaint();' in html
