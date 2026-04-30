"""Shared tuning-body renderers for dashboard-adjacent pages."""
from __future__ import annotations


def _render_chirp_threshold_body(
    chirp_detect_threshold: float,
    mutual_sync_threshold: float = 0.10,
) -> str:
    """Two independent threshold rows — quick-chirp (third-device up+down
    sweep; strong signal) vs mutual-sync (two-phone cross-detection; the
    far phone's chirp can land much quieter). Shared slider in the
    original design forced operators to tune for the weaker modality,
    losing false-positive margin on the stronger one."""
    q = f"{chirp_detect_threshold:.2f}"
    m = f"{mutual_sync_threshold:.2f}"
    return (
        '<form class="tuning-row" method="POST" '
        'action="/settings/chirp_threshold" id="tuning-chirp-form">'
        '<span class="tuning-label">Quick chirp thr</span>'
        f'<input type="range" name="threshold" min="0.02" max="0.60" step="0.01" '
        f'value="{q}" '
        'oninput="document.getElementById(\'tuning-chirp-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-chirp-num" name="threshold" '
        f'min="0.02" max="0.60" step="0.01" value="{q}" '
        'form="tuning-chirp-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '</form>'
        '<form class="tuning-row" method="POST" '
        'action="/settings/mutual_sync_threshold" id="tuning-mutual-form">'
        '<span class="tuning-label">Mutual sync thr</span>'
        f'<input type="range" name="threshold" min="0.02" max="0.60" step="0.01" '
        f'value="{m}" '
        'oninput="document.getElementById(\'tuning-mutual-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-mutual-num" name="threshold" '
        f'min="0.02" max="0.60" step="0.01" value="{m}" '
        'form="tuning-mutual-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '</form>'
    )


def _render_tuning_body(
    heartbeat_interval_s: float,
    tracking_exposure_cap: str = "frame_duration",
    capture_height_px: int = 1080,
) -> str:
    """Linked slider + segmented-control rows. Each form posts on
    submit — the `<input>`s share a `form` attribute and an `oninput`
    handler that mirrors slider <-> number, so the operator sees the
    number update as they drag. Submit fires on the change event after
    release (slider) or blur / Enter (number)."""
    ivl = f"{heartbeat_interval_s:g}"
    return (
        '<form class="tuning-row" method="POST" '
        'action="/settings/heartbeat_interval" id="tuning-hb-form">'
        '<span class="tuning-label">Heartbeat</span>'
        f'<input type="range" name="interval_s" min="1" max="10" step="0.5" '
        f'value="{ivl}" '
        'oninput="document.getElementById(\'tuning-hb-num\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        f'<input type="number" id="tuning-hb-num" name="interval_s" '
        f'min="1" max="10" step="0.5" value="{ivl}" '
        'form="tuning-hb-form" '
        'oninput="this.form.querySelector(\'input[type=range]\').value=this.value" '
        'onchange="this.form.requestSubmit()">'
        '<span class="tuning-unit">s</span>'
        '</form>'
        + ''.join(
            '<div class="tuning-row">'
            '<span class="tuning-label">Tracking exp</span>'
            '<div class="mode-segmented" role="radiogroup" aria-label="Tracking exposure cap">'
            + ''.join(
                f'<form class="inline" method="POST" action="/settings/tracking_exposure_cap">'
                f'<input type="hidden" name="mode" value="{mode}">'
                f'<button class="btn{"" if mode == tracking_exposure_cap else " secondary"} small" '
                f'type="submit">{label}</button>'
                f'</form>'
                for mode, label in (
                    ("frame_duration", "1/240"),
                    ("shutter_500", "1/500"),
                    ("shutter_1000", "1/1000"),
                )
            )
            + '</div>'
            '</div>'
            for _ in (0,)
        )
        + ''.join(
            '<div class="tuning-row">'
            '<span class="tuning-label">Capture</span>'
            '<div class="mode-segmented" role="radiogroup" aria-label="Capture resolution">'
            + ''.join(
                f'<form class="inline" method="POST" action="/settings/capture_height">'
                f'<input type="hidden" name="height" value="{h}">'
                f'<button class="btn{"" if h == capture_height_px else " secondary"} small" '
                f'type="submit">{h}p</button>'
                f'</form>'
                for h in (720, 1080)
            )
            + '</div>'
            '</div>'
            for _ in (0,)
        )
    )


def _render_strike_zone_body(strike_zone: dict | None) -> str:
    zone = strike_zone or {}
    height_cm = int(zone.get("batter_height_cm", 175))
    z_bottom_m = float(zone.get("z_bottom_m", 0.4725))
    z_top_m = float(zone.get("z_top_m", 0.93625))
    z_height_m = float(zone.get("z_height_m", z_top_m - z_bottom_m))
    width_m = float(zone.get("x_half_m", 0.2159)) * 2.0
    depth_m = float(zone.get("y_back_m", 0.432)) - float(zone.get("y_front_m", 0.0))
    return (
        '<form class="strike-zone-form" id="strike-zone-form" data-strike-zone-form>'
        '<div class="tuning-row">'
        '<span class="tuning-label">Batter height</span>'
        f'<input type="range" min="120" max="220" step="1" value="{height_cm}" '
        'name="height_cm" data-strike-zone-range>'
        f'<input type="number" min="120" max="220" step="1" value="{height_cm}" '
        'name="height_cm" data-strike-zone-number>'
        '<span class="tuning-unit">cm</span>'
        '</div>'
        '<div class="strike-zone-summary" data-strike-zone-summary>'
        '<div class="strike-zone-summary-row">'
        f'<span><span class="k">Bottom</span><span class="v" data-strike-zone-bottom>{z_bottom_m:.3f} m</span></span>'
        f'<span><span class="k">Top</span><span class="v" data-strike-zone-top>{z_top_m:.3f} m</span></span>'
        f'<span><span class="k">Height</span><span class="v" data-strike-zone-height>{z_height_m:.3f} m</span></span>'
        '</div>'
        '<div class="strike-zone-summary-row">'
        f'<span><span class="k">Width</span><span class="v" data-strike-zone-width>{width_m:.3f} m</span></span>'
        f'<span><span class="k">Depth</span><span class="v" data-strike-zone-depth>{depth_m:.3f} m</span></span>'
        '</div>'
        '</div>'
        '<div class="strike-zone-note">ABS-style single-height proxy for 3D display: bottom 27.0% and top 53.5% of standing height.</div>'
        '<div class="hsv-actions">'
        '<button class="btn" type="submit" data-strike-zone-apply>Apply strike zone</button>'
        '<span class="detection-apply-status" data-strike-zone-status></span>'
        '</div>'
        '</form>'
    )
