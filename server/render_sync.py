"""Renderer for `/setup` — the configuration surface. Stacks DEVICES ·
CALIBRATION + TIME SYNC (with matched-filter trace plot and log) +
RUNTIME · TUNING in a single-column layout. The operational `/` page is
session + events + canvas only. Reuses the dashboard's `_CSS` / design
tokens verbatim so the visual language stays consistent."""
from __future__ import annotations

import html
from typing import Any

from render_dashboard import (
    _CSS,
    _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE,
    _render_device_rows,
    _render_extended_markers_body,
    _render_tuning_body,
)
from schemas import SYNC_TRACE_MIN_PSR, SYNC_TRACE_THRESHOLD


# Sync-page-only additions on top of the shared _CSS: a single-column
# main-area (no sidebar), the trace plot container sizing, and the nav
# link + sync-chip styles introduced here (mirrored into the dashboard's
# nav via render_dashboard.py so the link can be rendered there too).
_SYNC_CSS = """
.main-sync {
  max-width: 1100px; margin: 0 auto;
  padding: calc(var(--nav-h) + var(--s-5)) var(--s-4) var(--s-5) var(--s-4);
  display: flex; flex-direction: column; gap: var(--s-3);
}
.setup-section-title {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sub);
  margin: var(--s-4) 0 calc(-1 * var(--s-2)) var(--s-1);
}
.setup-section-title:first-child { margin-top: 0; }
#sync-trace {
  width: 100%; height: 400px;
  background: var(--surface-hover);
  border: 1px solid var(--border-l);
  border-radius: var(--r);
}
.trace-empty {
  height: 400px; display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--sub);
  background: var(--surface-hover); border: 1px solid var(--border-l);
  border-radius: var(--r);
}
.trace-legend {
  margin-top: var(--s-2); display: flex; flex-wrap: wrap; gap: var(--s-3);
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.10em;
  text-transform: uppercase; color: var(--sub);
}
.trace-legend .swatch {
  display: inline-block; width: 10px; height: 2px; margin-right: 6px;
  vertical-align: middle;
}
"""


