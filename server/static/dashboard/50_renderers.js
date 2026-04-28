// === esc + chips + renderDevices ===

  // Per-cam preview render state. Separate from tickPreviewImages'
  // 200 ms refresh loop: this just suppresses the per-tick cache-bust
  // on the SSR <img src> so an idle /status tick doesn't force a fresh
  // /camera/{id}/preview GET every second. Enable/disable flips always
  // reset the src so the panel reflects the new state immediately.
  const _previewRenderState = new Map(); // cam -> { on, src, t }
  const _PREVIEW_REFRESH_MIN_MS = 1000;

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  function statusChip(cam, online, calibrated) {
    if (calibrated) return `<span class="chip calibrated">calibrated</span>`;
    if (online)     return `<span class="chip online">online</span>`;
    return `<span class="chip idle">offline</span>`;
  }

  // Mirrors server-side _render_battery_chip: hidden when device is offline
  // or hasn't reported battery yet.
  function batteryChip(device, online) {
    if (!online || !device) return '';
    const level = device.battery_level;
    if (typeof level !== 'number' || level < 0 || level > 1) return '';
    const pct = Math.max(0, Math.min(100, Math.round(level * 100)));
    const state = device.battery_state || 'unknown';
    let cls, icon;
    if (state === 'charging' || state === 'full') { cls = 'charging'; icon = '⚡'; }
    else if (pct <= 15) { cls = 'low';  icon = '▁'; }
    else if (pct <= 35) { cls = 'mid';  icon = '▃'; }
    else                 { cls = 'ok';   icon = '▅'; }
    return `<span class="chip battery ${cls}" title="battery · ${esc(state)}">${icon} ${pct}%</span>`;
  }

  function autoCalLabel(autoRun, autoLast, online) {
    if (autoRun) {
      return autoRun.summary || autoRun.status || 'running';
    }
    if (autoLast) {
      if (autoLast.status === 'completed') {
        const reproj = autoLast.result && autoLast.result.reprojection_px != null
          ? (' · ' + Number(autoLast.result.reprojection_px).toFixed(1) + 'px')
          : '';
        return `${autoLast.summary || 'Applied'}${reproj}`;
      }
      // Failed / cancelled: surface the server-side `detail` inline so
      // the operator sees *why* without having to pull server logs.
      const base = autoLast.summary || autoLast.status || 'failed';
      const det = autoLast.detail ? ` — ${autoLast.detail}` : '';
      return `${base}${det}`;
    }
    return online ? 'idle' : 'offline';
  }

  function autoCalButtonLabel(autoRun) {
    if (!autoRun) return 'Run auto-cal';
    switch (autoRun.status) {
      case 'searching': return 'Capturing…';
      case 'tracking': return 'Tracking…';
      case 'stabilizing': return 'Stabilizing…';
      case 'solving': return 'Solving…';
      default: return 'Auto-cal…';
    }
  }

  function renderDevices(state) {
    if (!devicesBox) return;
    const devByCam = new Map((state.devices || []).map(d => [d.camera_id, d]));
    const calibrated = new Set(state.calibrations || []);
    const syncPending = state.sync_commands || {};
    const previewReq = state.preview_requested || {};
    const previewPending = new Set(state.preview_pending || []);
    const calLastTs = state.calibration_last_ts || {};
    const autoCalActive = (state.auto_calibration && state.auto_calibration.active) || {};
    const autoCalLast = (state.auto_calibration && state.auto_calibration.last) || {};
    function hhmm(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      return d.toTimeString().slice(0, 5);
    }

    function row(cam, deviceRecord) {
      const online = !!deviceRecord;
      const timeSynced = !!(deviceRecord && deviceRecord.time_synced);
      const pending = !!syncPending[cam];
      const isCal = calibrated.has(cam);
      const previewOn = !!previewReq[cam];
      const previewBusy = previewPending.has(cam);
      const lastTs = calLastTs[cam];
      const autoRun = autoCalActive[cam] || null;
      const autoLast = autoCalLast[cam] || null;
      const calDot = isCal ? 'ok' : (online ? 'warn' : 'bad');
      const syncDot = !online ? 'bad' : (pending ? 'warn' : (timeSynced ? 'ok' : 'warn'));
      const autoDot = autoRun ? 'warn'
                    : (autoLast && autoLast.status === 'completed' ? 'ok'
                    : (autoLast && autoLast.status === 'failed' ? 'bad' : (online ? 'warn' : 'bad')));
      const syncLabel = !online ? 'offline' : (pending ? 'pending…' : (timeSynced ? 'synced' : 'not synced'));
      const calLabel = (isCal && lastTs) ? ('last ' + hhmm(lastTs))
                     : (!online ? 'offline' : (isCal ? 'calibrated' : 'pending'));
      const autoLabel = autoCalLabel(autoRun, autoLast, online);
      const previewDisabled = previewBusy || !online;
      const autoCalDisabled = !!autoRun || !online;
      const previewBtn = (`<button type="button" class="btn small preview-btn${previewOn ? ' active' : ''}" ` +
        `data-preview-cam="${esc(cam)}" data-preview-enabled="${previewOn ? 1 : 0}" ` +
        `${previewDisabled ? 'disabled' : ''}>` +
        `${previewBusy ? (previewOn ? 'PREVIEW ON…' : 'PREVIEW…') : (previewOn ? 'PREVIEW ON' : 'PREVIEW')}</button>`);
      const autoCalBtn = `<button type="button" class="btn small" data-auto-cal="${esc(cam)}" ${autoCalDisabled ? 'disabled' : ''}>` +
        `${autoCalButtonLabel(autoRun)}</button>`;
      const autoLogBtn = (autoLast && autoLast.status === 'failed')
        ? `<button type="button" class="btn small secondary" data-auto-cal-log="${esc(cam)}" title="Copy full auto-cal log to clipboard for debugging">Copy log</button>`
        : '';
      // Always render the panel so the row height stays stable; off
      // state shows a black placeholder. When on, the tickPreviewImages
      // loop (see below) cache-busts the <img src>.
      // Only hit the preview endpoint when actually watching — otherwise
      // the browser eagerly fetches the <img> src on every render and
      // spams 404s for cams with preview off.
      //
      // Don't cache-bust every renderDevices tick: that made each
      // /status tick re-fetch the same frame. Carry the previous src
      // forward unless (a) preview just flipped on/off, or (b) the
      // last refresh is older than the _PREVIEW_REFRESH_MIN_MS budget.
      // tickPreviewImages (74_preview_poll.js) still owns the real
      // refresh cadence.
      const prevState = _previewRenderState.get(cam);
      const prevOn = prevState && prevState.on;
      const nowMs = Date.now();
      let initialSrc = '';
      if (previewOn) {
        if (!prevState || prevOn !== true
            || !prevState.src
            || (nowMs - (prevState.t || 0) > _PREVIEW_REFRESH_MIN_MS)) {
          initialSrc = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + nowMs;
          _previewRenderState.set(cam, { on: true, src: initialSrc, t: nowMs });
        } else {
          initialSrc = prevState.src;
        }
      } else {
        _previewRenderState.set(cam, { on: false, src: '', t: nowMs });
      }
      // Merged single-pane cam-view: real MJPEG as base, virtual
      // reprojection drawn as semi-transparent canvas overlay. plate is
      // default-on; axes is a toggleable secondary layer. Empty src when
      // preview is off so the browser doesn't hammer a 404 endpoint.
      const camViewSrc = previewOn ? initialSrc : '';
      const camViewBlock =
        `<div class="cam-view${previewOn ? '' : ' is-offline'}" data-cam-view="${esc(cam)}" ` +
        `data-layers="plate,axes" data-layers-on="plate" data-default-opacity="70">` +
        `<img data-cam-img="${esc(cam)}" src="${camViewSrc}" alt="preview ${esc(cam)}">` +
        `<canvas data-cam-canvas="${esc(cam)}"></canvas>` +
        `<div class="cam-view-badges">` +
          `<span class="cam-view-badge cam-id">Cam ${esc(cam)}</span>` +
        `</div>` +
        `<div class="cam-view-toolbar">` +
          `<button type="button" class="cv-layer on" data-layer="plate">PLATE</button>` +
          `<button type="button" class="cv-layer" data-layer="axes">AXES</button>` +
          `<span class="cv-opacity">OVL` +
            `<input type="range" min="0" max="100" step="1" value="70" aria-label="Overlay opacity">` +
          `</span>` +
        `</div>` +
        `<div class="cam-view-extra"></div>` +
        `</div>`;
      const syncLedCls = !online ? 'offline'
                        : pending ? 'listening'
                        : timeSynced ? 'synced'
                        : 'waiting';
      const syncId = deviceRecord && deviceRecord.time_sync_id;
      const shortSid = syncId ? (syncId.length > 8 ? syncId.slice(-6) : syncId.replace(/^sy_/, '')) : '';
      const syncIdTxt = (timeSynced && syncId)
        ? `<span class="sync-id-chip" title="${esc(syncId)}">·${esc(shortSid)}</span>`
        : '';
      return `
        <div class="device">
          <div class="device-head">
            <span class="sync-led ${syncLedCls}" title="time sync · ${esc(syncLabel)}"></span>
            <div class="id">${esc(cam)}</div>
            <div class="sub">
              <span class="item ${syncDot}"><span class="dot ${syncDot}"></span>time sync · ${esc(syncLabel)}${syncIdTxt}</span>
              <span class="item ${calDot}"><span class="dot ${calDot}"></span>pose · ${esc(calLabel)}</span>
              <span class="item ${autoDot}" title="${esc(autoLabel)}"><span class="dot ${autoDot}"></span>auto-cal · ${esc(autoLabel)}</span>
            </div>
            <div class="chip-col">${batteryChip(deviceRecord, online)}${statusChip(cam, online, isCal)}</div>
          </div>
          <div class="device-actions">${previewBtn}${autoCalBtn}${autoLogBtn}</div>
          ${camViewBlock}
        </div>`;
    }

    const rows = EXPECTED.map(cam => row(cam, devByCam.get(cam))).join('');
    const extras = (state.devices || [])
      .filter(d => !EXPECTED.includes(d.camera_id))
      .map(d => row(d.camera_id, d)).join('');
    devicesBox.innerHTML = `<div class="devices-grid">${rows + extras}</div>`;
    // The innerHTML rebuild above destroys every cam-view DOM element.
    // BallTrackerCamView.mountAll re-mounts on the fresh DOM (preserves
    // user-toggled layer + opacity state internally, see Phase 1).
    if (window.BallTrackerCamView) window.BallTrackerCamView.mountAll();
    // Push online status + RMS extras for every rendered cam (EXPECTED A/B
    // plus any extra cams that registered with non-A/B ids). Calibration
    // truth is derived inside the runtime from setMeta payload — don't
    // pass it here.
    if (window.BallTrackerCamView) {
      const onlineSet = new Set((state.devices || []).map(d => d.camera_id));
      const renderedCams = new Set(EXPECTED);
      for (const d of (state.devices || [])) renderedCams.add(d.camera_id);
      const autoLast = (state.auto_calibration && state.auto_calibration.last) || {};
      for (const cam of renderedCams) {
        window.BallTrackerCamView.setStatus(cam, { online: onlineSet.has(cam) });
        // Auto-cal homography fit RMS — surfaces "is this camera correctly
        // placed?" as a number badge, complements the visual overlay.
        // Always call setExtras (with null when reproj is missing) so the
        // runtime can drop a stale badge if calibration moved to a path
        // that doesn't produce reproj (e.g. ChArUco upload after auto-cal).
        const reproj = autoLast[cam] && autoLast[cam].result && autoLast[cam].result.reprojection_px;
        window.BallTrackerCamView.setExtras(cam, {
          rms_px: typeof reproj === 'number' && isFinite(reproj) ? reproj : null,
        });
      }
    }
  }

  const MODE_LABELS = { camera_only: 'Camera-only' };
  const PATH_LABELS = {
    live: ['Live stream', 'iOS → WS'],
    server_post: ['Server post-pass', 'PyAV + OpenCV'],
  };

  function fmtElapsed(ms) {
    if (!ms || ms < 0) return '00:00.0';
    const total = ms / 1000;
    const m = Math.floor(total / 60);
    const s = Math.floor(total % 60);
    const ds = Math.floor((total * 10) % 10);
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${ds}`;
  }

