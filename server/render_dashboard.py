"""Dashboard renderer for `/` — three-zone layout (top nav + 440px
sidebar + full-bleed 3D canvas) styled after the PHYSICS_LAB design
system. The canvas shows a live 3D scene of the plate plus whichever
cameras have a calibration persisted; the sidebar carries devices,
session controls, and the events list. All three columns tick from
JSON endpoints (`/status`, `/calibration/state`, `/events`) so the page
never has to reload to reflect a new calibration or a new pitch."""
from __future__ import annotations

import datetime as _dt
import html
from typing import Any

from reconstruct import build_calibration_scene
from render_scene import _build_figure
from schemas import Device, Session


# --- Design-system tokens (mirrored in render_scene.py) ----------------------
_BG = "#F8F7F4"
_SURFACE = "#FCFBFA"
_BORDER_BASE = "#DBD6CD"
_BORDER_L = "#E8E4DB"
_INK = "#2A2520"
_SUB = "#7A756C"
_INK_LIGHT = "#5A5550"
_DEV = "#C0392B"
_CONTRA = "#4A6B8C"
_DUAL = "#D35400"
_ACCENT = "#E6B300"


_CSS = f"""
:root {{
  --bg: {_BG};
  --surface: {_SURFACE};
  --border-base: {_BORDER_BASE};
  --border-l: {_BORDER_L};
  --ink: {_INK};
  --sub: {_SUB};
  --ink-light: {_INK_LIGHT};
  --dev: {_DEV};
  --contra: {_CONTRA};
  --dual: {_DUAL};
  --accent: {_ACCENT};
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --nav-h: 52px;
  --sidebar-w: 440px;
}}

* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; height: 100%; background: var(--bg); color: var(--ink);
              font-family: var(--sans); font-weight: 300; line-height: 1.8;
              -webkit-font-smoothing: antialiased; }}

/* --- Top nav --- */
.nav {{ position: fixed; top: 0; left: 0; right: 0; height: var(--nav-h);
        background: var(--surface); border-bottom: 1px solid var(--border-base);
        display: flex; align-items: center; padding: 0 24px;
        z-index: 20; gap: 24px; }}
.nav .brand {{ font-family: var(--mono); font-weight: 700; font-size: 14px;
               letter-spacing: 0.16em; color: var(--ink); }}
.nav .brand .dot {{ display: inline-block; width: 7px; height: 7px; background: var(--ink);
                    margin-right: 10px; vertical-align: middle; border-radius: 0; }}
.nav .status-line {{ margin-left: auto; font-family: var(--mono); font-size: 11px;
                     letter-spacing: 0.08em; text-transform: uppercase; color: var(--sub);
                     display: flex; gap: 20px; align-items: center; }}
.nav .status-line .pair {{ display: flex; gap: 6px; align-items: center; }}
.nav .status-line .label {{ color: var(--sub); }}
.nav .status-line .val {{ color: var(--ink); font-weight: 500; }}
.nav .status-line .val.armed {{ color: var(--contra); }}
.nav .status-line .val.idle {{ color: var(--sub); }}

/* --- Main layout: sidebar + canvas --- */
.layout {{ display: flex; height: 100vh; padding-top: var(--nav-h); }}
.sidebar {{ width: var(--sidebar-w); flex-shrink: 0; overflow-y: auto;
            background: var(--surface); border-right: 1px solid var(--border-base);
            box-shadow: 4px 0 24px rgba(0,0,0,0.03);
            padding: 32px 24px; z-index: 10;
            display: flex; flex-direction: column; gap: 24px; }}
.canvas {{ flex: 1; position: relative; overflow: hidden;
           background: var(--bg); }}
#scene-root {{ position: absolute; inset: 0; }}

/* --- Scrollbar --- */
.sidebar::-webkit-scrollbar {{ width: 4px; }}
.sidebar::-webkit-scrollbar-track {{ background: transparent; }}
.sidebar::-webkit-scrollbar-thumb {{ background: var(--border-base); }}
.sidebar::-webkit-scrollbar-thumb:hover {{ background: var(--sub); }}

/* --- Card --- */
.card {{ background: var(--surface); border: 1px solid var(--border-base);
         border-radius: 4px; padding: 20px; }}
.card + .card {{ margin-top: 0; }}
.card-title {{ font-family: var(--mono); font-weight: 500; font-size: 12px;
               letter-spacing: 0.08em; text-transform: uppercase; color: var(--sub);
               margin: 0 0 14px 0; padding: 0; }}
.card-subtitle {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em;
                  text-transform: uppercase; color: var(--sub);
                  margin-top: 14px; margin-bottom: 6px; }}
.card section + section {{ border-top: 1px solid var(--border-l); margin-top: 14px;
                           padding-top: 14px; }}

/* --- Device rows --- */
.device {{ display: grid; grid-template-columns: 36px 1fr auto; align-items: center;
           gap: 12px; padding: 10px 0; }}
.device + .device {{ border-top: 1px solid var(--border-l); }}
.device .id {{ font-family: var(--mono); font-size: 16px; font-weight: 500; color: var(--ink);
               letter-spacing: 0.04em; }}
.device .meta {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em;
                 text-transform: uppercase; color: var(--sub); }}
.device .meta em {{ font-style: normal; color: var(--ink-light); }}
.device .sub {{ display: flex; gap: 14px; margin-top: 4px; }}
.device .sub .item {{ font-family: var(--mono); font-size: 9px; letter-spacing: 0.16em;
                      text-transform: uppercase; color: var(--sub);
                      display: flex; align-items: center; gap: 5px; }}
.device .sub .dot {{ width: 6px; height: 6px; border-radius: 50%;
                     background: var(--border-base); display: inline-block; }}
.device .sub .dot.ok {{ background: var(--contra); }}
.device .sub .dot.warn {{ background: var(--dual); }}
.device .sub .dot.bad {{ background: var(--dev); }}

/* --- Chip (pill) --- */
.chip {{ display: inline-block; padding: 4px 12px; border-radius: 12px;
         font-family: var(--mono); font-size: 10px; font-weight: 500;
         letter-spacing: 0.16em; text-transform: uppercase;
         border: 1px solid var(--border-base); color: var(--sub); background: transparent;
         transition: all 0.2s ease; }}
.chip.online {{ border-color: var(--contra); color: var(--contra); }}
.chip.calibrated {{ background: var(--contra); border-color: var(--contra); color: var(--surface); }}
.chip.armed {{ background: var(--contra); border-color: var(--contra); color: var(--surface); }}
.chip.idle {{ color: var(--sub); border-color: var(--border-base); }}
.chip.paired {{ background: var(--contra); border-color: var(--contra); color: var(--surface); }}
.chip.partial {{ color: var(--sub); border-color: var(--border-base); }}
.chip.paired_no_points {{ background: var(--dual); border-color: var(--dual); color: var(--surface); }}
.chip.error {{ background: var(--dev); border-color: var(--dev); color: var(--surface); }}
.chip.dual {{ color: var(--dual); border-color: var(--dual); }}
.chip.single {{ color: var(--sub); border-color: var(--border-base); }}
/* Capture-mode chips on event rows. camera-only shares the sub-palette
   (it's the existing path); on-device stands out so operators can spot
   mode-two sessions at a glance. */
.chip.camera-only {{ color: var(--sub); border-color: var(--border-base); }}
.chip.on-device {{ color: var(--contra); border-color: var(--contra); }}

/* --- Session block --- */
.session-head {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
.session-id {{ font-family: var(--mono); font-size: 14px; color: var(--ink);
               letter-spacing: 0.04em; }}
.session-actions {{ display: flex; gap: 8px; margin-top: 14px; }}
.mode-row {{ display: flex; gap: 8px; align-items: center; margin-top: 14px;
             flex-wrap: wrap; }}
.mode-label {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.1em;
                text-transform: uppercase; color: var(--sub); min-width: 48px; }}
.mode-locked {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em;
                 color: var(--sub); padding-left: 4px; }}

/* --- Buttons --- */
button.btn {{ font-family: var(--mono); font-size: 11px; font-weight: 500;
              letter-spacing: 0.08em; text-transform: uppercase;
              padding: 10px 16px; border-radius: 2px; cursor: pointer;
              background: var(--ink); color: var(--surface);
              border: 1px solid var(--ink); transition: all 0.2s ease; }}
button.btn:hover:not(:disabled) {{ background: var(--ink-light); }}
button.btn.secondary {{ background: transparent; color: var(--ink);
                        border-color: var(--border-base); }}
button.btn.secondary:hover:not(:disabled) {{ border-color: var(--ink); }}
button.btn.danger {{ background: transparent; color: var(--dev);
                     border-color: var(--dev); }}
button.btn.danger:hover:not(:disabled) {{ background: var(--dev); color: var(--surface); }}
button.btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}
form.inline {{ display: inline-block; margin: 0; }}

/* --- Events list --- */
.events-empty {{ color: var(--sub); font-size: 13px; padding: 12px 0; font-style: italic; }}
.event-item {{ position: relative; border-top: 1px solid var(--border-l); }}
.event-item:first-child {{ border-top: 0; }}
.event-item:hover {{ background: var(--bg); margin: 0 -8px; padding: 0 8px; }}
.event-row {{ display: block; text-decoration: none; color: inherit;
              padding: 12px 0; }}
.event-top {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
              padding-right: 32px; }}
.event-top .sid {{ font-family: var(--mono); font-size: 13px; color: var(--ink);
                   letter-spacing: 0.04em; }}
.event-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px 14px;
                font-family: var(--mono); font-size: 11px; color: var(--ink-light); }}
.event-stats .k {{ color: var(--sub); letter-spacing: 0.08em; text-transform: uppercase;
                   font-size: 9px; display: block; }}
.event-stats .v {{ font-variant-numeric: tabular-nums; color: var(--ink); }}
.event-delete-form {{ position: absolute; top: 10px; right: 4px; margin: 0; }}
.event-item:hover .event-delete-form {{ right: 12px; }}
.event-delete {{ background: transparent; border: 1px solid var(--border-base);
                 color: var(--sub); font-family: var(--mono); font-size: 13px;
                 line-height: 1; padding: 2px 8px 3px; border-radius: 2px;
                 cursor: pointer; transition: all 0.15s ease; }}
.event-delete:hover {{ border-color: var(--dev); color: var(--dev);
                       background: var(--surface); }}

/* --- Canvas overlay hint --- */
.canvas-hint {{ position: absolute; left: 20px; top: 20px; z-index: 5;
                font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em;
                text-transform: uppercase; color: var(--sub);
                background: var(--surface); border: 1px solid var(--border-l);
                padding: 6px 10px; pointer-events: none; }}
"""


