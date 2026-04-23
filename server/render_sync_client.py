"""Sync page client-side JS template."""
from __future__ import annotations

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

  // --- Nav chip mirror --------------------------------------------------
  // Three chips only (devices / cal / sync), matching
  // render_shared.py::_render_nav_status. No editorial headline.
  function renderNav(state) {
    if (!navStatus) return;
    const online = (state.devices || []).length;
    const cal = (state.calibrations || []).length;
    const synced = (state.devices || []).filter(d => d && d.time_synced).length;
    const expected = 2;
    const check = (label, value, ok) =>
      `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
    navStatus.innerHTML = `
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
    // Bars now carry an optional `peakFrac` to render a second marker at
    // the highest value observed during the current attempt, so the
    // operator can read the peak even after the phone stops reporting.
    function bar(valueRaw, maxVal, threshold, warnAt, peakRaw) {
      const value = Number(valueRaw || 0);
      const pct = Math.min(100, Math.max(0, (value / maxVal) * 100));
      const warn = warnAt != null && value >= warnAt;
      const thrPct = threshold != null
        ? Math.min(100, Math.max(0, (threshold / maxVal) * 100))
        : null;
      const thrMark = thrPct != null
        ? `<div class="thr-mark" style="left:${thrPct.toFixed(1)}%"></div>`
        : '';
      const peakNum = peakRaw == null ? null : Number(peakRaw);
      const peakMark = (peakNum != null && peakNum > value)
        ? `<div class="peak-mark" style="left:${Math.min(100, Math.max(0, (peakNum / maxVal) * 100)).toFixed(1)}%"></div>`
        : '';
      return `<div class="bar"><div class="fill${warn ? ' warn' : ''}" style="width:${pct.toFixed(1)}%"></div>${thrMark}${peakMark}</div>`;
    }
    function row(label, value, peak, bars) {
      const vFmt = (x) => (typeof x === 'number' ? x.toFixed(3) : '—');
      const extra = (typeof peak === 'number' && peak > (value || 0))
        ? ` <span class="peak">peak ${vFmt(peak)}</span>`
        : '';
      return `<div class="qct-row"><span class="label">${label}</span>${bars}<span class="value">${vFmt(value)}${extra}</span></div>`;
    }
    function ageTag(age) {
      if (age == null) return '';
      if (age < 2) return '<span class="age live">live</span>';
      if (age < 10) return `<span class="age">${age.toFixed(0)}s ago</span>`;
      return `<span class="age stale">stale ${age.toFixed(0)}s</span>`;
    }
    const parts = cams.map(cam => {
      const t = telem[cam] || {};
      const age = Number(t.age_s || 0);
      const isStale = age >= 2;
      const peak = Number(t.input_peak || 0);
      const clipping = peak >= 0.98;
      const clipClass = clipping ? 'warn' : (peak >= 0.7 ? '' : 'ok');
      const clipText = clipping ? 'CLIPPING' : (peak >= 0.7 ? 'hot' : 'headroom ok');
      const thr = Number(t.threshold || 0);
      const up = Number(t.up_peak || 0);
      const down = Number(t.down_peak || 0);
      const upFloor = Number(t.cfar_up_floor || 0);
      const downFloor = Number(t.cfar_down_floor || 0);
      return `<div class="qct-cam${isStale ? ' stale' : ''}">
        <div class="qct-head"><span>Cam ${cam} · ${t.armed ? 'armed' : 'cooldown'}${t.pending_up ? ' · pending up' : ''} ${ageTag(age)}</span>
          <span class="clip-chip ${clipClass}">${clipText}</span></div>
        ${row('input rms', t.input_rms, t.peak_input_rms, bar(t.input_rms, 0.5, null, null, t.peak_input_rms))}
        ${row('input peak', t.input_peak, t.peak_input_peak, bar(t.input_peak, 1.0, null, 0.98, t.peak_input_peak))}
        ${row('up peak', up, t.peak_up_peak, bar(up, 1.0, thr, null, t.peak_up_peak))}
        ${row('down peak', down, t.peak_down_peak, bar(down, 1.0, thr, null, t.peak_down_peak))}
        ${row('cfar up', upFloor, null, bar(upFloor, 0.2, null, null, null))}
        ${row('cfar dn', downFloor, null, bar(downFloor, 0.2, null, null, null))}
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
    const tuningActions = [
      '/settings/chirp_threshold',
      '/settings/mutual_sync_threshold',
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
