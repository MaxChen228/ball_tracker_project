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

  // Surgical render: full innerHTML on first paint or when the cam set
  // changes; field-level patching on subsequent ticks so button DOM
  // nodes survive (preserving :hover state across periodic state ticks).
  // Hover flicker root cause: each `innerHTML = ` destroys every button,
  // and the browser's :hover doesn't re-fire until the next mousemove.
  let _lastCamSetKey = null;

  function fmtAgeShort(s) {
    if (s < 60) return Math.floor(s) + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function hhmm(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 5);
  }
  function ageTxt(ts) {
    if (!ts) return '';
    const s = Math.max(0, Date.now() / 1000 - ts);
    return fmtAgeShort(s);
  }

  function _calPanelHTML(isCal, lastSolve, knownMarkers) {
    const parts = [];
    // 1. Status header
    if (isCal && lastSolve) {
      const age = (Date.now() / 1000) - Number(lastSolve.solved_at);
      parts.push('<div class="cal-status calibrated">CALIBRATED · ' +
                 esc(fmtAgeShort(age)) + '</div>');
    } else if (isCal) {
      parts.push('<div class="cal-status calibrated">CALIBRATED</div>');
    } else {
      parts.push('<div class="cal-status uncalibrated">NOT CALIBRATED</div>');
    }
    // 2. Last-solve summary
    if (lastSolve) {
      const lsIds = Array.isArray(lastSolve.marker_ids) ? lastSolve.marker_ids : [];
      const lsReproj = (typeof lastSolve.reproj_px === 'number') ? lastSolve.reproj_px : null;
      const lsSolver = lastSolve.solver || '?';
      const lsExt = Number(lastSolve.n_extended_used || 0);
      const lsTotal = lsIds.length;
      const lsPlate = lsTotal - lsExt;
      let breakdown = lsPlate + ' plate';
      if (lsExt > 0) breakdown += ' + ' + lsExt + ' ext';
      parts.push('<div class="cal-line last-solve"><span class="cal-line-label">last</span>' +
                 '<span class="cal-line-value">' + lsTotal + ' markers (' +
                 esc(breakdown) + ') · ' + esc(lsSolver) + '</span></div>');
      const metaParts = [];
      if (typeof lsReproj === 'number') {
        metaParts.push('<span class="reproj-badge" title="reprojection error vs 20 px hard limit">' +
                       'reproj <strong>' + lsReproj.toFixed(1) + '</strong> / 20 px</span>');
      }
      const dPos = lastSolve.delta_position_cm;
      const dAng = lastSolve.delta_angle_deg;
      if (typeof dPos === 'number' && typeof dAng === 'number') {
        metaParts.push('<span class="cal-delta" title="movement vs previous calibration">Δ <strong>' +
                       dPos.toFixed(1) + '</strong> cm / <strong>' + dAng.toFixed(2) + '</strong>°</span>');
      }
      if (metaParts.length > 0) {
        parts.push('<div class="cal-meta">' + metaParts.join('') + '</div>');
      }
    }
    // 3. Marker coverage
    const platePool = Array.isArray(knownMarkers.plate) ? knownMarkers.plate : [];
    const extPool = Array.isArray(knownMarkers.extended) ? knownMarkers.extended : [];
    if (platePool.length > 0 || extPool.length > 0) {
      const lastIds = new Set((lastSolve && lastSolve.marker_ids) || []);
      function chipFor(mid, kind) {
        const cls = lastIds.has(mid) ? 'used' : 'missing';
        return '<span class="marker-chip ' + cls + ' ' + kind +
               '" title="' + kind + ' marker ' + mid + ' · ' + cls + '">' +
               mid + '</span>';
      }
      const plateChips = platePool.map(m => chipFor(m, 'plate')).join('');
      let coverHTML = '<div class="marker-coverage">' +
        '<div class="marker-row"><span class="marker-row-label">PLATE</span>' +
        plateChips + '</div>';
      if (extPool.length > 0) {
        const extChips = extPool.map(m => chipFor(m, 'extended')).join('');
        coverHTML += '<div class="marker-row"><span class="marker-row-label">EXT</span>' +
                     extChips + '</div>';
      }
      coverHTML += '</div>';
      parts.push(coverHTML);
    }
    return parts.join('');
  }

  // Build a full row's HTML used for first paint / cam-set rebuild.
  function _buildRowHTML(cam, fields) {
    const { online, deviceRecord, isCal, previewOn, previewBusy, autoRun,
            autoLast, syncDot, syncLabel, calDot, calLabel,
            autoDot, autoLabel, syncLedCls, syncId, shortSid,
            previewDisabled, autoCalDisabled, calBtnLabel,
            initialSrc, lastSolve, knownMarkers } = fields;
    const previewBtnLabel = previewBusy
      ? (previewOn ? 'PREVIEW ON…' : 'PREVIEW…')
      : (previewOn ? 'PREVIEW ON' : 'PREVIEW');
    const previewBtn = `<button type="button" class="btn small preview-btn${previewOn ? ' active' : ''}" ` +
      `data-preview-cam="${esc(cam)}" data-preview-enabled="${previewOn ? 1 : 0}" ` +
      `${previewDisabled ? 'disabled' : ''}>${previewBtnLabel}</button>`;
    const autoCalBtn = `<button type="button" class="btn small" data-auto-cal="${esc(cam)}" ${autoCalDisabled ? 'disabled' : ''}>` +
      `${esc(calBtnLabel)}</button>`;
    const autoLogBtn = (autoLast && autoLast.status === 'failed')
      ? `<button type="button" class="btn small secondary" data-auto-cal-log="${esc(cam)}" title="Copy full auto-cal log to clipboard for debugging">Copy log</button>`
      : '';
    const calPanel = '<div class="cal-panel" data-cam="' + esc(cam) + '">' +
                     _calPanelHTML(isCal, lastSolve, knownMarkers) + '</div>';
    const camViewSrc = previewOn ? initialSrc : '';
    const imgTag = previewOn
      ? `<img data-cam-img="${esc(cam)}" src="${camViewSrc}" alt="preview ${esc(cam)}">`
      : '';
    const camViewBlock =
      `<div class="cam-view${previewOn ? '' : ' is-offline'}" data-cam-view="${esc(cam)}" ` +
      `data-layers="plate,axes" data-layers-on="plate" data-default-opacity="70">` +
      `${imgTag}` +
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
    const syncIdTxt = (fields.timeSynced && syncId)
      ? `<span class="sync-id-chip" title="${esc(syncId)}">·${esc(shortSid)}</span>`
      : '';
    return `
      <div class="device" data-cam-id="${esc(cam)}">
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
        ${calPanel}
        <div class="device-actions">${previewBtn}${autoCalBtn}${autoLogBtn}</div>
        ${camViewBlock}
      </div>`;
  }

  // Patch one cam's row in place. Buttons keep their DOM nodes so :hover
  // survives the tick. cal-panel + .sub + .chip-col use innerHTML
  // (no buttons inside). Cam-view <img> is added/removed surgically.
  function _patchRow(rowEl, cam, fields) {
    const { online, deviceRecord, isCal, previewOn, previewBusy, autoLast,
            syncDot, syncLabel, calDot, calLabel,
            autoDot, autoLabel, syncLedCls, syncId, shortSid, timeSynced,
            previewDisabled, autoCalDisabled, calBtnLabel,
            initialSrc, lastSolve, knownMarkers } = fields;

    const led = rowEl.querySelector('.sync-led');
    if (led) {
      led.className = 'sync-led ' + syncLedCls;
      led.setAttribute('title', 'time sync · ' + syncLabel);
    }
    const sub = rowEl.querySelector('.sub');
    if (sub) {
      const syncIdTxt = (timeSynced && syncId)
        ? `<span class="sync-id-chip" title="${esc(syncId)}">·${esc(shortSid)}</span>`
        : '';
      sub.innerHTML =
        `<span class="item ${syncDot}"><span class="dot ${syncDot}"></span>time sync · ${esc(syncLabel)}${syncIdTxt}</span>` +
        `<span class="item ${calDot}"><span class="dot ${calDot}"></span>pose · ${esc(calLabel)}</span>` +
        `<span class="item ${autoDot}" title="${esc(autoLabel)}"><span class="dot ${autoDot}"></span>auto-cal · ${esc(autoLabel)}</span>`;
    }
    const chipCol = rowEl.querySelector('.chip-col');
    if (chipCol) {
      chipCol.innerHTML = batteryChip(deviceRecord, online) + statusChip(cam, online, isCal);
    }
    const calPanel = rowEl.querySelector('.cal-panel');
    if (calPanel) {
      calPanel.innerHTML = _calPanelHTML(isCal, lastSolve, knownMarkers);
    }
    // Buttons: patch attributes / text in place, preserve node identity.
    const previewBtn = rowEl.querySelector('button[data-preview-cam]');
    if (previewBtn) {
      previewBtn.classList.toggle('active', previewOn);
      previewBtn.dataset.previewEnabled = previewOn ? '1' : '0';
      previewBtn.disabled = !!previewDisabled;
      previewBtn.textContent = previewBusy
        ? (previewOn ? 'PREVIEW ON…' : 'PREVIEW…')
        : (previewOn ? 'PREVIEW ON' : 'PREVIEW');
    }
    const autoCalBtn = rowEl.querySelector('button[data-auto-cal]');
    if (autoCalBtn) {
      autoCalBtn.disabled = !!autoCalDisabled;
      autoCalBtn.textContent = calBtnLabel;
    }
    // Auto-log button appears/disappears based on autoLast.status. If the
    // node exists but should be gone, drop it; if missing but should be
    // present, append it after autoCalBtn.
    const actions = rowEl.querySelector('.device-actions');
    const wantLog = !!(autoLast && autoLast.status === 'failed');
    let logBtn = actions && actions.querySelector('button[data-auto-cal-log]');
    if (wantLog && !logBtn && actions) {
      logBtn = document.createElement('button');
      logBtn.type = 'button';
      logBtn.className = 'btn small secondary';
      logBtn.dataset.autoCalLog = cam;
      logBtn.title = 'Copy full auto-cal log to clipboard for debugging';
      logBtn.textContent = 'Copy log';
      actions.appendChild(logBtn);
    } else if (!wantLog && logBtn) {
      logBtn.remove();
    }
    // Cam-view <img>: drop / restore based on previewOn. Empty src would
    // resolve to the document URL in some browsers (broken-icon flash);
    // also gates the polling loop in cam_view_ui's startPreviewPolling.
    const camView = rowEl.querySelector('.cam-view');
    if (camView) {
      camView.classList.toggle('is-offline', !previewOn);
      let img = camView.querySelector('img[data-cam-img]');
      if (previewOn) {
        if (!img) {
          img = document.createElement('img');
          img.dataset.camImg = cam;
          img.alt = 'preview ' + cam;
          img.src = initialSrc;
          // Keep <img> as the FIRST child so canvas overlay stays on top.
          camView.insertBefore(img, camView.firstChild);
        } else if (initialSrc && img.src !== initialSrc) {
          img.src = initialSrc;
        }
      } else if (img) {
        img.remove();
      }
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
    const lastSolves = state.calibration_last_solves || {};
    const knownMarkers = state.known_marker_ids || { plate: [], extended: [] };

    function fieldsFor(cam) {
      const deviceRecord = devByCam.get(cam) || null;
      const online = !!deviceRecord;
      const timeSynced = !!(deviceRecord && deviceRecord.time_synced);
      const pending = !!syncPending[cam];
      const isCal = calibrated.has(cam);
      const previewOn = !!previewReq[cam];
      const previewBusy = previewPending.has(cam);
      const lastTs = calLastTs[cam];
      const autoRun = autoCalActive[cam] || null;
      const autoLast = autoCalLast[cam] || null;
      const lastSolve = lastSolves[cam] || null;
      const calDot = isCal ? 'ok' : (online ? 'warn' : 'bad');
      const syncDot = !online ? 'bad' : (pending ? 'warn' : (timeSynced ? 'ok' : 'warn'));
      const autoDot = autoRun ? 'warn'
                    : (autoLast && autoLast.status === 'completed' ? 'ok'
                    : (autoLast && autoLast.status === 'failed' ? 'bad' : (online ? 'warn' : 'bad')));
      const syncLabel = !online ? 'offline' : (pending ? 'pending…' : (timeSynced ? 'synced' : 'not synced'));
      const calLabel = (isCal && lastTs) ? ('last ' + hhmm(lastTs) + ' (' + ageTxt(lastTs) + ')')
                     : (!online ? 'offline' : (isCal ? 'calibrated' : 'pending'));
      const autoLabel = autoCalLabel(autoRun, autoLast, online);
      const syncLedCls = !online ? 'offline'
                        : pending ? 'listening'
                        : timeSynced ? 'synced'
                        : 'waiting';
      const syncId = deviceRecord && deviceRecord.time_sync_id;
      const shortSid = syncId ? (syncId.length > 8 ? syncId.slice(-6) : syncId.replace(/^sy_/, '')) : '';
      const previewDisabled = previewBusy || !online;
      const autoCalDisabled = !!autoRun || !online;
      const calBtnLabel = autoRun
        ? autoCalButtonLabel(autoRun)
        : (isCal ? 'Recalibrate' : 'Calibrate');
      // Cache-busted preview src reuses the per-cam memo so a state tick
      // doesn't force a fresh fetch every time. tickPreviewImages owns
      // the actual refresh cadence.
      const prevState = _previewRenderState.get(cam);
      const nowMs = Date.now();
      let initialSrc = '';
      if (previewOn) {
        if (!prevState || prevState.on !== true || !prevState.src
            || (nowMs - (prevState.t || 0) > _PREVIEW_REFRESH_MIN_MS)) {
          initialSrc = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + nowMs;
          _previewRenderState.set(cam, { on: true, src: initialSrc, t: nowMs });
        } else {
          initialSrc = prevState.src;
        }
      } else {
        _previewRenderState.set(cam, { on: false, src: '', t: nowMs });
      }
      return {
        online, deviceRecord, timeSynced, isCal, previewOn, previewBusy,
        autoRun, autoLast, lastSolve, calDot, syncDot, autoDot,
        syncLabel, calLabel, autoLabel, syncLedCls, syncId, shortSid,
        previewDisabled, autoCalDisabled, calBtnLabel, initialSrc,
        knownMarkers,
      };
    }

    const expectedCams = EXPECTED.slice();
    const extraCams = (state.devices || [])
      .map(d => d.camera_id)
      .filter(c => !EXPECTED.includes(c));
    const camList = expectedCams.concat(extraCams);
    const camSetKey = camList.join(',');

    // First-tick fast path: if the SSR DOM already has rows matching
    // the current cam set (each .device tagged data-cam-id), we adopt
    // them and go straight to surgical patch — avoiding the SSR-then-
    // innerHTML-rebuild flash. Only fires when _lastCamSetKey is null
    // AND every cam has a corresponding SSR row.
    if (_lastCamSetKey === null) {
      const ssrCovers = camList.every(
        c => devicesBox.querySelector('.device[data-cam-id="' + c + '"]')
      );
      if (ssrCovers) _lastCamSetKey = camSetKey;
    }

    if (_lastCamSetKey !== camSetKey) {
      // Cam set changed (or first paint with no usable SSR) — full
      // innerHTML build. Subsequent ticks go through _patchRow and
      // never rebuild button DOM, so :hover survives.
      const rows = camList.map(cam => _buildRowHTML(cam, fieldsFor(cam))).join('');
      devicesBox.innerHTML = `<div class="devices-grid">${rows}</div>`;
      _lastCamSetKey = camSetKey;
      if (window.BallTrackerCamView) window.BallTrackerCamView.mountAll();
    } else {
      for (const cam of camList) {
        const rowEl = devicesBox.querySelector('.device[data-cam-id="' + cam + '"]');
        if (!rowEl) continue;
        _patchRow(rowEl, cam, fieldsFor(cam));
      }
    }
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

