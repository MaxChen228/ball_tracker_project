// === fmtNum + renderEvents ===

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  // Full time-sync controls live on /sync. The dashboard only mirrors
  // current sync state in the shared header.

  // Tracks the rendered row nodes so we can do a keyed increment diff on
  // every tick (add/remove/update in place) instead of nuking all HTML.
  // Why: the full innerHTML swap made checkbox state race with the
  // delegated change handler, and Plotly tooltips over the trash/cancel
  // buttons jumped whenever the tick arrived even if nothing changed.
  const _eventRowCache = new Map(); // sid -> { el, key }

  function _eventRowKey(e) {
    // Must cover every field rendered in the row. Missing a field =
    // stuck row. n_ball_frames_by_path is reduced to a compact string
    // (path:cam:count|...) so it fits a single-line JSON stringify.
    const pbc = e.n_ball_frames_by_path || {};
    const pbcStr = Object.keys(pbc).sort().map(p => {
      const cams = pbc[p] || {};
      return p + ':' + Object.keys(cams).sort().map(c => c + '=' + cams[c]).join(',');
    }).join('|');
    const ps = e.path_status || {};
    return JSON.stringify({
      s: e.status,
      pl: ps.live || '-', ps: ps.server_post || '-',
      n: e.n_triangulated,
      pc: pbcStr,
      d: e.duration_s != null ? Number(e.duration_s).toFixed(2) : null,
      z: e.peak_z_m != null ? Number(e.peak_z_m).toFixed(2) : null,
      mph: e.speed_mph != null ? Number(e.speed_mph).toFixed(1) : null,
      pr: e.processing_state || '-',
      st: e.server_post_ts || null,
      b: currentEventsBucket,
    });
  }

  function _eventRowHtml(e) {
    const sid = esc(e.session_id);
    const stat = (e.status || '').replace(/_/g, ' ');
    const triangulated = Number(e.n_triangulated || 0);
    const pathStatus = e.path_status || {};
    const pathCounts = e.n_ball_frames_by_path || {};
    const pathTitles = {
      live: 'Live — iOS real-time detection (WS streamed)',
      server_post: 'SVR — server-side detection on decoded MOV',
    };
    const pathChips = [['live', 'L'], ['server_post', 'S']]
      .map(([path, label]) => {
        const status = pathStatus[path] || '-';
        const counts = pathCounts[path] || {};
        const total = Object.values(counts).reduce((a, v) => a + Number(v || 0), 0);
        const cls = status === 'done' ? ' on' : status === 'error' ? ' err' : '';
        const countHtml = total > 0 ? `<span class="pc">${total}</span>` : '';
        const detail = Object.keys(counts).sort().map(c => `${c}:${counts[c]}`).join(', ');
        const title = detail ? `${pathTitles[path]} · ${detail}` : pathTitles[path];
        return `<span class="path-chip${cls}" title="${esc(title)}">${label}${countHtml}</span>`;
      })
      .join('');
    const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
    const trashMsg = `移動 session ${e.session_id} 到垃圾桶？`;
    const hasTraj = triangulated > 0;
    const color = hasTraj ? trajColorFor(e.session_id) : '';
    const checked = selectedTrajIds.has(e.session_id) ? 'checked' : '';
    const toggle = hasTraj
      ? `<label class="traj-toggle" title="Overlay trajectory on canvas">
           <input type="checkbox" data-traj-sid="${sid}" ${checked}>
           <span class="swatch" style="background:${color}"></span>
         </label>`
      : `<span class="traj-toggle-placeholder" aria-hidden="true"></span>`;
    const metaBits = [];
    if (triangulated > 0) metaBits.push(`<span class="k">pts</span><span class="v">${triangulated}</span>`);
    if (e.duration_s != null) metaBits.push(`<span class="k">dur</span><span class="v">${Number(e.duration_s).toFixed(2)}s</span>`);
    if (e.peak_z_m != null) metaBits.push(`<span class="k">z</span><span class="v">${Number(e.peak_z_m).toFixed(2)}m</span>`);
    if (e.speed_mph != null) metaBits.push(`<span class="k">mph</span><span class="v">${Number(e.speed_mph).toFixed(1)}</span>`);
    const metaHtml = metaBits.length ? `<div class="event-meta">${metaBits.join('')}</div>` : '';
    const processingState = e.processing_state ? `<span class="chip ${esc(e.processing_state)}">${esc(e.processing_state)}</span>` : '';
    const serverStatus = (e.path_status || {}).server_post || '-';
    const showRunServer = currentEventsBucket !== 'trash'
      && serverStatus !== 'done'
      && e.processing_state !== 'queued'
      && e.processing_state !== 'processing';
    const processingAction = e.processing_state === 'queued' || e.processing_state === 'processing'
      ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/cancel_processing">
           <button class="event-action warn" type="submit">Cancel</button>
         </form>`
      : showRunServer
        ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/run_server_post">
             <button class="event-action ok" type="submit">Run srv</button>
           </form>`
        : '';
    const lifecycleAction = currentEventsBucket === 'trash'
      ? `
          <form class="event-action-form" method="POST" action="/sessions/${sid}/restore">
            <button class="event-action ok" type="submit">Restore</button>
          </form>
          <form class="event-action-form" method="POST"
                action="/sessions/${sid}/delete"
                onsubmit="return confirm(${JSON.stringify(confirmMsg)});">
            <button class="event-action dev" type="submit">Delete</button>
          </form>`
      : `
          <form class="event-action-form" method="POST"
                action="/sessions/${sid}/trash"
                onsubmit="return confirm(${JSON.stringify(trashMsg)});">
            <button class="event-action dev" type="submit">Trash</button>
          </form>`;
    const statusChipHtml = (e.status === 'error')
      ? `<span class="chip ${esc(e.status || '')}">${esc(stat)}</span>`
      : '';
    return `
      ${toggle}
      <a class="event-row" href="/viewer/${sid}">
        <div class="event-head">
          <span class="sid">${sid}</span>
          ${pathChips}
        </div>
        ${metaHtml}
      </a>
      <div class="event-status">
        ${processingState}
        ${statusChipHtml}
      </div>
      <div class="event-actions">
        ${processingAction}
        ${lifecycleAction}
      </div>`;
  }

  function renderEvents(events) {
    if (!eventsBox) return;
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      _eventRowCache.clear();
      return;
    }

    // Fast path for a fresh mount / empty-state transition: the cache is
    // empty or the box was showing the empty placeholder. Fall through
    // to the key-diff branch below on subsequent ticks.
    const hasEmpty = eventsBox.querySelector('.events-empty');
    if (hasEmpty || _eventRowCache.size === 0) {
      eventsBox.innerHTML = '';
      _eventRowCache.clear();
    }

    const liveIds = new Set();
    for (let i = 0; i < events.length; i++) {
      const e = events[i];
      const sid = e.session_id;
      liveIds.add(sid);
      const key = _eventRowKey(e);
      let entry = _eventRowCache.get(sid);
      if (!entry) {
        const el = document.createElement('div');
        el.className = 'event-item';
        el.dataset.sid = sid;
        el.innerHTML = _eventRowHtml(e);
        eventsBox.appendChild(el);
        entry = { el, key };
        _eventRowCache.set(sid, entry);
      } else if (entry.key !== key) {
        // Preserve the live checkbox state across re-render so a user
        // mid-click isn't reset by an events tick. innerHTML swap is
        // safe: the delegated change handler (40_traj_handlers.js)
        // rebinds via event delegation.
        entry.el.innerHTML = _eventRowHtml(e);
        entry.key = key;
      }
      // Ensure DOM order matches the sorted events array. Cheap when
      // already in place; appendChild re-parents in place.
      if (eventsBox.children[i] !== entry.el) {
        eventsBox.insertBefore(entry.el, eventsBox.children[i] || null);
      }
    }

    // Remove rows for sessions that dropped off the current bucket view.
    for (const sid of Array.from(_eventRowCache.keys())) {
      if (liveIds.has(sid)) continue;
      const entry = _eventRowCache.get(sid);
      if (entry && entry.el.parentNode === eventsBox) {
        eventsBox.removeChild(entry.el);
      }
      _eventRowCache.delete(sid);
    }
  }

