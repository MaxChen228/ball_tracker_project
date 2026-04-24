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
        '<div class="canvas-mode-toggle" role="radiogroup" aria-label="Canvas mode">'
        '  <button type="button" data-canvas-mode="inspect" class="active">INSPECT</button>'
        '  <button type="button" data-canvas-mode="replay">REPLAY</button>'
        '</div>'
        '<div class="fit-filter-bar" role="group" aria-label="Fit filters">'
        '  <span class="ff-cell ff-source" title="Pick which triangulation pipeline drives the fit. live = iOS HSV streamed over WS. server_post = server-side decode of the uploaded MOV. Strict siblings — no fallback.">'
        '    <span class="ff-name">Source</span>'
        '    <button type="button" class="ff-src-pill" data-src="server_post" id="dash-src-svr" aria-pressed="true">svr</button>'
        '    <button type="button" class="ff-src-pill" data-src="live" id="dash-src-live" aria-pressed="false">live</button>'
        '  </span>'
        '  <span class="ff-cell" title="Drop triangulated points whose ray-midpoint gap exceeds this cap. Real ball pairs sit sub-cm; bad pairings blow up to m. Below ~20 cm starts clipping real frame-edge points.">'
        '    <span class="ff-name">Residual</span>'
        '    <input type="range" id="dash-residual-slider" min="0" max="200" step="1" value="20" aria-label="Residual cap (cm)">'
        '    <span class="ff-readout" id="dash-residual-readout">≤ 20 cm</span>'
        '  </span>'
        '  <span class="ff-cell" title="Spatial isolation outlier rejection. Reject points whose mean distance to 3 nearest neighbours exceeds median + κ·MAD. Lower κ = stricter.">'
        '    <span class="ff-name">Outlier</span>'
        '    <input type="range" id="dash-outlier-slider" min="10" max="60" step="1" value="30" aria-label="Outlier rejection (κ; 60 = off)">'
        '    <span class="ff-readout" id="dash-outlier-readout">κ ≤ 3.0</span>'
        '  </span>'
        '</div>'
        f"{scene_div}"
        '<div class="playback-bar" id="playback-bar">'
        '  <button type="button" class="playpause" id="playpause">▶</button>'
        '  <input type="range" id="scrub" min="0" max="1000" step="1" value="0">'
        '  <span class="time" id="time-readout">0.00 / 0.00 s</span>'
        '  <span class="speed" role="radiogroup" aria-label="Playback speed">'
        '    <button type="button" data-speed="0.25">0.25×</button>'
        '    <button type="button" data-speed="0.5">0.5×</button>'
        '    <button type="button" data-speed="1" class="active">1×</button>'
        '    <button type="button" data-speed="2">2×</button>'
        '  </span>'
        '</div>'
        "</section>"
        "</div>"
        f"<script>{dashboard_js}</script>"
        "</body></html>"
    )
