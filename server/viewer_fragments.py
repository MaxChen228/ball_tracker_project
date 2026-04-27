from __future__ import annotations

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
        # data-no-badges opts out of the runtime's status/calibration/RMS
        # badge slot — viewer surfaces those signals through its own
        # vid-head label, no .cam-view-badges container in the DOM. The
        # runtime treats the attr as an explicit "no badges, please" and
        # skips the missing-container warn.
        cam_view_attrs = (
            f' data-cam-view="{cam}"'
            ' data-no-badges'
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
