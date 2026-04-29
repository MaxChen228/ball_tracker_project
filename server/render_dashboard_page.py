"""Dashboard page-level orchestration."""
from __future__ import annotations

from typing import Any

from reconstruct import build_calibration_scene
from render_dashboard_client import _JS_TEMPLATE
from render_dashboard_events import _render_events_body
from render_dashboard_html import render_dashboard_html as _render_dashboard_html
from render_dashboard_intrinsics import _render_intrinsics_body
from render_dashboard_session import _render_hsv_body, _render_session_body
from render_dashboard_style import _CSS
from cam_view_ui import CAM_VIEW_RUNTIME_JS
from overlays_ui import OVERLAYS_RUNTIME_JS
from render_scene import _build_figure
from render_shared import _render_app_nav
from render_tuning import _render_tuning_body


def render_events_index_html(
    events: list[dict[str, Any]],
    trash_count: int = 0,
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    arm_readiness: dict[str, Any] | None = None,
    hsv_range: dict[str, int] | None = None,
    shape_gate: dict[str, float] | None = None,
    candidate_selector_tuning: dict[str, float] | None = None,
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
    session_html = _render_session_body(
        session, devices, calibrations, arm_readiness
    )
    hsv_html = _render_hsv_body(hsv_range, shape_gate, candidate_selector_tuning)
    tuning_html = _render_tuning_body(
        heartbeat_interval_s=heartbeat_interval_s,
        tracking_exposure_cap=tracking_exposure_cap,
        capture_height_px=capture_height_px,
    )
    events_html = _render_events_body(events)
    # SSR the intrinsics card with whatever we already know. The JS layer
    # refreshes from /calibration/intrinsics on mount + every 5 s so stale
    # counts self-heal without a page reload.
    intrinsics_records = [
        {
            "device_id": r.device_id,
            "device_model": r.device_model,
            "source_width_px": r.source_width_px,
            "source_height_px": r.source_height_px,
            "fx": r.intrinsics.fx,
            "fy": r.intrinsics.fy,
            "rms_reprojection_px": r.rms_reprojection_px,
            "n_images": r.n_images,
            "calibrated_at": r.calibrated_at,
            "distortion": r.intrinsics.distortion,
        }
        for r in sorted(state.device_intrinsics().values(), key=lambda rr: rr.device_id)
    ]
    online_roles = {
        dev.camera_id: {
            "device_id": dev.device_id,
            "device_model": dev.device_model,
        }
        for dev in state.online_devices()
    }
    intrinsics_html = _render_intrinsics_body(intrinsics_records, online_roles)
    return _render_dashboard_html(
        css=_CSS,
        nav_html=nav_html,
        session_html=session_html,
        hsv_html=hsv_html,
        tuning_html=tuning_html,
        intrinsics_html=intrinsics_html,
        events_html=events_html,
        scene_div=scene_div,
        overlays_js=OVERLAYS_RUNTIME_JS,
        cam_view_js=CAM_VIEW_RUNTIME_JS,
        dashboard_js=_JS_TEMPLATE,
        trash_count=trash_count,
    )
