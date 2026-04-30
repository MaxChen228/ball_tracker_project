"""Sanity tests for the shared overlay runtime injection.

Both dashboard and viewer must inject window.BallTrackerOverlays before
their main script. A regression that drops the kwarg or mis-orders the
script tags would silently desync the strike-zone toggle between the
two pages — assert the runtime appears in the rendered HTML.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

import main
from main import app
from overlays_ui import OVERLAYS_RUNTIME_JS, assert_overlays_present


def test_runtime_js_self_check():
    assert "BallTrackerOverlays" in OVERLAYS_RUNTIME_JS
    assert "strikeZoneVisible" in OVERLAYS_RUNTIME_JS
    # The legacy ballistic fit / speed colourbar overlay code is gone
    # (multi-segment fit is now persisted on SessionResult.segments and
    # rendered server-side). Guard against accidental restoration so the
    # dead client-side math doesn't sediment back in.
    assert "ballisticFit" not in OVERLAYS_RUNTIME_JS
    assert "speedTraces" not in OVERLAYS_RUNTIME_JS
    assert "computeSpeeds" not in OVERLAYS_RUNTIME_JS


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
    """Strike-zone + fit toggles live in the layer-toggles strip. The
    legacy Speed checkbox + svr/live source pills are gone (multi-segment
    fit now lives on SessionResult.segments and the segmenter runs on a
    single authoritative path); guard against accidental restoration."""
    import numpy as np
    from conftest import sid
    from test_viewer import _make_rig, _pitch, _record_pitch

    K, (R_a, t_a, _, H_a), _ = _make_rig()
    session_id = sid(99002)
    _record_pitch(_pitch("A", 99002, K, R_a, t_a, H_a, np.array([[0.1, 0.3, 1.0]])))
    body = TestClient(app).get(f"/viewer/{session_id}").text

    assert '<input type="checkbox" id="strike-zone-toggle"' in body
    assert '<input type="checkbox" id="fit-layer-toggle"' in body
    assert 'id="speed-toggle"' not in body
    assert 'id="fit-src-svr"' not in body
    assert 'id="fit-src-live"' not in body
    assert 'id="speed-bars"' not in body


def test_dashboard_overlay_controls_present():
    """Dashboard's overlay control surface keeps the strike-zone toggle.
    Fit / Speed / svr-live source pills are gone."""
    main.state.reset()
    body = TestClient(app).get("/").text
    assert 'id="dash-strike-zone-toggle"' in body
    assert 'id="dash-fit-toggle"' not in body
    assert 'id="dash-speed-toggle"' not in body
    assert 'id="dash-src-svr"' not in body
    assert 'id="dash-src-live"' not in body
