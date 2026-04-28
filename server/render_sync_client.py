"""Sync page client-side JS template."""
from __future__ import annotations

_JS_TEMPLATE = r"""
(function () {
  const syncBox = document.getElementById('sync-body');
  const navStatus = document.getElementById('nav-status');
  const tuningStatus = document.getElementById('tuning-status');

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

  // Nav chip mirror lives in the dashboard JS bundle (loaded on every
  // page) — sync used to ship its own renderNav writing to the same
  // #nav-status slot, which made the strip flicker between formats as
  // the two ticks fought each turn. Removed; the dashboard renderer is
  // the sole writer everywhere now.

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
      const ageTxt = ageS != null ? `${ageS.toFixed(0)}s ago` : null;
      const idChip = (synced && sid)
        ? `<span class="sid-chip" title="${esc(sid)}">·${esc(shortSyncId(sid))}</span>`
        : '';
      const pairBadge = (synced && bothPaired) ? ' <span class="pair-ok">paired</span>' : '';
      let metaInner;
      if (synced) {
        metaInner = `${idChip} last sync · ${ageTxt}${pairBadge}`;
      } else if (ageTxt) {
        const suffix = isListening ? ' · listening…'
                     : online      ? ' · awaiting new chirp'
                                   : ' · device offline';
        metaInner = `last sync · ${ageTxt}${suffix}`;
      } else {
        metaInner = isListening ? 'waiting for chirp…'
                  : online      ? 'press Run mutual sync'
                                : 'device offline';
      }
      const meta = `<div class="pcs-meta">${metaInner}</div>`;
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

  // Legacy textarea + execCommand path for browsers without Clipboard API.
  function copyTextSync(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (err) {
      console.error('copyTextSync fallback failed', err);
      return false;
    }
  }

  // Show a modal with the text selected, as a last-resort "at least the
  // user can manually Cmd+C" affordance when both Clipboard API and
  // execCommand fail (e.g. Safari after `await fetch()` consumes the
  // user-gesture activation).
  function showCopyFallback(text, anchorEl) {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.style.cssText = 'background:var(--surface);padding:16px;border:1px solid var(--border);border-radius:6px;max-width:80vw;max-height:80vh;display:flex;flex-direction:column;gap:8px;';
    const hdr = document.createElement('div');
    hdr.style.cssText = 'font-family:var(--mono);font-size:11px;color:var(--sub);letter-spacing:0.08em;text-transform:uppercase;';
    hdr.textContent = 'Safari blocked auto-copy — press ⌘C then Esc';
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.style.cssText = 'flex:1;min-width:60vw;min-height:60vh;font-family:var(--mono);font-size:11px;padding:8px;border:1px solid var(--border-l);';
    panel.appendChild(hdr);
    panel.appendChild(ta);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    ta.focus();
    ta.select();
    const close = () => { document.body.removeChild(overlay); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  }

  // Log buttons (Copy / Clear).
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.id === 'sync-report-copy') {
      const orig = t.textContent;
      t.disabled = true;
      t.textContent = 'Copying…';
      const logEl = document.getElementById('sync-log');
      const logText = logEl ? (logEl.textContent || '') : '';
      // Safari-safe async copy: wrap the fetch in a ClipboardItem
      // promise. navigator.clipboard.write() preserves the user-gesture
      // activation across the awaited fetch, which writeText() after
      // `await fetch()` does not.
      const buildBlob = async () => {
        const r = await fetch('/sync/debug_export', { cache: 'no-store' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const debugText = await r.text();
        const combined = `${debugText}\n\n=== EVENT LOG (live) ===\n${logText}`;
        return new Blob([combined], { type: 'text/plain' });
      };
      try {
        if (typeof ClipboardItem !== 'undefined' && navigator.clipboard && navigator.clipboard.write) {
          const item = new ClipboardItem({ 'text/plain': buildBlob() });
          await navigator.clipboard.write([item]);
          t.textContent = 'Copied!';
          setTimeout(() => { t.textContent = orig; t.disabled = false; }, 1800);
          return;
        }
        // Older browsers: fetch then sync-copy via textarea trick.
        const blob = await buildBlob();
        const text = await blob.text();
        if (copyTextSync(text)) {
          t.textContent = 'Copied!';
          setTimeout(() => { t.textContent = orig; t.disabled = false; }, 1800);
          return;
        }
        throw new Error('clipboard write blocked');
      } catch (err) {
        console.error('sync-report-copy failed', err);
        // Final fallback: show a modal with the text pre-selected so
        // the user can ⌘C manually. Fetch the report separately since
        // the ClipboardItem promise has already rejected.
        try {
          const blob = await buildBlob();
          const text = await blob.text();
          showCopyFallback(text, t);
          t.textContent = 'Manual copy';
        } catch (err2) {
          t.textContent = `Error: ${err2.message || err2}`;
        }
        t.disabled = false;
        setTimeout(() => { t.textContent = orig; }, 4000);
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
