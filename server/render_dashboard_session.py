"""Dashboard session-card partial renderers and labels."""
from __future__ import annotations

import html


_MODE_LABELS = {
    "camera_only": "Camera-only",
    "on_device": "On-device",
    "dual": "Dual",
}

_PATH_LABELS = {
    "live": ("Live stream", "iOS → WS"),
    "ios_post": ("iOS post-pass", "on-device analyzer"),
    "server_post": ("Server post-pass", "PyAV + OpenCV"),
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
            for path in ("live", "ios_post", "server_post")
            if path in active
        ) or '<span class="path-chip">none</span>'
        return (
            '<div class="path-lock">'
            '<span class="mode-label">Paths</span>'
            f'<div class="path-chip-row">{chips}</div>'
            "</div>"
        )

    rows: list[str] = []
    for path in ("live", "ios_post", "server_post"):
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
        return '<div class="active-empty">No active live stream.</div>'
    sid = html.escape(str(live_session.get("session_id", "—")))
    frame_counts = live_session.get("frame_counts") or {}
    paths = live_session.get("paths") or []
    path_chips = "".join(
        f'<span class="path-chip on">{html.escape(_PATH_LABELS.get(path, (path, ""))[0])}</span>'
        for path in paths
    ) or '<span class="path-chip">none</span>'
    point_count = int(live_session.get("point_count") or 0)
    a_frames = int(frame_counts.get("A") or 0)
    b_frames = int(frame_counts.get("B") or 0)
    return (
        '<div class="active-head">'
        '<span class="chip armed">live</span>'
        f'<span class="session-id">{sid}</span>'
        '</div>'
        f'<div class="path-chip-row">{path_chips}</div>'
        '<div class="active-grid">'
        f'<span><span class="k">A frames</span><span class="v">{a_frames}</span></span>'
        f'<span><span class="k">B frames</span><span class="v">{b_frames}</span></span>'
        f'<span><span class="k">Live 3D pts</span><span class="v">{point_count}</span></span>'
        '</div>'
    )


def _render_session_body(
    session: dict[str, object] | None,
    capture_mode: str = "camera_only",
    default_paths: list[str] | None = None,
    devices: list[dict[str, object]] | None = None,
    calibrations: list[str] | None = None,
) -> str:
    armed = session is not None and bool(session.get("armed"))
    devices = devices or []
    calibrated = set(calibrations or [])
    online = {str(d["camera_id"]) for d in devices}
    synced = {str(d["camera_id"]) for d in devices if d.get("time_synced")}
    missing: list[str] = []
    for cam in ("A", "B"):
        if cam not in online:
            missing.append(f"{cam} offline")
        elif cam not in calibrated:
            missing.append(f"{cam} not calibrated")
        elif cam not in synced:
            missing.append(f"{cam} not time-synced")
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
    arm_title = "; ".join(missing) if missing else "Ready to record"
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
    return (
        f'<div class="session-head">{chip_html}{sid_html}</div>'
        f'<div class="session-actions">{arm_btn}{stop_btn}{clear_btn}</div>'
        f"{gate_row}"
        '<div class="card-subtitle">Time Sync</div>'
        f'<div class="session-actions">{sync_trigger_btn}{sync_leds}</div>'
        f"{_render_detection_paths_body(default_paths, session)}"
    )
