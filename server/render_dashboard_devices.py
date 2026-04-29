"""Dashboard device-card partial renderers."""
from __future__ import annotations

import datetime as _dt
import html
import time as _time

from cam_view_ui import render_cam_view


def _fmt_hhmm(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _fmt_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


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


def _reproj_class(reproj: float | None) -> str | None:
    """PHYSICS_LAB palette: <5 ok / <15 warn / ≥15 bad. None → no badge."""
    if not isinstance(reproj, (int, float)):
        return None
    if reproj < 5.0:
        return "ok"
    if reproj < 15.0:
        return "warn"
    return "bad"


def _fmt_age(age_s: float) -> str:
    if age_s < 60:
        return f"{int(age_s)}s ago"
    if age_s < 3600:
        return f"{int(age_s / 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s / 3600)}h ago"
    return f"{int(age_s / 86400)}d ago"


def _render_marker_chip(
    marker_id: int, *, kind: str, state_cls: str,
) -> str:
    """One marker pill: id label + plate/extended kind + state color.
    `state_cls` ∈ {"used", "buffer", "missing"} drives the color."""
    return (
        f'<span class="marker-chip {state_cls} {kind}" '
        f'title="{kind} marker {marker_id} · {state_cls}">'
        f'{marker_id}</span>'
    )


def _render_marker_coverage(
    plate_ids: list[int],
    extended_ids: list[int],
    last_solve_ids: set[int],
    buffer_ids: set[int],
) -> str:
    """Marker coverage map for a single cam.

    Each known marker appears as a chip colored by state:
      - used: id was in the last successful solve → green
      - buffer: id is in the current accumulation buffer (not yet solved) → blue
      - missing: known but never used by this cam → gray

    Operator-facing first-principles question this answers: "which
    markers does the rig have, and which has my cam seen?"
    """
    if not plate_ids and not extended_ids:
        return ""

    def _chip(mid: int, kind: str) -> str:
        if mid in last_solve_ids and mid not in buffer_ids:
            return _render_marker_chip(mid, kind=kind, state_cls="used")
        if mid in buffer_ids:
            return _render_marker_chip(mid, kind=kind, state_cls="buffer")
        return _render_marker_chip(mid, kind=kind, state_cls="missing")

    plate_html = "".join(_chip(mid, "plate") for mid in plate_ids)
    ext_html = "".join(_chip(mid, "extended") for mid in extended_ids)
    sections = [
        f'<div class="marker-row"><span class="marker-row-label">PLATE</span>{plate_html}</div>'
    ]
    if extended_ids:
        sections.append(
            f'<div class="marker-row"><span class="marker-row-label">EXT</span>{ext_html}</div>'
        )
    return f'<div class="marker-coverage">{"".join(sections)}</div>'


def _render_buffer_block(
    cam_id: str,
    buf: dict[str, object] | None,
    is_calibrated: bool,
    *,
    plate_ids: list[int],
    extended_ids: list[int],
    now: float,
) -> str:
    """Per-cam calibration info panel.

    Layout (top → bottom):
      1. Status line: phase + age (always-visible last calibrated time)
      2. Active accumulation strip: ids + (n/5) when buffer non-empty
      3. Last successful solve: marker count + reproj + delta vs prior
      4. Failure counter (when failure_count > 0)
      5. Marker coverage map (plate + extended chips, color-coded)

    Designed so an operator can see "what state am I in" and
    "what's already been done" without re-running calibration.
    Persists across buffer clears via `last_solve` field on the buffer
    summary (see state_calibration.MarkerAccumulatorStore.summary)."""
    count = int(buf.get("count", 0)) if buf else 0
    buffer_id_list: list[int] = list(buf.get("marker_ids", [])) if buf else []
    failure_count = int(buf.get("failure_count", 0)) if buf else 0
    last_solve_dict = (buf or {}).get("last_solve") if buf else None
    last_reproj = buf.get("last_reproj_px") if buf else None

    parts: list[str] = []

    # 1. Status line
    if count > 0:
        ready = bool(buf.get("ready", False))
        cls = "ready" if ready else "accumulating"
        label = "READY TO SOLVE" if ready else f"ACCUMULATING {count}/5"
        parts.append(f'<div class="cal-status {cls}">{label}</div>')
    elif is_calibrated and last_solve_dict:
        age = now - float(last_solve_dict["solved_at"])
        parts.append(
            f'<div class="cal-status calibrated">CALIBRATED · '
            f'{html.escape(_fmt_age(age))}</div>'
        )
    elif is_calibrated:
        parts.append('<div class="cal-status calibrated">CALIBRATED</div>')
    else:
        parts.append('<div class="cal-status uncalibrated">NOT CALIBRATED</div>')

    # 2. Active accumulation
    if count > 0:
        ids_str = "[" + ", ".join(str(i) for i in buffer_id_list) + "]"
        parts.append(
            f'<div class="cal-line accum">'
            f'<span class="cal-line-label">accum</span>'
            f'<span class="cal-line-value">{html.escape(ids_str)}</span>'
            f'</div>'
        )

    # 3. Last successful solve summary
    if last_solve_dict:
        ls_ids = list(last_solve_dict.get("marker_ids") or [])
        ls_reproj = last_solve_dict.get("reproj_px")
        ls_solver = last_solve_dict.get("solver") or "?"
        ls_n_ext = int(last_solve_dict.get("n_extended_used") or 0)
        delta_pos = last_solve_dict.get("delta_position_cm")
        delta_ang = last_solve_dict.get("delta_angle_deg")

        n_total = len(ls_ids)
        n_plate = n_total - ls_n_ext
        breakdown = f"{n_plate} plate"
        if ls_n_ext > 0:
            breakdown += f" + {ls_n_ext} ext"
        parts.append(
            f'<div class="cal-line last-solve">'
            f'<span class="cal-line-label">last</span>'
            f'<span class="cal-line-value">'
            f'{n_total} markers ({html.escape(breakdown)}) · '
            f'{html.escape(ls_solver)}</span>'
            f'</div>'
        )

        meta_parts: list[str] = []
        rcls = _reproj_class(ls_reproj)
        if rcls is not None:
            meta_parts.append(
                f'<span class="reproj-badge {rcls}" '
                f'title="last solve reprojection error">'
                f'reproj <strong>{ls_reproj:.1f}</strong> px</span>'
            )
        if isinstance(delta_pos, (int, float)) and isinstance(delta_ang, (int, float)):
            meta_parts.append(
                f'<span class="cal-delta" '
                f'title="movement vs previous calibration">'
                f'Δ <strong>{delta_pos:.1f}</strong> cm / '
                f'<strong>{delta_ang:.2f}</strong>°</span>'
            )
        if meta_parts:
            parts.append(f'<div class="cal-meta">{"".join(meta_parts)}</div>')

    # 4. Failure counter (only relevant when buffer kept after a bad solve)
    if failure_count > 0:
        rcls = _reproj_class(last_reproj)
        reproj_str = (
            f"reproj {last_reproj:.1f} px"
            if isinstance(last_reproj, (int, float)) else ""
        )
        parts.append(
            f'<div class="cal-line cal-fail">'
            f'<span class="cal-line-label">failed</span>'
            f'<span class="cal-line-value">'
            f'{failure_count}/3 consecutive · {html.escape(reproj_str)}'
            f'</span></div>'
        )

    # 5. Marker coverage map
    last_solve_set = (
        set(last_solve_dict.get("marker_ids") or []) if last_solve_dict else set()
    )
    parts.append(_render_marker_coverage(
        plate_ids, extended_ids, last_solve_set, set(buffer_id_list),
    ))

    return f'<div class="cal-panel" data-cam="{html.escape(cam_id)}">{"".join(parts)}</div>'


def _render_device_rows(
    devices: list[dict[str, object]],
    calibrations: list[str],
    calibration_last_ts: dict[str, float] | None = None,
    preview_requested: dict[str, bool] | None = None,
    compare_mode: str = "toggle",
    cam_view_layers: tuple[str, ...] = ("plate", "axes"),
    cam_view_layers_on: tuple[str, ...] = ("plate", "axes"),
    calibration_buffers: dict[str, dict[str, object]] | None = None,
    known_marker_ids: dict[str, list[int]] | None = None,
) -> str:
    """Merged Devices card row — status + per-cam calibration actions +
    per-cam preview toggle + inline MJPEG panel. JS will replace within
    1 s; SSR paints usable buttons so there's no flash of empty state."""
    device_by_id = {str(d["camera_id"]): d for d in devices}
    calibrated = set(calibrations)
    calibration_last_ts = calibration_last_ts or {}
    preview_requested = preview_requested or {}
    calibration_buffers = calibration_buffers or {}
    known_marker_ids = known_marker_ids or {}
    plate_marker_ids = list(known_marker_ids.get("plate") or [])
    extended_marker_ids = list(known_marker_ids.get("extended") or [])
    now = _time.time()

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
        if is_cal and last_ts:
            cal_label = (
                f"last {html.escape(_fmt_hhmm(last_ts))} "
                f"({html.escape(_fmt_age(_time.time() - last_ts))})"
            )
        else:
            cal_label = "pending" if online else "offline"
        disabled_attr = "" if online else " disabled"
        buf = calibration_buffers.get(cam_id) or {}
        buf_count = int(buf.get("count", 0))
        # Button label reflects state:
        #   empty + calibrated → "Re-calibrate" (one-click full redo from scratch)
        #   empty + uncalibrated → "Calibrate"
        #   non-empty buffer → "Calibrate (n/5)" so operator sees progress
        if buf_count > 0:
            cal_btn_label = f"Calibrate ({buf_count}/5)"
        elif is_cal:
            cal_btn_label = "Re-calibrate"
        else:
            cal_btn_label = "Calibrate"
        auto_cal_btn = (
            f'<button type="button" class="btn small" '
            f'data-auto-cal="{html.escape(cam_id)}"{disabled_attr}>'
            f'{html.escape(cal_btn_label)}</button>'
        )
        # Clear button only when buffer has something to clear; idempotent
        # against an empty buffer but the button shouldn't be a no-op
        # affordance.
        clear_btn = (
            f'<button type="button" class="btn small secondary" '
            f'data-clear-buffer="{html.escape(cam_id)}"{disabled_attr}>Clear</button>'
            if buf_count > 0 else ""
        )
        preview_btn = (
            f'<button type="button" class="btn small preview-btn{" active" if preview_on else ""}" '
            f'data-preview-cam="{html.escape(cam_id)}" '
            f'data-preview-enabled="{1 if preview_on else 0}"{disabled_attr}>'
            f'{"PREVIEW ON" if preview_on else "PREVIEW"}</button>'
        ) if not always_on else ""
        # Merged single-pane: real MJPEG as base, virtual reprojection
        # drawn as semi-transparent canvas overlay. Calibration
        # correctness reads as overlay-vs-image alignment.
        preview_off = (not always_on and not preview_on)
        compare_block = render_cam_view(
            cam_id,
            preview_src=("" if preview_off else f"/camera/{html.escape(cam_id)}/preview?t=0"),
            layers=list(cam_view_layers),
            layers_on=list(cam_view_layers_on),
            default_opacity=70,
            cam_label=f"Cam {cam_id}",
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
        buffer_block = _render_buffer_block(
            cam_id, buf, is_cal,
            plate_ids=plate_marker_ids,
            extended_ids=extended_marker_ids,
            now=now,
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
            f"{buffer_block}"
            f'<div class="device-actions">{preview_btn}{auto_cal_btn}{clear_btn}</div>'
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
