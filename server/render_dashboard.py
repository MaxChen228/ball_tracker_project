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
  --surface-hover: #F3F0EA;
  --border-base: {_BORDER_BASE};
  --border-l: {_BORDER_L};
  --ink: {_INK};
  --sub: {_SUB};
  --ink-light: {_INK_LIGHT};
  --dev: {_DEV};
  --contra: {_CONTRA};
  --dual: {_DUAL};
  --accent: {_ACCENT};
  /* Semantic state washes — mirror kg admin's badge palette so chips
     read as subdued backgrounds rather than saturated pills. */
  --passed:      #256246; --passed-bg:  rgba(37,98,70,.08);
  --warn:        #9B6B16; --warn-bg:    rgba(155,107,22,.08);
  --failed:      #A7372A; --failed-bg:  rgba(167,55,42,.08);
  --idle-bg:     rgba(105,114,125,.06);
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --nav-h: 52px;
  --sidebar-w: 440px;
  /* Unified 8px-grid spacing + single border-radius. Use var(--r)
     everywhere; the old 4/12/2 mix collapses to one rhythm. */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px; --s-4: 16px; --s-5: 24px;
  --r: 3px;
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
.nav .status-line .val.armed {{ color: var(--passed); }}
.nav .status-line .val.idle {{ color: var(--sub); }}
/* Warn-tinted count when < 2 cams report — makes "0/2 devices" jump out
   as actionable instead of reading as a flat label next to "1/2 cal". */
.nav .status-line .val.partial {{ color: var(--warn); }}
.nav .status-line .val.full    {{ color: var(--passed); }}

/* --- Main layout: sidebar + canvas --- */
.layout {{ display: flex; height: 100vh; padding-top: var(--nav-h); }}
.sidebar {{ width: var(--sidebar-w); flex-shrink: 0; overflow-y: auto;
            background: var(--surface); border-right: 1px solid var(--border-base);
            padding: var(--s-5) var(--s-4); z-index: 10;
            display: flex; flex-direction: column; gap: var(--s-3); }}
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
         border-radius: var(--r); padding: var(--s-4); }}
.card + .card {{ margin-top: 0; }}
.card-title {{ font-family: var(--mono); font-weight: 500; font-size: 11px;
               letter-spacing: 0.12em; text-transform: uppercase; color: var(--sub);
               margin: 0 0 var(--s-3) 0; padding: 0 0 var(--s-2) 0;
               border-bottom: 1px solid var(--border-l); }}
.card-subtitle {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.16em;
                  text-transform: uppercase; color: var(--sub);
                  margin-top: var(--s-3); margin-bottom: var(--s-1); }}
.card section + section {{ border-top: 1px solid var(--border-l); margin-top: var(--s-3);
                           padding-top: var(--s-3); }}

/* --- Device rows --- */
/* Middle column uses minmax so a wider chip (CALIBRATED vs OFFLINE) can't
   squeeze the sub-row into a second line and make A / B rows different
   heights. `auto` → min-content keeps the chip column tight. */
.device {{ display: grid; grid-template-columns: 28px minmax(0, 1fr) min-content;
           align-items: center;
           gap: var(--s-3); padding: var(--s-2) 0; }}
.device + .device {{ border-top: 1px solid var(--border-l); }}
.device .id {{ font-family: var(--mono); font-size: 14px; font-weight: 600; color: var(--ink);
               letter-spacing: 0.04em; }}
.device .meta {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                 text-transform: uppercase; color: var(--sub); }}
.device .meta em {{ font-style: normal; color: var(--ink-light); }}
/* Two-column grid forces both items onto the same line and guarantees
   identical row height regardless of per-device label length. */
.device .sub {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
                gap: var(--s-2); margin-top: var(--s-1); }}
