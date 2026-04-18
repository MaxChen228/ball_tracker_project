"""Dashboard renderer for `/` — A/B badges, session state, Arm/Cancel controls, events table. Extracted from viewer.py."""
from __future__ import annotations

import datetime as _dt
import html
from typing import Any

from schemas import Device, Session


_INDEX_CSS = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 24px; color: #222; background: #fafafa; }
    h1 { font-size: 22px; margin: 0 0 4px 0; }
    .subtitle { color: #666; margin-bottom: 20px; font-size: 14px; }
    .control { background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
               border-radius: 8px; padding: 16px 18px; max-width: 980px;
               margin-bottom: 20px; }
    .control .row { display: flex; flex-wrap: wrap; gap: 20px 28px;
                    align-items: center; }
    .control .label { font-size: 12px; color: #888; text-transform: uppercase;
                      letter-spacing: 0.05em; margin-bottom: 4px; }
    .control .block { min-width: 130px; }
    .badge { display: inline-block; padding: 3px 9px; border-radius: 10px;
             font-size: 12px; font-weight: 600; margin-right: 6px;
             font-variant-numeric: tabular-nums; }
    .badge.online  { background: #e0f7ea; color: #1e7d45; }
    .badge.offline { background: #eef1f5; color: #555; }
    .session-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                  font-size: 13px; color: #333; }
    .session-state { font-weight: 600; }
    .session-state.armed   { color: #1e7d45; }
    .session-state.idle    { color: #666; }
    form.inline { display: inline-block; margin: 0; }
    button { border: none; border-radius: 6px; padding: 8px 16px;
             font-size: 14px; font-weight: 600; cursor: pointer; }
    button.arm    { background: #0b6bcb; color: white; }
    button.arm:hover    { background: #0958a8; }
    button.cancel { background: #fdecec; color: #b3261e; }
    button.cancel:hover { background: #f9d6d6; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    table { border-collapse: collapse; width: 100%; max-width: 980px;
            background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
            border-radius: 6px; overflow: hidden; }
    th, td { padding: 10px 14px; text-align: left; font-size: 14px;
             border-bottom: 1px solid #eee; }
    th { background: #f2f4f7; font-weight: 600; color: #333; }
    tr:hover td { background: #f7faff; }
    td.num { font-variant-numeric: tabular-nums; text-align: right; }
    a { color: #0b6bcb; text-decoration: none; font-weight: 500; }
    a:hover { text-decoration: underline; }
    .status { display: inline-block; padding: 2px 8px; border-radius: 10px;
              font-size: 12px; font-weight: 600; }
    .status.paired { background: #e0f7ea; color: #1e7d45; }
    .status.paired_no_points { background: #fff3cd; color: #8a6d00; }
    .status.partial { background: #eef1f5; color: #555; }
    .status.error { background: #fdecec; color: #b3261e; }
    .empty { color: #888; font-style: italic; padding: 24px; text-align: center; }
"""


# Live-refresh poll: pulls /status every second to keep the devices badge
# and session panel current. Events table stays static — it mutates via
# /pitch arrivals and a full page reload is cheap enough.
_INDEX_JS = """
(function () {
  const fmtMono = (v) => '<span class="session-id">' + v + '</span>';
  async function refresh() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();

      const devNode = document.getElementById('devices-badges');
      if (devNode) {
        const seen = new Set((s.devices || []).map(d => d.camera_id));
        const expected = ['A', 'B'];
        devNode.innerHTML = expected.map(id =>
          `<span class="badge ${seen.has(id) ? 'online' : 'offline'}">${id}</span>`
        ).join('') + (s.devices || []).filter(d => !expected.includes(d.camera_id))
          .map(d => `<span class="badge online">${d.camera_id}</span>`).join('');
      }

      const sessNode = document.getElementById('session-state');
      const armBtn = document.getElementById('arm-btn');
      const cancelBtn = document.getElementById('cancel-btn');
      if (sessNode) {
        if (s.session && s.session.armed) {
          sessNode.innerHTML = '<span class="session-state armed">ARMED</span> ' + fmtMono(s.session.id);
          if (armBtn) armBtn.disabled = true;
          if (cancelBtn) cancelBtn.disabled = false;
        } else {
          const tail = s.session ? ` (last: ${s.session.end_reason || 'ended'})` : '';
          sessNode.innerHTML = '<span class="session-state idle">IDLE</span>' + tail;
          if (armBtn) armBtn.disabled = false;
          if (cancelBtn) cancelBtn.disabled = true;
        }
      }
    } catch (e) { /* silent — next tick will retry */ }
  }
  refresh();
  setInterval(refresh, 1000);
})();
"""


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_opt_float(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def _render_devices_badges(devices: list[dict[str, Any]]) -> str:
    """Initial server-rendered state of the devices panel. The JS tick will
    override this within 1 s, but rendering it correctly on first paint
    avoids an "offline" flash."""
    seen = {d["camera_id"] for d in devices}
    expected = ["A", "B"]
    parts = []
    for cam in expected:
        cls = "online" if cam in seen else "offline"
        parts.append(f'<span class="badge {cls}">{html.escape(cam)}</span>')
    for d in devices:
        if d["camera_id"] not in expected:
            parts.append(
                f'<span class="badge online">{html.escape(d["camera_id"])}</span>'
            )
    return "".join(parts)


def _render_session_state(session: dict[str, Any] | None) -> str:
    if session is not None and session.get("armed"):
        return (
            f'<span class="session-state armed">ARMED</span> '
            f'<span class="session-id">{html.escape(session["id"])}</span>'
        )
    if session is not None:
        reason = html.escape(session.get("end_reason") or "ended")
        return f'<span class="session-state idle">IDLE</span> (last: {reason})'
    return '<span class="session-state idle">IDLE</span>'


def _render_control_panel(
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
) -> str:
    armed = session is not None and session.get("armed")
    arm_btn = (
        '<form class="inline" method="POST" action="/sessions/arm">'
        f'<button id="arm-btn" class="arm" type="submit"'
        f'{" disabled" if armed else ""}>準備完成 · Arm</button>'
        "</form>"
    )
    cancel_btn = (
        '<form class="inline" method="POST" action="/sessions/cancel">'
        f'<button id="cancel-btn" class="cancel" type="submit"'
        f'{"" if armed else " disabled"}>取消 · Cancel</button>'
        "</form>"
    )
    return (
        '<div class="control">'
        '<div class="row">'
        '<div class="block">'
        '<div class="label">Devices</div>'
        f'<div id="devices-badges">{_render_devices_badges(devices)}</div>'
        "</div>"
        '<div class="block" style="min-width:240px">'
        '<div class="label">Session</div>'
        f'<div id="session-state">{_render_session_state(session)}</div>'
        "</div>"
        '<div class="block" style="margin-left:auto">'
        f'{arm_btn} {cancel_btn}'
        "</div>"
        "</div>"
        "</div>"
    )


def render_events_index_html(
    events: list[dict[str, Any]],
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
) -> str:
    """Render the dashboard: devices panel + session panel + Arm/Cancel
    controls + events table with links into each session's 3D viewer.

    All inputs match the shape of their /status and /events counterparts,
    so the JSON and HTML sides of the server describe the same data."""
    devices = devices or []

    if not events:
        events_body = (
            '<div class="empty">No sessions received yet. Arm a session from the panel above, '
            "then throw a ball within the camera&rsquo;s view.</div>"
        )
    else:
        rows: list[str] = []
        for e in events:
            cams = ", ".join(html.escape(c) for c in e["cameras"]) or "—"
            counts = e.get("n_ball_frames", {}) or {}
            counts_str = ", ".join(
                f"{html.escape(c)}:{n}" for c, n in sorted(counts.items())
            ) or "—"
            status = html.escape(e["status"])
            err = e.get("error") or ""
            err_html = f' <span title="{html.escape(err)}">⚠</span>' if err else ""
            mode = "dual" if len(e["cameras"]) >= 2 else "single"
            sid = html.escape(e["session_id"])
            rows.append(
                "<tr>"
                f'<td><a href="/viewer/{sid}"><span class="session-id">{sid}</span></a></td>'
                f'<td>{cams} <span class="badge {"online" if mode == "dual" else "offline"}">{mode}</span></td>'
                f'<td><span class="status {status}">{status}</span>{err_html}</td>'
                f'<td>{_fmt_received_at(e["received_at"])}</td>'
                f'<td>{counts_str}</td>'
                f'<td class="num">{e["n_triangulated"]}</td>'
                f'<td class="num">{_fmt_opt_float(e["mean_residual_m"], ".4f")}</td>'
                f'<td class="num">{_fmt_opt_float(e["peak_z_m"], ".2f")}</td>'
                f'<td class="num">{_fmt_opt_float(e["duration_s"], ".2f")}</td>'
                "</tr>"
            )
        events_body = (
            "<table>"
            "<thead><tr>"
            "<th>Session</th><th>Cams / mode</th><th>Status</th><th>Received</th>"
            "<th>Ball frames</th><th>3D pts</th><th>Mean resid (m)</th>"
            "<th>Peak Z (m)</th><th>Duration (s)</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>ball_tracker dashboard</title>"
        f"<style>{_INDEX_CSS}</style>"
        "</head><body>"
        "<h1>ball_tracker dashboard</h1>"
        f'<div class="subtitle">{len(events)} session(s) · click a row to open the 3D viewer</div>'
        f"{_render_control_panel(devices, session)}"
        f"{events_body}"
        f"<script>{_INDEX_JS}</script>"
        "</body></html>"
    )
