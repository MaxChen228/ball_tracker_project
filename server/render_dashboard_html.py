"""HTML shell assembly for the dashboard document."""
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
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
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
        '<div class="canvas-hint">Drag to rotate</div>'
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
