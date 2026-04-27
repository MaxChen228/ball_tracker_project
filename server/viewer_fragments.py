from __future__ import annotations

import datetime as _dt

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
        body = '<div class="vid-frame empty">no clips on disk</div>'
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
        body = (
            f'<div class="vid-frame">'
            f'<div class="vid-media"{media_style}>'
            f'<video data-cam="{cam}" preload="auto" playsinline muted '
            f'src="{url}"></video>'
            f'<canvas class="virt-overlay-canvas" data-cam-canvas="{cam}"></canvas>'
            f"{pp_html}"
            f"</div>"
            f"</div>"
        )
        hint = "synced to chirp"
    cam_view_attrs = ""
    toolbar_html = ""
    if entry is not None:
        # Layer set: plate (calibration alignment), axes (orientation
        # reference), detection_live + detection_svr (per-frame ball
        # detection from each pipeline, drawn as half-transparent dots
        # on top of the video). Operator picks which pipeline they
        # trust by checkbox; both default-on so divergence is visible.
        cam_view_attrs = (
            f' data-cam-view="{cam}"'
            ' data-layers="plate,axes,detection_live,detection_svr"'
            ' data-layers-on="plate,detection_live,detection_svr"'
            ' data-default-opacity="65"'
        )
        toolbar_html = (
            '<div class="cam-view-toolbar">'
            '<button type="button" class="cv-layer on" data-layer="plate">PLATE</button>'
            '<button type="button" class="cv-layer" data-layer="axes">AXES</button>'
            '<button type="button" class="cv-layer on" data-layer="detection_live">LIVE</button>'
            '<button type="button" class="cv-layer on" data-layer="detection_svr">SVR</button>'
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


def virtual_cell_html(
    cam: str,
    *,
    pose_available: bool,
    image_width_px: int | None = None,
    image_height_px: int | None = None,
) -> str:
    """Phase 6 retired the standalone virtual cell — virtual reprojection
    is now drawn as a semi-transparent canvas overlay on top of the real
    video inside `video_cell_html`. Returns empty string so existing
    callers (videos-col grid build) keep working without change."""
    del cam, pose_available, image_width_px, image_height_px
    return ""


def hero_meta_subline(health: dict) -> str:
    parts: list[str] = [f'session {health["session_id"]}']
    dur = health.get("duration_s")
    if dur is not None:
        parts.append(f"duration {dur:.2f}s")
    rx = health.get("received_at")
    if rx is not None:
        ts = _dt.datetime.fromtimestamp(rx).strftime("%m-%d %H:%M")
        parts.append(f"received {ts}")
    mode = health.get("mode")
    if mode:
        label = (
            "live-only" if mode == "live_only"
            else "camera-only"
        )
        parts.append(f"mode {label}")
    return " · ".join(parts)


def health_banner_html(health: dict) -> str:
    tri_n = health.get("triangulated_count", 0)
    sub = hero_meta_subline(health)
    note = "points triangulated"
    if tri_n > 0:
        hero_block = (
            f'<div class="hero-card ok">'
            f'<div class="hero-title">3D Trajectory</div>'
            f'<div class="hero-tri">{tri_n}</div>'
            f'<div class="hero-note">{note}</div>'
            f'<div class="hero-sub">{sub}</div>'
            f"</div>"
        )
    else:
        hero_block = (
            f'<div class="hero-card">'
            f'<div class="hero-title">3D Trajectory</div>'
            f'<div class="hero-tri zero">—</div>'
            f'<div class="hero-note">no triangulation</div>'
            f'<div class="hero-sub">{sub}</div>'
            f"</div>"
        )

    cam_rows = "".join(
        cam_card_html(cam_id, health["cameras"][cam_id])
        for cam_id in ("A", "B")
    )
    fail_strip = failure_strip_html(health)
    return (
        f'<div class="health">'
        f'<div class="health-row">'
        f"{hero_block}"
        f'<div class="cam-stack">{cam_rows}</div>'
        f"</div>"
        f"{fail_strip}"
        f"</div>"
    )


def cam_card_html(cam_id: str, cam: dict) -> str:
    if not cam["received"]:
        return (
            f'<div class="cam-card missing">'
            f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
            f'<span class="cam-state fail">never uploaded</span>'
            f'<span class="cam-note">single-camera session, '
            f"triangulation skipped</span>"
            f"</div>"
        )

    checks = [
        ("calibrated", cam["calibrated"], "intrinsics + homography"),
        ("time synced", cam["time_synced"], "chirp anchor"),
    ]
    checks_html = "".join(
        f'<span class="check {"pass" if ok else "fail"}" title="{tip}">'
        f'<span class="mark">{"✓" if ok else "✗"}</span>{label}'
        f"</span>"
        for (label, ok, tip) in checks
    )

    counts = cam.get("counts_by_path") or {}
    _PATHS = (
        ("live", "L", "iOS live stream (on-device detection, streamed over WS)"),
        ("server_post", "S", "server post (PyAV decode + server-side detection)"),
    )

    def _rate_bits(total: int, det: int) -> tuple[str, int]:
        if total == 0:
            return ("empty", 0)
        r = det / total
        klass = "fail" if r < 0.05 else "pending" if r < 0.30 else "ok"
        pct = max(2, round(r * 100)) if det > 0 else 0
        return (klass, pct)

    # Default active path: prefer server_post if it has data, else live.
    active_path = next(
        (k for k in ("server_post", "live") if (counts.get(k) or {}).get("total", 0) > 0),
        "server_post",
    )
    path_chips: list[str] = []
    init_pct = 0
    init_klass = "empty"
    for key, abbr, tip in _PATHS:
        c = counts.get(key) or {"total": 0, "detected": 0, "fps": None}
        total = c.get("total", 0)
        det = c.get("detected", 0)
        fps = c.get("fps")
        has_data = total > 0
        klass = "on" if has_data else "off"
        rate_klass, pct = _rate_bits(total, det)
        ratio_txt = f"{det}/{total}" if has_data else "—"
        fps_txt = (
            f'<span class="fps" title="effective fps = frames / duration">'
            f"{fps:.0f} fps</span>"
            if isinstance(fps, (int, float)) else ""
        )
        is_active = has_data and key == active_path
        if is_active:
            init_pct = pct
            init_klass = rate_klass
            klass += " active"
        disabled = "" if has_data else "disabled"
        path_chips.append(
            f'<button type="button" class="path-stat {klass}" '
            f'data-path="{key}" data-pct="{pct}" data-rate-klass="{rate_klass}" '
            f'aria-pressed="{"true" if is_active else "false"}" '
            f'title="{tip}" {disabled}>'
            f'<span class="lbl">{abbr}</span>'
            f'<span class="val">{ratio_txt}</span>'
            f"{fps_txt}</button>"
        )
    stats_html = "".join(path_chips)
    telemetry_html = ""
    if init_klass == "empty":
        rate_html = '<span class="rate-empty">—</span>'
    else:
        rate_html = (
            f'<span class="rate-bar"><span class="rate-fill {init_klass}" '
            f'style="width:{init_pct}%"></span></span>'
        )

    return (
        f'<div class="cam-card received">'
        f'<div class="cam-head">'
        f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
        f'<span class="cam-state ok">uploaded</span>'
        f'<span class="cam-checks">{checks_html}</span>'
        f"</div>"
        f'<div class="cam-rate">{rate_html}<span class="cam-stats">'
        f"{stats_html}</span></div>"
        f"{telemetry_html}"
        f"</div>"
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
                reasons.append(
                    f"{' + '.join('Cam ' + c for c in no_detect)} detected no ball "
                    f"in any frame — check lighting / HSV range"
                )
            else:
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
