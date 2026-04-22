from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


def _device_rows(state: Any) -> list[dict[str, Any]]:
    return [
        {
            "camera_id": d.camera_id,
            "last_seen_at": d.last_seen_at,
            "time_synced": d.time_synced,
        }
        for d in state.online_devices()
    ]


def build_pages_router(
    *,
    get_state: Callable[[], Any],
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def events_index() -> HTMLResponse:
        from render_dashboard import render_events_index_html

        state = get_state()
        session = state.session_snapshot()
        sync_run = state.current_sync()
        return HTMLResponse(
            render_events_index_html(
                events=state.events(),
                devices=_device_rows(state),
                session=session.to_dict() if session is not None else None,
                calibrations=sorted(state.calibrations().keys()),
                capture_mode=state.current_mode().value,
                default_paths=sorted(p.value for p in state.default_paths()),
                live_session=state.live_session_summary(),
                sync=sync_run.to_dict() if sync_run is not None else None,
                sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
                chirp_detect_threshold=state.chirp_detect_threshold(),
                heartbeat_interval_s=state.heartbeat_interval_s(),
                tracking_exposure_cap=state.tracking_exposure_cap().value,
                capture_height_px=state.capture_height_px(),
                calibration_last_ts={
                    cam: path.stat().st_mtime
                    for cam in state.calibrations().keys()
                    for path in [state._calibration_path(cam)]
                    if path.exists()
                },
                preview_requested=state._preview.requested_map(),
            )
        )

    @router.get("/sync", response_class=HTMLResponse)
    def sync_page() -> HTMLResponse:
        from render_sync import render_sync_html

        state = get_state()
        session = state.session_snapshot()
        sync_run = state.current_sync()
        last_sync = state.last_sync_result()
        return HTMLResponse(
            render_sync_html(
                devices=_device_rows(state),
                session=session.to_dict() if session is not None else None,
                calibrations=sorted(state.calibrations().keys()),
                sync=sync_run.to_dict() if sync_run is not None else None,
                last_sync=last_sync.model_dump() if last_sync is not None else None,
                sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
                chirp_detect_threshold=state.chirp_detect_threshold(),
                heartbeat_interval_s=state.heartbeat_interval_s(),
                capture_height_px=state.capture_height_px(),
                tracking_exposure_cap=state.tracking_exposure_cap().value,
            )
        )

    @router.get("/setup", response_class=HTMLResponse)
    def setup_page() -> HTMLResponse:
        from render_sync import render_setup_html

        state = get_state()
        session = state.session_snapshot()
        return HTMLResponse(
            render_setup_html(
                devices=_device_rows(state),
                session=session.to_dict() if session is not None else None,
                calibrations=sorted(state.calibrations().keys()),
                sync_cooldown_remaining_s=state.sync_cooldown_remaining_s(),
                calibration_last_ts={
                    cam: path.stat().st_mtime
                    for cam in state.calibrations().keys()
                    for path in [state._calibration_path(cam)]
                    if path.exists()
                },
                markers_count=len(state._marker_registry.all_records()),
                preview_requested=state._preview.requested_map(),
            )
        )

    return router
