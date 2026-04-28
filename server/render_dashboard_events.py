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
            # Per-cam values in fixed A·B order so the chip width is
            # stable across rows and one-cam vs two-cam sessions don't
            # silently merge into a single sum. Aligns with viewer cam
            # cards which have always shown per-cam stats; the previous
            # cross-cam sum here meant "L|67" was ambiguous between
            # "A=67,B=0" (broken pairing) and "A=33,B=34" (healthy).
            if status == "done":
                cls = " on"
            elif status == "error":
                cls = " err"
            else:
                cls = ""
            if counts:
                a_str = str(int(counts["A"])) if "A" in counts else "—"
                b_str = str(int(counts["B"])) if "B" in counts else "—"
                count_html = f'<span class="pc">{a_str}·{b_str}</span>'
            else:
                count_html = ""
            title = path_chip_titles.get(path, path)
            if counts:
                title += " · " + ", ".join(f"{c}:{n}" for c, n in sorted(counts.items()))
            return (
                f'<span class="path-chip{cls}" title="{html.escape(title)}">'
                f"{label}{count_html}</span>"
            )

        path_html = "".join(_path_chip(p, l) for p, l in path_chip_specs)
        # GT path chip — existence-only ✓/— per cam, no count (those
        # live in the GT JSON itself, fetched on demand by /report/{sid}).
        # Chip lights `on` only when both cams have GT — partial GT is
        # still surfaced via the ✓·— glyph but the chip stays neutral.
        has_gt_map = e.get("has_gt") or {}
        if has_gt_map:
            a = "✓" if has_gt_map.get("A") else "—"
            b = "✓" if has_gt_map.get("B") else "—"
            gt_cls = " on" if (has_gt_map.get("A") and has_gt_map.get("B")) else ""
            path_html += (
                f'<span class="path-chip{gt_cls}" '
                f'title="GT — SAM 3 ground truth (A·B)">'
                f'G<span class="pc">{a}·{b}</span></span>'
            )
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
        # GT pipeline buttons. Layout: Run GT (queue SAM 3) → Validate
        # (run three-way comparison) → Report (open /report/{sid}).
        # Each gates on the prerequisite artefact existing.
        has_gt = e.get("has_gt") or {}
        has_val = e.get("has_validation") or {}
        gt_done_all = bool(has_gt) and all(has_gt.values())
        val_done_any = any(has_val.values())
        if not e.get("trashed"):
            gt_html = (
                f'<form class="event-action-form" method="POST" action="/sessions/{sid}/run_gt_labelling">'
                f'<button class="event-action accent" type="submit" '
                f'title="Queue SAM 3 GT labelling for both cams">Run GT</button>'
                f"</form>"
            )
            if gt_done_all:
                gt_html += (
                    f'<form class="event-action-form" method="POST" action="/sessions/{sid}/run_validation">'
                    f'<button class="event-action accent" type="submit" '
                    f'title="Run three-way validation (live vs server vs GT)">Validate</button>'
                    f"</form>"
                )
            if val_done_any:
                gt_html += (
                    f'<a class="event-action accent" href="/report/{sid}" '
                    f'title="Open three-way validation report">Report</a>'
                )
            processing_html += gt_html
        # Only surface status chips that carry real signal. The path
        # chips already encode "was each pipeline completed?" via their
        # count + on/off/err state, so `partial` / `paired` /
        # `paired_no_points` are visual noise that just ate sidebar
        # width. `error` is the only result-status chip worth showing;
        # processing-state chips (queued/processing/canceled/completed)
        # are actionable and stay.
        status_chip_html = (
            f'<span class="chip {status}">{stat_label}</span>'
            if status == "error" else ""
        )
        # Surface live-path calibration gaps inline. Cams listed here had
        # live frames arriving while `data/calibrations/<cam>.json` was
        # missing, so live rays silently dropped — without this pill the
        # operator would only see an empty L|n count and have to tail
        # the log to understand why.
        missing_cal = e.get("live_missing_calibration") or []
        missing_cal_html = (
            f'<span class="chip error" title="live frames dropped: no calibration on file">'
            f'no cal: {html.escape(",".join(missing_cal))}</span>'
            if missing_cal else ""
        )
        # server_post background-task failure, tooltip shows the raw
        # exception string so operator doesn't need to ssh into the server.
        sp_errors = e.get("server_post_errors") or {}
        sp_error_html = ""
        if sp_errors:
            tip = "; ".join(f"{cam}: {msg}" for cam, msg in sorted(sp_errors.items()))
            cams_label = ",".join(sorted(sp_errors.keys()))
            sp_error_html = (
                f'<span class="chip error" title="{html.escape(tip)}">'
                f'srv err: {html.escape(cams_label)}</span>'
            )
        item_classes = "event-item"
        if processing_state in {"queued", "processing"}:
            item_classes += " processing"
        parts.append(
            f'<div class="{item_classes}">'
            f"{toggle_html}"
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-head">'
            f'<span class="sid">{sid}</span>'
            f"{path_html}"
            f"</div>"
            f"{meta_html}"
            f"</a>"
            f'<div class="event-status">{processing_chip}{status_chip_html}{missing_cal_html}{sp_error_html}</div>'
            f'<div class="event-actions">{processing_html}{lifecycle_html}</div>'
            f"</div>"
        )
    return "".join(parts)