_JS_TEMPLATE = r"""
(function () {
  const EXPECTED = ['A', 'B'];

  const sceneRoot = document.getElementById('scene-root');
  const devicesBox = document.getElementById('devices-body');
  const sessionBox = document.getElementById('session-body');
  const eventsBox = document.getElementById('events-body');
  const navStatus = document.getElementById('nav-status');

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  function statusChip(cam, online, calibrated) {
    if (calibrated) return `<span class="chip calibrated">calibrated</span>`;
    if (online)     return `<span class="chip online">online</span>`;
    return `<span class="chip idle">offline</span>`;
  }

  function renderDevices(state) {
    const devByCam = new Map((state.devices || []).map(d => [d.camera_id, d]));
    const calibrated = new Set(state.calibrations || []);

    function row(cam, deviceRecord) {
      const online = !!deviceRecord;
      const timeSynced = !!(deviceRecord && deviceRecord.time_synced);
      const isCal = calibrated.has(cam);
      const meta = !online ? 'Not seen'
                   : isCal ? 'Ready · pose known'
                           : 'Awaiting calibration';
      const calDot = isCal ? 'ok' : (online ? 'warn' : 'bad');
      const syncDot = !online ? 'bad' : (timeSynced ? 'ok' : 'warn');
      const syncLabel = !online ? 'offline' : (timeSynced ? 'synced' : 'not synced');
      const calLabel = !online ? 'offline' : (isCal ? 'calibrated' : 'pending');
      return `
        <div class="device">
          <div class="id">${esc(cam)}</div>
          <div>
            <div class="meta">${esc(meta)}</div>
            <div class="sub">
              <span class="item"><span class="dot ${syncDot}"></span>time sync · ${esc(syncLabel)}</span>
              <span class="item"><span class="dot ${calDot}"></span>pose · ${esc(calLabel)}</span>
            </div>
          </div>
          <div>${statusChip(cam, online, isCal)}</div>
        </div>`;
    }

    const rows = EXPECTED.map(cam => row(cam, devByCam.get(cam))).join('');
    const extras = (state.devices || [])
      .filter(d => !EXPECTED.includes(d.camera_id))
      .map(d => row(d.camera_id, d)).join('');
    devicesBox.innerHTML = rows + extras;
  }

  const MODE_LABELS = { camera_only: 'Camera-only', on_device: 'On-device' };

  function renderSession(state) {
    const s = state.session;
    const armed = !!(s && s.armed);
    const chip = armed ? `<span class="chip armed">armed</span>` : `<span class="chip idle">idle</span>`;
    const sid = s && s.id ? `<span class="session-id">${esc(s.id)}</span>` : '';
    const clearBtn = (!armed && s && s.id)
      ? `<form class="inline" method="POST" action="/sessions/clear">
           <button class="btn" type="submit">Clear</button>
         </form>`
      : '';
    const captureMode = state.capture_mode || 'camera_only';
    let modeRow;
    if (armed) {
      const sessionMode = (s && s.mode) || captureMode;
      const label = MODE_LABELS[sessionMode] || sessionMode;
      modeRow = `<div class="mode-row">
          <span class="mode-label">Mode</span>
          <span class="mode-locked">locked · ${esc(label)}</span>
        </div>`;
    } else {
      const btn = (val) => {
        const active = val === captureMode;
        const cls = active ? 'btn' : 'btn secondary';
        return `<form class="inline" method="POST" action="/sessions/set_mode">
            <input type="hidden" name="mode" value="${val}">
            <button class="${cls}" type="submit">${MODE_LABELS[val]}</button>
          </form>`;
      };
      modeRow = `<div class="mode-row">
          <span class="mode-label">Mode</span>
          ${btn('camera_only')}${btn('on_device')}
        </div>`;
    }
    sessionBox.innerHTML = `
      <div class="session-head">${chip}${sid}</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm">
          <button class="btn" type="submit" ${armed ? 'disabled' : ''}>Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" ${armed ? '' : 'disabled'}>Stop</button>
        </form>
        ${clearBtn}
      </div>
      ${modeRow}`;

    // Mirror into the nav's tiny status strip.
    if (navStatus) {
      const online = (state.devices || []).length;
      const cal = (state.calibrations || []).length;
      navStatus.innerHTML = `
        <span class="pair"><span class="label">Devices</span><span class="val">${online}/2</span></span>
        <span class="pair"><span class="label">Calibrated</span><span class="val">${cal}/2</span></span>
        <span class="pair"><span class="label">Session</span>` +
        (armed
          ? `<span class="val armed">${esc(s.id || '—')}</span>`
          : `<span class="val idle">idle</span>`) +
        `</span>`;
    }
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  function renderEvents(events) {
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      return;
    }
    eventsBox.innerHTML = events.map(e => {
      const cams = (e.cameras || []).join(' · ') || '—';
      const mode = (e.cameras || []).length >= 2 ? 'dual' : 'single';
      const stat = (e.status || '').replace(/_/g, ' ');
      const peakZ = fmtNum(e.peak_z_m, 2);
      const duration = fmtNum(e.duration_s, 2);
      const mean = fmtNum(e.mean_residual_m, 4);
      const sid = esc(e.session_id);
      // Delete form is a sibling of the event-row link so submitting it
      // does not navigate via the wrapping anchor. Confirm dialog guards
      // against accidental clicks — once removed, disk files are gone.
      const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
      const captureMode = e.mode === 'on_device' ? 'on-device' : 'camera-only';
      const captureModeLabel = captureMode;
      return `
        <div class="event-item">
          <a class="event-row" href="/viewer/${sid}">
            <div class="event-top">
              <span class="sid">${sid}</span>
              <span class="chip ${esc(mode)}">${mode}</span>
              <span class="chip ${esc(e.status || '')}">${esc(stat)}</span>
              <span class="chip ${esc(captureMode)}">${esc(captureModeLabel)}</span>
            </div>
            <div class="event-stats">
              <span><span class="k">Cams</span><span class="v">${esc(cams)}</span></span>
              <span><span class="k">3D pts</span><span class="v">${e.n_triangulated || 0}</span></span>
              <span><span class="k">Mean resid (m)</span><span class="v">${mean}</span></span>
              <span><span class="k">Peak Z (m)</span><span class="v">${peakZ}</span></span>
              <span><span class="k">Duration (s)</span><span class="v">${duration}</span></span>
            </div>
          </a>
          <form class="event-delete-form" method="POST"
                action="/sessions/${sid}/delete"
                onsubmit="return confirm(${JSON.stringify(confirmMsg)});">
            <button class="event-delete" type="submit"
                    aria-label="Delete session ${sid}">&times;</button>
          </form>
        </div>`;
    }).join('');
  }

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;

  async function tickStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      // /status does not include calibrations; merge the last-known set so
      // the devices card shows "calibrated" chips between calibration ticks.
      s.calibrations = currentCalibrations || [];
      currentDevices = s.devices || [];
      currentSession = s.session || null;
      renderDevices(s);
      renderSession(s);
    } catch (e) { /* silent retry next tick */ }
  }

  async function tickCalibration() {
    try {
      const r = await fetch('/calibration/state', { cache: 'no-store' });
      if (!r.ok) return;
      const payload = await r.json();
      currentCalibrations = (payload.calibrations || []).map(c => c.camera_id);
      renderDevices({ devices: currentDevices || [], calibrations: currentCalibrations });
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations });
      if (payload.plot && sceneRoot && window.Plotly) {
        Plotly.react(sceneRoot, payload.plot.data || [], payload.plot.layout || {}, { responsive: true });
      }
    } catch (e) { /* silent */ }
  }

  async function tickEvents() {
    try {
      const r = await fetch('/events', { cache: 'no-store' });
      if (!r.ok) return;
      const events = await r.json();
      renderEvents(events);
    } catch (e) { /* silent */ }
  }

  // Prime all three immediately, then stagger polling so the UI stays
  // current without hammering the server. Status carries arming state
  // (1 s) and is the only high-frequency tick.
  tickStatus();
  tickCalibration();
  tickEvents();
  setInterval(tickStatus, 1000);
  setInterval(tickCalibration, 5000);
  setInterval(tickEvents, 5000);
})();
"""


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render_device_rows(
    devices: list[dict[str, Any]],
    calibrations: list[str],
) -> str:
    """Server-rendered initial paint. JS will replace this within 1 s but we
    avoid a flash of empty content on first load."""
    device_by_id = {d["camera_id"]: d for d in devices}
    calibrated = set(calibrations)

    def render_row(cam_id: str) -> str:
        dev = device_by_id.get(cam_id)
        online = dev is not None
        time_synced = bool(dev.get("time_synced")) if dev else False
        is_cal = cam_id in calibrated
        if not online:
            meta, chip_cls, chip_label = "Not seen", "idle", "offline"
        elif is_cal:
            meta, chip_cls, chip_label = "Ready · pose known", "calibrated", "calibrated"
        else:
            meta, chip_cls, chip_label = "Awaiting calibration", "online", "online"
        cal_dot = "ok" if is_cal else ("warn" if online else "bad")
        sync_dot = "ok" if time_synced else ("warn" if online else "bad")
        sync_label = "synced" if time_synced else ("not synced" if online else "offline")
        cal_label = "calibrated" if is_cal else ("pending" if online else "offline")
        return (
            f'<div class="device">'
            f'<div class="id">{html.escape(cam_id)}</div>'
            f'<div>'
            f'<div class="meta">{html.escape(meta)}</div>'
            f'<div class="sub">'
            f'<span class="item"><span class="dot {sync_dot}"></span>time sync · {sync_label}</span>'
            f'<span class="item"><span class="dot {cal_dot}"></span>pose · {cal_label}</span>'
            f'</div>'
            f'</div>'
            f'<div><span class="chip {chip_cls}">{chip_label}</span></div>'
            f'</div>'
        )

    rows = [render_row(cam) for cam in ("A", "B")]
    rows.extend(render_row(d["camera_id"]) for d in devices if d["camera_id"] not in ("A", "B"))
    return "".join(rows)


