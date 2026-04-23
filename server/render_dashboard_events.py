"""Dashboard event-list partial renderers."""
from __future__ import annotations

import html
from typing import Any

from render_dashboard_session import _PATH_LABELS


def _render_events_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<div class="events-empty">No sessions received yet.</div>'
    parts: list[str] = []
    for e in events:
        sid = html.escape(e["session_id"])
        cams = " · ".join(html.escape(c) for c in e.get("cameras", [])) or "—"
        status = html.escape(e.get("status", ""))
        stat_label = status.replace("_", " ")
        mode_val = e.get("mode")
        capture_mode = (
            "live-only" if mode_val == "live_only"
            else "camera-only"
        )
        # Each pipeline gets an independent chip showing: state (on/err/-)
        # + detected-frame count summed across A/B. "L 67" reads quickly as
        # "live produced 67 detections"; "S —" means server pipeline never
        # ran. Status and count come from separate sources so we can show
        # e.g. "error" even when the count is 0.
        path_status = e.get("path_status") or {}
        path_counts = e.get("n_ball_frames_by_path") or {}
        path_chip_specs = (("live", "L"), ("server_post", "S"))
        path_chip_titles = {
            "live": "Live — iOS real-time detection (WS streamed)",
            "server_post": "SVR — server-side detection on decoded MOV",
        }
        def _path_chip(path: str, label: str) -> str:
            status = path_status.get(path, "-")
            counts = path_counts.get(path) or {}
            total = sum(int(v) for v in counts.values())
            if status == "done":
                cls = " on"
            elif status == "error":
                cls = " err"
            else:
                cls = ""
            count_html = f'<span class="pc">{total}</span>' if total > 0 else ""
            title = path_chip_titles.get(path, path)
            if counts:
                title += " · " + ", ".join(f"{c}:{n}" for c, n in sorted(counts.items()))
            return (
                f'<span class="path-chip{cls}" title="{html.escape(title)}">'
                f"{label}{count_html}</span>"
            )
        path_html = "".join(_path_chip(p, l) for p, l in path_chip_specs)
        mean = "—" if e.get("mean_residual_m") is None else format(e["mean_residual_m"], ".4f")
        peak_z = "—" if e.get("peak_z_m") is None else format(e["peak_z_m"], ".2f")
        duration = "—" if e.get("duration_s") is None else format(e["duration_s"], ".2f")
        has_metrics = (
            (e.get("n_triangulated") or 0) > 0
            or mean != "—" or peak_z != "—" or duration != "—"
        )
        stats_html = (
            f'<div class="event-stats">'
            f'<span><span class="k">Cams</span><span class="v">{cams}</span></span>'
            f'<span><span class="k">3D pts</span><span class="v">{e.get("n_triangulated", 0)}</span></span>'
            f'<span><span class="k">Mean resid (m)</span><span class="v">{mean}</span></span>'
            f'<span><span class="k">Peak Z (m)</span><span class="v">{peak_z}</span></span>'
            f'<span><span class="k">Duration (s)</span><span class="v">{duration}</span></span>'
            f"</div>"
        ) if has_metrics else ""
        has_traj = (e.get("n_triangulated") or 0) > 0
        if has_traj:
            toggle_html = (
                '<label class="traj-toggle" title="Overlay trajectory on canvas">'
                f'<input type="checkbox" data-traj-sid="{sid}">'
                '<span class="swatch"></span>'
                "</label>"
            )
        else:
            toggle_html = '<span class="traj-toggle-placeholder" aria-hidden="true"></span>'
        processing_state = e.get("processing_state")
        processing_chip = (
            f'<span class="chip {html.escape(processing_state)}">{html.escape(processing_state)}</span>'
            if processing_state else ""
        )
        if e.get("trashed"):
            lifecycle_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/restore">'
                f'<button class="event-action ok" type="submit">Restore</button>'
                f"</form>"
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/delete" '
                f'onsubmit="return confirm(\'刪除 session {sid}？此動作無法復原。\');">'
                f'<button class="event-action dev" type="submit">Delete</button>'
                f"</form>"
            )
        else:
            lifecycle_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/trash" '
                f'onsubmit="return confirm(\'移動 session {sid} 到垃圾桶？\');">'
                f'<button class="event-action dev" type="submit">Trash</button>'
                f"</form>"
            )
        processing_html = ""
        if processing_state in {"queued", "processing"}:
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/cancel_processing">'
                f'<button class="event-action warn" type="submit">Cancel Proc</button>'
                f"</form>"
            )
        elif processing_state == "canceled" and e.get("processing_resumable"):
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/resume_processing">'
                f'<button class="event-action ok" type="submit">Resume</button>'
                f"</form>"
            )
        parts.append(
            f'<div class="event-item">'
            f"{toggle_html}"
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-top">'
            f'<span class="sid">{sid}</span>'
            f'<span class="capmode">{capture_mode}</span>'
            f'<span class="event-top-spacer"></span>'
            f"{processing_chip}"
            f'<span class="chip {status}">{stat_label}</span>'
            f"</div>"
            f'<div class="event-paths-row">{path_html}</div>'
            f"{stats_html}"
            f"</a>"
            f'<div class="event-actions">{processing_html}{lifecycle_html}</div>'
            f"</div>"
        )
    return "".join(parts)
