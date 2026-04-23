"""Dashboard session-card partial renderers and labels."""
from __future__ import annotations

import html


_PATH_LABELS = {
    "live": ("Live stream", "iOS → WS"),
    "server_post": ("Server post-pass", "PyAV + OpenCV"),
}

_HSV_PRESETS = {
    "tennis": {
        "label": "Tennis",
        "h_min": 25,
        "h_max": 55,
        "s_min": 90,
        "s_max": 255,
        "v_min": 90,
        "v_max": 255,
    },
    "baseball": {
        "label": "Baseball",
        "h_min": 100,
        "h_max": 130,
        "s_min": 140,
        "s_max": 255,
        "v_min": 40,
        "v_max": 255,
    },
}


def _render_detection_paths_body(
    default_paths: list[str] | None,
    session: dict[str, object] | None = None,
) -> str:
    armed = bool(session and session.get("armed"))
    active = set(default_paths or ["server_post"])
    if armed:
        active = set(session.get("paths") or active)
        chips = "".join(
            f'<span class="path-chip on">{html.escape(_PATH_LABELS.get(path, (path, ""))[0])}</span>'
            for path in ("live", "server_post")
            if path in active
        ) or '<span class="path-chip">none</span>'
        return (
            '<div class="path-lock">'
            '<span class="mode-label">Paths</span>'
            f'<div class="path-chip-row">{chips}</div>'
            "</div>"
        )

    rows: list[str] = []
    for path in ("live", "server_post"):
        title, subtitle = _PATH_LABELS.get(path, (path, ""))
        checked = " checked" if path in active else ""
        rows.append(
            '<label class="path-option">'
            f'<input type="checkbox" name="paths" value="{path}"{checked}>'
            '<span class="copy">'
            f'<span class="title">{html.escape(title)}</span>'
            f'<span class="sub">{html.escape(subtitle)}</span>'
            "</span>"
            "</label>"
        )
    return (
        '<form method="POST" action="/detection/paths" id="paths-form">'
        '<div class="paths-stack">'
        f'{"".join(rows)}'
        '</div>'
        '<div class="paths-actions">'
        '<button class="btn" type="submit">Apply</button>'
        '</div>'
        '</form>'
    )


def _render_active_session_body(live_session: dict[str, object] | None) -> str:
    if not live_session:
        return '<div class="active-empty">No active session.</div>'
    sid = html.escape(str(live_session.get("session_id", "—")))
    frame_counts = live_session.get("frame_counts") or {}
    paths = live_session.get("paths") or []
    paths_on = set(str(p) for p in paths)
    paths_completed = set(str(p) for p in (live_session.get("paths_completed") or []))
    armed = bool(live_session.get("armed", True))
    path_chips = "".join(
        f'<span class="path-chip on">{html.escape(_PATH_LABELS.get(path, (path, ""))[0])}</span>'
        for path in paths
    ) or '<span class="path-chip">none</span>'
    point_count = int(live_session.get("point_count") or 0)
    a_frames = int(frame_counts.get("A") or 0)
    b_frames = int(frame_counts.get("B") or 0)
    if "live" in paths_on:
        live_body = (
            '<div class="active-grid">'
            f'<span><span class="k">A frames</span><span class="v">{a_frames}</span></span>'
            f'<span><span class="k">B frames</span><span class="v">{b_frames}</span></span>'
            f'<span><span class="k">Live 3D pts</span><span class="v">{point_count}</span></span>'
            '</div>'
        )
    else:
        live_body = '<div class="active-empty">Live stream disabled for this session.</div>'
    postpass_chips = []
    for path, label in (("server_post", "srv"),):
        if path not in paths_on:
            continue
        state = "done" if path in paths_completed else ("pending" if armed else "stopped")
        postpass_chips.append(
            f'<span class="postpass-chip {state}">{html.escape(label)}: {state}</span>'
        )
    postpass_html = (
        f'<div class="postpass-row">{"".join(postpass_chips)}</div>'
        if postpass_chips
        else ""
    )
    return (
        '<div class="active-head">'
        f'<span class="chip armed">{"●REC" if armed else "ended"}</span>'
        f'<span class="session-id">{sid}</span>'
        '</div>'
        f'<div class="path-chip-row">{path_chips}</div>'
        f'{live_body}'
        f'{postpass_html}'
    )


