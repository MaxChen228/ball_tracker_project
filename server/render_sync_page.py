"""Page-level orchestration for `/setup` and `/sync`."""
from __future__ import annotations

from typing import Any

from render_dashboard_client import _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE
from render_dashboard_devices import _render_device_rows
from render_shared import _CSS, _render_app_nav
from render_sync import (
    _JS_TEMPLATE, _SYNC_CSS, _render_sync_body, _render_sync_legend,
    _render_burst_params_body,
)
from schemas import SYNC_TRACE_MIN_PSR, SYNC_TRACE_THRESHOLD


def render_setup_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    calibration_last_ts: dict[str, float] | None = None,
    markers_count: int = 0,
    preview_requested: dict[str, bool] | None = None,
) -> str:
    del markers_count
    devices = devices or []
    calibrations = calibrations or []

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · setup</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
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
        '<div class="setup-section-title">Devices &middot; Calibration</div>'
        '<div class="card">'
        '<h2 class="card-title">Devices &middot; Calibration</h2>'
        f'<div id="devices-body">{_render_device_rows(devices, calibrations, calibration_last_ts, preview_requested, compare_mode="toggle")}</div>'
        "</div>"
        "</main>"
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
    sync_js = (
        _JS_TEMPLATE
        .replace("__THRESHOLD__", repr(float(SYNC_TRACE_THRESHOLD)))
        .replace("__MIN_PSR__", repr(float(SYNC_TRACE_MIN_PSR)))
    )
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · sync</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}{_SYNC_CSS}</style>"
        "</head><body data-page=\"sync\">"
        f'{_render_app_nav("sync", devices, session, calibrations, sync, sync_cooldown_remaining_s)}'
        '<main class="main-sync">'
        '<div class="setup-section-title">Per-device sync state</div>'
        '<div class="card">'
        '<h2 class="card-title">Device sync</h2>'
        '<div id="per-cam-sync" class="per-cam-sync"><div class="trace-empty">Waiting for device status…</div></div>'
        '</div>'
        '<div class="setup-section-title">Sync Control</div>'
        '<div class="card">'
        '<h2 class="card-title">Sync Control</h2>'
        f'<div id="sync-body">{_render_sync_body(sync, last_sync, devices, session, sync_cooldown_remaining_s)}</div>'
        "</div>"
        '<div class="setup-section-title">Burst Params</div>'
        '<div class="card">'
        '<h2 class="card-title">Burst Params</h2>'
        '<div class="card-subtitle">A and B emit staggered bursts. Server pushes these to iOS in each sync_run — no rebuild needed.</div>'
        '<div id="tuning-status" class="tuning-status"></div>'
        f'<div id="burst-params-body">{_render_burst_params_body(sync_params)}</div>'
        "</div>"
        '<div class="setup-section-title">Matched-filter trace</div>'
        '<div class="card">'
        '<h2 class="card-title">Matched-filter trace</h2>'
        '<div id="sync-trace"><div class="trace-empty">No sync run yet.</div></div>'
        f'{_render_sync_legend()}'
        '<div class="sync-log-head" style="margin-top: var(--s-3)">'
        '<span class="sync-log-label">Event log</span>'
        '<button type="button" class="btn secondary small" id="sync-report-copy" title="Fetch /sync/debug_export + event log, copy combined report to clipboard">Copy report</button>'
        '<button type="button" class="btn secondary small" id="sync-log-clear">Clear</button>'
        '</div>'
        '<pre class="sync-log" id="sync-log"></pre>'
        "</div>"
        "</main>"
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        f"<script>{sync_js}</script>"
        "</body></html>"
    )