_MODE_LABELS = {
    "camera_only": "Camera-only",
    "on_device": "On-device",
}


def _render_session_body(
    session: dict[str, Any] | None,
    capture_mode: str = "camera_only",
) -> str:
    armed = session is not None and session.get("armed")
    chip_html = (
        '<span class="chip armed">armed</span>'
        if armed
        else '<span class="chip idle">idle</span>'
    )
    sid_html = (
        f'<span class="session-id">{html.escape(session["id"])}</span>'
        if session and session.get("id")
        else ""
    )
    arm_btn = (
        '<form class="inline" method="POST" action="/sessions/arm">'
        f'<button class="btn" type="submit"{" disabled" if armed else ""}>Arm session</button>'
        "</form>"
    )
    stop_btn = (
        '<form class="inline" method="POST" action="/sessions/stop">'
        f'<button class="btn danger" type="submit"{"" if armed else " disabled"}>Stop</button>'
        "</form>"
    )
    clear_btn = ""
    if not armed and session and session.get("id"):
        clear_btn = (
            '<form class="inline" method="POST" action="/sessions/clear">'
            '<button class="btn" type="submit">Clear</button>'
            "</form>"
        )

    # Mode picker. When armed, the armed session's snapshot mode is shown as
    # locked — flipping only affects the next arm, so disabling the buttons
    # avoids the mental-model drift of "I clicked but nothing changed".
    if armed:
        session_mode = (session or {}).get("mode", capture_mode)
        mode_row = (
            '<div class="mode-row">'
            '<span class="mode-label">Mode</span>'
            f'<span class="mode-locked">locked · {html.escape(_MODE_LABELS.get(session_mode, session_mode))}</span>'
            "</div>"
        )
    else:
        def _mode_button(value: str) -> str:
            active = value == capture_mode
            cls = "btn" if active else "btn secondary"
            return (
                '<form class="inline" method="POST" action="/sessions/set_mode">'
                f'<input type="hidden" name="mode" value="{value}">'
                f'<button class="{cls}" type="submit">{_MODE_LABELS[value]}</button>'
                "</form>"
            )
        mode_row = (
            '<div class="mode-row">'
            '<span class="mode-label">Mode</span>'
            f'{_mode_button("camera_only")}{_mode_button("on_device")}'
            "</div>"
        )

    return (
        f'<div class="session-head">{chip_html}{sid_html}</div>'
        f'<div class="session-actions">{arm_btn}{stop_btn}{clear_btn}</div>'
        f'{mode_row}'
    )


