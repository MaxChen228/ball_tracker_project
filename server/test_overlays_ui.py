"""Sanity tests for the shared overlay runtime injection.

Both dashboard and viewer must inject window.BallTrackerOverlays before
their main script. A regression that drops the kwarg or mis-orders the
script tags would silently desync strike-zone / fit / speed flags
between the two pages — assert the runtime appears in the rendered HTML.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import main
from main import app
from overlays_ui import OVERLAYS_RUNTIME_JS, assert_overlays_present


def test_runtime_js_self_check():
    assert "BallTrackerOverlays" in OVERLAYS_RUNTIME_JS
    assert "ballisticFit" in OVERLAYS_RUNTIME_JS
    assert "fitTraces" in OVERLAYS_RUNTIME_JS
    assert "strikeZoneVisible" in OVERLAYS_RUNTIME_JS
    assert "speedVisible" in OVERLAYS_RUNTIME_JS
    assert "speedTraces" in OVERLAYS_RUNTIME_JS
    assert "computeSpeeds" in OVERLAYS_RUNTIME_JS
    assert "viridisColor" in OVERLAYS_RUNTIME_JS


def test_dashboard_injects_overlays_runtime():
    main.state.reset()
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert_overlays_present(r.text)
    # And the dashboard's main IIFE must run AFTER it: the overlays
    # script tag's index in the document precedes the main script.
    body = r.text
    overlays_idx = body.find("BallTrackerOverlays")
    main_idx = body.find("=== boot")
    assert overlays_idx > 0 and main_idx > 0
    assert overlays_idx < main_idx, "overlays runtime must load before dashboard JS IIFE"


def test_setup_injects_overlays_runtime():
    """`/setup` shares the dashboard JS bundle, which references
    window.BallTrackerOverlays at module-eval time (30_traces.js). A
    missing injection crashes the IIFE before any click handler is wired,
    silently disabling every button on the page (PREVIEW, Run auto-cal,
    etc). Guard the injection so the regression that prompted this test
    can't recur."""
    main.state.reset()
    with TestClient(app) as client:
        r = client.get("/setup")
    assert r.status_code == 200
    assert_overlays_present(r.text)
    body = r.text
    overlays_idx = body.find("BallTrackerOverlays")
    main_idx = body.find("=== boot")
    assert overlays_idx > 0 and main_idx > 0
    assert overlays_idx < main_idx, "overlays runtime must load before dashboard JS IIFE"


def test_sync_injects_overlays_runtime():
    """`/sync` shares the same dashboard JS bundle as `/setup` and `/`,
    so the overlays runtime must be present for any of its buttons to
    work. Same regression class as test_setup_injects_overlays_runtime."""
    main.state.reset()
    with TestClient(app) as client:
        r = client.get("/sync")
    assert r.status_code == 200
    assert_overlays_present(r.text)
    body = r.text
    overlays_idx = body.find("BallTrackerOverlays")
    main_idx = body.find("=== boot")
    assert overlays_idx > 0 and main_idx > 0
    assert overlays_idx < main_idx, "overlays runtime must load before dashboard JS IIFE"


def test_viewer_injects_overlays_runtime():
    """Hit /viewer/{sid} via TestClient — the runtime script must be in
    the rendered HTML so dashboard + viewer share strike/fit/speed flags."""
    import numpy as np
    from conftest import sid
    from test_viewer import _make_rig, _pitch, _record_pitch

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(99001)
    _record_pitch(_pitch("A", 99001, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    client = TestClient(app)
    r = client.get(f"/viewer/{session_id}")
    assert r.status_code == 200
    assert_overlays_present(r.text)


def test_viewer_overlay_controls_in_layer_toggles():
    """Phase 4 moved fit-toggle from scene-toolbar into layer-toggles so
    the four overlay layers (Fit / Speed / Strike zone / source pills)
    read as one consistent control surface. Guard against accidental
    relocation back to a button in the toolbar."""
    import numpy as np
    from conftest import sid
    from test_viewer import _make_rig, _pitch, _record_pitch

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(99002)
    _record_pitch(_pitch("A", 99002, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    body = TestClient(app).get(f"/viewer/{session_id}").text

    # fit-toggle is now an <input type="checkbox">, not a <button>.
    assert '<input type="checkbox" id="fit-toggle">' in body
    assert '<input type="checkbox" id="speed-toggle">' in body
    assert '<input type="checkbox" id="strike-zone-toggle"' in body
    # Source pills + speed-bars container exist.
    assert 'id="fit-src-svr"' in body and 'id="fit-src-live"' in body
    assert 'id="speed-bars"' in body
    # speed-bars wrapper must not eat 3D drag — pointer-events:none on the
    # outer container, re-enabled on the inner Plotly child.
    assert "pointer-events:none" in body


def test_dashboard_overlay_controls_present():
    """Dashboard fit-filter-bar must expose the same Fit / Speed / Strike
    zone overlay checkboxes plus svr/live source pills."""
    main.state.reset()
    body = TestClient(app).get("/").text
    for control_id in (
        "dash-fit-toggle", "dash-speed-toggle", "dash-strike-zone-toggle",
        "dash-src-svr", "dash-src-live",
    ):
        assert f'id="{control_id}"' in body, f"missing {control_id} in dashboard HTML"
