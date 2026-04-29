"""Dashboard event-list partial renderers.

Card structure (3 visual rows max, row 2/3 collapse when empty):

  row1: [swatch] HH:MM  s_xxxxxxxx                       [status chips]
  row2: [L|252·157] [S|0·0] [G|✓·—]   ·   65 pts · 1.40 s · 1.65 m · 28 mph
  row3:                                          [Run srv] [Trash] [...]

DOM root keeps `.event-item` + `data-sid` because `86_live_stream.js`
selects on it for the flash-done SSE animation, and `40_traj_handlers.js`
delegates clicks on `.traj-toggle`. Internals renamed `.ev-*` so the new
flexbox layout cannot be confused with the prior CSS-grid one."""
from __future__ import annotations

import html
from typing import Any


def _render_events_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<div class="events-empty">No sessions received yet.</div>'
    parts: list[str] = []
    last_day: str | None = None
    for e in events:
        day = e.get("created_day") or "—"
        if day != last_day:
            parts.append(
                f'<div class="event-day" data-day="{html.escape(day)}">'
                f'{html.escape(day)}</div>'
            )
            last_day = day
        parts.append(_render_card(e))
    return "".join(parts)


def _render_card(e: dict[str, Any]) -> str:
    sid = html.escape(e["session_id"])
    hm = html.escape(e.get("created_hm") or "—:—")
    n_tri = int(e.get("n_triangulated") or 0)
    processing_state = e.get("processing_state") or ""
    trashed = bool(e.get("trashed"))

    classes = ["event-item"]
    if processing_state in {"queued", "processing"}:
        classes.append("processing")

    swatch_html = _swatch_html(sid, n_tri > 0)
    statuses_html = _statuses_html(e)
    pipes_html = _pipes_html(e)
    actions_html = _actions_html(e, sid, processing_state, trashed)

    row1 = (
        f'<div class="ev-row1">'
        f'{swatch_html}'
        f'<span class="ev-time">{hm}</span>'
        f'<a class="ev-sid" href="/viewer/{sid}">{sid}</a>'
        f'<span class="ev-spacer"></span>'
        f'{statuses_html}'
        f'</div>'
    )
    row2 = (
        f'<div class="ev-row2">{pipes_html}</div>'
        if pipes_html else ""
    )
    row3 = (
        f'<div class="ev-row3">{actions_html}</div>'
        if actions_html else ""
    )
    return (
        f'<div class="{" ".join(classes)}" data-sid="{sid}">'
        f'{row1}{row2}{row3}'
        f'</div>'
    )


def _swatch_html(sid: str, has_traj: bool) -> str:
    if has_traj:
        return (
            '<label class="traj-toggle" title="Overlay trajectory on canvas">'
            f'<input type="checkbox" data-traj-sid="{sid}">'
            '<span class="swatch"></span>'
            '</label>'
        )
    return '<span class="swatch swatch-empty" aria-hidden="true"></span>'


def _statuses_html(e: dict[str, Any]) -> str:
    """Right-aligned chips on row 1: processing state, error, missing-cal,
    server_post error. Path-completion is encoded by the pipe chips on
    row 2 (on/err/neutral), so we don't dup `paired`/`partial` here."""
    chips: list[str] = []

    proc = e.get("processing_state") or ""
    if proc:
        chips.append(f'<span class="chip {html.escape(proc)}">{html.escape(proc)}</span>')

    if e.get("status") == "error":
        chips.append('<span class="chip error">error</span>')

    missing = e.get("live_missing_calibration") or []
    if missing:
        chips.append(
            f'<span class="chip error" '
            f'title="live frames dropped: no calibration on file">'
            f'no cal: {html.escape(",".join(missing))}</span>'
        )

    sp_errors = e.get("server_post_errors") or {}
    if sp_errors:
        tip = "; ".join(f"{cam}: {msg}" for cam, msg in sorted(sp_errors.items()))
        cams = ",".join(sorted(sp_errors.keys()))
        chips.append(
            f'<span class="chip error" title="{html.escape(tip)}">'
            f'srv err: {html.escape(cams)}</span>'
        )

    return f'<div class="ev-statuses">{"".join(chips)}</div>' if chips else ""


_PIPE_TITLES = {
    "live": "Live — iOS real-time detection (WS streamed)",
    "server_post": "Server — HSV detection on decoded MOV",
}


def _pipe_chip(label: str, status: str, counts: dict[str, int] | None,
               title_base: str) -> str:
    cls = "ev-pipe"
    if status == "done":
        cls += " on"
    elif status == "error":
        cls += " err"
    counts = counts or {}
    if counts:
        a = str(int(counts["A"])) if "A" in counts else "—"
        b = str(int(counts["B"])) if "B" in counts else "—"
        body = f'<b>{a}·{b}</b>'
        title = title_base + " · " + ", ".join(
            f"{c}:{n}" for c, n in sorted(counts.items())
        )
    else:
        body = '<b>—</b>'
        title = title_base
    return f'<span class="{cls}" title="{html.escape(title)}">{label}{body}</span>'


def _pipes_html(e: dict[str, Any]) -> str:
    """Live + server detection pipe chips."""
    path_status = e.get("path_status") or {}
    path_counts = e.get("n_ball_frames_by_path") or {}
    bits = [
        _pipe_chip("L", path_status.get("live", "-"),
                   path_counts.get("live"), _PIPE_TITLES["live"]),
        _pipe_chip("S", path_status.get("server_post", "-"),
                   path_counts.get("server_post"), _PIPE_TITLES["server_post"]),
    ]
    return f'<div class="ev-pipes">{"".join(bits)}</div>'


def _actions_html(e: dict[str, Any], sid: str,
                  processing_state: str, trashed: bool) -> str:
    parts: list[str] = []

    path_status = e.get("path_status") or {}
    server_status = path_status.get("server_post") or "-"
    if processing_state in {"queued", "processing"}:
        parts.append(_form_btn(f"/sessions/{sid}/cancel_processing", "Cancel", "warn"))
    elif not trashed and server_status != "done":
        parts.append(_form_btn(f"/sessions/{sid}/run_server_post", "Run srv", "ok"))

    if trashed:
        parts.append(_form_btn(
            f"/sessions/{sid}/restore", "Restore", "ok",
        ))
        parts.append(_form_btn(
            f"/sessions/{sid}/delete", "Delete", "dev",
            confirm=f"刪除 session {sid}？此動作無法復原。",
        ))
    else:
        parts.append(_form_btn(
            f"/sessions/{sid}/trash", "Trash", "dev",
            confirm=f"移動 session {sid} 到垃圾桶？",
        ))

    return "".join(parts)


def _form_btn(action: str, label: str, variant: str,
              *, confirm: str | None = None, title: str | None = None) -> str:
    onsubmit = (
        f' onsubmit="return confirm({_js_string(confirm)});"'
        if confirm else ""
    )
    title_attr = f' title="{html.escape(title)}"' if title else ""
    return (
        f'<form class="ev-action-form" method="POST" action="{action}"{onsubmit}>'
        f'<button class="ev-btn {variant}" type="submit"{title_attr}>{label}</button>'
        f'</form>'
    )


def _js_string(s: str) -> str:
    """JSON-encode for safe inline JS (matches the prior `JSON.stringify`-
    equivalent escaping the JS renderer used)."""
    import json
    return html.escape(json.dumps(s), quote=True)