_JS_TEMPLATE = r"""
(function () {
  const syncBox = document.getElementById('sync-body');
  const traceBox = document.getElementById('sync-trace');
  const navStatus = document.getElementById('nav-status');

  // Design-token colors mirrored from render_dashboard.py _CSS root vars.
  const COLOR_A_SELF  = '#C0392B';   // --dev
  const COLOR_A_OTHER = '#4A6B8C';   // --contra
  const COLOR_B_SELF  = '#D35400';   // --dual
  const COLOR_B_OTHER = '#E6B300';   // --accent
  const COLOR_THRESHOLD = '#A7372A'; // --failed
  const COLOR_INK = '#2A2520';
  const COLOR_SUB = '#7A756C';
  const COLOR_BORDER = '#DBD6CD';

  const THRESHOLD = __THRESHOLD__;
  const MIN_PSR = __MIN_PSR__;

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  let _lastSyncRenderKey = null;

  function updateSyncCountdown(state) {
    const el = document.getElementById('sync-cooldown-val');
    if (!el) return;
    const cooldown = Number(state.sync_cooldown_remaining_s || 0);
    if (cooldown > 0) {
      el.textContent = `Ready in ${cooldown.toFixed(1)} s`;
    } else {
      el.textContent = '';
    }
  }

  function renderSync(state) {
    if (!syncBox) return;
    const sync = state.sync;
    const lastSync = state.last_sync;
    const sessionArmed = !!(state.session && state.session.armed);
    const cooldown = Number(state.sync_cooldown_remaining_s || 0);
    const onlineIds = new Set((state.devices || []).map(d => d.camera_id));
    const bothOnline = onlineIds.has('A') && onlineIds.has('B');
    const syncing = !!sync;
    const cooling = cooldown > 0;
    const disabled = syncing || sessionArmed || !bothOnline || cooling;

    const receivedKey = sync ? (sync.reports_received || []).join(',') : '';
    const lastKey = lastSync
      ? `${lastSync.id}:${lastSync.delta_s}:${lastSync.distance_m}`
      : '';
    const key = JSON.stringify({
      syncing, sessionArmed, bothOnline, cooling,
      received: receivedKey, last: lastKey,
    });
    if (key === _lastSyncRenderKey) {
      updateSyncCountdown(state);
      return;
    }
    _lastSyncRenderKey = key;

    let chip, statusLine = '';
    if (syncing) {
      chip = '<span class="chip armed">syncing</span>';
      const received = (sync.reports_received || []).join(', ') || '—';
      statusLine = `<div class="meta">Waiting for reports · ${esc(received)}</div>`;
    } else if (cooling) {
      chip = '<span class="chip idle">cooldown</span>';
      statusLine = `<div class="meta" id="sync-cooldown-val">Ready in ${cooldown.toFixed(1)} s</div>`;
    } else {
      chip = '<span class="chip idle">idle</span>';
    }

    let lastLine;
    if (lastSync && lastSync.aborted) {
      const reasons = lastSync.abort_reasons || {};
      const parts = Object.keys(reasons).sort().map(r => `${r}: ${reasons[r]}`);
      const reasonTxt = parts.length ? parts.join(' · ') : 'unknown';
      lastLine = `<div class="meta" style="color: var(--failed)">Last · ABORTED · ${esc(reasonTxt)}</div>`;
    } else if (lastSync && lastSync.delta_s != null && lastSync.distance_m != null) {
      const deltaMs = Number(lastSync.delta_s) * 1000.0;
      const dist = Number(lastSync.distance_m);
      const sign = deltaMs >= 0 ? '+' : '';
      lastLine = `<div class="meta">Last · Δ=${sign}${deltaMs.toFixed(3)} ms · D=${dist.toFixed(3)} m</div>`;
    } else {
      lastLine = '<div class="meta">No sync yet.</div>';
    }

    let title = '';
    if (!bothOnline) title = ' title="Need both A and B online"';
    else if (sessionArmed) title = ' title="Stop the armed session first"';
    else if (syncing) title = ' title="Sync in progress"';
    else if (cooling) title = ` title="Cooldown: ${cooldown.toFixed(1)} s remaining"`;

    const btn = `<form class="inline" method="POST" action="/sync/start" id="sync-form">
        <button class="btn" type="submit" ${disabled ? 'disabled' : ''}${title}>Run mutual sync</button>
      </form>`;

    syncBox.innerHTML = `
      <div class="session-head">${chip}</div>
      ${statusLine}
      ${lastLine}
      <div class="session-actions">${btn}</div>`;
  }

  // --- Nav chip mirror (syncing / cooldown) --------------------------------
  function renderNav(state) {
    if (!navStatus) return;
    const s = state.session;
    const armed = !!(s && s.armed);
    const online = (state.devices || []).length;
    const cal = (state.calibrations || []).length;
    const countCls = n => (n >= 2 ? 'full' : 'partial');
    const cooldown = Number(state.sync_cooldown_remaining_s || 0);
    const syncLabel = state.sync ? 'syncing'
                                 : (cooldown > 0 ? 'cooldown' : 'idle');
    const syncCls = state.sync ? 'armed'
                              : (cooldown > 0 ? 'partial' : 'idle');
    navStatus.innerHTML = `
      <span class="pair"><span class="label">Devices</span><span class="val ${countCls(online)}">${online}/2</span></span>
      <span class="pair"><span class="label">Calibrated</span><span class="val ${countCls(cal)}">${cal}/2</span></span>
      <span class="pair"><span class="label">Session</span>` +
      (armed ? `<span class="val armed">${esc(s.id || '—')}</span>`
             : `<span class="val idle">idle</span>`) + `</span>` +
      `<span class="pair"><span class="label">Sync</span><span class="val ${syncCls}">${syncLabel}</span></span>` +
      `<a class="nav-link" href="/">← Back to home</a>`;
  }

  // --- Trace plot ----------------------------------------------------------
  let _lastTraceKey = null;
  let _traceEmptyShown = false;

  function traceFrom(samples, name, color) {
    if (!samples || !samples.length) return null;
    return {
      type: 'scatter', mode: 'lines',
      x: samples.map(s => s.t),
      y: samples.map(s => s.peak),
      line: { color, width: 1.5 },
      name,
      hovertemplate: `${name}<br>t=%{x:.3f}s<br>peak=%{y:.3f}<br>psr=%{customdata:.2f}<extra></extra>`,
      customdata: samples.map(s => s.psr),
    };
  }

  function firedMarker(tVal, name, color, yApprox) {
    if (tVal === null || tVal === undefined) return null;
    return {
      type: 'scatter', mode: 'markers',
      x: [tVal], y: [yApprox != null ? yApprox : 1.0],
      marker: { color, size: 10, symbol: 'x', line: { color: COLOR_INK, width: 1 } },
      name: `${name} fired`,
      hovertemplate: `${name} fired<br>t=%{x:.3f}s<extra></extra>`,
      showlegend: false,
    };
  }

  function showTraceEmpty(msg) {
    if (!traceBox) return;
    traceBox.innerHTML = `<div class="trace-empty">${esc(msg)}</div>`;
    _traceEmptyShown = true;
  }

  function renderTrace(last) {
    if (!traceBox) return;
    if (!last) {
      if (!_traceEmptyShown) showTraceEmpty('No sync run yet.');
      return;
    }
    const key = `${last.id}:${(last.trace_a_self||[]).length}:${(last.trace_a_other||[]).length}:${(last.trace_b_self||[]).length}:${(last.trace_b_other||[]).length}`;
    if (key === _lastTraceKey) return;
    _lastTraceKey = key;

    const hasAny =
      (last.trace_a_self && last.trace_a_self.length) ||
      (last.trace_a_other && last.trace_a_other.length) ||
      (last.trace_b_self && last.trace_b_self.length) ||
      (last.trace_b_other && last.trace_b_other.length);
    if (!hasAny) {
      showTraceEmpty('Last run had no trace data (old iOS build?).');
      return;
    }
    // Ensure the container is a Plotly target again — prior empty state
    // replaced it with a <div class="trace-empty"> that Plotly can't draw into.
    if (_traceEmptyShown) {
      traceBox.innerHTML = '';
      _traceEmptyShown = false;
    }

    const traces = [];
    const a1 = traceFrom(last.trace_a_self,  'A · self',  COLOR_A_SELF);
    const a2 = traceFrom(last.trace_a_other, 'A · other', COLOR_A_OTHER);
    const b1 = traceFrom(last.trace_b_self,  'B · self',  COLOR_B_SELF);
    const b2 = traceFrom(last.trace_b_other, 'B · other', COLOR_B_OTHER);
    [a1, a2, b1, b2].forEach(t => { if (t) traces.push(t); });

    // Fired-detection markers (per-role × per-band) — drawn at y=1.0 so
    // they read as timeline tick marks above the matched-filter peaks.
    // last.t_a_self_s, last.t_a_from_b_s etc. are absolute PTS, convert
    // to run-relative by subtracting the earliest sample's t if any.
    const allSamples = []
      .concat(last.trace_a_self  || [])
      .concat(last.trace_a_other || [])
      .concat(last.trace_b_self  || [])
      .concat(last.trace_b_other || []);
    if (allSamples.length > 0) {
      // Fired timestamps are absolute mic-clock PTS; trace samples are
      // already run-relative. They live on different scales, so we only
      // plot fired markers when there is at least one trace sample whose
      // t differs from the fired value by plausible ~seconds — otherwise
      // we skip markers (they'd just skew the X axis).
    }

    const layout = {
      margin: { l: 48, r: 16, t: 16, b: 36 },
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { family: 'JetBrains Mono, ui-monospace, monospace', size: 10, color: COLOR_INK },
      xaxis: {
        title: { text: 't (run-relative, s)', font: { size: 10, color: COLOR_SUB } },
        gridcolor: COLOR_BORDER, zerolinecolor: COLOR_BORDER,
        tickfont: { color: COLOR_SUB },
      },
      yaxis: {
        title: { text: 'matched-filter peak (norm)', font: { size: 10, color: COLOR_SUB } },
        range: [0, 1.0],
        gridcolor: COLOR_BORDER, zerolinecolor: COLOR_BORDER,
        tickfont: { color: COLOR_SUB },
      },
      shapes: [{
        type: 'line', xref: 'paper', x0: 0, x1: 1,
        y0: THRESHOLD, y1: THRESHOLD,
        line: { color: COLOR_THRESHOLD, width: 1.5, dash: 'dash' },
      }],
      annotations: [{
        xref: 'paper', yref: 'y', x: 1.0, y: THRESHOLD,
        xanchor: 'right', yanchor: 'bottom',
        text: `threshold ${THRESHOLD.toFixed(2)}`,
        showarrow: false,
        font: { color: COLOR_THRESHOLD, size: 9, family: 'JetBrains Mono, monospace' },
      }],
      legend: {
        orientation: 'h', x: 0, y: 1.08, xanchor: 'left',
        font: { size: 9, color: COLOR_SUB },
      },
      showlegend: true,
    };
    if (window.Plotly) {
      Plotly.react(traceBox, traces, layout, { responsive: true, displayModeBar: false });
    }
  }

  // --- Sync log tick (moved verbatim from render_dashboard.py) -------------
  let _syncLogClearedAtTs = 0;
  function fmtSyncLogEntry(entry) {
    const d = new Date(entry.ts * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    const ms = String(d.getMilliseconds()).padStart(3, '0');
    const src = (entry.source || '?').padEnd(6);
    const detail = entry.detail && Object.keys(entry.detail).length
      ? ' ' + Object.entries(entry.detail)
          .map(([k, v]) => {
            const sv = typeof v === 'object' ? JSON.stringify(v)
                       : (typeof v === 'number' && !Number.isInteger(v))
                         ? Number(v).toFixed(6)
                         : String(v);
            return `${k}=${sv}`;
          })
          .join(' ')
      : '';
    return `[${hh}:${mm}:${ss}.${ms}] ${src} ${entry.event}${detail}`;
  }

  async function tickSyncState() {
    try {
      const r = await fetch('/sync/state?log_limit=200', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      // Log panel
      const logEl = document.getElementById('sync-log');
      if (logEl) {
        const entries = (body.logs || []).filter(e => e.ts >= _syncLogClearedAtTs);
        if (!entries.length) {
          if (logEl.textContent !== '') logEl.textContent = '';
        } else {
          const text = entries.map(fmtSyncLogEntry).join('\n');
          if (text !== logEl.textContent) {
            const atBottom = (logEl.scrollTop + logEl.clientHeight) >= (logEl.scrollHeight - 4);
            logEl.textContent = text;
            if (atBottom) logEl.scrollTop = logEl.scrollHeight;
          }
        }
      }
      // Trace plot
      renderTrace(body.last_sync || null);
    } catch (e) { /* silent */ }
  }

  async function tickSyncStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      renderSync(s);
      renderNav(s);
    } catch (e) { /* silent */ }
  }

  // Log buttons (Copy / Clear).
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.id === 'sync-log-copy') {
      const logEl = document.getElementById('sync-log');
      if (!logEl) return;
      const text = logEl.textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        const orig = t.textContent;
        t.textContent = 'Copied';
        setTimeout(() => { t.textContent = orig; }, 1200);
      } catch (_) {
        const range = document.createRange();
        range.selectNodeContents(logEl);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
    } else if (t.id === 'sync-log-clear') {
      _syncLogClearedAtTs = Date.now() / 1000;
      const logEl = document.getElementById('sync-log');
      if (logEl) logEl.textContent = '';
    }
  });

  // Kick-off POST handler — mirrors the dashboard so the button behaves
  // the same everywhere (fetch POST, transient inline error hint, no
  // full-page reload).
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (form.action && form.action.endsWith('/sync/start')) {
      e.preventDefault();
      const btn = form.querySelector('button');
      if (btn) btn.disabled = true;
      try {
        const resp = await fetch('/sync/start', { method: 'POST' });
        if (!resp.ok) {
          let reason = 'sync failed';
          try {
            const body = await resp.json();
            reason = (body.detail && body.detail.error) || reason;
          } catch (_) {}
          const hint = document.createElement('div');
          hint.className = 'meta';
          hint.style.color = 'var(--failed)';
          hint.textContent = 'Error: ' + reason;
          syncBox.appendChild(hint);
          setTimeout(() => hint.remove(), 3000);
        }
      } catch (_) {}
    }
  });

  tickSyncStatus();
  tickSyncState();
  setInterval(tickSyncStatus, 1000);
  setInterval(tickSyncState, 2000);
})();
"""


