"""Renderers for `/setup` and `/sync`.

`/setup` is the geometry-only camera calibration surface.
`/sync` is the dedicated time-sync + runtime-tuning surface.
Both pages reuse the dashboard design tokens so navigation and controls
stay visually consistent across the app."""
from __future__ import annotations

import html
from typing import Any

from render_dashboard_client import _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE
from render_dashboard_devices import _render_device_rows
from render_shared import _CSS, _render_app_nav
from render_sync_style import _SYNC_CSS
from render_sync_client import _JS_TEMPLATE


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
    p = sync_params or {}
    emit_a = ", ".join(str(v) for v in p.get("emit_a_at_s", [0.3, 0.5, 0.7]))
    emit_b = ", ".join(str(v) for v in p.get("emit_b_at_s", [1.8, 2.0, 2.2]))
    dur = p.get("record_duration_s", 4.0)
    win = p.get("search_window_s", 0.3)
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


from render_sync_page import render_setup_html, render_sync_html
