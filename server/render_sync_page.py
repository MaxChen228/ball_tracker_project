"""Page-level orchestration for `/setup` and `/sync`.

Also hosts the two card-body fragments (`_render_sync_body`,
`_render_burst_params_body`) — they only have one caller each (the
`render_sync_html` page below) and were previously split into a separate
`render_sync.py` module purely as a circular-import shim. Consolidated
here so the sync namespace is a single file.
"""
from __future__ import annotations

import html
from typing import Any

from cam_view_ui import CAM_VIEW_RUNTIME_JS
from overlays_ui import OVERLAYS_RUNTIME_JS
from render_dashboard_client import _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE
from render_dashboard_devices import _render_device_rows
from render_dashboard_style import _CSS
from render_shared import _render_app_nav
from render_sync_client import _JS_TEMPLATE
from render_sync_style import _SYNC_CSS


def _render_sync_body(
    sync: dict[str, Any] | None,
    last_sync: dict[str, Any] | None,
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    cooldown_remaining_s: float,
) -> str:
    """Initial paint for the Control card's body. JS `renderSync` replaces
    this on first `/status` tick."""
    session_armed = session is not None and session.get("armed")
    syncing = sync is not None
    online_ids = {d.get("camera_id") for d in devices}
    # Mutual sync is pair-wise (chirp exchange between exactly 2
    # phones), so "ready to start" still requires both A + B online
    # even when the rig grows to ≥ 3 cameras. N-way sync topology is
    # a future phase.
    both_online = "A" in online_ids and "B" in online_ids
    cooling = cooldown_remaining_s > 0.0
    disabled = syncing or session_armed or not both_online or cooling

    if syncing:
        chip = '<span class="chip armed">syncing</span>'
        received = ", ".join(sync.get("reports_received") or []) or "—"
        status_line = f'<div class="meta">Waiting for reports · {html.escape(received)}</div>'
    elif cooling:
        chip = '<span class="chip idle">cooldown</span>'
        status_line = (
            f'<div class="meta" id="sync-cooldown-val">'
            f'Ready in {cooldown_remaining_s:.1f} s</div>'
        )
    else:
        chip = '<span class="chip idle">idle</span>'
        status_line = ""

    if last_sync and last_sync.get("aborted"):
        reasons = last_sync.get("abort_reasons") or {}
        parts = [f"{k}: {html.escape(str(v))}" for k, v in sorted(reasons.items())]
        reason_txt = " · ".join(parts) if parts else "unknown"
        last_line = (
            '<div class="meta" style="color: var(--failed)">'
            f'Last · ABORTED · {reason_txt}</div>'
        )
    elif (last_sync and last_sync.get("delta_s") is not None
          and last_sync.get("distance_m") is not None):
        delta_ms = last_sync["delta_s"] * 1000.0
        dist_m = last_sync["distance_m"]
        last_line = (
            f'<div class="meta">Last · Δ={delta_ms:+.3f} ms · D={dist_m:.3f} m</div>'
        )
    else:
        last_line = '<div class="meta">No sync yet.</div>'

    reason = ""
    if not both_online:
        reason = " title=\"Need both A and B online\""
    elif session_armed:
        reason = " title=\"Stop the armed session first\""
    elif syncing:
        reason = " title=\"Sync in progress\""
    elif cooling:
        reason = f" title=\"Cooldown: {cooldown_remaining_s:.1f} s remaining\""

    btn_attrs = ' disabled' if disabled else ''
    # Quick chirp lives on the dashboard now (fallback path only).
    # /sync is the mutual-sync tuning surface; only the mutual button
    # ships here.
    mutual_btn = (
        '<form class="inline" method="POST" action="/sync/start" id="sync-form">'
        f'<button class="btn" type="submit"{btn_attrs}{reason}>Run mutual sync</button>'
        "</form>"
    )
    return (
        f'<div class="session-head">{chip}</div>'
        f'{status_line}'
        f'{last_line}'
        '<div class="card-subtitle">Methods</div>'
        f'<div class="session-actions">{mutual_btn}</div>'
    )