def _render_sync_body(
    sync: dict[str, Any] | None,
    last_sync: dict[str, Any] | None,
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    cooldown_remaining_s: float,
) -> str:
    """Initial paint for the Control card's body. JS `renderSync` replaces
    this on first `/status` tick."""
    session_armed = session is not None and session.get("armed")
    syncing = sync is not None
    online_ids = {d.get("camera_id") for d in devices}
    both_online = "A" in online_ids and "B" in online_ids
    cooling = cooldown_remaining_s > 0.0
    disabled = syncing or session_armed or not both_online or cooling

    if syncing:
        chip = '<span class="chip armed">syncing</span>'
        received = ", ".join(sync.get("reports_received") or []) or "—"
        status_line = f'<div class="meta">Waiting for reports · {html.escape(received)}</div>'
    elif cooling:
        chip = '<span class="chip idle">cooldown</span>'
        status_line = (
            f'<div class="meta" id="sync-cooldown-val">'
            f'Ready in {cooldown_remaining_s:.1f} s</div>'
        )
    else:
        chip = '<span class="chip idle">idle</span>'
        status_line = ""

    if last_sync and last_sync.get("aborted"):
        reasons = last_sync.get("abort_reasons") or {}
        parts = [f"{k}: {html.escape(str(v))}" for k, v in sorted(reasons.items())]
        reason_txt = " · ".join(parts) if parts else "unknown"
        last_line = (
            '<div class="meta" style="color: var(--failed)">'
            f'Last · ABORTED · {reason_txt}</div>'
        )
    elif (last_sync and last_sync.get("delta_s") is not None
          and last_sync.get("distance_m") is not None):
        delta_ms = last_sync["delta_s"] * 1000.0
        dist_m = last_sync["distance_m"]
        last_line = (
            f'<div class="meta">Last · Δ={delta_ms:+.3f} ms · D={dist_m:.3f} m</div>'
        )
    else:
        last_line = '<div class="meta">No sync yet.</div>'

    reason = ""
    if not both_online:
        reason = " title=\"Need both A and B online\""
    elif session_armed:
        reason = " title=\"Stop the armed session first\""
    elif syncing:
        reason = " title=\"Sync in progress\""
    elif cooling:
        reason = f" title=\"Cooldown: {cooldown_remaining_s:.1f} s remaining\""

    btn_attrs = ' disabled' if disabled else ''
    btn = (
        '<form class="inline" method="POST" action="/sync/start" id="sync-form">'
        f'<button class="btn" type="submit"{btn_attrs}{reason}>Run mutual sync</button>'
        "</form>"
    )
    return (
        f'<div class="session-head">{chip}</div>'
        f'{status_line}'
        f'{last_line}'
        f'<div class="session-actions">{btn}</div>'
    )


