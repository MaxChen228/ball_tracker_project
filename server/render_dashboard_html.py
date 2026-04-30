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

from scene_runtime import (
    fit_extension_seconds_slider_html,
    fit_line_width_slider_html,
    layer_chip_with_popover_html,
    point_size_slider_html,
)


def render_dashboard_html(
    *,
    css: str,
    nav_html: str,
    session_html: str,
    hsv_html: str,
    tuning_html: str,
    strike_zone_html: str,
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
        # Bounded poll for scene mount: WebGL context creation can fail
        # (no GPU / driver issue / too many contexts), in which case
        # `mountScene` throws and `window.BallTrackerScene` never lands.
        # Cap retries at 2.5 s (50 × 50 ms) and surface a visible error
        # in #scene-root so the operator knows the 3D pipeline failed
        # rather than waiting forever for a scene that will never appear.
        'let _attempts = 0;'
        'function _hookup() {'
        '  if (window.BallTrackerScene) {'
        '    setupDashboardLayers(window.BallTrackerScene);'
        '    if (typeof window.BallTrackerDashboardRepaint === "function") {'
        '      window.BallTrackerDashboardRepaint();'
        '    }'
        '    return;'
        '  }'
        '  if (++_attempts > 50) {'
        '    const root = document.getElementById("scene-root");'
        '    if (root) root.innerHTML = '
        '      "<div style=\\"padding:24px;font-family:monospace;color:#C0392B;\\">"'
        '      + "3D scene failed to mount — likely a WebGL context issue. "'
        '      + "Check the browser console for the actual error.</div>";'
        '    console.error("BallTrackerScene never mounted after 2.5 s of polling");'
        '    return;'
        '  }'
        '  setTimeout(_hookup, 50);'
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
        '<div class="card" data-collapsible-key="dash:card:hsv">'
        '<h2 class="card-title" data-collapsible-header>Detection HSV</h2>'
        f'<div id="hsv-body" data-collapsible-body>{hsv_html}</div>'
        "</div>"
        '<div class="card" data-collapsible-key="dash:card:tuning">'
        '<h2 class="card-title" data-collapsible-header>Capture Tuning</h2>'
        f'<div id="tuning-body" data-collapsible-body>{tuning_html}</div>'
        "</div>"
        '<div class="card" data-collapsible-key="dash:card:strike-zone">'
        '<h2 class="card-title" data-collapsible-header>Strike Zone</h2>'
        f'<div id="strike-zone-body" data-collapsible-body>{strike_zone_html}</div>'
        "</div>"
        '<div class="card" data-collapsible-key="dash:card:intrinsics">'
        '<h2 class="card-title" data-collapsible-header>Intrinsics (ChArUco)</h2>'
        f'<div id="intrinsics-body" data-collapsible-body>{intrinsics_html}</div>'
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
        '  <span class="ff-cell" title="Switch the 3D fit between LIVE (iOS on-device detection) and SVR (server_post offline detection). SVR is disabled until server_post has run for the selected session.">'
        '    <div class="ff-path-toggle" role="group" aria-label="Fit path">'
        '      <button type="button" class="ff-path active" data-fit-path="live">LIVE</button>'
        '      <button type="button" class="ff-path" data-fit-path="server_post">SVR</button>'
        '    </div>'
        '  </span>'
        '  <span class="ff-cell" title="Toggle the strike-zone wireframe in the 3D canvas. Default on. Same overlay flag as the viewer.">'
        '    <label class="ff-checkbox">'
        '      <input type="checkbox" id="dash-strike-zone-toggle" checked>'
        '      <span class="ff-name">Strike zone</span>'
        '    </label>'
        '  </span>'
        '  <span class="ff-cell">'
        f'    {layer_chip_with_popover_html(group_key="traj", label="Show points", checkbox_id="dash-show-points-toggle", checked=False, popover_id="dash-traj-popover", title="Show raw triangulated points coloured by segment under the fit curves. ▾ for point-size slider.", popover_inner_html=point_size_slider_html(slot_id="dash-point-size"))}'
        '  </span>'
        '  <span class="ff-cell">'
        f'    {layer_chip_with_popover_html(group_key="fit", label="Fit", popover_id="dash-fit-popover", title="Fit curve display settings — line width, dashed extension.", popover_inner_html=fit_line_width_slider_html(slot_id="dash-fit-line-width") + fit_extension_seconds_slider_html(slot_id="dash-fit-extension"))}'
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
