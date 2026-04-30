"""Dashboard session-card partial renderers and labels."""
from __future__ import annotations

import html

from presets import Preset, hsv_as_dict


_MODE_LABELS = {
    "camera_only": "Camera-only",
}

_PATH_LABELS = {
    "live": ("Live stream", "iOS → WS"),
    "server_post": ("Server post-pass", "PyAV + OpenCV"),
}


def _render_hsv_axis_row(axis: str, upper: int, lo: int, hi: int) -> str:
    lo_key = f"{axis}_min"
    hi_key = f"{axis}_max"
    return (
        '<div class="hsv-row">'
        f'<div class="hsv-label">{html.escape(axis.upper())}</div>'
        '<div class="hsv-pair">'
        f'<label><span>Min</span>'
        f'<input type="range" min="0" max="{upper}" value="{lo}" data-hsv-range="{lo_key}">'
        f'<input class="hsv-num" type="number" name="{lo_key}" min="0" max="{upper}" value="{lo}" data-hsv-number="{lo_key}">'
        '</label>'
        f'<label><span>Max</span>'
        f'<input type="range" min="0" max="{upper}" value="{hi}" data-hsv-range="{hi_key}">'
        f'<input class="hsv-num" type="number" name="{hi_key}" min="0" max="{upper}" value="{hi}" data-hsv-number="{hi_key}">'
        '</label>'
        '</div>'
        '</div>'
    )


def _render_shape_row(
    *,
    name: str,
    label: str,
    hint: str,
    val: float,
) -> str:
    """0..1 slider+number pair for the shape-gate card."""
    slider_val = int(round(val * 100))
    return (
        f'<label class="shape-row" title="{html.escape(hint)}">'
        f'<span class="shape-label">{html.escape(label)}</span>'
        f'<input type="range" min="0" max="100" step="1" value="{slider_val}" data-shape-range="{name}">'
        f'<input class="hsv-num" type="number" step="0.01" min="0" max="1" name="{name}" '
        f'value="{val:.2f}" data-shape-number="{name}">'
        '</label>'
    )


def _render_manage_modal(presets: list[Preset], active_preset: object) -> str:
    """List-of-presets modal opened by the Manage button. SSR'd into
    the page and toggled via the native `<dialog>` element so we don't
    need a JS modal lib. Each row carries `data-*` slug attributes that
    the JS in 15_hsv_controls.js binds Use / Duplicate / Delete handlers
    to.

    The currently-bound preset (if any) is marked `★current`. Built-in
    seeds are deletable — restart re-seeds — so this does not lock
    them in the UI; that simplification matches CLAUDE.md's "experimental
    phase" stance over a UX-side readonly affordance.
    """
    if not presets:
        rows = '<tr><td colspan="3" class="preset-empty">No presets — boot seed_builtins should have written tennis + blue_ball; check server log.</td></tr>'
    else:
        row_html = []
        for p in presets:
            current = "★ current" if p.name == active_preset else ""
            row_html.append(
                '<tr>'
                f'<td><code>{html.escape(p.name)}</code> '
                f'<span class="preset-current-tag">{current}</span></td>'
                f'<td>{html.escape(p.label)}</td>'
                '<td class="preset-actions">'
                f'<button type="button" class="btn small" data-preset-use="{html.escape(p.name)}">Use</button>'
                f'<button type="button" class="btn small secondary" data-preset-duplicate="{html.escape(p.name)}">Duplicate</button>'
                f'<button type="button" class="btn small danger" data-preset-delete="{html.escape(p.name)}">Delete</button>'
                '</td>'
                '</tr>'
            )
        rows = "".join(row_html)
    return (
        '<dialog id="preset-manage-modal" class="preset-modal">'
        '<div class="preset-modal-head">'
        '<h3>Preset library</h3>'
        '<button type="button" class="btn small secondary" data-preset-modal-close>Close</button>'
        '</div>'
        '<table class="preset-table">'
        '<thead><tr><th>Slug</th><th>Label</th><th>Actions</th></tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table>'
        '<div class="preset-modal-status" data-preset-modal-status></div>'
        '</dialog>'
    )


