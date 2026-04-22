"""Renderers for `/setup` and `/sync`.

`/setup` is the geometry-only camera calibration surface.
`/sync` is the dedicated time-sync + runtime-tuning surface.
Both pages reuse the dashboard design tokens so navigation and controls
stay visually consistent across the app."""
from __future__ import annotations

import html
from typing import Any

from render_dashboard import (
    _CSS,
    _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE,
    _render_app_nav,
    _render_chirp_threshold_body,
    _render_device_rows,
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
.page-hero {
  display: flex; align-items: end; justify-content: space-between; gap: var(--s-3);
  flex-wrap: wrap;
}
.page-hero-copy { display: flex; flex-direction: column; gap: 8px; max-width: 720px; }
.page-kicker {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sub);
}
.page-title {
  font-family: var(--mono); font-size: 28px; line-height: 1.05; letter-spacing: 0.02em;
  color: var(--ink); margin: 0;
}
.page-copy { color: var(--ink-light); max-width: 720px; }
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
.main-sync .telemetry-panel {
  position: static;
  left: auto;
  right: auto;
  top: auto;
  bottom: auto;
  z-index: auto;
  max-width: none;
}
.main-sync .telemetry-body {
  max-height: 360px;
}
.main-sync .camera-compare .preview-panel .placeholder {
  display: none;
}
.tuning-status {
  min-height: 18px;
  margin-bottom: var(--s-2);
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--sub);
}
.tuning-status.ok { color: var(--passed); }
.tuning-status.error { color: var(--failed); }
.quick-chirp-telemetry { display: grid; gap: var(--s-3); }
.qct-cam { border: 1px solid var(--border); padding: var(--s-3); }
.qct-head { display: flex; align-items: center; justify-content: space-between;
            font-family: var(--mono); font-size: 11px; letter-spacing: 0.10em;
            text-transform: uppercase; margin-bottom: var(--s-2); }
.qct-head .clip-chip { padding: 2px 8px; border: 1px solid var(--border);
                       font-size: 10px; color: var(--sub); }
.qct-head .clip-chip.warn { color: var(--failed); border-color: var(--failed); }
.qct-head .clip-chip.ok { color: var(--passed); border-color: var(--passed); }
.qct-row { display: grid; grid-template-columns: 100px 1fr 80px;
           align-items: center; gap: var(--s-2); margin-bottom: var(--s-1);
           font-family: var(--mono); font-size: 11px; }
.qct-row .label { color: var(--sub); letter-spacing: 0.08em;
                  text-transform: uppercase; }
.qct-row .bar { position: relative; height: 10px; background: var(--border); }
.qct-row .bar .fill { position: absolute; left: 0; top: 0; bottom: 0;
                      background: var(--ink); }
.qct-row .bar .fill.warn { background: var(--failed); }
.qct-row .bar .thr-mark { position: absolute; top: -2px; bottom: -2px;
                          width: 2px; background: var(--failed); }
