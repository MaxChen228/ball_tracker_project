// === quick sync card — single-emitter, N-listener (multi-cam phase) ===
//
// Polls /sync/quick_state every 1 s while a run is active so the lazy
// server-side timeout (state_sync.py::_check_quick_sync_timeout_locked
// fires only when current_quick_sync() is read) actually advances. When
// no run is active the same poll still ticks so a newly-solved run from
// another browser becomes visible here.
//
// Completion judgment: /sync/quick_state returns {quick_sync, last_quick_sync}.
//   active:    quick_sync != null,  last_quick_sync == null
//   solved:    quick_sync == null,  last_quick_sync != null && !.aborted
//   aborted:   quick_sync == null,  last_quick_sync != null && .aborted
// Apply is enabled only in the solved state with last_quick_sync.id set.

  let _quickSyncLastSeenId = null;

  function _escQS(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _renderQuickSyncEmitterSelect(onlineCams, currentSelection) {
    const sel = document.getElementById('quick-sync-emitter');
    const startBtn = document.getElementById('quick-sync-start');
    if (!sel || !startBtn) return;
    // Only rebuild options when the online set changes — preserves the
    // operator's current selection during normal ticks.
    const currentOpts = Array.from(sel.options).map(o => o.value).join(',');
    const wantOpts = onlineCams.join(',');
    if (currentOpts === wantOpts && onlineCams.length > 0) {
      return;
    }
    sel.innerHTML = '';
    if (onlineCams.length === 0) {
      const opt = document.createElement('option');
      opt.textContent = '(no online cams)';
      sel.appendChild(opt);
      sel.disabled = true;
      startBtn.disabled = true;
      return;
    }
    sel.disabled = false;
    for (const cam of onlineCams) {
      const opt = document.createElement('option');
      opt.value = cam;
      opt.textContent = cam;
      sel.appendChild(opt);
    }
    if (currentSelection && onlineCams.includes(currentSelection)) {
      sel.value = currentSelection;
    }
  }

  function _renderQuickSyncDynamic(state) {
    const dyn = document.getElementById('quick-sync-dynamic');
    if (!dyn) return;
    const active = state.quick_sync;
    const last = state.last_quick_sync;
    if (active) {
      // Run in progress — show emitter, received-so-far progress.
      const received = (active.reports_received || []).join(', ') || '—';
      const listeners = (active.listener_cam_ids || []).join(', ');
      dyn.classList.remove('muted');
      dyn.innerHTML =
        `<div class="quick-sync-row"><span class="qs-label">Run:</span> ` +
        `${_escQS(active.id)}</div>` +
        `<div class="quick-sync-row"><span class="qs-label">Emitter:</span> ` +
        `${_escQS(active.emitter_cam_id)}</div>` +
        `<div class="quick-sync-row"><span class="qs-label">Listeners:</span> ` +
        `${_escQS(listeners)}</div>` +
        `<div class="quick-sync-row"><span class="qs-label">Received:</span> ` +
        `${_escQS(received)} / ${(active.listener_cam_ids || []).length}</div>`;
      return;
    }
    if (last) {
      if (last.aborted) {
        dyn.classList.remove('muted');
        dyn.innerHTML =
          `<div class="quick-sync-row"><span class="qs-label">Last:</span> ` +
          `${_escQS(last.id)}</div>` +
          `<div class="quick-sync-row" style="color:#C0392B;">` +
          `aborted — emitter self-hear missing</div>`;
        return;
      }
      // Solved. Show anchors + Apply button.
      const anchors = last.anchors_pts_s || {};
      const missing = last.missing_cam_ids || [];
      const anchorRows = Object.keys(anchors).sort().map(cam => {
        return `<div class="quick-sync-row qs-anchor">` +
               `<span class="qs-label">${_escQS(cam)}:</span> ` +
               `<span class="qs-anchor-val">${Number(anchors[cam]).toFixed(6)}</span></div>`;
      }).join('');
      const missingRow = missing.length
        ? `<div class="quick-sync-row" style="color:#C0392B;">` +
          `Missing: ${_escQS(missing.join(', '))}</div>`
        : '';
      const applied = (_quickSyncLastSeenId === last.id) ? ' (re-apply ok)' : '';
      dyn.classList.remove('muted');
      dyn.innerHTML =
        `<div class="quick-sync-row"><span class="qs-label">Solved:</span> ` +
        `${_escQS(last.id)}${applied}</div>` +
        `<div class="quick-sync-anchors">${anchorRows}</div>` +
        missingRow +
        `<div class="quick-sync-row">` +
        `<button type="button" class="btn" id="quick-sync-apply" ` +
        `data-sync-id="${_escQS(last.id)}">Apply anchors</button></div>`;
      return;
    }
    dyn.classList.add('muted');
    dyn.innerHTML = 'No quick sync run yet. Pick an emitter and Start.';
  }

  function _quickSyncSessionArmed() {
    return !!(currentSession && currentSession.armed);
  }

  async function tickQuickSync() {
    // Refresh emitter dropdown from live online state — quick_start
    // returns 409 emitter_offline if the picked cam isn't online, so
    // the dropdown must follow currentDevices, not EXPECTED.
    const onlineCams = (currentDevices || [])
      .filter(d => d && d.online)
      .map(d => d.camera_id);
    const sel = document.getElementById('quick-sync-emitter');
    _renderQuickSyncEmitterSelect(onlineCams, sel ? sel.value : null);

    const startBtn = document.getElementById('quick-sync-start');
    if (startBtn) {
      const armed = _quickSyncSessionArmed();
      // Same gating as the legacy Quick chirp button: don't open an
      // audio window mid-recording.
      startBtn.disabled = (onlineCams.length === 0) || armed;
      startBtn.title = armed ? 'Stop the armed session first' : '';
    }

    try {
      const r = await fetch('/sync/quick_state', { cache: 'no-store' });
      if (!r.ok) return;
      const state = await r.json();
      _renderQuickSyncDynamic(state);
    } catch (e) {
      console.debug('[tickQuickSync] transient', e);
    }
  }

  async function _quickSyncStart() {
    const sel = document.getElementById('quick-sync-emitter');
    if (!sel || !sel.value) return;
    const emitter = sel.value;
    try {
      const r = await fetch('/sync/quick_start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ emitter_cam_id: emitter }),
      });
      if (!r.ok) {
        const body = await r.text();
        console.warn('[quick_sync] start failed', r.status, body);
        return;
      }
      // Immediate refresh so the operator sees the run flip to active
      // without waiting up to 1 s for the next tick.
      tickQuickSync();
    } catch (e) {
      console.warn('[quick_sync] start exception', e);
    }
  }

  async function _quickSyncApply(syncId) {
    if (!syncId) return;
    try {
      const r = await fetch(`/sync/quick_apply/${encodeURIComponent(syncId)}`, {
        method: 'POST',
      });
      if (!r.ok) {
        const body = await r.text();
        console.warn('[quick_sync] apply failed', r.status, body);
        return;
      }
      _quickSyncLastSeenId = syncId;
      tickQuickSync();
    } catch (e) {
      console.warn('[quick_sync] apply exception', e);
    }
  }

  // Event delegation on the card root so Start/Apply work even after
  // dynamic re-render replaces the elements.
  document.addEventListener('click', function(ev) {
    const t = ev.target;
    if (!t || !t.id) return;
    if (t.id === 'quick-sync-start') {
      ev.preventDefault();
      _quickSyncStart();
    } else if (t.id === 'quick-sync-apply') {
      ev.preventDefault();
      _quickSyncApply(t.getAttribute('data-sync-id'));
    }
  });
