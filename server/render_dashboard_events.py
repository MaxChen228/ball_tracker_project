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
        status = html.escape(e.get("status", ""))
        stat_label = status.replace("_", " ")
        # Per-pipeline chip: state (on/err/-) + detection count. "L|67"
        # reads quickly as "live produced 67 detections"; "S|—" means
        # server pipeline never ran.
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
        peak_z = e.get("peak_z_m")
        duration = e.get("duration_s")
        n_tri = int(e.get("n_triangulated") or 0)
        meta_bits: list[str] = []
        if n_tri > 0:
            meta_bits.append(
                f'<span class="k">pts</span><span class="v">{n_tri}</span>'
            )
        if duration is not None:
            meta_bits.append(
                f'<span class="k">dur</span><span class="v">{duration:.2f}s</span>'
            )
        if peak_z is not None:
            meta_bits.append(
                f'<span class="k">z</span><span class="v">{peak_z:.2f}m</span>'
            )
        meta_html = f'<div class="event-meta">{"".join(meta_bits)}</div>' if meta_bits else ""
        has_traj = n_tri > 0
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
        server_status = (path_status or {}).get("server_post") or "-"
        show_run_server = (
            not e.get("trashed")
            and server_status != "done"
            and processing_state not in {"queued", "processing"}
        )
        if processing_state in {"queued", "processing"}:
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/cancel_processing">'
                f'<button class="event-action warn" type="submit">Cancel</button>'
                f"</form>"
            )
        elif show_run_server:
            processing_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/run_server_post">'
                f'<button class="event-action ok" type="submit">Run srv</button>'
                f"</form>"
            )
        parts.append(
            f'<div class="event-item">'
            f"{toggle_html}"
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-head">'
            f'<span class="sid">{sid}</span>'
            f"{path_html}"
            f'<span class="event-spacer"></span>'
            f"{processing_chip}"
            f'<span class="chip {status}">{stat_label}</span>'
            f"</div>"
            f"{meta_html}"
            f"</a>"
            f'<div class="event-actions">{processing_html}{lifecycle_html}</div>'
            f"</div>"
        )
    return "".join(parts)