def _render_events_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<div class="events-empty">No sessions received yet.</div>'
    parts: list[str] = []
    for e in events:
        sid = html.escape(e["session_id"])
        cams = " · ".join(html.escape(c) for c in e.get("cameras", [])) or "—"
        cam_mode = "dual" if len(e.get("cameras", [])) >= 2 else "single"
        status = html.escape(e.get("status", ""))
        stat_label = status.replace("_", " ")
        capture_mode = "on-device" if e.get("mode") == "on_device" else "camera-only"
        mean = "—" if e.get("mean_residual_m") is None else format(e["mean_residual_m"], ".4f")
        peak_z = "—" if e.get("peak_z_m") is None else format(e["peak_z_m"], ".2f")
        duration = "—" if e.get("duration_s") is None else format(e["duration_s"], ".2f")
        parts.append(
            # event-row is a link into the viewer; the delete form is a
            # sibling (not a descendant) so the button submit doesn't
            # navigate via the wrapping anchor.
            f'<div class="event-item">'
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-top">'
            f'<span class="sid">{sid}</span>'
            f'<span class="chip {cam_mode}">{cam_mode}</span>'
            f'<span class="chip {status}">{stat_label}</span>'
            f'<span class="chip {capture_mode}">{capture_mode}</span>'
            f"</div>"
            f'<div class="event-stats">'
            f'<span><span class="k">Cams</span><span class="v">{cams}</span></span>'
            f'<span><span class="k">3D pts</span><span class="v">{e.get("n_triangulated", 0)}</span></span>'
            f'<span><span class="k">Mean resid (m)</span><span class="v">{mean}</span></span>'
            f'<span><span class="k">Peak Z (m)</span><span class="v">{peak_z}</span></span>'
            f'<span><span class="k">Duration (s)</span><span class="v">{duration}</span></span>'
            f"</div>"
            f"</a>"
            f'<form class="event-delete-form" method="POST" '
            f'action="/sessions/{sid}/delete" '
            f'onsubmit="return confirm(\'刪除 session {sid}？此動作無法復原。\');">'
            f'<button class="event-delete" type="submit" '
            f'aria-label="Delete session {sid}">&times;</button>'
            f"</form>"
            f"</div>"
        )
    return "".join(parts)


