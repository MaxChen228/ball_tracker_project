"""Dashboard session-card partial renderers and labels."""
from __future__ import annotations

import html


_MODE_LABELS = {
    "camera_only": "Camera-only",
}

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


def _render_shape_gate_body(shape_gate: dict[str, object] | None) -> str:
    """Aspect / fill thresholds applied after HSV + connected-components.
    Lives inside the DETECTION HSV card as a sub-form so operators tune
    the full blob filter in one place. Hot-reloaded to iOS over WS."""
    current = {"aspect_min": 0.70, "fill_min": 0.55}
    if shape_gate:
        for key in current:
            if key in shape_gate:
                try:
                    current[key] = float(shape_gate[key])
                except (TypeError, ValueError):
                    pass

    def _row(name: str, label: str, hint: str) -> str:
        val = current[name]
        slider_val = int(round(val * 100))
        return (
            '<label class="shape-row" title="' + html.escape(hint) + '">'
            f'<span class="shape-label">{html.escape(label)}</span>'
            f'<input type="range" min="0" max="100" step="1" value="{slider_val}" data-shape-range="{name}">'
            f'<input class="hsv-num" type="number" step="0.01" min="0" max="1" name="{name}" value="{val:.2f}" data-shape-number="{name}">'
            '</label>'
        )

    return (
        '<form method="POST" action="/detection/shape_gate" id="shape-gate-form" class="hsv-form shape-gate-form">'
        '<div class="hsv-subtitle">Shape gate</div>'
        '<div class="hsv-grid">'
        f'{_row("aspect_min", "ASPECT", "min(w,h)/max(w,h) — 1.0 = perfect square bbox. Lower lets elongated blobs through.")}'
        f'{_row("fill_min", "FILL", "area / (w*h) — π/4 ≈ 0.785 theoretical; real balls measure 0.63-0.70. Lower accepts partial occlusion.")}'
        '</div>'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit">Apply shape gate</button>'
        '</div>'
        '</form>'
    )


def _render_candidate_selector_body(
    tuning: dict[str, object] | None,
) -> str:
    """Server-side selector weights. Sits inside the DETECTION HSV card,
    below the shape-gate sub-form. Operator only sees `w_dist` (slider);
    `w_area = 1 - w_dist` is enforced server-side. Applies to BOTH live
    (`live_pairing._resolve_candidates`) and `server_post` paths."""
    current = {
        "r_px_expected": 12.0,
        "w_dist": 0.7,
        "dist_cost_sat_radii": 8.0,
    }
    if tuning:
        for key in current:
            if key in tuning:
                try:
                    current[key] = float(tuning[key])
                except (TypeError, ValueError):
                    pass

    r_val = current["r_px_expected"]
    w_dist = current["w_dist"]
    sat = current["dist_cost_sat_radii"]
    w_dist_slider = int(round(w_dist * 100))

    return (
        '<form method="POST" action="/detection/candidate_selector" '
        'id="candidate-selector-form" class="hsv-form shape-gate-form">'
        '<div class="hsv-subtitle">Candidate selector</div>'
        '<div class="hsv-grid">'
        '<label class="shape-row" title="Expected ball radius in px — normalizes the distance cost. '
        'Falls back here when caller has no calibration-driven prior.">'
        '<span class="shape-label">RADIUS</span>'
        '<input class="hsv-num" type="number" step="1" min="1" max="200" name="r_px_expected" '
        f'value="{r_val:.1f}" data-cs-number="r_px_expected">'
        '</label>'
        '<label class="shape-row" title="Weight on distance cost vs area cost. '
        'w_area = 1 - w_dist is derived server-side.">'
        '<span class="shape-label">W_DIST</span>'
        f'<input type="range" min="0" max="100" step="1" value="{w_dist_slider}" data-cs-range="w_dist">'
        f'<input class="hsv-num" type="number" step="0.01" min="0" max="1" name="w_dist" '
        f'value="{w_dist:.2f}" data-cs-number="w_dist">'
        '</label>'
        '<label class="shape-row" title="Distance-cost saturation radii. Beyond this many ball-radii '
        'from the predicted point, cost is clamped to 1 (area decides).">'
        '<span class="shape-label">SAT</span>'
        '<input class="hsv-num" type="number" step="0.5" min="1" max="50" name="dist_cost_sat_radii" '
        f'value="{sat:.1f}" data-cs-number="dist_cost_sat_radii">'
        '</label>'
        '</div>'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit">Apply selector</button>'
        '</div>'
        '</form>'
    )


