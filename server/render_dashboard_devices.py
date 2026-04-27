"""Dashboard device-card partial renderers."""
from __future__ import annotations

import datetime as _dt
import html

from cam_view_ui import render_cam_view
from render_compare import render_live_compare_camera


def _fmt_hhmm(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _render_battery_chip(
    level: float | None, state_: str | None, online: bool
) -> str:
    """Battery pill next to the online/offline chip. Renders nothing when
    the phone isn't connected (per spec: "if connected") or when the
    device hasn't reported battery yet."""
    if not online or level is None:
        return ""
    pct = int(round(float(level) * 100))
    pct = max(0, min(100, pct))
    if state_ == "charging" or state_ == "full":
        icon = "⚡"
        cls = "charging"
    elif pct <= 15:
        icon = "▁"
        cls = "low"
    elif pct <= 35:
        icon = "▃"
        cls = "mid"
    else:
        icon = "▅"
        cls = "ok"
    title_state = html.escape(state_ or "unknown")
    return (
        f'<span class="chip battery {cls}" title="battery · {title_state}">'
        f'{icon} {pct}%</span>'
    )


def _render_device_rows(
    devices: list[dict[str, object]],
    calibrations: list[str],
    calibration_last_ts: dict[str, float] | None = None,
    preview_requested: dict[str, bool] | None = None,
    compare_mode: str = "toggle",
    use_cam_view: bool = False,
    cam_view_layers: tuple[str, ...] = ("plate", "axes"),
    cam_view_layers_on: tuple[str, ...] = ("plate", "axes"),
) -> str:
    """Merged Devices card row — status + per-cam calibration actions +
    per-cam preview toggle + inline MJPEG panel. JS will replace within
    1 s; SSR paints usable buttons so there's no flash of empty state."""
    device_by_id = {str(d["camera_id"]): d for d in devices}
    calibrated = set(calibrations)
    calibration_last_ts = calibration_last_ts or {}
    preview_requested = preview_requested or {}

    def render_row(cam_id: str) -> str:
        dev = device_by_id.get(cam_id)
        online = dev is not None
        time_synced = bool(dev.get("time_synced")) if dev else False
        is_cal = cam_id in calibrated
        always_on = compare_mode == "always_on"
        preview_on = always_on or bool(preview_requested.get(cam_id))
        last_ts = calibration_last_ts.get(cam_id) if is_cal else None
        if not online:
            chip_cls, chip_label = "idle", "offline"
        elif is_cal:
            chip_cls, chip_label = "calibrated", "calibrated"
        else:
            chip_cls, chip_label = "online", "online"
        cal_dot = "ok" if is_cal else ("warn" if online else "bad")
        sync_dot = "ok" if time_synced else ("warn" if online else "bad")
        sync_label = "synced" if time_synced else ("not synced" if online else "offline")
        cal_label = (
            f"last {html.escape(_fmt_hhmm(last_ts))}" if (is_cal and last_ts)
            else ("pending" if online else "offline")
        )
        disabled_attr = "" if online else " disabled"
        auto_cal_btn = (
            f'<button type="button" class="btn small" '
            f'data-auto-cal="{html.escape(cam_id)}"{disabled_attr}>Run auto-cal</button>'
        )
        preview_btn = (
            f'<button type="button" class="btn small preview-btn{" active" if preview_on else ""}" '
            f'data-preview-cam="{html.escape(cam_id)}" '
            f'data-preview-enabled="{1 if preview_on else 0}"{disabled_attr}>'
            f'{"PREVIEW ON" if preview_on else "PREVIEW"}</button>'
        ) if not always_on else ""
        if use_cam_view:
            # New merged single-pane: real MJPEG as base, virtual
            # reprojection drawn as semi-transparent canvas overlay.
            # Calibration correctness reads as overlay-vs-image alignment.
            preview_off = (not always_on and not preview_on)
            compare_block = render_cam_view(
                cam_id,
                preview_src=("" if preview_off else f"/camera/{html.escape(cam_id)}/preview?t=0"),
                layers=list(cam_view_layers),
                layers_on=list(cam_view_layers_on),
                default_opacity=70,
                cam_label=f"Cam {cam_id}",
            )
        else:
            compare_block = render_live_compare_camera(
                cam_id,
                preview_src=f"/camera/{html.escape(cam_id)}/preview?t=0",
                preview_placeholder=("" if always_on else ("…" if preview_on else "Preview off")),
                virt_placeholder=("loading…" if is_cal else "not calibrated"),
                preview_off=(not always_on and not preview_on),
            )
        sync_led_cls = "offline" if not online else ("synced" if time_synced else "waiting")
        auto_dot = "warn" if online else "bad"
        battery_level = dev.get("battery_level") if dev else None
        battery_state = dev.get("battery_state") if dev else None
        battery_chip = _render_battery_chip(
            float(battery_level) if isinstance(battery_level, (int, float)) else None,
            str(battery_state) if isinstance(battery_state, str) else None,
            online,
        )
        return (
            f'<div class="device">'
            f'<div class="device-head">'
            f'<span class="sync-led {sync_led_cls}" title="time sync · {sync_label}"></span>'
            f'<div class="id">{html.escape(cam_id)}</div>'
            f'<div class="sub">'
            f'<span class="item {sync_dot}"><span class="dot {sync_dot}"></span>time sync · {sync_label}</span>'
            f'<span class="item {cal_dot}"><span class="dot {cal_dot}"></span>pose · {cal_label}</span>'
            f'<span class="item {auto_dot}"><span class="dot {auto_dot}"></span>auto-cal · {"idle" if online else "offline"}</span>'
            f'</div>'
            f'<div class="chip-col">{battery_chip}<span class="chip {chip_cls}">{chip_label}</span></div>'
            f'</div>'
            f'<div class="device-actions">{preview_btn}{auto_cal_btn}</div>'
            f"{compare_block}"
            f"</div>"
        )

    rows = [render_row(cam) for cam in ("A", "B")]
    rows.extend(render_row(str(d["camera_id"])) for d in devices if d["camera_id"] not in ("A", "B"))
    return f'<div class="devices-grid">{"".join(rows)}</div>'


def _render_extended_markers_body(
    device_ids: list[str],
    extended_markers: list[dict[str, object]] | None = None,
) -> str:
    """Extended-markers subsection — register new markers by auto-projecting
    them through the plate homography from a selected camera's preview
    frame. Sits at the bottom of the merged Devices card."""
    extended_markers = extended_markers or []
    cam_options = "".join(
        f'<option value="{html.escape(cam)}">{html.escape(cam)}</option>'
        for cam in device_ids
    )
    if extended_markers:
        list_items = "".join(
            f'<div class="marker-row">'
            f'<span class="mid">#{int(row["id"])}</span>'
            f'<span class="mxy">({float(row["wx"]):+0.3f}, {float(row["wy"]):+0.3f}) m</span>'
            f'<button type="button" data-marker-remove="{int(row["id"])}" '
            f'title="Remove marker {int(row["id"])}">&times;</button>'
            f"</div>"
            for row in extended_markers
        )
        list_html = f'<div class="marker-list">{list_items}</div>'
    else:
        list_html = '<div class="marker-list-empty">No extended markers registered.</div>'
    return (
        '<div class="calib-sub">'
        '<h3>Extended markers</h3>'
        '<div class="calib-register-row">'
        f'<select id="marker-register-cam">{cam_options}</select>'
        '<button type="button" class="btn small" id="marker-register-btn">Register from this camera</button>'
        '<button type="button" class="btn small secondary" id="marker-clear-btn">Clear all</button>'
        '</div>'
        f'<div id="marker-list">{list_html}</div>'
        '</div>'
    )
