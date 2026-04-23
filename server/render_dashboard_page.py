"""Dashboard page-level orchestration."""
from __future__ import annotations

from typing import Any

from reconstruct import build_calibration_scene
from render_dashboard_client import _JS_TEMPLATE
from render_dashboard_events import _render_events_body
from render_dashboard_html import render_dashboard_html as _render_dashboard_html
from render_dashboard_session import _render_active_session_body, _render_session_body
from render_dashboard_style import _CSS
from render_scene import _build_figure
from render_shared import _render_app_nav


def render_events_index_html(
    events: list[dict[str, Any]],
    trash_count: int = 0,
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    arm_readiness: dict[str, Any] | None = None,
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
    """Render the dashboard page shell and initial SSR partials."""
    del chirp_detect_threshold
    del heartbeat_interval_s
    del tracking_exposure_cap
    del capture_height_px
    del calibration_last_ts
    del extended_markers
    del preview_requested

    devices = devices or []
    calibrations = calibrations or []

    from main import state  # local import: avoid circular at module load time

    scene = build_calibration_scene(state.calibrations())
    fig = _build_figure(scene)
    fig.update_layout(
        title=None,
        margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-6.0, 6.0],
        scene_yaxis_range=[-6.0, 6.0],
        scene_zaxis_range=[-0.2, 3.5],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
        scene_uirevision="dashboard-canvas",
    )
    scene_div = fig.to_html(include_plotlyjs=False, full_html=False, div_id="scene-root")

    nav_html = _render_app_nav(
        "dashboard", devices, session, calibrations, sync, sync_cooldown_remaining_s, arm_readiness
    )
    active_html = _render_active_session_body(live_session)
    session_html = _render_session_body(
        session, capture_mode, default_paths, devices, calibrations, arm_readiness
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