def _render_chain_filter_body(params: dict[str, object] | None) -> str:
    """Per-camera temporal noise gate. Server-side only — applied wherever
    `chain_filter_annotate` runs (boot replay, live persist, server_post)."""
    current = {"max_frame_gap": 15, "max_jump_px": 160.0, "min_run_len": 10}
    if params:
        for key in current:
            if key in params:
                try:
                    current[key] = type(current[key])(params[key])
                except (TypeError, ValueError):
                    pass
    gap = int(current["max_frame_gap"])
    jump = float(current["max_jump_px"])
    run = int(current["min_run_len"])
    return (
        '<form method="POST" action="/detection/chain_filter" '
        'id="chain-filter-form" class="hsv-form shape-gate-form">'
        '<div class="hsv-subtitle">Chain filter</div>'
        '<div class="hsv-grid">'
        '<label class="shape-row" title="Max pixel jump between consecutive detections. '
        'Larger jumps split the chain; the short side gets tagged rejected_jump.">'
        '<span class="shape-label">JUMP</span>'
        '<input class="hsv-num" type="number" step="1" min="1" max="2000" name="max_jump_px" '
        f'value="{jump:.0f}" data-cf-number="max_jump_px">'
        '</label>'
        '<label class="shape-row" title="Min consecutive detections to keep a chain. '
        'Shorter chains are tagged rejected_flicker (or rejected_jump if a jump bounded them).">'
        '<span class="shape-label">RUN</span>'
        '<input class="hsv-num" type="number" step="1" min="1" max="240" name="min_run_len" '
        f'value="{run}" data-cf-number="min_run_len">'
        '</label>'
        '<label class="shape-row" title="Max frame-index gap before a quiet stretch breaks the chain. '
        'Frames=240/s so 15 ≈ 62 ms.">'
        '<span class="shape-label">GAP</span>'
        '<input class="hsv-num" type="number" step="1" min="1" max="240" name="max_frame_gap" '
        f'value="{gap}" data-cf-number="max_frame_gap">'
        '</label>'
        '</div>'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit">Apply chain filter</button>'
        '</div>'
        '</form>'
    )


def _render_hsv_body(
    hsv_range: dict[str, object] | None,
    shape_gate: dict[str, object] | None = None,
    candidate_selector_tuning: dict[str, object] | None = None,
    chain_filter_params: dict[str, object] | None = None,
) -> str:
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
    sg = shape_gate or {"aspect_min": 0.70, "fill_min": 0.55}
    cs = candidate_selector_tuning or {"r_px_expected": 12.0, "w_dist": 0.7, "dist_cost_sat_radii": 8.0}
    cf = chain_filter_params or {"max_jump_px": 160.0, "min_run_len": 10, "max_frame_gap": 15}
    hsv_summary = (
        f'h[{current["h_min"]}-{current["h_max"]}] '
        f's[{current["s_min"]}-{current["s_max"]}] '
        f'v[{current["v_min"]}-{current["v_max"]}]'
    )
    sg_summary = f'aspect≥{float(sg.get("aspect_min", 0.70)):.2f} fill≥{float(sg.get("fill_min", 0.55)):.2f}'
    cs_summary = (
        f'r{float(cs.get("r_px_expected", 12.0)):.0f} '
        f'wD{float(cs.get("w_dist", 0.7)):.2f} '
        f'sat{float(cs.get("dist_cost_sat_radii", 8.0)):.0f}'
    )
    cf_summary = (
        f'jump{float(cf.get("max_jump_px", 160.0)):.0f} '
        f'run{int(cf.get("min_run_len", 10))} '
        f'gap{int(cf.get("max_frame_gap", 15))}'
    )
    hsv_form = (
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
    return (
        '<details class="tune-section" open>'
        f'<summary><span class="tune-name">HSV</span><span class="tune-summary">{html.escape(hsv_summary)}</span></summary>'
        f'{hsv_form}'
        '</details>'
        '<details class="tune-section">'
        f'<summary><span class="tune-name">Shape gate</span><span class="tune-summary">{html.escape(sg_summary)}</span></summary>'
        f'{_render_shape_gate_body(shape_gate)}'
        '</details>'
        '<details class="tune-section">'
        f'<summary><span class="tune-name">Selector</span><span class="tune-summary">{html.escape(cs_summary)}</span></summary>'
        f'{_render_candidate_selector_body(candidate_selector_tuning)}'
        '</details>'
        '<details class="tune-section">'
        f'<summary><span class="tune-name">Chain filter</span><span class="tune-summary">{html.escape(cf_summary)}</span></summary>'
        f'{_render_chain_filter_body(chain_filter_params)}'
        '</details>'
    )


def _render_session_body(
    session: dict[str, object] | None,
    capture_mode: str = "camera_only",
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
    )
