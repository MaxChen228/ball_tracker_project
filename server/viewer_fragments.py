from __future__ import annotations

import html

from render_scene_theme import (
    _CAMERA_COLORS,
    _FALLBACK_CAMERA_COLOR,
)


def _camera_color(camera_id: str) -> str:
    return _CAMERA_COLORS.get(camera_id, _FALLBACK_CAMERA_COLOR)


def video_cell_html(
    cam: str,
    entry: tuple[str, float] | None,
    *,
    never_coming: bool = False,
    image_width_px: int | None = None,
    image_height_px: int | None = None,
    cx: float | None = None,
    cy: float | None = None,
) -> str:
    color = _camera_color(cam)
    if entry is None:
        if never_coming:
            return (
                f'<div class="vid-cell collapsed">'
                f'<span class="vid-label" '
                f'style="color:{color};border-color:{color};">CAM {cam}</span>'
                f'<span class="vid-hint">never uploaded</span>'
                f"</div>"
            )
        # Reserve a 16:9 (or calibration-aspect) box so the cell takes the
        # same internal footprint as a populated cell — avoids the page
        # shifting once the MOV upload lands. Aspect inherits from the
        # base `.vid-media` rule; `.vid-media.empty` adds the dashed
        # border + centred message.
        if image_width_px and image_height_px:
            media_style = f' style="aspect-ratio:{image_width_px}/{image_height_px}"'
        else:
            media_style = ""
        body = (
            f'<div class="vid-frame">'
            f'<div class="vid-media empty"{media_style}>'
            f'<div class="vid-empty-msg">awaiting upload</div>'
            f'</div>'
            f'</div>'
        )
        hint = "awaiting upload"
    else:
        url, _ = entry
        if image_width_px and image_height_px:
            media_style = f' style="aspect-ratio:{image_width_px}/{image_height_px}"'
        else:
            media_style = ""
        # Phase 6 retired the HTML `pp-cross`: the cam-view runtime's
        # `plate` layer already draws a principal-point cross on the
        # canvas overlay, and rendering both produced two crosses at
        # the same pixel.
        del cx, cy
        pp_html = ""
        # Phase 6 merged pane: real video as base, virtual reprojection
        # painted into the overlay <canvas> by BallTrackerCamView. Plate
        # outline + per-frame detection blob (registered by viewer JS as
        # the `detection_blobs` layer) sit on top of the video so
        # calibration drift / detection misalignment reads as overlay
        # offset against the real ball.
        # vid-hud: per-cam DOM HUD overlay layered above video + canvas.
        # Updated each setFrame() with this cam's LIVE / SVR (idx, frame#,
        # filter mark) so the operator can read current-frame state directly
        # off the video without consulting the timeline label. DOM rather
        # than canvas so it's immune to the OVL opacity slider — at OVL=0
        # (pure video) the HUD still reads.
        body = (
            f'<div class="vid-frame">'
            f'<div class="vid-media"{media_style}>'
            f'<video data-cam="{cam}" preload="auto" playsinline muted '
            f'src="{url}"></video>'
            f'<canvas class="virt-overlay-canvas" data-cam-canvas="{cam}"></canvas>'
            f'<div class="vid-hud" data-cam-hud="{cam}"></div>'
            f"{pp_html}"
            f"</div>"
            f"</div>"
        )
        hint = "synced to chirp"
    cam_view_attrs = ""
    toolbar_html = ""
    if entry is not None:
        # Layers: PLATE + AXES (calibration overlays) + one BLOBS layer
        # per detection path. Pre-fan-out the toolbar carried a WIN
        # (winner-dot) layer per path on top of BLOBS — that's gone:
        # fan-out triangulation has no single winner, every shape-gate
        # candidate gets its own ray + 3D point, and the cost_threshold
        # slider in the viewer header controls which candidates render
        # across canvas BLOBS + 3D rays + 3D points. Keeping a "winner"
        # toggle would only redraw the lowest-cost candidate as a dot
        # already drawn as a BLOBS ring — pure noise.
        cam_view_attrs = (
            f' data-cam-view="{cam}"'
            ' data-no-badges'
            ' data-layers="plate,axes,detection_blobs_live,detection_blobs_svr"'
            ' data-layers-on="plate,detection_blobs_live"'
            ' data-default-opacity="65"'
        )
        # Path-grouped toolbar: LIVE / SVR each get a single BLOBS chip
        # because there is no winner-vs-cands distinction anymore.
        # BLOBS visibility is gated by the session-level cost_threshold
        # slider in the viewer header (see
        # `session_cost_threshold_strip_html`).
        toolbar_html = (
            '<div class="cam-view-toolbar">'
            '<button type="button" class="cv-layer on" data-layer="plate">PLATE</button>'
            '<button type="button" class="cv-layer" data-layer="axes">AXES</button>'
            '<span class="cv-path-group" data-path="live">'
            '<span class="cv-path-lbl">LIVE</span>'
            '<button type="button" class="cv-layer on" data-layer="detection_blobs_live">BLOBS</button>'
            '</span>'
            '<span class="cv-path-group" data-path="svr">'
            '<span class="cv-path-lbl">SVR</span>'
            '<button type="button" class="cv-layer" data-layer="detection_blobs_svr">BLOBS</button>'
            '</span>'
            '<span class="cv-opacity">OVL'
            '<input type="range" min="0" max="100" step="1" value="65" aria-label="Overlay opacity">'
            '</span>'
            '</div>'
        )
    return (
        f'<div class="vid-cell"{cam_view_attrs}>'
        f'<div class="vid-head">'
        f'<span class="vid-label" style="color:{color};border-color:{color};">'
        f"CAM {cam}</span>"
        f'<span class="vid-hint">{hint}</span>'
        f"</div>"
        f"{body}"
        f"{toolbar_html}"
        f"</div>"
    )