def _render_burst_params_body(sync_params: dict[str, Any] | None) -> str:
    """Editable burst params card body. Values hydrated by JS tickSyncParams."""
    if sync_params is None:
        p: dict[str, Any] = {
            "emit_a_at_s": [0.3, 0.5, 0.7],
            "emit_b_at_s": [1.8, 2.0, 2.2],
            "record_duration_s": 4.0,
            "search_window_s": 0.3,
        }
    else:
        required = ("emit_a_at_s", "emit_b_at_s", "record_duration_s", "search_window_s")
        missing = [key for key in required if key not in sync_params]
        if missing:
            raise KeyError(f"sync_params missing required keys: {missing}")
        p = sync_params
    emit_a = ", ".join(str(v) for v in p["emit_a_at_s"])
    emit_b = ", ".join(str(v) for v in p["emit_b_at_s"])
    dur = p["record_duration_s"]
    win = p["search_window_s"]
    return (
        '<form class="inline" action="/settings/sync_params" method="POST">'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-emit-a">A emit (s)</label>'
        f'<input id="sp-emit-a" name="emit_a_at_s" class="tuning-input" value="{html.escape(emit_a)}" '
        'title="Comma-separated offsets (s from recording start) at which Cam A emits its chirp burst">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-emit-b">B emit (s)</label>'
        f'<input id="sp-emit-b" name="emit_b_at_s" class="tuning-input" value="{html.escape(emit_b)}" '
        'title="Comma-separated offsets for Cam B — must not overlap A\'s window">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-duration">Record (s)</label>'
        f'<input id="sp-duration" name="record_duration_s" class="tuning-input" type="number" '
        f'min="1" max="30" step="0.5" value="{dur}" title="Total recording window; must exceed last emission + chirp + propagation">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-window">Search window (s)</label>'
        f'<input id="sp-window" name="search_window_s" class="tuning-input" type="number" '
        f'min="0.05" max="2.0" step="0.05" value="{win}" title="±seconds the server searches around each expected emission for a peak">'
        '</div>'
        '<div class="tuning-row">'
        '<button class="btn secondary" type="submit">Apply</button>'
        '</div>'
        '</form>'
    )


def render_setup_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    calibration_last_ts: dict[str, float] | None = None,
    markers_count: int = 0,
    preview_requested: dict[str, bool] | None = None,
    calibration_last_solves: dict[str, dict[str, Any]] | None = None,
    known_marker_ids: dict[str, list[int]] | None = None,
) -> str:
    del markers_count
    devices = devices or []
    calibrations = calibrations or []
    calibration_last_solves = calibration_last_solves or {}
    known_marker_ids = known_marker_ids or {}

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · setup</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        f"<style>{_CSS}{_SYNC_CSS}</style>"
        "</head><body data-page=\"setup\">"
        f'{_render_app_nav("setup", devices, session, calibrations, None, sync_cooldown_remaining_s)}'
        '<main class="main-sync">'
        '<section class="card page-hero">'
        '<div class="page-hero-copy">'
        '<div class="page-kicker">Calibration workflow</div>'
        '<h1 class="page-title">Camera Position Setup</h1>'
        '</div>'
        '</section>'
        '<div class="card">'
        '<h2 class="card-title">Devices &middot; Calibration</h2>'
        f'<div id="devices-body">{_render_device_rows(devices, calibrations, calibration_last_ts, preview_requested, compare_mode="toggle", calibration_last_solves=calibration_last_solves, known_marker_ids=known_marker_ids)}</div>'
        '<div class="reset-rig-row">'
        '<button type="button" class="btn small danger" data-reset-rig="1" '
        'title="Wipes all calibrations + extended markers + last-solve records. ChArUco intrinsics survive.">'
        'Reset rig</button>'
        '</div>'
        "</div>"
        "</main>"
        f"<script>{CAM_VIEW_RUNTIME_JS}</script>"
        f"<script>{OVERLAYS_RUNTIME_JS}</script>"
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        "</body></html>"
    )


def render_sync_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync: dict[str, Any] | None = None,
    last_sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    sync_params: dict[str, Any] | None = None,
) -> str:
    devices = devices or []
    calibrations = calibrations or []
    sync_js = _JS_TEMPLATE
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · sync</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        f"<style>{_CSS}{_SYNC_CSS}</style>"
        "</head><body data-page=\"sync\">"
        f'{_render_app_nav("sync", devices, session, calibrations, sync, sync_cooldown_remaining_s)}'
        '<main class="main-sync">'
        '<div class="card">'
        '<h2 class="card-title">Device sync</h2>'
        '<div id="per-cam-sync" class="per-cam-sync"><div class="trace-empty">Waiting for device status…</div></div>'
        '</div>'
        '<div class="card">'
        '<h2 class="card-title">Sync Control</h2>'
        f'<div id="sync-body">{_render_sync_body(sync, last_sync, devices, session, sync_cooldown_remaining_s)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Burst Params</h2>'
        '<p style="font-size:12px;color:var(--sub);margin:0 0 var(--s-3) 0;">A and B emit staggered bursts. Server pushes these to iOS in each sync_run — no rebuild needed.</p>'
        '<div id="tuning-status" class="tuning-status"></div>'
        f'<div id="burst-params-body">{_render_burst_params_body(sync_params)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Event log</h2>'
        '<p style="font-size:12px;color:var(--sub);margin:0 0 var(--s-3) 0;">Copy report bundles the full mutual-sync debug export (math breakdown + anomaly flags + recent log tail) for paste-to-AI diagnosis.</p>'
        '<div class="sync-log-head">'
        '<button type="button" class="btn secondary small" id="sync-report-copy" title="Fetch /sync/debug_export + event log, copy combined report to clipboard">Copy report</button>'
        '<button type="button" class="btn secondary small" id="sync-log-clear">Clear</button>'
        '</div>'
        '<pre class="sync-log" id="sync-log"></pre>'
        "</div>"
        "</main>"
        f"<script>{OVERLAYS_RUNTIME_JS}</script>"
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        f"<script>{sync_js}</script>"
        "</body></html>"
    )
