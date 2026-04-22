"""Dashboard renderer for `/` — three-zone layout (top nav + 440px
sidebar + full-bleed 3D canvas) styled after the PHYSICS_LAB design
system. The canvas shows a live 3D scene of the plate plus whichever
cameras have a calibration persisted; the sidebar carries devices,
session controls, and the events list. All three columns tick from
JSON endpoints (`/status`, `/calibration/state`, `/events`) so the page
never has to reload to reflect a new calibration or a new pitch."""
from __future__ import annotations

import html
from typing import Any

from reconstruct import build_calibration_scene
from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    DRAW_PLATE_OVERLAY_JS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
)
from render_dashboard_client import _JS_TEMPLATE, _JS_TEMPLATE_RAW, _resolve_js_template
from render_dashboard_devices import (
    _render_device_rows,
    _render_extended_markers_body,
)
from render_dashboard_events import _render_events_body
from render_dashboard_html import render_dashboard_html as _render_dashboard_html
from render_dashboard_style import _CSS
from render_dashboard_session import (
    _PATH_LABELS,
    _render_active_session_body,
    _render_detection_paths_body,
    _render_session_body,
)
from render_scene import _build_figure
from render_shared import (
    _render_app_nav,
    _render_nav_status,
    _render_primary_nav,
)
from render_tuning import (
    _render_chirp_threshold_body,
    _render_tuning_body,
)
from schemas import Device, Session





def render_events_index_html(
    events: list[dict[str, Any]],
    trash_count: int = 0,
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    capture_mode: str = "camera_only",
    default_paths: list[str] | None = None,
    live_session: dict[str, Any] | None = None,
    sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    chirp_detect_threshold: float = 0.18,
    heartbeat_interval_s: float = 1.0,
    tracking_exposure_cap: str = "frame_duration",
    capture_height_px: int = 1080,
    calibration_last_ts: dict[str, float] | None = None,
    extended_markers: list[dict[str, Any]] | None = None,
    preview_requested: dict[str, bool] | None = None,
) -> str:
    """Render the dashboard: top nav + sidebar (devices / session / events)
    + a canvas showing the current calibration scene. All three panels
    hydrate from JSON ticks after first paint — the initial SSR avoids a
    flash of empty content while the first fetch is in flight."""
    devices = devices or []
    calibrations = calibrations or []

    from main import state  # local import: avoid circular at module load time

    scene = build_calibration_scene(state.calibrations())
    fig = _build_figure(scene)
    # Dashboard tweaks vs viewer defaults:
    #  - title=None: corner pill + nav already say what this is
    #  - fixed bbox + manual aspect ratio: with aspectmode="data" a single
    #    3m-distant camera blows up the bounding box and shrinks the
    #    50 cm plate to a dot. Pinning ±3.5 m XY / 2 m Z to the rig
    #    geometry keeps the plate readable whether 0, 1, or 2 cams are
    #    calibrated. Viewer leaves "data" so the ball trajectory still
    #    fits naturally.
    fig.update_layout(
        title=None, margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-6.0, 6.0],
        scene_yaxis_range=[-6.0, 6.0],
        scene_zaxis_range=[-0.2, 3.5],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
        # Pin scene uirevision to the SAME string both at first SSR paint
        # and on every /calibration/state tick. Without this override,
        # _build_figure's default ("viewer-scene") disagrees with whatever
        # the dashboard JS sends via Plotly.react, and each mismatch
        # triggers Plotly to treat UI state (camera/zoom) as "stale" and
        # snap it back to the default eye position. Same-string across
        # all paints = camera stays wherever the user dragged it.
        scene_uirevision="dashboard-canvas",
    )
    scene_div = fig.to_html(include_plotlyjs=False, full_html=False, div_id="scene-root")

    nav_html = _render_app_nav(
        "dashboard", devices, session, calibrations, sync, sync_cooldown_remaining_s
    )
    active_html = _render_active_session_body(live_session)
    session_html = _render_session_body(
        session, capture_mode, default_paths, devices, calibrations
    )
    events_html = _render_events_body(events)
    return _render_dashboard_html(
        css=_CSS,
        nav_html=nav_html,
        active_html=active_html,
        session_html=session_html,
        events_html=events_html,
        scene_div=scene_div,
        dashboard_js=_JS_TEMPLATE,
        trash_count=trash_count,
    )