def session_cost_threshold_strip_html(
    cost_threshold: float | None, session_id: str,
) -> str:
    """Per-session cost_threshold slider for the viewer's nav bar.

    Drag = client-side preview only (filter blobs + 3D points by
    `cost > threshold`). Apply = POST /sessions/{sid}/recompute with
    the chosen value, server reruns pairing + segmenter on existing
    candidates and overwrites SessionResult. Initial value is
    `SessionResult.cost_threshold` (None → 1.0 = "no filter") which the
    server injected onto the page.

    `session_id` is interpolated into the Apply handler's URL — the
    pattern is `^s_[0-9a-f]{4,32}$` (validated server-side too) so no
    HTML injection here, but escaping anyway is cheap.
    """
    initial = 1.0 if cost_threshold is None else float(cost_threshold)
    sid_attr = html.escape(session_id, quote=True)
    return (
        '<div class="session-tuning" role="group" '
        'aria-label="Per-session selector cost threshold">'
        '<label class="st-label">Cost ≤</label>'
        '<input type="range" min="0" max="1" step="0.01" '
        f'value="{initial:.2f}" data-session-cost-threshold '
        'aria-label="cost threshold" '
        'oninput="window._setCostThreshold && window._setCostThreshold(this.value); '
        'document.querySelector(\'[data-session-cost-value]\').textContent = '
        '(+this.value).toFixed(2); '
        'document.querySelector(\'[data-session-recompute]\').disabled = false;">'
        f'<span class="st-value" data-session-cost-value>{initial:.2f}</span>'
        '<button type="button" class="st-apply" data-session-recompute disabled '
        f'data-session-id="{sid_attr}" '
        'onclick="window._applyCostThreshold && window._applyCostThreshold(this);">'
        'Apply</button>'
        '</div>'
    )


def health_nav_strip_html(health: dict) -> str:
    """Compact per-session status strip for the viewer's nav bar. Replaces
    the legacy `.health` row (hero card + per-cam cards) — same data, ~1/3
    the vertical space. Failures are surfaced separately via
    `failure_strip_html`, which renders below the nav as its own banner."""
    tri_n = health.get("triangulated_count", 0)
    sid = health.get("session_id", "")
    dur = health.get("duration_s")
    mode = health.get("mode")

    tri_klass = "ok" if tri_n > 0 else "zero"
    tri_label = f"{tri_n}" if tri_n > 0 else "—"
    tri_title = "points triangulated" if tri_n > 0 else "no triangulation"

    meta_bits: list[str] = [f'<span class="hs-sid">{sid}</span>']
    if dur is not None:
        meta_bits.append(f'<span class="hs-dur">{dur:.2f}s</span>')
    if mode == "camera_only":
        meta_bits.append('<span class="hs-mode">camera-only</span>')
    elif mode == "live_only":
        meta_bits.append('<span class="hs-mode">live-only</span>')

    cams_html = "".join(
        cam_strip_chip_html(cam_id, health["cameras"][cam_id])
        for cam_id in ("A", "B")
    )
    return (
        f'<div class="health-strip" role="status" aria-label="Session health">'
        f'<span class="hs-tri {tri_klass}" title="{tri_title}">'
        f'<span class="hs-tri-n">{tri_label}</span>'
        f'<span class="hs-tri-lbl">pts</span></span>'
        f'<span class="hs-meta">{" · ".join(meta_bits)}</span>'
        f'<span class="hs-cams">{cams_html}</span>'
        f"</div>"
    )