def _render_hsv_body(hsv_range: dict[str, object] | None) -> str:
    current = {
        "h_min": 25,
        "h_max": 55,
        "s_min": 90,
        "s_max": 255,
        "v_min": 90,
        "v_max": 255,
    }
    if hsv_range:
        for key in current:
            if key in hsv_range:
                current[key] = int(hsv_range[key])

    def _row(axis: str, upper: int) -> str:
        lo_key = f"{axis}_min"
        hi_key = f"{axis}_max"
        return (
            '<div class="hsv-row">'
            f'<div class="hsv-label">{html.escape(axis.upper())}</div>'
            '<div class="hsv-pair">'
            f'<label><span>Min</span><input type="range" min="0" max="{upper}" value="{current[lo_key]}" data-hsv-range="{lo_key}"><input class="hsv-num" type="number" name="{lo_key}" min="0" max="{upper}" value="{current[lo_key]}" data-hsv-number="{lo_key}"></label>'
            f'<label><span>Max</span><input type="range" min="0" max="{upper}" value="{current[hi_key]}" data-hsv-range="{hi_key}"><input class="hsv-num" type="number" name="{hi_key}" min="0" max="{upper}" value="{current[hi_key]}" data-hsv-number="{hi_key}"></label>'
            '</div>'
            '</div>'
        )

    preset_buttons = "".join(
        f'<button type="button" class="btn small secondary" data-hsv-preset="{name}" '
        f'data-h-min="{preset["h_min"]}" data-h-max="{preset["h_max"]}" '
        f'data-s-min="{preset["s_min"]}" data-s-max="{preset["s_max"]}" '
        f'data-v-min="{preset["v_min"]}" data-v-max="{preset["v_max"]}">'
        f'{html.escape(str(preset["label"]))}</button>'
        for name, preset in _HSV_PRESETS.items()
    )
    return (
        '<form method="POST" action="/detection/hsv" id="hsv-form" class="hsv-form">'
        '<div class="hsv-presets">'
        f'{preset_buttons}'
        '</div>'
        '<div class="hsv-grid">'
        f'{_row("h", 179)}'
        f'{_row("s", 255)}'
        f'{_row("v", 255)}'
        '</div>'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit">Apply HSV</button>'
        '</div>'
        '</form>'
    )


def _render_session_body(
    session: dict[str, object] | None,
    capture_mode: str = "camera_only",
    default_paths: list[str] | None = None,
    devices: list[dict[str, object]] | None = None,
    calibrations: list[str] | None = None,
    arm_readiness: dict[str, object] | None = None,
) -> str:
    armed = session is not None and bool(session.get("armed"))
    devices = devices or []
    calibrated = set(calibrations or [])
    online = {str(d["camera_id"]) for d in devices}
    synced = {str(d["camera_id"]) for d in devices if d.get("time_synced")}
    if arm_readiness is None:
        usable = sorted(cam for cam in online if cam in calibrated)
        uncalibrated = sorted(cam for cam in online if cam not in calibrated)
        missing: list[str] = []
        warnings: list[str] = []
        if not online:
            missing.append("no camera online")
        elif uncalibrated:
            missing.extend(f"{cam} not calibrated" for cam in uncalibrated)
        elif len(usable) >= 2:
            missing.extend(f"{cam} not time-synced" for cam in usable if cam not in synced)
        else:
            warnings.append(f"single-camera session ({usable[0]}); no triangulation")
    else:
        missing = [str(v) for v in (arm_readiness.get("blockers") or [])]
        warnings = [str(v) for v in (arm_readiness.get("warnings") or [])]
    arm_ok = not missing
    chip_html = (
        '<span class="chip armed">armed</span>'
        if armed
        else '<span class="chip idle">idle</span>'
    )
    sid_html = (
        f'<span class="session-id">{html.escape(str(session["id"]))}</span>'
        if session and session.get("id")
        else ""
    )
    arm_disabled = armed or not arm_ok
    arm_title = "; ".join(missing or warnings) if (missing or warnings) else "Ready to record"
    arm_btn = (
        '<form class="inline" method="POST" action="/sessions/arm">'
        f'<button class="btn" type="submit"{" disabled" if arm_disabled else ""} '
        f'title="{html.escape(arm_title)}">Arm session</button>'
        "</form>"
    )
    stop_btn = (
        '<form class="inline" method="POST" action="/sessions/stop">'
        f'<button class="btn danger" type="submit"{"" if armed else " disabled"}>Stop</button>'
        "</form>"
    )
    sync_trigger_btn = (
        '<form class="inline" method="POST" action="/sync/trigger">'
        f'<button class="btn secondary" type="submit"{" disabled" if armed else ""}>Quick chirp</button>'
        "</form>"
    )

    def _sync_led_html(cam: str) -> str:
        dev = next((d for d in devices if d.get("camera_id") == cam), None)
        if dev is None:
            cls, tip = "off", f"{cam}: offline"
        elif dev.get("time_synced"):
            age = dev.get("time_sync_age_s")
            age_txt = f" · {age:.0f}s ago" if isinstance(age, (int, float)) else ""
            cls, tip = "synced", f"{cam}: synced{age_txt}"
        else:
            cls, tip = "waiting", f"{cam}: waiting"
        return f'<span class="sync-led {cls}" title="{html.escape(tip)}">{cam}</span>'

    sync_leds = _sync_led_html("A") + _sync_led_html("B")

    clear_btn = ""
    if not armed and session and session.get("id"):
        clear_btn = (
            '<form class="inline" method="POST" action="/sessions/clear">'
            '<button class="btn" type="submit">Clear</button>'
            "</form>"
        )

    gate_row = ""
    if not armed and missing:
        gate_row = (
            '<div class="arm-gate">'
            f'<span class="gate-label">Need:</span> {html.escape(", ".join(missing))}'
            "</div>"
        )
    elif not armed and warnings:
        gate_row = (
            '<div class="arm-gate">'
            f'<span class="gate-label">Mode:</span> {html.escape(", ".join(warnings))}'
            "</div>"
        )
    return (
        f'<div class="session-head">{chip_html}{sid_html}</div>'
        f'<div class="session-actions">{arm_btn}{stop_btn}{clear_btn}</div>'
        f"{gate_row}"
        '<div class="card-subtitle">Time Sync</div>'
        f'<div class="session-actions">{sync_trigger_btn}{sync_leds}</div>'
        f"{_render_detection_paths_body(default_paths, session)}"
    )