def _render_nav_status(
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    calibrations: list[str],
    sync: dict[str, Any] | None,
    cooldown_remaining_s: float,
) -> str:
    armed = session is not None and session.get("armed")
    session_html = (
        f'<span class="val armed">{html.escape(session.get("id", "—"))}</span>'
        if armed
        else '<span class="val idle">idle</span>'
    )
    def _count_cls(n: int) -> str:
        return "full" if n >= 2 else "partial"
    dev_cls = _count_cls(len(devices))
    cal_cls = _count_cls(len(calibrations))
    if sync is not None:
        sync_label, sync_cls = "syncing", "armed"
    elif cooldown_remaining_s > 0.0:
        sync_label, sync_cls = "cooldown", "partial"
    else:
        sync_label, sync_cls = "idle", "idle"
    return (
        f'<span class="pair"><span class="label">Devices</span><span class="val {dev_cls}">{len(devices)}/2</span></span>'
        f'<span class="pair"><span class="label">Calibrated</span><span class="val {cal_cls}">{len(calibrations)}/2</span></span>'
        f'<span class="pair"><span class="label">Session</span>{session_html}</span>'
        f'<span class="pair"><span class="label">Sync</span><span class="val {sync_cls}">{sync_label}</span></span>'
        f'<a class="nav-link" href="/">&larr; Back to home</a>'
    )