def cam_strip_chip_html(cam_id: str, cam: dict) -> str:
    """One inline chip per camera for the nav-bar health strip. Drops the
    legacy rate-bar (a 1px wide bar that was always 100% post-arrival,
    no info) in favour of pure path-stat chips. Detection-rate tier is
    encoded in each chip's `data-rate-klass` attribute and visualised by
    border colour via the `.path-stat[data-rate-klass=...]` CSS rules."""
    if not cam["received"]:
        return (
            f'<span class="hs-cam missing" data-cam="{cam_id}" '
            f'title="single-camera session, triangulation skipped">'
            f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
            f'<span class="hs-fail">never uploaded</span>'
            f"</span>"
        )

    checks = [
        ("✓" if cam["calibrated"] else "✗", cam["calibrated"], "calibrated"),
        ("✓" if cam["time_synced"] else "✗", cam["time_synced"], "time synced"),
    ]
    checks_html = "".join(
        f'<span class="hs-check {"pass" if ok else "fail"}" title="{tip}">'
        f"{mark}</span>"
        for (mark, ok, tip) in checks
    )

    counts = cam.get("counts_by_path") or {}
    _PATHS = (
        ("live", "L", "iOS live stream (on-device detection, streamed over WS)"),
        ("server_post", "S", "server post (PyAV decode + server-side detection)"),
    )

    def _rate_klass(total: int, det: int) -> str:
        if total == 0:
            return "empty"
        r = det / total
        return "fail" if r < 0.05 else "pending" if r < 0.30 else "ok"

    path_chips: list[str] = []
    for key, abbr, tip in _PATHS:
        c = counts.get(key) or {"total": 0, "detected": 0, "fps": None}
        total = c.get("total", 0)
        det = c.get("detected", 0)
        fps = c.get("fps")
        has_data = total > 0
        rate_klass = _rate_klass(total, det)
        ratio_txt = f"{det}/{total}" if has_data else "—"
        fps_txt = (
            f'<span class="fps" title="effective fps = frames / duration">'
            f"{fps:.0f} fps</span>"
            if isinstance(fps, (int, float)) else ""
        )
        klass = "on" if has_data else "off"
        path_chips.append(
            f'<span class="path-stat {klass}" data-path="{key}" '
            f'data-rate-klass="{rate_klass}" title="{tip}">'
            f'<span class="lbl">{abbr}</span>'
            f'<span class="val">{ratio_txt}</span>'
            f"{fps_txt}</span>"
        )

    return (
        f'<span class="hs-cam received" data-cam="{cam_id}">'
        f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
        f'<span class="hs-checks">{checks_html}</span>'
        f'<span class="hs-paths">{"".join(path_chips)}</span>'
        f"</span>"
    )


def failure_strip_html(health: dict) -> str:
    cams = health["cameras"]
    tri_n = health.get("triangulated_count", 0)
    server_err = health.get("error")
    reasons: list[str] = []
    missing = [c for c in ("A", "B") if not cams[c]["received"]]
    if missing:
        reasons.append(
            f"{' + '.join('Cam ' + c for c in missing)} never uploaded "
            f"— triangulation skipped"
        )
    else:
        uncal = [c for c in ("A", "B") if not cams[c]["calibrated"]]
        if uncal:
            reasons.append(
                f"{' + '.join('Cam ' + c for c in uncal)} missing calibration "
                f"(intrinsics or homography) — run Calibration screen"
            )
        unsyn = [c for c in ("A", "B") if not cams[c]["time_synced"]]
        if unsyn:
            reasons.append(
                f"{' + '.join('Cam ' + c for c in unsyn)} has no chirp anchor "
                f"— re-run 時間校正 before arming"
            )
        if server_err:
            reasons.append(f"server error: {server_err}")
        elif tri_n == 0 and all(cams[c]["received"] for c in ("A", "B")):
            def _any_detected_across_paths(cam: dict) -> bool:
                counts = cam.get("counts_by_path") or {}
                return any((counts.get(k) or {}).get("detected", 0) > 0
                           for k in ("live", "server_post"))
            no_detect = [c for c in ("A", "B") if not _any_detected_across_paths(cams[c])]
            if no_detect:
                # `no_detect` stands on its own — fires regardless of mode,
                # because zero detections across both paths is a real
                # failure signal even in the live-only intermediate state.
                reasons.append(
                    f"{' + '.join('Cam ' + c for c in no_detect)} detected no ball "
                    f"in any frame — check lighting / HSV range"
                )
            elif health.get("mode") == "camera_only":
                # Only complain about empty triangulation once the MOV is
                # on disk — i.e. /pitch landed and SessionResult should
                # have been built. In `live_only` (MOV still uploading)
                # an empty result is by-design, not a failure.
                reasons.append(
                    "triangulation produced no points — check A/B pairing "
                    "window or frame timing"
                )

    if not reasons:
        return ""
    body = "<br>".join(reasons)
    return (
        f'<div class="fail-strip">'
        f'<span class="icon">!</span>'
        f"<span>{body}</span>"
        f"</div>"
    )
