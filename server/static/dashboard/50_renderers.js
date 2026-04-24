// === esc + chips + renderDevices ===

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
      const initialSrc = previewOn
        ? ('/camera/' + encodeURIComponent(cam) + '/preview?t=' + Date.now())
        : '';
      const previewPanel = `<div class="preview-panel${previewOn ? '' : ' off'}" data-preview-panel="${esc(cam)}">` +
        `<img data-preview-img="${esc(cam)}" src="${initialSrc}" alt="preview ${esc(cam)}">` +
        `<svg class="plate-overlay" data-preview-overlay="${esc(cam)}" aria-hidden="true"><polygon></polygon></svg>` +
        `<div class="placeholder">${previewOn ? '…' : 'Preview off'}</div>` +
        `</div>`;
      const virtCell = `<div class="virt-cell" data-virt-cell="${esc(cam)}">` +
        `<canvas data-virt-canvas="${esc(cam)}"></canvas>` +
        `<div class="virt-label">VIRT · ${esc(cam)}</div>` +
        `<div class="placeholder">${isCal ? 'loading…' : 'not calibrated'}</div>` +
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
          ${previewPanel}
          ${virtCell}
        </div>`;
    }

    const rows = EXPECTED.map(cam => row(cam, devByCam.get(cam))).join('');
    const extras = (state.devices || [])
      .filter(d => !EXPECTED.includes(d.camera_id))
      .map(d => row(d.camera_id, d)).join('');
    devicesBox.innerHTML = `<div class="devices-grid">${rows + extras}</div>`;
    // The innerHTML rebuild above destroys any existing canvases inside
    // the virt cells and preview overlays — redraw them on the fresh DOM.
    if (typeof redrawAllVirtCanvases === 'function') redrawAllVirtCanvases();
    if (typeof redrawAllPreviewPlateOverlays === 'function') redrawAllPreviewPlateOverlays();
  }

  const MODE_LABELS = { camera_only: 'Camera-only' };
  const PATH_LABELS = {
    live: ['Live stream', 'iOS → WS'],
    server_post: ['Server post-pass', 'PyAV + OpenCV'],
  };

  // Instantaneous fps derived from the most recent pair of frame_count
  // samples. Returns 0 when <2 samples or the window is too short to be
  // meaningful. Keeps the sparkline-per-cam history bounded to 60 entries
  // (~60s at 1Hz frame_count emission) so arbitrary-long sessions don't
  // grow unbounded.
  const FPS_HISTORY_CAP = 60;
  function pushFrameSample(liveSession, cam, count) {
    liveSession.frame_samples = liveSession.frame_samples || { A: [], B: [] };
    const arr = liveSession.frame_samples[cam] = liveSession.frame_samples[cam] || [];
    const now = Date.now();
    const prev = arr.length ? arr[arr.length - 1] : null;
    arr.push({ t: now, count });
    if (arr.length > FPS_HISTORY_CAP) arr.shift();
    // fps from most recent two samples
    if (arr.length >= 2) {
      const a = arr[arr.length - 2];
      const b = arr[arr.length - 1];
      const dtS = Math.max(0.001, (b.t - a.t) / 1000);
      liveSession.frame_fps = liveSession.frame_fps || {};
      liveSession.frame_fps[cam] = Math.max(0, (b.count - a.count) / dtS);
    }
    return prev;
  }

  function drawSparkline(canvas, samples) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!samples || samples.length < 2) return;
    // Derive per-sample fps on the fly
    const fps = [];
    for (let i = 1; i < samples.length; i++) {
      const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
      fps.push((samples[i].count - samples[i - 1].count) / dtS);
    }
    const maxFps = Math.max(240, ...fps);  // keep 240 as visual cap
    ctx.strokeStyle = '#C0392B';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    fps.forEach((f, i) => {
      const x = (i / (fps.length - 1 || 1)) * W;
      const y = H - (f / maxFps) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function fmtElapsed(ms) {
    if (!ms || ms < 0) return '00:00.0';
    const total = ms / 1000;
    const m = Math.floor(total / 60);
    const s = Math.floor(total % 60);
    const ds = Math.floor((total * 10) % 10);
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${ds}`;
  }

  // Dashboard Session Monitor card was removed — the operator's only
  // during-stream concern is the live 3D canvas. fps/frame telemetry
  // still gets tracked on `currentLiveSession` (frame_samples +
  // frame_fps) via pushFrameSample so post-session consumers (e.g. the
  // viewer page, telemetry panel) have the data. This stub keeps the
  // legacy call sites from erroring.
  function renderActiveSession(_liveSession) {
    return;
  }
