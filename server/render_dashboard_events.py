"""Dashboard event-list partial renderers.

Card structure (3 visual rows max, row 2/3 collapse when empty):

  row1: [swatch] HH:MM  s_xxxxxxxx                       [status chips]
  row2: [L|252·157] [S|0·0] [G|✓·—]   ·   65 pts · 1.40 s · 1.65 m · 28 mph
  row3:                                          [Run srv] [Trash] [...]

DOM root keeps `.event-item` + `data-sid` because `86_live_stream.js`
selects on it for the flash-done SSE animation, and `40_traj_handlers.js`
delegates row clicks on `.event-item[data-sid]` to drive trajectory
selection. Internals renamed `.ev-*` so the new flexbox layout cannot
be confused with the prior CSS-grid one."""
from __future__ import annotations

import html
from typing import Any

from viewer_fragments import format_snapshot_params


def _render_events_body(events: list[dict[str, Any]]) -> str:
    """Group sessions by `created_day` into collapsible folds. Each day
    gets its own `event-day-group` (collapse key `dash:event-day:<day>`),
    so toggling the day header folds the whole day's cards together. The
    JS path in `60_events_render.js` mirrors this DOM shape so live
    poll/SSE updates land in the right group."""
    if not events:
        return '<div class="events-empty">No sessions received yet.</div>'
    parts: list[str] = []
    last_day: str | None = None
    for e in events:
        day = e.get("created_day") or "—"
        if day != last_day:
            if last_day is not None:
                parts.append("</div></div>")
            day_esc = html.escape(day)
            parts.append(
                f'<div class="event-day-group" '
                f'data-collapsible-key="dash:event-day:{day_esc}">'
                f'<div class="event-day" data-collapsible-header '
                f'data-day="{day_esc}">{day_esc}</div>'
                '<div class="event-day-body" data-collapsible-body>'
            )
            last_day = day
        parts.append(_render_card(e))
    if last_day is not None:
        parts.append("</div></div>")
    return "".join(parts)