def _render_nav_status(
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    calibrations: list[str],
) -> str:
    armed = session is not None and session.get("armed")
    session_html = (
        f'<span class="val armed">{html.escape(session.get("id", "—"))}</span>'
        if armed
        else '<span class="val idle">idle</span>'
    )
    return (
        f'<span class="pair"><span class="label">Devices</span><span class="val">{len(devices)}/2</span></span>'
        f'<span class="pair"><span class="label">Calibrated</span><span class="val">{len(calibrations)}/2</span></span>'
        f'<span class="pair"><span class="label">Session</span>{session_html}</span>'
    )


def render_events_index_html(
    events: list[dict[str, Any]],
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    capture_mode: str = "camera_only",
) -> str:
    """Render the dashboard: top nav + sidebar (devices / session / events)
    + a canvas showing the current calibration scene. All three panels
    hydrate from JSON ticks after first paint — the initial SSR avoids a
    flash of empty content while the first fetch is in flight."""
    devices = devices or []
    calibrations = calibrations or []

    from main import state  # local import: avoid circular at module load time

    scene = build_calibration_scene(state.calibrations())
    fig = _build_figure(scene)
    scene_div = fig.to_html(include_plotlyjs=False, full_html=False, div_id="scene-root")

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}</style>"
        "</head><body>"
        '<nav class="nav">'
        '<span class="brand"><span class="dot"></span>BALL_TRACKER</span>'
        f'<div class="status-line" id="nav-status">{_render_nav_status(devices, session, calibrations)}</div>'
        "</nav>"
        '<div class="layout">'
        '<aside class="sidebar">'
        '<div class="card">'
        '<h2 class="card-title">Devices</h2>'
        f'<div id="devices-body">{_render_device_rows(devices, calibrations)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Session</h2>'
        f'<div id="session-body">{_render_session_body(session, capture_mode)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Events</h2>'
        f'<div id="events-body">{_render_events_body(events)}</div>'
        "</div>"
        "</aside>"
        '<section class="canvas">'
        '<div class="canvas-hint">Live calibration preview · drag to rotate</div>'
        f"{scene_div}"
        "</section>"
        "</div>"
        f"<script>{_JS_TEMPLATE}</script>"
        "</body></html>"
    )
