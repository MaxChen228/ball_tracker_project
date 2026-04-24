// === fmtNum + renderEvents ===

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  // Full time-sync controls live on /sync. The dashboard only mirrors
  // current sync state in the shared header.

  function renderEvents(events) {
    if (!eventsBox) return;
    let evHtml;
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      return;
    }
    evHtml = events.map(e => {
      const sid = esc(e.session_id);
      const stat = (e.status || '').replace(/_/g, ' ');
      const triangulated = Number(e.n_triangulated || 0);
      // Two pipelines, two independent chips. State (on/err/-) from
      // path_status; count from n_ball_frames_by_path.
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
      // Trajectory overlay toggle: only sessions with triangulated points qualify.
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
      // Only surface real signal: path chips already encode per-
      // pipeline completion, so `partial`/`paired`/`paired_no_points`
      // are noise. `error` is the only result-status chip worth
      // showing; processing states (queued/processing/...) stay.
      const statusChipHtml = (e.status === 'error')
        ? `<span class="chip ${esc(e.status || '')}">${esc(stat)}</span>`
        : '';
      return `
        <div class="event-item">
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
          </div>
        </div>`;
    }).join('');
    eventsBox.innerHTML = evHtml;
  }