.qct-row .value { text-align: right; font-variant-numeric: tabular-nums; }
"""


_JS_TEMPLATE = r"""
(function () {
  const syncBox = document.getElementById('sync-body');
  const traceBox = document.getElementById('sync-trace');
  const navStatus = document.getElementById('nav-status');
  const tuningStatus = document.getElementById('tuning-status');

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

  function setTuningStatus(msg, cls) {
    if (!tuningStatus) return;
    tuningStatus.className = 'tuning-status' + (cls ? (' ' + cls) : '');
    tuningStatus.textContent = msg || '';
  }

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
    // Quick chirp is a separate, legacy single-listener path — third
    // device plays the up+down chirp and whichever phone hears it
    // anchors its clock. Only gated on armed session; independent of
    // the mutual-sync run state.
    const quickDisabled = sessionArmed;
    const quickTitle = sessionArmed ? ' title="Stop the armed session first"' : '';
    const quickBtn = `<form class="inline" method="POST" action="/sync/trigger" id="sync-trigger-form">
        <button class="btn secondary" type="submit" ${quickDisabled ? 'disabled' : ''}${quickTitle}>Quick chirp</button>
      </form>`;

    syncBox.innerHTML = `
      <div class="session-head">${chip}</div>
      ${statusLine}
      ${lastLine}
      <div class="card-subtitle">Methods</div>
      <div class="session-actions">${quickBtn}${btn}</div>`;
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
      `<span class="pair"><span class="label">Sync</span><span class="val ${syncCls}">${syncLabel}</span></span>`;
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
      // Quick-chirp live telemetry
      renderQuickChirpTelemetry(body.telemetry || {});
    } catch (e) { /* silent */ }
  }

  function renderQuickChirpTelemetry(telem) {
    const host = document.getElementById('quick-chirp-telemetry');
    if (!host) return;
    const cams = Object.keys(telem).sort();
    if (!cams.length) {
      host.innerHTML = '<div class="trace-empty">No phone listening.</div>';
      return;
    }
    function bar(valueRaw, maxVal, threshold, warnAt) {
      const value = Number(valueRaw || 0);
      const pct = Math.min(100, Math.max(0, (value / maxVal) * 100));
      const warn = warnAt != null && value >= warnAt;
      const thrPct = threshold != null
        ? Math.min(100, Math.max(0, (threshold / maxVal) * 100))
        : null;
      const thrMark = thrPct != null
        ? `<div class="thr-mark" style="left:${thrPct.toFixed(1)}%"></div>`
        : '';
      return `<div class="bar"><div class="fill${warn ? ' warn' : ''}" style="width:${pct.toFixed(1)}%"></div>${thrMark}</div>`;
    }
    function row(label, value, bars, fmt) {
      const v = typeof value === 'number' ? (fmt || ((x) => x.toFixed(3)))(value) : '—';
      return `<div class="qct-row"><span class="label">${label}</span>${bars}<span class="value">${v}</span></div>`;
    }
    const parts = cams.map(cam => {
      const t = telem[cam] || {};
      const peak = Number(t.input_peak || 0);
      const clipping = peak >= 0.98;
      const clipClass = clipping ? 'warn' : (peak >= 0.7 ? '' : 'ok');
      const clipText = clipping ? 'CLIPPING' : (peak >= 0.7 ? 'hot' : 'headroom ok');
      const thr = Number(t.threshold || 0);
      const up = Number(t.up_peak || 0);
      const down = Number(t.down_peak || 0);
      const upFloor = Number(t.cfar_up_floor || 0);
      const downFloor = Number(t.cfar_down_floor || 0);
      return `<div class="qct-cam">
        <div class="qct-head"><span>Cam ${cam} · ${t.armed ? 'armed' : 'cooldown'}${t.pending_up ? ' · pending up' : ''}</span>
          <span class="clip-chip ${clipClass}">${clipText}</span></div>
        ${row('input rms', t.input_rms, bar(t.input_rms, 0.5, null, null))}
        ${row('input peak', t.input_peak, bar(t.input_peak, 1.0, null, 0.98))}
        ${row('up peak', up, bar(up, 1.0, thr, null))}
        ${row('down peak', down, bar(down, 1.0, thr, null))}
        ${row('cfar up', upFloor, bar(upFloor, 0.2, null, null))}
        ${row('cfar dn', downFloor, bar(downFloor, 0.2, null, null))}
      </div>`;
    });
    host.innerHTML = parts.join('');
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
    if (!(form instanceof HTMLFormElement)) return;
    if (form.action && (form.action.endsWith('/sync/start') || form.action.endsWith('/sync/trigger'))) {
      // Both kick-off paths: intercept so the 303 → / redirect the server
      // sends to HTML form callers doesn't yank us off the debug page.
      e.preventDefault();
      const btn = form.querySelector('button');
      if (btn) btn.disabled = true;
      const isQuick = form.action.endsWith('/sync/trigger');
      // Pre-build the Audio element INSIDE the click handler so the
      // browser's autoplay policy counts this as a user gesture. If we
      // did `new Audio().play()` inside setTimeout later, Safari/Chrome
      // would reject the call as non-gesture and the chirp wouldn't play.
      const chirpAudio = isQuick ? new Audio('/chirp.wav') : null;
      try {
        const resp = await fetch(form.action, {
          method: 'POST',
          headers: { 'Accept': 'application/json' },
        });
        if (!resp.ok) {
          let reason = isQuick ? 'quick chirp failed' : 'sync failed';
          try {
            const body = await resp.json();
            reason = (body.detail && body.detail.error) || body.detail || reason;
          } catch (_) {}
          const hint = document.createElement('div');
          hint.className = 'meta';
          hint.style.color = 'var(--failed)';
          hint.textContent = 'Error: ' + reason;
          syncBox.appendChild(hint);
          setTimeout(() => hint.remove(), 3000);
        } else if (chirpAudio) {
          // Give iOS a moment to receive the WS sync_command and spin up
          // the mic detector before we start the waveform. 500 ms is safe
          // across LAN + iOS capture-session reconfiguration latency; the
          // chirp WAV has 500 ms leading silence of its own too, so the
          // effective detection window is 1 s of slack before the sweep.
          setTimeout(() => {
            chirpAudio.play().catch((err) => {
              const hint = document.createElement('div');
              hint.className = 'meta';
              hint.style.color = 'var(--failed)';
              hint.textContent = 'Browser blocked autoplay: ' + (err.message || err);
              syncBox.appendChild(hint);
              setTimeout(() => hint.remove(), 4000);
            });
          }, 500);
        }
        tickSyncStatus();
        tickSyncState();
      } catch (_) {}
      finally {
        if (btn) btn.disabled = false;
      }
      return;
    }
    const tuningActions = [
      '/settings/chirp_threshold',
      '/settings/heartbeat_interval',
      '/settings/tracking_exposure_cap',
      '/settings/capture_height',
    ];
    if (tuningActions.some(path => form.action.endsWith(path))) {
      e.preventDefault();
      const btn = form.querySelector('button');
      const field = form.querySelector('input[name="threshold"], input[name="interval_s"], input[name="mode"], input[name="height"]');
      if (btn instanceof HTMLButtonElement) btn.disabled = true;
      if (field instanceof HTMLInputElement) field.disabled = true;
      setTuningStatus('Applying runtime tuning…', '');
      try {
        const resp = await fetch(form.action, { method: 'POST', body: new FormData(form) });
        if (!resp.ok) {
          let reason = 'update failed';
          try {
            const body = await resp.json();
            reason = body.detail || reason;
          } catch (_) {}
          setTuningStatus('Runtime tuning rejected: ' + reason, 'error');
          return;
        }
        setTuningStatus('Runtime tuning applied.', 'ok');
        setTimeout(() => {
          if (tuningStatus && tuningStatus.textContent === 'Runtime tuning applied.') {
            setTuningStatus('', '');
          }
        }, 1500);
        tickSyncStatus();
      } catch (_) {
        setTuningStatus('Runtime tuning update failed.', 'error');
      } finally {
        if (btn instanceof HTMLButtonElement) btn.disabled = false;
        if (field instanceof HTMLInputElement) field.disabled = false;
      }
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
    mutual_btn = (
        '<form class="inline" method="POST" action="/sync/start" id="sync-form">'
        f'<button class="btn" type="submit"{btn_attrs}{reason}>Run mutual sync</button>'
        "</form>"
    )
    quick_btn = (
        '<form class="inline" method="POST" action="/sync/trigger" id="sync-trigger-form">'
        f'<button class="btn secondary" type="submit"{" disabled" if session_armed else ""}>Quick chirp</button>'
        "</form>"
    )
    return (
        f'<div class="session-head">{chip}</div>'
        f'{status_line}'
        f'{last_line}'
        '<div class="card-subtitle">Methods</div>'
        f'<div class="session-actions">{quick_btn}{mutual_btn}</div>'
    )


def _render_sync_legend() -> str:
    return (
        '<div class="trace-legend">'
        '<span><span class="swatch" style="background:#C0392B"></span>A · self</span>'
        '<span><span class="swatch" style="background:#4A6B8C"></span>A · other</span>'
        '<span><span class="swatch" style="background:#D35400"></span>B · self</span>'
        '<span><span class="swatch" style="background:#E6B300"></span>B · other</span>'
        f'<span><span class="swatch" style="background:#A7372A"></span>threshold {SYNC_TRACE_THRESHOLD:.2f} (min PSR {SYNC_TRACE_MIN_PSR:.1f})</span>'
        '</div>'
    )


def render_setup_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    calibration_last_ts: dict[str, float] | None = None,
    markers_count: int = 0,
    preview_requested: dict[str, bool] | None = None,
) -> str:
    """Pure calibration surface. `/setup` keeps only camera positioning /
    plate reprojection tasks; time-sync and runtime tuning live on `/sync`."""
    devices = devices or []
    calibrations = calibrations or []

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
        f'{_render_app_nav("setup", devices, session, calibrations, None, sync_cooldown_remaining_s)}'
        '<main class="main-sync">'
        '<section class="card page-hero">'
        '<div class="page-hero-copy">'
        '<div class="page-kicker">Calibration workflow</div>'
        '<h1 class="page-title">Camera Position Setup</h1>'
        '</div>'
        '</section>'
        # DEVICES · CALIBRATION
        '<div class="setup-section-title">Devices &middot; Calibration</div>'
        '<div class="card">'
        '<h2 class="card-title">Devices &middot; Calibration</h2>'
        f'<div id="devices-body">{_render_device_rows(devices, calibrations, calibration_last_ts, preview_requested, compare_mode="toggle")}</div>'
        "</div>"
        "</main>"
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        "</body></html>"
    )