.device .sub .item {{ font-family: var(--mono); font-size: 9px; letter-spacing: 0.12em;
                      text-transform: uppercase; color: var(--sub);
                      display: flex; align-items: center; gap: var(--s-1);
                      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.device .sub .dot {{ width: 6px; height: 6px; border-radius: 50%;
                     background: var(--border-base); display: inline-block; }}
.device .sub .dot.ok {{ background: var(--passed); }}
.device .sub .dot.warn {{ background: var(--warn); }}
.device .sub .dot.bad {{ background: var(--failed); }}

/* --- Chip (pill) — kg-admin badge style: flat, rectangular, subdued bg wash.
   Single rectangle geometry with three semantic variants (passed/warn/failed)
   replacing the former 10+ custom colors. */
.chip {{ display: inline-block; padding: 2px 8px; border-radius: var(--r);
         font-family: var(--mono); font-size: 10px; font-weight: 500;
         letter-spacing: 0.10em; text-transform: uppercase;
         border: 1px solid var(--border-base); color: var(--sub); background: transparent;
         transition: border-color 0.15s ease, color 0.15s ease; }}
/* Green wash — online / calibrated / armed / paired successes */
.chip.online, .chip.calibrated, .chip.armed, .chip.paired
  {{ color: var(--passed); border-color: var(--passed); background: var(--passed-bg); }}
/* Amber wash — degraded / partial / paired-no-points / on-device accent */
.chip.partial, .chip.paired_no_points, .chip.on-device
  {{ color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }}
/* Red wash — explicit errors */
.chip.error {{ color: var(--failed); border-color: var(--failed); background: var(--failed-bg); }}
/* Neutral (grey) — idle / single / camera-only default */
.chip.idle, .chip.single, .chip.camera-only
  {{ color: var(--sub); border-color: var(--border-base); background: transparent; }}
/* Cam-identity dual chip — retains the B-camera orange tint so per-cam
   rows still read as paired vs single at a glance. */
.chip.dual {{ color: var(--dual); border-color: var(--dual); background: rgba(211,84,0,0.06); }}

/* --- Session block --- */
.session-head {{ display: flex; align-items: center; gap: var(--s-2); margin-bottom: var(--s-2); }}
.session-id {{ font-family: var(--mono); font-size: 13px; color: var(--ink);
               letter-spacing: 0.04em; }}
.session-actions {{ display: flex; gap: var(--s-2); margin-top: var(--s-3); }}
.mode-row {{ display: flex; gap: var(--s-2); align-items: center; margin-top: var(--s-3);
             flex-wrap: wrap; }}
.mode-label {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                text-transform: uppercase; color: var(--sub); min-width: 44px; }}
.mode-locked {{ font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em;
                 color: var(--sub); padding-left: var(--s-1); }}
/* Segmented control: the three mode buttons share one outer border and
   collapse their individual borders/radius so the eye reads them as a
   single exclusive choice, not three separate CTAs. */
.mode-segmented {{ display: inline-flex; border: 1px solid var(--border-base);
                    border-radius: var(--r); overflow: hidden; }}
.mode-segmented form.inline {{ display: inline-flex; margin: 0; }}
.mode-segmented form.inline + form.inline button.btn {{
  border-left: 1px solid var(--border-base); }}
.mode-segmented button.btn,
.mode-segmented button.btn.secondary {{
  border: 0; border-radius: 0; padding: 6px 12px; font-size: 10px;
  letter-spacing: 0.10em; }}
.mode-segmented button.btn.secondary {{
  background: transparent; color: var(--sub); }}
.mode-segmented button.btn.secondary:hover:not(:disabled) {{
  background: var(--surface-hover); color: var(--ink); border: 0; }}

/* --- Buttons — unified geometry, single border-radius. Standard is
   36px tall, mini variant (used in event delete) is 24px. --- */
button.btn {{ font-family: var(--mono); font-size: 11px; font-weight: 500;
              letter-spacing: 0.08em; text-transform: uppercase;
              padding: 8px 14px; border-radius: var(--r); cursor: pointer;
              background: var(--ink); color: var(--surface);
              border: 1px solid var(--ink); transition: border-color 0.15s, background 0.15s, color 0.15s; }}
button.btn:hover:not(:disabled) {{ background: var(--ink-light); }}
button.btn.secondary {{ background: transparent; color: var(--ink);
                        border-color: var(--border-base); }}
button.btn.secondary:hover:not(:disabled) {{ border-color: var(--ink); }}
button.btn.danger {{ background: transparent; color: var(--dev);
                     border-color: var(--dev); }}
button.btn.danger:hover:not(:disabled) {{ background: var(--dev); color: var(--surface); }}
button.btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}
form.inline {{ display: inline-block; margin: 0; }}

