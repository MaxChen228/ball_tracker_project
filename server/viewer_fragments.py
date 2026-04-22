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
            frame_style = f' style="aspect-ratio:{image_width_px}/{image_height_px}"'
        else:
            frame_style = ""
        pp_html = ""
        if (
            cx is not None
            and cy is not None
            and image_width_px
            and image_height_px
        ):
            pct_x = cx / image_width_px * 100.0
            pct_y = cy / image_height_px * 100.0
            pp_html = (
                f'<div class="pp-cross" '
                f'style="left:{pct_x:.3f}%;top:{pct_y:.3f}%"></div>'
            )
        body = (
            f'<div class="vid-frame"{frame_style}>'
            f'<video data-cam="{cam}" preload="auto" playsinline muted '
            f'src="{url}"></video>'
            f'<svg class="plate-overlay-real" id="real-plate-overlay-{cam}" '
            f'aria-hidden="true"><polygon></polygon></svg>'
            f"{pp_html}"
            f"</div>"
        )
        hint = "synced to chirp"
    return (
        f'<div class="vid-cell">'
        f'<div class="vid-head">'
        f'<span class="vid-label" style="color:{color};border-color:{color};">'
        f"CAM {cam}</span>"
        f'<span class="vid-hint">{hint}</span>'
        f"</div>"
        f"{body}"
        f"</div>"
    )


def virtual_cell_html(
    cam: str,
    *,
    pose_available: bool,
    image_width_px: int | None = None,
    image_height_px: int | None = None,
) -> str:
    label_color = _camera_color(cam)
    if not pose_available:
        body = '<div class="virt-frame empty">no calibration</div>'
    else:
        w = image_width_px or 4
        h = image_height_px or 3
        aspect_style = f'style="aspect-ratio:{w}/{h}"'
        body = (
            f'<div class="virt-frame" {aspect_style} id="virt-frame-{cam}">'
            f'<canvas id="virt-canvas-{cam}"></canvas>'
            f"</div>"
        )
    hint_title = (
        "2D reprojection through the camera's full K + distortion + "
        "extrinsics. The plate pentagon should overlap the real plate "
        "visible in the MOV above; the trajectory should track the "
        "actual ball. Divergence = calibration / triangulation error."
    )
    return (
        f'<div class="virt-cell">'
        f'<div class="vid-head">'
        f'<span class="vid-label virt" title="{hint_title}">VIRT</span>'
        f'<span class="vid-label" style="color:{label_color};'
        f'border-color:{label_color};">CAM {cam}</span>'
        f'<span class="vid-hint" title="{hint_title}">reprojected K·[R|t]·P</span>'
        f"</div>"
        f"{body}"
        f"</div>"
    )


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
            "on-device" if mode == "on_device"
            else "dual" if mode == "dual"
            else "camera-only"
        )
        parts.append(f"mode {label}")
    return " · ".join(parts)


def health_banner_html(health: dict) -> str:
    tri_n = health.get("triangulated_count", 0)
    tri_od = health.get("triangulated_count_on_device", 0) or 0
    sub = hero_meta_subline(health)
    note = (
        f"points triangulated · server {tri_n} · iOS {tri_od}"
        if tri_od and health.get("mode") == "dual"
        else "points triangulated"
    )
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

    n_det = cam["n_detected"]
    n_frames = cam["n_frames"]
    telemetry = cam.get("capture_telemetry") or {}
    stats_html = (
        f'<span class="n">{n_det}</span>'
        f'<span class="of"> detected / {n_frames} frames</span>'
    )
    telemetry_html = ""
    if telemetry:
        dims = (
            f'{telemetry.get("width_px")}×{telemetry.get("height_px")}'
            if telemetry.get("width_px") and telemetry.get("height_px")
            else "—"
        )
        fps = telemetry.get("applied_fps") or telemetry.get("target_fps")
        fps_text = f"{fps:.0f} fps" if isinstance(fps, (int, float)) else "—"
        fov = telemetry.get("format_fov_deg")
        fov_text = f"{fov:.1f}°" if isinstance(fov, (int, float)) else "—"
        exposure = telemetry.get("tracking_exposure_cap") or "—"
        telemetry_html = (
            f'<div class="cam-note">'
            f"capture {dims} · {fps_text} · fov {fov_text} · exp {exposure}"
            f"</div>"
        )
    if n_frames == 0:
        rate_html = '<span class="rate-empty">—</span>'
    else:
        ratio = n_det / n_frames
        if ratio < 0.05:
            rate_class = "fail"
        elif ratio < 0.30:
            rate_class = "pending"
        else:
            rate_class = "ok"
        pct = max(2, round(ratio * 100)) if n_det > 0 else 0
        rate_html = (
            f'<span class="rate-bar"><span class="rate-fill {rate_class}" '
            f'style="width:{pct}%"></span></span>'
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
            no_detect = [c for c in ("A", "B") if cams[c]["n_detected"] == 0]
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
