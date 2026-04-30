"""Regression guard: /markers Spatial View needs Plotly CDN.

PR #96 (Plotly→Three.js migration) dropped the Plotly CDN <script> tag
from render_markers.py with the rationale "this page has no 3D scene".
That was wrong — Spatial View calls `Plotly.react` + `plotly_click`
inline. Without the CDN the IIFE throws ReferenceError on first render
and the page silently breaks. Migrating /markers Spatial View to
Three.js is a separate task (different scene + click semantics from
the dashboard pitch-trajectory scene); until then the CDN must stay.
"""
from __future__ import annotations


def test_markers_html_loads_plotly_cdn() -> None:
    from fastapi.testclient import TestClient
    import main
    from main import app

    main.state.reset()
    with TestClient(app) as client:
        resp = client.get("/markers")
    assert resp.status_code == 200
    html = resp.text
    assert "cdn.plot.ly" in html, (
        "/markers dropped its Plotly CDN tag. Spatial View calls "
        "Plotly.react inline and will throw ReferenceError without the CDN. "
        "Restore the <script src=\"https://cdn.plot.ly/...\"> tag in "
        "render_markers.py, or port Spatial View to Three.js first."
    )
    assert "Plotly.react" in html, (
        "Spatial View no longer calls Plotly.react — if /markers was ported "
        "to Three.js, drop this test along with the CDN tag."
    )
