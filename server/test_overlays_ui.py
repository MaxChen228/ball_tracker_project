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
