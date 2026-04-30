"""HTML shell assembly for the dashboard document.

Phase 2 of the 3D migration: the Plotly CDN is gone and the 3D scene
boots via Three.js (`scene_runtime_html` in the head, plus a
module-type boot script that imports `dashboard_layers.js` after the
scene is mounted). The legacy classic-script IIFE (overlays / cam_view
/ dashboard_js) still loads as inline `<script>` blocks — those don't
participate in the importmap chain and don't need to migrate to
modules until the runtime API stabilises.
"""
from __future__ import annotations


def render_dashboard_html(
    *,
    css: str,
    nav_html: str,
    session_html: str,
    hsv_html: str,
    tuning_html: str,
    intrinsics_html: str,
    events_html: str,
    scene_div: str,
    scene_runtime_html: str,
    view_presets_toolbar_html: str,
    overlays_js: str,
    cam_view_js: str,
    dashboard_js: str,
    trash_count: int,
) -> str:
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        # Three.js scene runtime — importmap + theme JSON + boot module.
        # Loaded as a module so `import` statements resolve via the
        # importmap. Modules defer until after classic scripts finish,
        # so the inline IIFE bundle below runs first; the IIFE accesses
        # `window.BallTrackerScene` / `BallTrackerDashboardScene` lazily
        # at use-time, treating absence as "not ready, retry next tick".
        f"{scene_runtime_html}"
        # Dashboard-specific layer module — wires camera markers, fit
        # curves, points, live trail. Module is loaded AFTER the scene
        # runtime mounts (importmap-resolved), exposes its API via
        # `window.BallTrackerDashboardScene`.
        '<script type="module">'
        'import { setupDashboardLayers } from "/static/threejs/dashboard_layers.js";'
        # Wait for the scene runtime to expose its mounted instance,
        # then bind the dashboard-specific layers. Polling beats
        # listening for an event because both modules race for execution
        # order and an event from the runtime would need to either be
        # queued or replayed; a 50 ms poll is bounded + simple.
        'function _hookup() {'
        '  if (!window.BallTrackerScene) { setTimeout(_hookup, 50); return; }'
        '  setupDashboardLayers(window.BallTrackerScene);'
        '}'
        '_hookup();'
        '</script>'
        f"<style>{css}</style>"
        "</head><body data-page=\"dashboard\">"
        f"{nav_html}"
        '<div class="layout">'
        '<aside class="sidebar">'
        '<div class="card">'
        '<h2 class="card-title">Session</h2>'
        f'<div id="session-body">{session_html}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Detection HSV</h2>'
        f'<div id="hsv-body">{hsv_html}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Capture Tuning</h2>'
        f'<div id="tuning-body">{tuning_html}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Intrinsics (ChArUco)</h2>'
        f'<div id="intrinsics-body">{intrinsics_html}</div>'
        "</div>"
        '<div class="card">'
        '<div class="events-toolbar">'
        '<h2 class="card-title" style="margin:0;border-bottom:0;padding:0;">Events</h2>'
        '<div class="events-filters">'
        '<button type="button" class="events-filter active" data-events-bucket="active">Active</button>'
        f'<button type="button" class="events-filter" data-events-bucket="trash">Trash {trash_count}</button>'
        '</div>'
        '</div>'
        f'<div id="events-body">{events_html}</div>'
        "</div>"
        "</aside>"
        '<section class="canvas">'
        '<div id="degraded-banner" class="degraded-banner" role="alert" style="display:none">'
        '  <span class="degraded-icon">⚠</span>'
        '  <span data-degraded-body>Live stream degraded.</span>'
        '</div>'
        f'{view_presets_toolbar_html}'
        '<div class="fit-filter-bar" role="group" aria-label="Canvas filters">'
        '  <span class="ff-cell" title="Toggle the strike-zone wireframe in the 3D canvas. Default on. Same overlay flag as the viewer.">'
        '    <label class="ff-checkbox">'
        '      <input type="checkbox" id="dash-strike-zone-toggle" checked>'
        '      <span class="ff-name">Strike zone</span>'
        '    </label>'
        '  </span>'
        '  <span class="ff-cell" title="Show raw triangulated points coloured by segment under the fit curves. Default off — fit curves alone are usually enough.">'
        '    <label class="ff-checkbox">'
        '      <input type="checkbox" id="dash-show-points-toggle">'
        '      <span class="ff-name">Show points</span>'
        '    </label>'
        '  </span>'
        '</div>'
        '<div class="latest-pitch-badge" id="latest-pitch-badge" hidden>'
        '  <span class="lpb-speed" id="lpb-speed">—</span>'
        '  <span class="lpb-units">kph</span>'
        '  <span class="lpb-meta" id="lpb-meta"></span>'
        '</div>'
        f"{scene_div}"
        "</section>"
        "</div>"
        f"<script>{overlays_js}</script>"
        f"<script>{cam_view_js}</script>"
        f"<script>{dashboard_js}</script>"
        "</body></html>"
    )