def render_sync_html(
    devices: list[dict[str, Any]] | None = None,
    session: dict[str, Any] | None = None,
    calibrations: list[str] | None = None,
    sync: dict[str, Any] | None = None,
    last_sync: dict[str, Any] | None = None,
    sync_cooldown_remaining_s: float = 0.0,
    chirp_detect_threshold: float = 0.18,
    heartbeat_interval_s: float = 1.0,
    capture_height_px: int = 1080,
    tracking_exposure_cap: str = "frame_duration",
) -> str:
    devices = devices or []
    calibrations = calibrations or []
    sync_js = (
        _JS_TEMPLATE
        .replace("__THRESHOLD__", repr(float(SYNC_TRACE_THRESHOLD)))
        .replace("__MIN_PSR__", repr(float(SYNC_TRACE_MIN_PSR)))
    )
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · sync</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}{_SYNC_CSS}</style>"
        "</head><body data-page=\"sync\">"
        f'{_render_app_nav("sync", devices, session, calibrations, sync, sync_cooldown_remaining_s)}'
        '<main class="main-sync">'
        '<section class="card page-hero">'
        '<div class="page-hero-copy">'
        '<div class="page-kicker">Audio workflow</div>'
        '<h1 class="page-title">Time Sync</h1>'
        '</div>'
        '</section>'
        '<div class="setup-section-title">Time Sync</div>'
        '<div class="card">'
        '<h2 class="card-title">Sync Control</h2>'
        f'<div id="sync-body">{_render_sync_body(sync, last_sync, devices, session, sync_cooldown_remaining_s)}</div>'
        f'{_render_chirp_threshold_body(chirp_detect_threshold)}'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Matched-filter trace</h2>'
        '<div id="sync-trace"><div class="trace-empty">No sync run yet.</div></div>'
        f'{_render_sync_legend()}'
        "</div>"
        '<div class="card">'
        '<h2 class="card-title">Quick-chirp telemetry &middot; live</h2>'
        '<div class="card-subtitle">While a phone is in 時間校正 listening state, its mic level + matched-filter peaks appear here. Use to tune speaker volume: input_peak &ge; 0.98 means ADC is clipping and will hurt correlation.</div>'
        '<div id="quick-chirp-telemetry" class="quick-chirp-telemetry"><div class="trace-empty">No phone listening.</div></div>'
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
        '<div class="setup-section-title">Runtime &middot; Tuning</div>'
        '<div class="card">'
        '<h2 class="card-title">Runtime &middot; Tuning</h2>'
        '<div id="tuning-status" class="tuning-status"></div>'
        f'<div id="tuning-body">{_render_tuning_body(heartbeat_interval_s, tracking_exposure_cap, capture_height_px)}</div>'
        "</div>"
        '<div class="setup-section-title">Diagnostics</div>'
        '<div class="card">'
        '<h2 class="card-title">Telemetry</h2>'
        '<details id="telemetry-panel" class="telemetry-panel">'
        '  <summary>Open diagnostics</summary>'
        '  <div id="telemetry-body" class="telemetry-body"></div>'
        '</details>'
        "</div>"
        "</main>"
        f"<script>{_DASHBOARD_JS_TEMPLATE}</script>"
        f"<script>{sync_js}</script>"
        "</body></html>"
    )