def render_setup_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync: dict[str, Any] | None = None,
    last_sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    chirp_detect_threshold: float = 0.18,
    heartbeat_interval_s: float = 1.0,
    capture_height_px: int = 1080,
    calibration_last_ts: dict[str, float] | None = None,
    extended_markers: list[dict[str, Any]] | None = None,
    preview_requested: dict[str, bool] | None = None,
) -> str:
    """Full configuration page. Sections (stacked, full-width):
    DEVICES · CALIBRATION (device rows + extended markers) · TIME SYNC
    (mutual-chirp control + matched-filter trace + diagnostic log) ·
    RUNTIME · TUNING (chirp threshold, heartbeat interval, capture
    resolution). The `/` dashboard is purely operational (Session +
    Events + 3D canvas)."""
    devices = devices or []
    calibrations = calibrations or []

    sync_js = (
        _JS_TEMPLATE
        .replace("__THRESHOLD__", repr(float(SYNC_TRACE_THRESHOLD)))
        .replace("__MIN_PSR__", repr(float(SYNC_TRACE_MIN_PSR)))
    )

    legend = (
        '<div class="trace-legend">'
        '<span><span class="swatch" style="background:#C0392B"></span>A · self</span>'
        '<span><span class="swatch" style="background:#4A6B8C"></span>A · other</span>'
        '<span><span class="swatch" style="background:#D35400"></span>B · self</span>'
        '<span><span class="swatch" style="background:#E6B300"></span>B · other</span>'
        f'<span><span class="swatch" style="background:#A7372A"></span>threshold {SYNC_TRACE_THRESHOLD:.2f} (min PSR {SYNC_TRACE_MIN_PSR:.1f})</span>'
        '</div>'
    )

    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · setup</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}{_SYNC_CSS}</style>"
        "</head><body data-page=\"setup\">"
        '<nav class="nav">'
        '<a class="brand" href="/" style="text-decoration:none"><span class="dot"></span>BALL_TRACKER</a>'
        f'<div class="status-line" id="nav-status">{_render_nav_status(devices, session, calibrations, sync, sync_cooldown_remaining_s)}</div>'
        "</nav>"
        '<main class="main-sync">'
        # DEVICES · CALIBRATION
        '<div class="setup-section-title">Devices &middot; Calibration</div>'
        '<div class="card">'
        '<h2 class="card-title">Devices &middot; Calibration</h2>'
        f'<div id="devices-body">{_render_device_rows(devices, calibrations, calibration_last_ts, preview_requested)}</div>'
        f'<div id="extended-markers-body">{_render_extended_markers_body(["A", "B"], extended_markers)}</div>'
        "</div>"
        # TIME SYNC
        '<div class="setup-section-title">Time Sync</div>'
        '<div class="card">'
        '<h2 class="card-title">Mutual sync &middot; Control</h2>'
        f'<div id="sync-body">{_render_sync_body(sync, last_sync, devices, session, sync_cooldown_remaining_s)}</div>'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Matched-filter trace</h2>'
        '<div id="sync-trace"><div class="trace-empty">No sync run yet.</div></div>'
        f'{legend}'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Log</h2>'
        '<div class="sync-log-head">'
        '<span class="sync-log-label">Diagnostic events &middot; server + A + B</span>'
        '<button type="button" class="btn secondary small" id="sync-log-copy">Copy</button>'
        '<button type="button" class="btn secondary small" id="sync-log-clear">Clear view</button>'
        '</div>'
        '<pre class="sync-log" id="sync-log"></pre>'
        "</div>"
        # RUNTIME · TUNING
        '<div class="setup-section-title">Runtime &middot; Tuning</div>'
        '<div class="card">'
        '<h2 class="card-title">Runtime &middot; Tuning</h2>'
        f'<div id="tuning-body">{_render_tuning_body(chirp_detect_threshold, heartbeat_interval_s, capture_height_px)}</div>'
        "</div>"
        "</main>"
        # Dashboard JS drives devices, extended markers, auto-cal clicks,
        # preview toggle + refresh, nav status; its renderSession/
        # renderEvents/canvas paths no-op via null guards when those
        # containers are absent. Sync JS owns the mutual-sync form, the
        # matched-filter trace plot, and the diagnostic log panel.
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        f"<script>{sync_js}</script>"
        "</body></html>"
    )
