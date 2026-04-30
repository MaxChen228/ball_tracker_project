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
  const _eventDayCache = new Map(); // 'YYYY-MM-DD' -> el

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
    // Selected = currently loaded into dashboard 3D scene. Drives the
    // .event-item.selected background tint + the swatch fill.
    if (selectedTrajIds.has(e.session_id)) {
      cls.push('selected');
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
      pr: e.processing_state || '-',
      st: e.server_post_ts || null,
      b: currentEventsBucket,
      hm: e.created_hm || '',
      sp,
      // Selected state drives the swatch fill — must be in the diff key,
      // otherwise toggling selection doesn't repaint the swatch.
      sel: selectedTrajIds.has(e.session_id) ? 1 : 0,
    });
  }

  function _pipeChip(label, status, counts, title, sid, isLiveProgress) {
    let cls = '';
    if (status === 'done') cls = ' on';
    else if (status === 'err' || status === 'error') cls = ' err';
    else if (status === 'streaming') cls = ' streaming';
    else if (status === 'armed') cls = ' armed';
    if (isLiveProgress) {
      const prog = serverPostProgress.get(sid);
      const fmt = (camKey) => {
        const p = prog[camKey];
        if (!p) return '—';
        return p.total ? `${p.done}/${p.total}` : `${p.done}`;
      };
      const body = `<b>${fmt('A')}·${fmt('B')}</b>`;
      return `<span class="ev-pipe${cls}" title="${esc(title + ' · in progress')}">${label}${body}</span>`;
    }
    counts = counts || {};
    let body;
    let titleFull = title;
    if (Object.keys(counts).length) {
      const a = 'A' in counts ? String(Number(counts.A || 0)) : '—';
      const b = 'B' in counts ? String(Number(counts.B || 0)) : '—';
      body = `<b>${a}·${b}</b>`;
      const detail = Object.keys(counts).sort().map(c => `${c}:${counts[c]}`).join(', ');
      if (detail) titleFull += ' · ' + detail;
    } else {
      body = '<b>—</b>';
    }
    return `<span class="ev-pipe${cls}" title="${esc(titleFull)}">${label}${body}</span>`;
  }

  function _eventRowHtml(e) {
    const sid = esc(e.session_id);
    const hm = esc(e.created_hm || '—:—');
    const triangulated = Number(e.n_triangulated || 0);
    const trashed = currentEventsBucket === 'trash';
    const pathStatus = e.path_status || {};
    const pathCounts = e.n_ball_frames_by_path || {};
    const pipeTitles = {
      live: 'Live — iOS real-time detection (WS streamed)',
      server_post: 'Server — HSV detection on decoded MOV',
    };

    // --- swatch (row1 leading) — pure has-traj indicator. The whole row
    // is the click target for "load this fit into dashboard 3D"
    // (40_traj_handlers.js); the swatch is no longer an interactive
    // checkbox proxy. Selected sessions get a filled dot, unselected
    // sessions an outline; rows without triangulation get the pale dash.
    const hasTraj = triangulated > 0;
    const isSelected = selectedTrajIds.has(e.session_id);
    const color = hasTraj ? trajColorFor(e.session_id) : '';
    const swatch = hasTraj
      ? `<span class="swatch${isSelected ? ' selected' : ''}" style="border-color:${color};${isSelected ? `background:${color};` : ''}" aria-hidden="true"></span>`
      : `<span class="swatch swatch-empty" aria-hidden="true"></span>`;

    // --- row1 right: status chips ---
    const statusChips = [];
    if (e.processing_state) {
      statusChips.push(`<span class="chip ${esc(e.processing_state)}">${esc(e.processing_state)}</span>`);
    }
    if (e.status === 'error') {
      statusChips.push(`<span class="chip error">error</span>`);
    }
    if (Array.isArray(e.live_missing_calibration) && e.live_missing_calibration.length) {
      statusChips.push(`<span class="chip error" title="live frames dropped: no calibration on file">no cal: ${esc(e.live_missing_calibration.join(','))}</span>`);
    }
    const spErr = e.server_post_errors || {};
    const spKeys = Object.keys(spErr);
    if (spKeys.length) {
      const tip = spKeys.sort().map(k => `${k}: ${spErr[k]}`).join('; ');
      statusChips.push(`<span class="chip error" title="${esc(tip)}">srv err: ${esc(spKeys.sort().join(','))}</span>`);
    }
    const statusesHtml = statusChips.length
      ? `<div class="ev-statuses">${statusChips.join('')}</div>` : '';

    // --- row2: pipes + metrics ---
    const liveStatus = pathStatus.live || '-';
    const srvStatus = pathStatus.server_post || '-';
    const inFlight = typeof serverPostProgress !== 'undefined' && serverPostProgress.has(e.session_id);
    const pipesHtml = `<div class="ev-pipes">
      ${_pipeChip('L', liveStatus, pathCounts.live, pipeTitles.live, e.session_id, false)}
      ${_pipeChip('S', srvStatus, pathCounts.server_post, pipeTitles.server_post, e.session_id, inFlight)}
    </div>`;

    const metricsHtml = '';

    // --- row3: actions ---
    const actBits = [];
    if (e.processing_state === 'queued' || e.processing_state === 'processing') {
      actBits.push(_formBtn(`/sessions/${sid}/cancel_processing`, 'Cancel', 'warn'));
    } else if (!trashed && srvStatus !== 'done') {
      actBits.push(_formBtn(`/sessions/${sid}/run_server_post`, 'Run srv', 'ok'));
    }
    if (trashed) {
      actBits.push(_formBtn(`/sessions/${sid}/restore`, 'Restore', 'ok'));
      actBits.push(_formBtn(`/sessions/${sid}/delete`, 'Delete', 'dev', `刪除 session ${e.session_id}？此動作無法復原。`));
    } else {
      actBits.push(_formBtn(`/sessions/${sid}/trash`, 'Trash', 'dev', `移動 session ${e.session_id} 到垃圾桶？`));
    }
    const actionsHtml = actBits.length ? `<div class="ev-row3">${actBits.join('')}</div>` : '';

    // Whole row is the dashboard-3D-load click target. The viewer link is
    // a small secondary affordance on row 1 right side; clicks on it (and
    // on action <form>s in row 3) are stopPropagation'd in 40_traj_handlers
    // so they don't double as a row click.
    return `
      <div class="ev-row1">
        ${swatch}
        <span class="ev-time">${hm}</span>
        <span class="ev-sid">${sid}</span>
        <span class="ev-spacer"></span>
        ${statusesHtml}
        <a class="ev-viewer-link" href="/viewer/${sid}" title="Open in viewer">→ viewer</a>
      </div>
      <div class="ev-row2">${pipesHtml}${metricsHtml}</div>
      ${actionsHtml}`;
  }

  function _formBtn(action, label, variant, confirm, title) {
    const onsubmit = confirm
      ? ` onsubmit="return confirm(${JSON.stringify(confirm).replace(/"/g, '&quot;')});"`
      : '';
    const titleAttr = title ? ` title="${esc(title)}"` : '';
    return `<form class="ev-action-form" method="POST" action="${action}"${onsubmit}><button class="ev-btn ${variant}" type="submit"${titleAttr}>${label}</button></form>`;
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
      _eventDayCache.clear();
    }

    // Walk events in (already-sorted) order and emit a `.event-day-header`
    // whenever the local-tz day flips. Day headers + rows share one DOM
    // child list so the sequence is `[hdr, row, row, hdr, row, ...]`.
    const liveIds = new Set();
    const liveDays = new Set();
    let domIndex = 0;
    let lastDay = null;
    for (let i = 0; i < events.length; i++) {
      const e = events[i];
      const sid = e.session_id;
      const day = e.created_day || '—';
      liveIds.add(sid);
      if (day !== lastDay) {
        liveDays.add(day);
        let dayEl = _eventDayCache.get(day);
        if (!dayEl) {
          dayEl = document.createElement('div');
          dayEl.className = 'event-day';
          dayEl.dataset.day = day;
          dayEl.textContent = day;
          _eventDayCache.set(day, dayEl);
        }
        if (eventsBox.children[domIndex] !== dayEl) {
          eventsBox.insertBefore(dayEl, eventsBox.children[domIndex] || null);
        }
        domIndex++;
        lastDay = day;
      }
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
      if (eventsBox.children[domIndex] !== entry.el) {
        eventsBox.insertBefore(entry.el, eventsBox.children[domIndex] || null);
      }
      domIndex++;
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
    for (const day of Array.from(_eventDayCache.keys())) {
      if (liveDays.has(day)) continue;
      const el = _eventDayCache.get(day);
      if (el && el.parentNode === eventsBox) eventsBox.removeChild(el);
      _eventDayCache.delete(day);
    }
  }