def _render_hsv_body(
    detection_config: dict[str, object] | None,
    presets: list[Preset],
) -> str:
    """Phase 3 of unified-config redesign: single form, single Apply,
    identity header. The previous three-form / three-Apply layout is
    gone — every slider edits a shared form which the JS Apply button
    POSTs as a full triple to `/detection/config` in one shot.

    `detection_config` is the wire shape returned by
    `GET /detection/config` (so the dashboard can refetch on mount and
    re-hydrate the same way without an alternative shape). When None
    or missing fields, falls back to Tennis-preset values so the SSR
    boot path renders something coherent.
    """
    cfg = detection_config or {}
    hsv = cfg.get("hsv") or {}
    sg = cfg.get("shape_gate") or {}
    preset_name = cfg.get("preset")
    modified_fields = cfg.get("modified_fields") or []
    presets_by_name = {p.name: p for p in presets}

    h_lo, h_hi = int(hsv.get("h_min", 25)), int(hsv.get("h_max", 55))
    s_lo, s_hi = int(hsv.get("s_min", 90)), int(hsv.get("s_max", 255))
    v_lo, v_hi = int(hsv.get("v_min", 90)), int(hsv.get("v_max", 255))
    aspect_min = float(sg.get("aspect_min", 0.70))
    fill_min = float(sg.get("fill_min", 0.55))

    # Identity header ------------------------------------------------
    # Three meaningful states surface a tag; the fourth (custom — no
    # preset bound) renders nothing because the un-highlighted preset
    # buttons already convey "not on any preset" and a static "Custom"
    # pill is just clutter.
    #   pure     — preset matches a known preset, no modifications
    #              (preset button shows `.active`)
    #   modified — preset matches a known preset, values differ;
    #              tag carries the modified count + Reset button
    #   deleted  — preset name is set but the on-disk preset has been
    #              deleted (operator removed it via DELETE /presets/
    #              <name>). The next `set_detection_config` clears the
    #              dangling reference; until then the header signals
    #              the broken identity instead of silently dropping it.
    identity_html = ""
    if preset_name is not None:
        if preset_name not in presets_by_name:
            identity_label = f"{preset_name} (preset deleted)"
            identity_class = "identity-deleted"
        elif modified_fields:
            identity_label = f"{presets_by_name[preset_name].label} · modified ({len(modified_fields)})"
            identity_class = "identity-modified"
        else:
            identity_label = f"{presets_by_name[preset_name].label}"
            identity_class = "identity-pure"

        reset_btn = ""
        if preset_name in presets_by_name and modified_fields:
            # Only show reset when there's something to revert to. A
            # dangling reference has no target → no reset affordance.
            reset_btn = (
                f'<button type="button" class="btn small" '
                f'data-detection-reset-preset="{html.escape(preset_name)}" '
                f'title="Snap back to {html.escape(presets_by_name[preset_name].label)} preset values">'
                'Reset to preset</button>'
            )

        identity_html = (
            '<div class="detection-identity">'
            f'<span class="identity-tag {identity_class}">{html.escape(identity_label)}</span>'
            f'{reset_btn}'
            '</div>'
        )

    # Preset picker --------------------------------------------------
    def _preset_button(p: Preset) -> str:
        d = hsv_as_dict(p)
        active = " active" if p.name == preset_name and not modified_fields else ""
        return (
            f'<button type="button" class="btn small secondary{active}" data-hsv-preset="{html.escape(p.name)}" '
            f'data-h-min="{d["h_min"]}" data-h-max="{d["h_max"]}" '
            f'data-s-min="{d["s_min"]}" data-s-max="{d["s_max"]}" '
            f'data-v-min="{d["v_min"]}" data-v-max="{d["v_max"]}" '
            f'data-aspect-min="{p.shape_gate.aspect_min:.2f}" '
            f'data-fill-min="{p.shape_gate.fill_min:.2f}">'
            f'{html.escape(p.label)}</button>'
        )
    preset_buttons = "".join(_preset_button(p) for p in presets)

    # Sub-section markup --------------------------------------------
    hsv_block = (
        '<div class="detection-section">'
        '<div class="hsv-subtitle">HSV</div>'
        '<div class="hsv-grid">'
        f'{_render_hsv_axis_row("h", 179, h_lo, h_hi)}'
        f'{_render_hsv_axis_row("s", 255, s_lo, s_hi)}'
        f'{_render_hsv_axis_row("v", 255, v_lo, v_hi)}'
        '</div>'
        '</div>'
    )
    shape_block = (
        '<div class="detection-section">'
        '<div class="hsv-subtitle">Shape gate</div>'
        '<div class="hsv-grid">'
        + _render_shape_row(
            name="aspect_min", label="ASPECT",
            hint="min(w,h)/max(w,h) — 1.0 = perfect square bbox. Lower lets elongated blobs through.",
            val=aspect_min,
        )
        + _render_shape_row(
            name="fill_min", label="FILL",
            hint="area / (w*h) — π/4 ≈ 0.785 theoretical; real balls measure 0.63-0.70. Lower accepts partial occlusion.",
            val=fill_min,
        )
        + '</div></div>'
    )

    # "Save as new" + "Manage" — phase 3 of the preset library refactor.
    # Save-as-new POSTs the current form values under a new slug
    # (operator supplies name+label via prompt). Manage opens a modal
    # that lists every preset on disk and exposes Use / Duplicate /
    # Delete actions per row. Both rely on the JS in 15_hsv_controls.js.
    library_actions = (
        '<div class="hsv-library-actions">'
        '<button type="button" class="btn small" data-preset-save-as>'
        '+ Save as new</button>'
        '<button type="button" class="btn small secondary" data-preset-manage>'
        'Manage…</button>'
        '</div>'
    )
    manage_modal = _render_manage_modal(presets, preset_name)

    # Single Apply button at the bottom drives a JS fetch to
    # /detection/config carrying the (HSV, shape_gate) pair. No
    # per-section Apply: phase 3 collapses sub-button-presses into one.
    return (
        f'{identity_html}'
        '<div class="hsv-presets">'
        f'{preset_buttons}'
        '</div>'
        f'{library_actions}'
        f'{manage_modal}'
        '<form id="detection-config-form" class="hsv-form" data-detection-config-form>'
        f'{hsv_block}{shape_block}'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit" data-detection-apply>Apply detection config</button>'
        '<span class="detection-apply-status" data-detection-apply-status></span>'
        '</div>'
        '</form>'
    )


def _render_session_body(
    session: dict[str, object] | None,
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