def _render_card(e: dict[str, Any]) -> str:
    sid = html.escape(e["session_id"])
    hm = html.escape(e.get("created_hm") or "—:—")
    n_tri = int(e.get("n_triangulated") or 0)
    processing_state = e.get("processing_state") or ""
    trashed = bool(e.get("trashed"))
    starred = bool(e.get("starred"))

    classes = ["event-item"]
    if processing_state in {"queued", "processing"}:
        classes.append("processing")

    swatch_html = _swatch_html(sid, n_tri > 0)
    star_html = _star_html(sid, starred)
    statuses_html = _statuses_html(e)
    pipes_html = _pipes_html(e)
    cfg_html = _cfg_strip_html(e)
    actions_html = _actions_html(e, sid, processing_state, trashed)

    row1 = (
        f'<div class="ev-row1">'
        f'{swatch_html}'
        f'{star_html}'
        f'<span class="ev-time">{hm}</span>'
        f'<span class="ev-sid">{sid}</span>'
        f'<span class="ev-spacer"></span>'
        f'{statuses_html}'
        f'<a class="ev-viewer-link" href="/viewer/{sid}" title="Open in viewer">→ viewer</a>'
        f'</div>'
    )
    row2 = (
        f'<div class="ev-row2">{pipes_html}{cfg_html}</div>'
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
    """Pure has-traj indicator. SSR ships unselected; the JS layer flips
    the `.selected` class on next render (after operator clicks the row)."""
    del sid  # selection state is client-side only on first paint
    if has_traj:
        return '<span class="swatch" aria-hidden="true"></span>'
    return '<span class="swatch swatch-empty" aria-hidden="true"></span>'


def _star_html(sid: str, starred: bool) -> str:
    """Per-session star toggle. POSTs to /sessions/{sid}/{star,unstar};
    server persists into session_meta.json and 303-redirects back."""
    action = "unstar" if starred else "star"
    glyph = "★" if starred else "☆"
    cls = "ev-star-btn on" if starred else "ev-star-btn"
    label = "unstar session" if starred else "star session"
    return (
        f'<form class="ev-action-form" method="post" action="/sessions/{sid}/{action}">'
        f'<button type="submit" class="{cls}" aria-label="{label}" title="{label}">'
        f'{glyph}</button></form>'
    )


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
    elif status == "streaming":
        cls += " streaming"
    elif status == "armed":
        cls += " armed"
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
    """Live + server detection pipe chips, plus segment count."""
    path_status = e.get("path_status") or {}
    path_counts = e.get("n_ball_frames_by_path") or {}
    seg_by_path = e.get("n_segments_by_path") or {}
    seg_live = int(seg_by_path.get("live") or 0)
    seg_srv = int(seg_by_path.get("server_post") or 0)
    seg_zero = (seg_live == 0 and seg_srv == 0)
    seg_title = f"Ballistic fit segments · live:{seg_live}, server_post:{seg_srv}"
    bits = [
        _pipe_chip("L", path_status.get("live", "-"),
                   path_counts.get("live"), _PIPE_TITLES["live"]),
        _pipe_chip("S", path_status.get("server_post", "-"),
                   path_counts.get("server_post"), _PIPE_TITLES["server_post"]),
        f'<span class="ev-segs{" zero" if seg_zero else ""}"'
        f' title="{html.escape(seg_title)}">SEG '
        f'<b>{seg_live}&middot;{seg_srv}</b></span>',
    ]
    return f'<div class="ev-pipes">{"".join(bits)}</div>'


def _cfg_strip_html(e: dict[str, Any]) -> str:
    """Live + server_post config chips driven by frozen snapshots.

    Snapshot values are the source of truth. If `preset_name` still
    exists on disk we use its label for the tooltip; if the preset was
    deleted or the config was custom (`preset_name=None`), we still show
    the exact frozen HSV / gate values from the snapshot.
    """
    from main import state as _state

    live_cfg = e.get("live_config_used")
    srv_cfg = e.get("server_post_config_used")
    if live_cfg is None and srv_cfg is None:
        return ""

    def _tip(cfg: dict[str, Any]) -> str:
        # `cfg` is canonical `DetectionConfigSnapshotPayload` shape:
        # `{algorithm_id, params, preset_name}`. Delegate to viewer's
        # shared formatter so dashboard chip tooltip and viewer SVR/LIVE
        # pill tooltip stay identical — single source of truth for
        # per-algorithm dispatch.
        return format_snapshot_params(cfg["algorithm_id"], cfg["params"])

    def _chip(label: str, cfg: dict[str, Any] | None) -> str:
        if cfg is None:
            return (
                f'<span class="ev-cfg-chip none" title="{html.escape(label)}: not set">'
                f'{html.escape(label)} <b>—</b></span>'
            )
        name = cfg.get("preset_name")
        tip = _tip(cfg)
        if name is None:
            return (
                f'<span class="ev-cfg-chip" title="{html.escape(label)}: custom — {html.escape(tip)}">'
                f'{html.escape(label)} <b>custom</b></span>'
            )
        try:
            p = _state.load_preset(name)
        except KeyError:
            return (
                f'<span class="ev-cfg-chip deleted" title="{html.escape(label)}: '
                f'preset {html.escape(name)} no longer on disk — {html.escape(tip)}">'
                f'{html.escape(label)} <b>{html.escape(name)}</b> '
                f'<i>(deleted)</i></span>'
            )
        return (
            f'<span class="ev-cfg-chip" title="{html.escape(p.label)} — {html.escape(tip)}">'
            f'{html.escape(label)} <b>{html.escape(name)}</b></span>'
        )

    return (
        f'<div class="ev-cfg-strip">{_chip("Live", live_cfg)}'
        f'{_chip("Svr", srv_cfg)}</div>'
    )


def _actions_html(e: dict[str, Any], sid: str,
                  processing_state: str, trashed: bool) -> str:
    parts: list[str] = []

    path_status = e.get("path_status") or {}
    server_status = path_status.get("server_post") or "-"
    if processing_state in {"queued", "processing"}:
        parts.append(_form_btn(f"/sessions/{sid}/cancel_processing", "Cancel", "warn"))
    elif not trashed and server_status != "done":
        # Operator picks the preset to detect under via the inline
        # <select>. Default selection = current dashboard active preset
        # (usually what the operator just dialled in via Apply on the
        # HSV card); they can pick any other named preset to compare
        # detection results. Re-running overwrites the prior result.
        parts.append(_run_srv_form(sid))

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


def _run_srv_form(sid: str) -> str:
    """`Run srv` button with inline preset selector. Default option is
    the sticky server_post preset (`state.active_server_post_preset_name`)
    — distinct from the live-detection active preset
    (`detection_config().preset`) because server_post is the operator's
    rerun choice for offline detection, not the iOS live config.
    Operator can pick any on-disk preset to detect under. Submits as
    application/x-www-form-urlencoded with `preset_name` field only to
    the deprecation-alias endpoint `/sessions/{sid}/run_server_post`;
    the server derives `algorithm_id` from the preset (canonical), so
    the form never disagrees with what runs."""
    from main import state as _state

    active = _state.active_server_post_preset_name()
    options = "".join(
        f'<option value="{html.escape(p.name)}"'
        f'{" selected" if p.name == active else ""}>'
        f'{html.escape(p.label)} ({html.escape(p.name)} · {html.escape(p.algorithm_id)})</option>'
        for p in _state.list_presets()
    )
    return (
        f'<form class="ev-action-form" method="POST" '
        f'action="/sessions/{sid}/run_server_post">'
        f'<select class="ev-cfg-select" name="preset_name" '
        f'title="Detection preset to run server-side (algorithm derived from preset)">{options}</select>'
        f'<button class="ev-btn ok" type="submit">Run srv</button>'
        f'</form>'
    )


def _form_btn(action: str, label: str, variant: str,
              *, confirm: str | None = None, title: str | None = None,
              hidden: dict[str, str] | None = None) -> str:
    onsubmit = (
        f' onsubmit="return confirm({_js_string(confirm)});"'
        if confirm else ""
    )
    title_attr = f' title="{html.escape(title)}"' if title else ""
    hidden_html = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in (hidden or {}).items()
    )
    return (
        f'<form class="ev-action-form" method="POST" action="{action}"{onsubmit}>'
        f'{hidden_html}'
        f'<button class="ev-btn {variant}" type="submit"{title_attr}>{label}</button>'
        f'</form>'
    )


def _js_string(s: str) -> str:
    """JSON-encode for safe inline JS (matches the prior `JSON.stringify`-
    equivalent escaping the JS renderer used)."""
    import json
    return html.escape(json.dumps(s), quote=True)
