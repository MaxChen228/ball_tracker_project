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

  function _eventRowClasses(e, existingClassName = '') {
    // Drives the row-level visual treatment (e.g. orange pulse while a
    // server_post job is queued/processing). The Phase 4 SSE listener
    // also adds a transient .flash-done class for ~700 ms on success;
    // preserve it across re-renders so a row diff that lands mid-flash
    // doesn't strip the class and abort the animation.
    const cls = ['event-item'];
    if (e.processing_state === 'queued' || e.processing_state === 'processing') {
      cls.push('processing');
    }
    if (existingClassName && existingClassName.indexOf('flash-done') !== -1) {
      cls.push('flash-done');
    }
    return cls.join(' ');
  }

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
    // Server-post in-flight snapshot. Must be part of the diff key,
    // otherwise SSE-driven progress updates won't trigger a row
    // re-render — busting `_lastEvKey` only forces a global walk; the
    // per-row key still has to differ for the row's innerHTML to swap.
    const sp = (typeof serverPostProgress !== 'undefined'
                && serverPostProgress.has(e.session_id))
      ? JSON.stringify(serverPostProgress.get(e.session_id))
      : null;
    return JSON.stringify({
      s: e.status,
      pl: ps.live || '-', ps: ps.server_post || '-',
      n: e.n_triangulated,
      pc: pbcStr,
      d: e.duration_s != null ? Number(e.duration_s).toFixed(2) : null,
      z: e.peak_z_m != null ? Number(e.peak_z_m).toFixed(2) : null,
      mph: e.ballistic_speed_mph != null ? Number(e.ballistic_speed_mph).toFixed(1) : null,
      pr: e.processing_state || '-',
      st: e.server_post_ts || null,
      b: currentEventsBucket,
      sp,
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
        // Per-cam in fixed A·B order — see render_dashboard_events.py
        // for the rationale. Must stay in sync with the SSR path so a
        // page reload doesn't shift between two formats.
        const cls = status === 'done' ? ' on' : status === 'error' ? ' err' : '';

        // Server_post in-flight: replace the stable post-completion
        // counts with a live `done/total · done/total` so the chip
        // ticks during the 8-20 s decode. Falls back to bare `done`
        // (no slash) when probe_frame_count returned null. Once
        // server_post_done fires, the entry is dropped from the map
        // and we fall through to the stable-counts branch below.
        if (path === 'server_post'
            && typeof serverPostProgress !== 'undefined'
            && serverPostProgress.has(sid)) {
          const prog = serverPostProgress.get(sid);
          const fmt = (camKey) => {
            const p = prog[camKey];
            if (!p) return '—';
            return p.total ? `${p.done}/${p.total}` : `${p.done}`;
          };
          const countHtml = `<span class="pc">${fmt('A')}·${fmt('B')}</span>`;
          const tip = `${pathTitles[path]} · in progress`;
          return `<span class="path-chip${cls}" title="${esc(tip)}">${label}${countHtml}</span>`;
        }

        const hasCounts = Object.keys(counts).length > 0;
        let countHtml = '';
        if (hasCounts) {
          const aStr = ('A' in counts) ? String(Number(counts.A || 0)) : '—';
          const bStr = ('B' in counts) ? String(Number(counts.B || 0)) : '—';
          countHtml = `<span class="pc">${aStr}·${bStr}</span>`;
        }
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
    if (e.ballistic_speed_mph != null) metaBits.push(`<span class="k">mph</span><span class="v">${Number(e.ballistic_speed_mph).toFixed(1)}</span>`);
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
        el.className = _eventRowClasses(e);
        el.dataset.sid = sid;
        el.innerHTML = _eventRowHtml(e);
        eventsBox.appendChild(el);
        entry = { el, key };
        _eventRowCache.set(sid, entry);
      } else if (entry.key !== key) {
        // Preserve the live checkbox state across re-render so a user
        // mid-click isn't reset by an events tick. innerHTML swap is
        // safe: the delegated change handler (40_traj_handlers.js)
        // rebinds via event delegation. Pass the current className so
        // _eventRowClasses can preserve transient SSE-driven classes
        // like .flash-done that aren't derived from event data.
        entry.el.className = _eventRowClasses(e, entry.el.className);
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

