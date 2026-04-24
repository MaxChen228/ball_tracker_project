"""Compatibility facade for dashboard rendering modules."""
from __future__ import annotations

from render_dashboard_client import _JS_TEMPLATE, _JS_TEMPLATE_RAW, _resolve_js_template
from render_dashboard_devices import _render_device_rows, _render_extended_markers_body
from render_dashboard_events import _render_events_body
from render_dashboard_intrinsics import _render_intrinsics_body
from render_dashboard_page import render_events_index_html
from render_dashboard_session import (
    _PATH_LABELS,
    _render_session_body,
)
from render_dashboard_style import _CSS
from render_shared import _render_app_nav, _render_nav_status, _render_primary_nav
from render_tuning import _render_chirp_threshold_body, _render_tuning_body

__all__ = [
    "_CSS",
    "_JS_TEMPLATE",
    "_JS_TEMPLATE_RAW",
    "_PATH_LABELS",
    "_render_app_nav",
    "_render_chirp_threshold_body",
    "_render_device_rows",
    "_render_events_body",
    "_render_extended_markers_body",
    "_render_intrinsics_body",
    "_render_nav_status",
    "_render_primary_nav",
    "_render_session_body",
    "_render_tuning_body",
    "_resolve_js_template",
    "render_events_index_html",
]