/* --- Events list — dense, hover-highlighted rows inspired by kg admin
   tables. Row-level hover wash replaces the former negative-margin hack. */
.events-empty {{ color: var(--sub); font-size: 12px; padding: var(--s-3) 0;
                 font-style: italic; font-family: var(--mono); }}
.event-item {{ display: flex; align-items: flex-start;
               border-top: 1px solid var(--border-l);
               transition: background 0.12s ease; }}
.event-item:first-child {{ border-top: 0; }}
.event-item:hover {{ background: var(--surface-hover); }}
.event-row {{ flex: 1; min-width: 0; display: block; text-decoration: none;
              color: inherit; padding: var(--s-2) var(--s-2); }}
/* Trajectory overlay toggle — only visible on sessions with 3D pts. The
   coloured dot mirrors the trace tint Plotly uses in the canvas so the
   operator can match checkbox → line without guessing. Clicking the
   checkbox does NOT navigate (it's outside the <a class="event-row">). */
.traj-toggle {{ flex: 0 0 auto; padding: var(--s-2) 0 0 var(--s-2);
                display: flex; align-items: center; gap: 4px;
                cursor: pointer; user-select: none; }}
.traj-toggle input[type=checkbox] {{ accent-color: var(--ink);
                                      width: 13px; height: 13px; margin: 0;
                                      cursor: pointer; }}
.traj-toggle .swatch {{ width: 10px; height: 10px; border-radius: 50%;
                         border: 1px solid rgba(0,0,0,0.12);
                         display: inline-block; }}
.traj-toggle-placeholder {{ flex: 0 0 auto;
                             width: calc(13px + 10px + var(--s-2) + 8px); }}
.event-top {{ display: flex; align-items: center; gap: var(--s-2); margin-bottom: var(--s-1);
              flex-wrap: wrap; }}
.event-top .sid {{ font-family: var(--mono); font-size: 12px; font-weight: 500;
                   color: var(--ink); letter-spacing: 0.04em; margin-right: var(--s-1); }}
.event-stats {{ display: grid; grid-template-columns: repeat(3, 1fr);
                gap: var(--s-1) var(--s-3);
                font-family: var(--mono); font-size: 11px; color: var(--ink-light); }}
.event-stats .k {{ color: var(--sub); letter-spacing: 0.10em; text-transform: uppercase;
                   font-size: 9px; display: block; margin-bottom: 1px; }}
.event-stats .v {{ font-variant-numeric: tabular-nums; color: var(--ink); }}
.event-delete-form {{ flex: 0 0 auto; margin: var(--s-2) var(--s-1) 0 0; }}
.event-delete {{ background: transparent; border: 1px solid var(--border-base);
                 color: var(--sub); font-family: var(--mono); font-size: 13px;
                 line-height: 1; padding: 2px 8px 3px; border-radius: var(--r);
                 cursor: pointer; transition: border-color 0.15s, color 0.15s, background 0.15s; }}
.event-delete:hover {{ border-color: var(--dev); color: var(--dev);
                       background: var(--surface); }}

/* --- Canvas overlay hint --- */
.canvas-hint {{ position: absolute; left: var(--s-4); top: var(--s-4); z-index: 5;
                font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
                text-transform: uppercase; color: var(--sub);
                background: var(--surface); border: 1px solid var(--border-l);
                border-radius: var(--r); padding: var(--s-1) var(--s-2); pointer-events: none; }}
