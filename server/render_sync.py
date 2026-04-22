"""Renderers for `/setup` and `/sync`.

`/setup` is the geometry-only camera calibration surface.
`/sync` is the dedicated time-sync + runtime-tuning surface.
Both pages reuse the dashboard design tokens so navigation and controls
stay visually consistent across the app."""
from __future__ import annotations

import html
from typing import Any

from render_dashboard_client import _JS_TEMPLATE as _DASHBOARD_JS_TEMPLATE
from render_dashboard_devices import _render_device_rows
from render_shared import _CSS, _render_app_nav

from schemas import SYNC_TRACE_MIN_PSR, SYNC_TRACE_THRESHOLD


# Sync-page-only additions on top of the shared _CSS: a single-column
# main-area (no sidebar), the trace plot container sizing, and the nav
# link + sync-chip styles introduced here (mirrored into the dashboard's
# nav via render_dashboard.py so the link can be rendered there too).
_SYNC_CSS = """
.main-sync {
  max-width: 1100px; margin: 0 auto;
  padding: calc(var(--nav-offset) + var(--s-5)) var(--s-4) var(--s-5) var(--s-4);
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
.wav-link {
  color: var(--ink); text-decoration: underline; text-decoration-style: dotted;
  text-underline-offset: 2px; font-family: var(--mono); font-size: 11px;
}
.wav-link:hover { color: var(--accent); }
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
.qct-row .bar .peak-mark { position: absolute; top: -3px; bottom: -3px;
                           width: 2px; background: var(--passed); opacity: 0.7; }
.qct-row .value { text-align: right; font-variant-numeric: tabular-nums; }
.qct-row .value .peak { color: var(--passed); font-size: 10px;
                        margin-left: 4px; opacity: 0.85; }
.qct-head .age { margin-left: 8px; padding: 1px 6px; font-size: 10px;
                 border: 1px solid var(--border); color: var(--sub); }
.qct-head .age.live { color: var(--passed); border-color: var(--passed); }
.qct-head .age.stale { color: var(--sub); border-style: dashed; }
.qct-cam.stale .qct-row .fill { opacity: 0.5; }
.qct-cam.stale { opacity: 0.92; }
.per-cam-sync { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
                gap: var(--s-3); }
.pcs-cam { border: 1px solid var(--border); padding: var(--s-3);
           display: flex; align-items: center; gap: var(--s-3); }
.pcs-cam .led { width: 14px; height: 14px; border-radius: 50%;
                background: var(--border); flex-shrink: 0; }
.pcs-cam.synced .led { background: var(--passed);
                       box-shadow: 0 0 8px rgba(125, 255, 192, 0.5); }
.pcs-cam.offline .led { background: var(--border); }
.pcs-cam.listening .led { background: var(--warn);
                          box-shadow: 0 0 6px rgba(255, 207, 122, 0.5); }
.pcs-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pcs-head { font-family: var(--mono); font-size: 12px; letter-spacing: 0.10em;
            text-transform: uppercase; color: var(--ink); }
.pcs-meta { font-family: var(--mono); font-size: 10px; color: var(--sub);
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pcs-meta .sync-id { color: var(--ink); font-weight: 500; }
.pcs-meta .sid-chip { font-family: var(--mono); color: var(--ink);
                      background: var(--panel-alt, rgba(0,0,0,0.04));
                      padding: 1px 4px; border-radius: 3px;
                      letter-spacing: 0.08em; cursor: help; }
.pcs-meta .pair-ok { margin-left: 6px; padding: 1px 5px;
                     font-size: 9px; color: var(--passed);
                     border: 1px solid var(--passed);
                     text-transform: uppercase; letter-spacing: 0.10em; }
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
    // Phase A: every attempt persists both cams' raw WAVs to
    // data/sync_audio/. Surface a pair of download links on the last-
    // sync row so the operator (or an AI pasting Copy-AI-Debug) can
    // grab the exact recordings for offline replay + tuning.
    function wavLinks(sid) {
      if (!sid) return '';
      return ` · <a class="wav-link" href="/sync/audio/${esc(sid)}_A.wav">A.wav</a>` +
             ` · <a class="wav-link" href="/sync/audio/${esc(sid)}_B.wav">B.wav</a>`;
    }
    if (lastSync && lastSync.aborted) {
      const reasons = lastSync.abort_reasons || {};
      const parts = Object.keys(reasons).sort().map(r => `${r}: ${reasons[r]}`);
      const reasonTxt = parts.length ? parts.join(' · ') : 'unknown';
      lastLine = `<div class="meta" style="color: var(--failed)">Last · ABORTED · ${esc(reasonTxt)}${wavLinks(lastSync.id)}</div>`;
    } else if (lastSync && lastSync.delta_s != null && lastSync.distance_m != null) {
      const deltaMs = Number(lastSync.delta_s) * 1000.0;
      const dist = Number(lastSync.distance_m);
      const sign = deltaMs >= 0 ? '+' : '';
      lastLine = `<div class="meta">Last · Δ=${sign}${deltaMs.toFixed(3)} ms · D=${dist.toFixed(3)} m${wavLinks(lastSync.id)}</div>`;
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
    // Quick chirp fallback lives on the dashboard now. /sync is the
    // mutual-sync tuning surface.

    syncBox.innerHTML = `
      <div class="session-head">${chip}</div>
      ${statusLine}
      ${lastLine}
      <div class="card-subtitle">Methods</div>
      <div class="session-actions">${btn}</div>`;
  }

  // --- Nav chip mirror (syncing / cooldown) --------------------------------
  function renderNav(state) {
    if (!navStatus) return;
    const s = state.session;
    const armed = !!(s && s.armed);
    const online = (state.devices || []).length;
    const cal = (state.calibrations || []).length;
    const synced = (state.devices || []).filter(d => d && d.time_synced).length;
    const expected = 2;
    const cooldown = Number(state.sync_cooldown_remaining_s || 0);
    let badgeCls = 'ready';
    let badge = 'Ready';
    let headline = 'ready to arm';
    let context = 'all prerequisites satisfied';
    if (armed) {
      badgeCls = 'recording';
      badge = 'Recording';
      headline = esc(s.id || '—');
      context = 'session active';
    } else if (state.sync) {
      badgeCls = 'syncing';
      badge = 'Sync';
      headline = 'sync in progress';
      context = 'complete on /sync';
    } else if (online < expected) {
      badgeCls = 'blocked';
      badge = 'Blocked';
      headline = 'bring both devices online';
      context = `${online}/${expected} devices available`;
    } else if (cal < expected) {
      badgeCls = 'blocked';
      badge = 'Blocked';
      headline = 'finish calibration';
      context = `${cal}/${expected} cameras calibrated`;
    } else if (synced < expected) {
      badgeCls = 'blocked';
      badge = 'Blocked';
      headline = 'run time sync';
      context = `${synced}/${expected} cameras synced`;
    } else if (cooldown > 0) {
      badgeCls = 'cooldown';
      badge = 'Cooldown';
      headline = 'sync settling';
      context = `${cooldown.toFixed(0)}s remaining`;
    }
    const check = (label, value, ok) =>
      `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
    navStatus.innerHTML = `
      <div class="status-main">
        <span class="status-badge ${badgeCls}">${badge}</span>
        <span class="status-headline">${headline}</span>
        <span class="status-context">${context}</span>
      </div>
      <div class="status-checks">
        ${check('Devices', `${online}/${expected}`, online >= expected)}
        ${check('Cal', `${cal}/${expected}`, cal >= expected)}
        ${check('Sync', `${synced}/${expected}`, synced >= expected)}
      </div>`;
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
      renderPerCamSync(s);
    } catch (e) { /* silent */ }
  }

  // Short, operator-useful slice of a full `sy_xxxxxxxx` id — the
  // important property at a glance is "do both cams share the SAME
  // id?". Full hex is noise; last 6 chars keep the signal.
  function shortSyncId(sid) {
    if (!sid) return '';
    return sid.length > 8 ? sid.slice(-6) : sid.replace(/^sy_/, '');
  }
  function renderPerCamSync(state) {
    const host = document.getElementById('per-cam-sync');
    if (!host) return;
    const devs = state.devices || [];
    const pending = state.sync_commands || {};
    const run = state.sync || null;
    const expected = ['A', 'B'];
    const byId = new Map(devs.map(d => [d.camera_id, d]));
    // Check whether both cams ended up with the same id — the
    // operator cares about pair agreement, not absolute value.
    const syncedIds = expected
      .map(cam => byId.get(cam))
      .filter(d => d && d.time_synced && d.time_sync_id)
      .map(d => d.time_sync_id);
    const bothPaired = syncedIds.length === 2 && syncedIds[0] === syncedIds[1];
    const cards = expected.map(cam => {
      const d = byId.get(cam);
      const online = !!d;
      const synced = online && !!d.time_synced;
      const isListening = online && !!(pending[cam] || (run && !run.ended_at));
      const cls = !online ? 'offline'
                : synced  ? 'synced'
                : isListening ? 'listening'
                : 'waiting';
      const headLabel = !online ? 'offline'
                      : synced  ? 'synced'
                      : isListening ? 'listening…'
                      : 'not synced';
      const sid = d && d.time_sync_id;
      const ageS = d && typeof d.time_sync_age_s === 'number' ? d.time_sync_age_s : null;
      const ageTxt = ageS != null ? ` · ${ageS.toFixed(0)}s ago` : '';
      const idChip = (synced && sid)
        ? `<span class="sid-chip" title="${esc(sid)}">·${esc(shortSyncId(sid))}</span>`
        : '';
      const pairBadge = (synced && bothPaired) ? ' <span class="pair-ok">paired</span>' : '';
      const meta = synced
        ? `<div class="pcs-meta">${idChip}${ageTxt}${pairBadge}</div>`
        : isListening
          ? `<div class="pcs-meta">waiting for chirp…</div>`
          : online
            ? `<div class="pcs-meta">press Run mutual sync</div>`
            : `<div class="pcs-meta">device offline</div>`;
      return `<div class="pcs-cam ${cls}">
        <div class="led"></div>
        <div class="pcs-body">
          <div class="pcs-head">Cam ${esc(cam)} · ${headLabel}</div>
          ${meta}
        </div>
      </div>`;
    });
    host.innerHTML = cards.join('');
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
    } else if (t.id === 'sync-ai-debug-copy') {
      const orig = t.textContent;
      t.textContent = 'Fetching…';
      t.disabled = true;
      try {
        const r = await fetch('/sync/debug_export', { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        await navigator.clipboard.writeText(text);
        t.textContent = 'Copied!';
        setTimeout(() => { t.textContent = orig; t.disabled = false; }, 1800);
      } catch (err) {
        t.textContent = 'Error';
        t.disabled = false;
        setTimeout(() => { t.textContent = orig; }, 2000);
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
    if (form.action && form.action.endsWith('/sync/start')) {
      // Mutual sync kick-off: intercept so the 303 → / redirect the
      // server sends to HTML form callers doesn't yank us off the
      // debug page. Quick chirp auto-play lives on the dashboard;
      // mutual sync needs no auto-played audio (phones emit their own
      // chirps via MutualSyncAudio).
      e.preventDefault();
      const btn = form.querySelector('button');
      if (btn) btn.disabled = true;
      try {
        const resp = await fetch(form.action, {
          method: 'POST',
          headers: { 'Accept': 'application/json' },
        });
        if (!resp.ok) {
          let reason = 'sync failed';
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
        }
        tickSyncStatus();
        tickSyncState();
      } catch (_) {}
      finally {
        if (btn) btn.disabled = false;
      }
      return;
    }
    if (form.action && form.action.endsWith('/settings/sync_params')) {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      setTuningStatus('Applying burst params…', '');
      try {
        const parseArr = (s) => s.split(',').map(x => parseFloat(x.trim())).filter(n => !isNaN(n));
        const body = {
          emit_a_at_s: parseArr(form.querySelector('[name="emit_a_at_s"]')?.value || ''),
          emit_b_at_s: parseArr(form.querySelector('[name="emit_b_at_s"]')?.value || ''),
          record_duration_s: parseFloat(form.querySelector('[name="record_duration_s"]')?.value || '4'),
          search_window_s: parseFloat(form.querySelector('[name="search_window_s"]')?.value || '0.3'),
        };
        const resp = await fetch('/settings/sync_params', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!resp.ok) {
          let reason = 'update failed';
          try { const b = await resp.json(); reason = b.detail || reason; } catch (_) {}
          setTuningStatus('Rejected: ' + reason, 'error');
          return;
        }
        setTuningStatus('Burst params applied.', 'ok');
        setTimeout(() => { if (tuningStatus && tuningStatus.textContent === 'Burst params applied.') setTuningStatus('', ''); }, 1500);
      } catch (_) {
        setTuningStatus('Update failed.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    }
  });

  async function tickSyncParams() {
    try {
      const r = await fetch('/sync/params', { cache: 'no-store' });
      if (!r.ok) return;
      const p = await r.json();
      const fA = document.getElementById('sp-emit-a');
      const fB = document.getElementById('sp-emit-b');
      const fD = document.getElementById('sp-duration');
      const fW = document.getElementById('sp-window');
      if (fA && document.activeElement !== fA) fA.value = (p.emit_a_at_s || []).join(', ');
      if (fB && document.activeElement !== fB) fB.value = (p.emit_b_at_s || []).join(', ');
      if (fD && document.activeElement !== fD) fD.value = p.record_duration_s ?? 4.0;
      if (fW && document.activeElement !== fW) fW.value = p.search_window_s ?? 0.3;
    } catch (_) {}
  }

  tickSyncStatus();
  tickSyncState();
  tickSyncParams();
  setInterval(tickSyncStatus, 1000);
  setInterval(tickSyncState, 2000);
  setInterval(tickSyncParams, 5000);
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
    # Quick chirp lives on the dashboard now (fallback path only).
    # /sync is the mutual-sync tuning surface; only the mutual button
    # ships here.
    mutual_btn = (
        '<form class="inline" method="POST" action="/sync/start" id="sync-form">'
        f'<button class="btn" type="submit"{btn_attrs}{reason}>Run mutual sync</button>'
        "</form>"
    )
    return (
        f'<div class="session-head">{chip}</div>'
        f'{status_line}'
        f'{last_line}'
        '<div class="card-subtitle">Methods</div>'
        f'<div class="session-actions">{mutual_btn}</div>'
    )


def _render_burst_params_body(sync_params: dict[str, Any] | None) -> str:
    """Editable burst params card body. Values hydrated by JS tickSyncParams."""
    p = sync_params or {}
    emit_a = ", ".join(str(v) for v in p.get("emit_a_at_s", [0.3, 0.5, 0.7]))
    emit_b = ", ".join(str(v) for v in p.get("emit_b_at_s", [1.8, 2.0, 2.2]))
    dur = p.get("record_duration_s", 4.0)
    win = p.get("search_window_s", 0.3)
    return (
        '<form class="inline" action="/settings/sync_params" method="POST">'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-emit-a">A emit (s)</label>'
        f'<input id="sp-emit-a" name="emit_a_at_s" class="tuning-input" value="{html.escape(emit_a)}" '
        'title="Comma-separated offsets (s from recording start) at which Cam A emits its chirp burst">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-emit-b">B emit (s)</label>'
        f'<input id="sp-emit-b" name="emit_b_at_s" class="tuning-input" value="{html.escape(emit_b)}" '
        'title="Comma-separated offsets for Cam B — must not overlap A\'s window">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-duration">Record (s)</label>'
        f'<input id="sp-duration" name="record_duration_s" class="tuning-input" type="number" '
        f'min="1" max="30" step="0.5" value="{dur}" title="Total recording window; must exceed last emission + chirp + propagation">'
        '</div>'
        '<div class="tuning-row">'
        '<label class="tuning-label" for="sp-window">Search window (s)</label>'
        f'<input id="sp-window" name="search_window_s" class="tuning-input" type="number" '
        f'min="0.05" max="2.0" step="0.05" value="{win}" title="±seconds the server searches around each expected emission for a peak">'
        '</div>'
        '<div class="tuning-row">'
        '<button class="btn secondary" type="submit">Apply</button>'
        '</div>'
        '</form>'
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


from render_sync_page import render_setup_html, render_sync_html