"""


_JS_TEMPLATE = r"""
(function () {
  const EXPECTED = ['A', 'B'];

  const sceneRoot = document.getElementById('scene-root');
  const devicesBox = document.getElementById('devices-body');
  const sessionBox = document.getElementById('session-body');
  const eventsBox = document.getElementById('events-body');
  const navStatus = document.getElementById('nav-status');

  // --- Trajectory overlay state --------------------------------------------
  // Persisted set of session_ids whose triangulated trajectory is currently
  // painted on top of the calibration canvas. Cached /results/{sid} payloads
  // avoid refetching across ticks; basePlot is the last /calibration/state
  // fig spec so checkbox toggles can repaint without waiting for the 5 s
  // tick. Palette is deliberately disjoint from the A/B camera colours in
  // render_scene.py so the trajectory lines don't get confused with cams.
  const TRAJ_STORAGE_KEY = 'ball_tracker_dashboard_selected_trajectories';
  const TRAJ_PALETTE = ['#256246', '#9B6B16', '#A7372A', '#4A6B8C', '#5A5550', '#C97A2B'];
  const selectedTrajIds = (() => {
    try {
      const raw = localStorage.getItem(TRAJ_STORAGE_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch { return new Set(); }
  })();
  const trajCache = new Map();       // sid -> {points, points_on_device}
  let basePlot = null;               // last /calibration/state .plot payload

  function persistTrajSelection() {
    try { localStorage.setItem(TRAJ_STORAGE_KEY, JSON.stringify([...selectedTrajIds])); }
    catch { /* storage full / private mode — ignore, selection stays in-memory */ }
  }

  // Stable hash → palette index so the same session always gets the same
  // colour across reloads even though the Set iteration order is random.
  function trajColorFor(sid) {
    let h = 0;
    for (let i = 0; i < sid.length; ++i) h = ((h << 5) - h + sid.charCodeAt(i)) | 0;
    return TRAJ_PALETTE[Math.abs(h) % TRAJ_PALETTE.length];
  }

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      const entry = {
        points: data.points || [],
        points_on_device: data.points_on_device || [],
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  function trajTracesFor(sid, result, color) {
    // Prefer on-device points when present (pre-encode, wider detection
    // window in dual sessions); fall back to server points. Single line
    // trace with markers so sparse trajectories (few pts) still read.
    const src = (result.points_on_device && result.points_on_device.length)
      ? result.points_on_device : (result.points || []);
    if (!src.length) return [];
    return [{
      type: 'scatter3d',
      mode: 'lines+markers',
      x: src.map(p => p.x_m),
      y: src.map(p => p.y_m),
      z: src.map(p => p.z_m),
      line: { color, width: 4 },
      marker: { color, size: 3, opacity: 0.85 },
      name: `traj ${sid}`,
      hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
      customdata: src.map(p => p.t_rel_s),
      showlegend: true,
    }];
  }

  // Tracks whether the dashboard 3D canvas has been painted at least once,
  // so subsequent paints can omit `scene.camera` and not stomp on the
  // user's orbit. Declared above repaintCanvas so the function-scope
  // closure binds without temporal-dead-zone risk.
  let canvasFirstPaintDone = false;

  async function repaintCanvas() {
    if (!basePlot || !window.Plotly) return;
    const extraTraces = [];
    // Load any missing trajectories in parallel — checkbox clicks before
    // the first tick should still paint immediately.
    await Promise.all([...selectedTrajIds].map(sid => ensureTrajLoaded(sid)));
    for (const sid of selectedTrajIds) {
      const result = trajCache.get(sid);
      if (!result) continue;
      extraTraces.push(...trajTracesFor(sid, result, trajColorFor(sid)));
    }
    // After the first paint, strip `scene.camera` from the layout we
    // hand to Plotly.react — otherwise every 5 s tickCalibration push
    // resets the user's orbit back to the default eye position. Plotly
    // leaves the existing camera alone if the new layout doesn't carry
    // one. First paint must keep the camera so the initial view is the
    // designed default rather than wherever Plotly auto-fits.
    let layout = basePlot.layout || {};
    if (canvasFirstPaintDone && layout.scene && layout.scene.camera) {
      layout = JSON.parse(JSON.stringify(layout));
      delete layout.scene.camera;
    }
    Plotly.react(
      sceneRoot,
      [...(basePlot.data || []), ...extraTraces],
      layout,
      { responsive: true, scrollZoom: true },
    );
    canvasFirstPaintDone = true;
  }

  // Plotly's built-in 3D wheel-zoom is tuned for mouse wheels and feels
  // sluggish on trackpads (especially pinch-to-zoom which arrives as
  // ctrl+wheel with tiny deltas). Replace it with a direct camera.eye
  // scale so each wheel tick = ~10 % distance change and trackpad
  // gestures get the same per-event treatment as a mouse wheel click.
  if (sceneRoot) {
    sceneRoot.addEventListener('wheel', (e) => {
      if (!sceneRoot._fullLayout || !sceneRoot._fullLayout.scene) return;
      const cam = sceneRoot._fullLayout.scene.camera;
      if (!cam || !cam.eye) return;
      e.preventDefault();
      // Wheel-down (positive deltaY) = zoom out, wheel-up = zoom in.
      // Magnitude scaled by sqrt so trackpad's many-tiny-events feels
      // continuous instead of jittery; mouse wheel's chunky events
      // still produce a noticeable but bounded jump per click.
      const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
      const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
      Plotly.relayout(sceneRoot, {
        'scene.camera.eye': {
          x: cam.eye.x * factor,
          y: cam.eye.y * factor,
          z: cam.eye.z * factor,
        },
      });
    }, { passive: false });
  }

  // Delegated change handler — event list re-renders on every tick, so we
  // can't rebind per-checkbox. Capture click on the wrapping <label> to
  // prevent the event-row <a> from swallowing the toggle.
  eventsBox.addEventListener('click', (e) => {
    if (e.target.closest('.traj-toggle')) e.stopPropagation();
  });
  eventsBox.addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-traj-sid]');
    if (!cb) return;
    const sid = cb.dataset.trajSid;
    if (cb.checked) selectedTrajIds.add(sid);
    else selectedTrajIds.delete(sid);
    persistTrajSelection();
    repaintCanvas();
  });

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

  const MODE_LABELS = { camera_only: 'Camera-only', on_device: 'On-device', dual: 'Dual' };

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
          <div class="mode-segmented" role="radiogroup" aria-label="Capture mode">
            ${btn('camera_only')}${btn('on_device')}${btn('dual')}
          </div>
        </div>`;
    }
    const sessHtml = `
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
    sessionBox.innerHTML = sessHtml;

    // Mirror into the nav's tiny status strip.
    if (navStatus) {
      const online = (state.devices || []).length;
      const cal = (state.calibrations || []).length;
      const countCls = n => (n >= 2 ? 'full' : 'partial');
      const navHtml = `
        <span class="pair"><span class="label">Devices</span><span class="val ${countCls(online)}">${online}/2</span></span>
        <span class="pair"><span class="label">Calibrated</span><span class="val ${countCls(cal)}">${cal}/2</span></span>
        <span class="pair"><span class="label">Session</span>` +
        (armed
          ? `<span class="val armed">${esc(s.id || '—')}</span>`
          : `<span class="val idle">idle</span>`) +
        `</span>`;
      navStatus.innerHTML = navHtml;
    }
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  function renderEvents(events) {
    let evHtml;
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      return;
    }
    evHtml = events.map(e => {
      const cams = (e.cameras || []).join(' · ') || '—';
      const mode = (e.cameras || []).length >= 2 ? 'dual' : 'single';
      const stat = (e.status || '').replace(/_/g, ' ');
      const peakZ = fmtNum(e.peak_z_m, 2);
      const duration = fmtNum(e.duration_s, 2);
      const mean = fmtNum(e.mean_residual_m, 4);
      const sid = esc(e.session_id);
      // Sessions with no triangulated output (error / no sync / single cam)
      // carry all-null metrics; skip the stats row entirely so the events
      // list stays dense and the meaningful rows don't drown in "—" cells.
      const hasMetrics = (e.n_triangulated || 0) > 0
        || peakZ !== '—' || duration !== '—' || mean !== '—';
      // Delete form is a sibling of the event-row link so submitting it
      // does not navigate via the wrapping anchor. Confirm dialog guards
      // against accidental clicks — once removed, disk files are gone.
      const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
      const captureMode = e.mode === 'on_device' ? 'on-device'
                        : e.mode === 'dual'       ? 'dual'
                        : 'camera-only';
      const captureModeLabel = captureMode;
      // Trajectory overlay toggle: only sessions with 3D points qualify.
      // Sibling to event-row (not inside) so the checkbox click doesn't
      // trigger the wrapping link's navigation.
      const hasTraj = (e.n_triangulated || 0) > 0;
      const color = hasTraj ? trajColorFor(e.session_id) : '';
      const checked = selectedTrajIds.has(e.session_id) ? 'checked' : '';
      const toggle = hasTraj
        ? `<label class="traj-toggle" title="Overlay trajectory on canvas">
             <input type="checkbox" data-traj-sid="${sid}" ${checked}>
             <span class="swatch" style="background:${color}"></span>
           </label>`
        : `<span class="traj-toggle-placeholder" aria-hidden="true"></span>`;
      return `
        <div class="event-item">
          ${toggle}
          <a class="event-row" href="/viewer/${sid}">
            <div class="event-top">
              <span class="sid">${sid}</span>
              <span class="chip ${esc(mode)}">${mode}</span>
              <span class="chip ${esc(e.status || '')}">${esc(stat)}</span>
              <span class="chip ${esc(captureMode)}">${esc(captureModeLabel)}</span>
            </div>
            ${hasMetrics ? `<div class="event-stats">
              <span><span class="k">Cams</span><span class="v">${esc(cams)}</span></span>
              <span><span class="k">3D pts</span><span class="v">${e.n_triangulated || 0}</span></span>
              <span><span class="k">Mean resid (m)</span><span class="v">${mean}</span></span>
              <span><span class="k">Peak Z (m)</span><span class="v">${peakZ}</span></span>
              <span><span class="k">Duration (s)</span><span class="v">${duration}</span></span>
            </div>` : ''}
          </a>
          <form class="event-delete-form" method="POST"
                action="/sessions/${sid}/delete"
                onsubmit="return confirm(${JSON.stringify(confirmMsg)});">
            <button class="event-delete" type="submit"
                    aria-label="Delete session ${sid}">&times;</button>
          </form>
        </div>`;
    }).join('');
    eventsBox.innerHTML = evHtml;
  }

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentCaptureMode = 'camera_only';

  // Keys used to skip re-renders when nothing changed. We compare serialised
  // state data rather than innerHTML strings because the browser re-serialises
  // HTML differently from the raw template literals we build.
  let _lastDevKey = null;
  let _lastSessKey = null;
  let _lastNavKey = null;
  let _lastEvKey = null;

  const _origRenderDevices = renderDevices;
  renderDevices = function(state) {
    const key = JSON.stringify({
      devices: (state.devices || []).map(d => ({ id: d.camera_id, ts: d.time_synced })),
      calibrations: (state.calibrations || []).slice().sort(),
    });
    if (key === _lastDevKey) return;
    _lastDevKey = key;
    _origRenderDevices(state);
  };

  const _origRenderSession = renderSession;
  renderSession = function(state) {
    const s = state.session;
    const sessKey = JSON.stringify({
      armed: !!(s && s.armed), id: s && s.id, mode: s && s.mode,
      capture_mode: state.capture_mode,
    });
    const navKey = JSON.stringify({
      online: (state.devices || []).length,
      cal: (state.calibrations || []).length,
      armed: !!(s && s.armed), id: s && s.id,
    });
    if (sessKey === _lastSessKey && navKey === _lastNavKey) return;
    _lastSessKey = sessKey;
    _lastNavKey = navKey;
    _origRenderSession(state);
  };

  const _origRenderEvents = renderEvents;
  renderEvents = function(events) {
    const key = JSON.stringify((events || []).map(e => ({
      id: e.session_id, status: e.status, n: e.n_triangulated,
    })));
    if (key === _lastEvKey) return;
    _lastEvKey = key;
    _origRenderEvents(events);
  };

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
      currentCaptureMode = s.capture_mode || 'camera_only';
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
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations, capture_mode: currentCaptureMode });
      if (payload.plot && sceneRoot && window.Plotly) {
        // Cache the base plot so checkbox toggles can re-overlay
        // trajectories between /calibration/state ticks without refetching.
        basePlot = payload.plot;
        repaintCanvas();
      }
    } catch (e) { /* silent */ }
  }

  async function tickEvents() {
    try {
      const r = await fetch('/events', { cache: 'no-store' });
      if (!r.ok) return;
      const events = await r.json();
      // Prune selection for sessions the user deleted server-side so the
      // canvas doesn't keep painting a phantom trajectory whose checkbox
      // no longer exists.
      const liveIds = new Set(events.map(e => e.session_id));
      let pruned = false;
      for (const sid of [...selectedTrajIds]) {
        if (!liveIds.has(sid)) {
          selectedTrajIds.delete(sid);
          trajCache.delete(sid);
          pruned = true;
        }
      }
      if (pruned) { persistTrajSelection(); repaintCanvas(); }
      renderEvents(events);
    } catch (e) { /* silent */ }
  }

  // Mode toggle: intercept form submit via fetch + optimistic update so
  // the button state never bounces back to the previous value between the
  // POST and the next tickStatus round-trip.
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (form.action && form.action.endsWith('/sessions/set_mode')) {
      e.preventDefault();
      const mode = (form.querySelector('input[name="mode"]') || {}).value;
      if (!mode) return;
      currentCaptureMode = mode;
      // Invalidate key so the next renderSession call repaints.
      _lastSessKey = null;
      renderSession({ devices: currentDevices || [], session: currentSession,
                      calibrations: currentCalibrations || [], capture_mode: currentCaptureMode });
      try { await fetch('/sessions/set_mode', { method: 'POST', body: new FormData(form) }); }
      catch (_) {}
    }
  });

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
    "dual": "Dual",
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
            '<div class="mode-segmented" role="radiogroup" aria-label="Capture mode">'
            f'{_mode_button("camera_only")}{_mode_button("on_device")}{_mode_button("dual")}'
            "</div></div>"
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
        mode_val = e.get("mode")
        capture_mode = (
            "on-device" if mode_val == "on_device"
            else "dual" if mode_val == "dual"
            else "camera-only"
        )
        mean = "—" if e.get("mean_residual_m") is None else format(e["mean_residual_m"], ".4f")
        peak_z = "—" if e.get("peak_z_m") is None else format(e["peak_z_m"], ".2f")
        duration = "—" if e.get("duration_s") is None else format(e["duration_s"], ".2f")
        # Collapse the stats row when the session produced no usable
        # metrics — saves two rows of "—" clutter for error / single-cam
        # sessions. Mirror of the JS-tick guard in renderEvents().
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
        # SSR renders the checkbox placeholder unchecked (localStorage is
        # browser-side); the first tickEvents round-trip rehydrates the
        # persisted selection within ~one paint. Column width matches the
        # JS-rendered variant so there's no layout shift on rehydrate.
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
        parts.append(
            # event-row is a link into the viewer; the delete form + traj
            # toggle are siblings (not descendants) so their clicks don't
            # navigate via the wrapping anchor.
            f'<div class="event-item">'
            f"{toggle_html}"
            f'<a class="event-row" href="/viewer/{sid}">'
            f'<div class="event-top">'
            f'<span class="sid">{sid}</span>'
            f'<span class="chip {cam_mode}">{cam_mode}</span>'
            f'<span class="chip {status}">{stat_label}</span>'
            f'<span class="chip {capture_mode}">{capture_mode}</span>'
            f"</div>"
            f"{stats_html}"
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
    def _count_cls(n: int) -> str:
        return "full" if n >= 2 else ("partial" if n >= 1 else "partial")
    dev_cls = _count_cls(len(devices))
    cal_cls = _count_cls(len(calibrations))
    return (
        f'<span class="pair"><span class="label">Devices</span><span class="val {dev_cls}">{len(devices)}/2</span></span>'
        f'<span class="pair"><span class="label">Calibrated</span><span class="val {cal_cls}">{len(calibrations)}/2</span></span>'
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
    # Dashboard tweaks vs viewer defaults:
    #  - title=None: corner pill + nav already say what this is
    #  - fixed bbox + manual aspect ratio: with aspectmode="data" a single
    #    3m-distant camera blows up the bounding box and shrinks the
    #    50 cm plate to a dot. Pinning ±3.5 m XY / 2 m Z to the rig
    #    geometry keeps the plate readable whether 0, 1, or 2 cams are
    #    calibrated. Viewer leaves "data" so the ball trajectory still
    #    fits naturally.
    fig.update_layout(
        title=None, margin=dict(l=0, r=0, t=8, b=0),
        scene_xaxis_range=[-3.5, 3.5],
        scene_yaxis_range=[-3.5, 3.5],
        scene_zaxis_range=[-0.2, 2.0],
        scene_aspectmode="manual",
        scene_aspectratio=dict(x=1.0, y=1.0, z=0.45),
    )
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
