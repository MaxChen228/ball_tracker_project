// === sync LED + arm readiness + renderSession ===

  // Per-cam sync indicator shown next to Quick chirp. States:
  //   off     → device not in registry (no heartbeat recently).
  //   waiting → device online but no valid time-sync anchor yet.
  //   synced  → cam is holding an anchor from a recent successful sync.
  // Reads directly off `state.devices[*].time_synced` since the server
  // owns that truth. `time_sync_age_s` tooltip so operator can tell how
  // fresh "synced" is.
  function renderSyncLed(state, cam) {
    const devs = (state && state.devices) || [];
    const dev = devs.find(d => d.camera_id === cam);
    let cls = 'off';
    let tip = cam + ': offline';
    if (dev) {
      if (dev.time_synced) {
        cls = 'synced';
        const age = (typeof dev.time_sync_age_s === 'number')
          ? ' · ' + dev.time_sync_age_s.toFixed(0) + 's ago' : '';
        tip = cam + ': synced' + age;
      } else {
        cls = 'waiting';
        tip = cam + ': waiting';
      }
    }
    return `<span class="sync-led ${cls}" title="${esc(tip)}">${esc(cam)}</span>`;
  }

  function armReadiness(state) {
    if (state && state.arm_readiness) return state.arm_readiness;
    const devices = (state && state.devices) || [];
    const calibrations = new Set((state && state.calibrations) || []);
    const online = devices.map(d => String(d.camera_id)).filter(Boolean);
    const synced = new Set(devices.filter(d => d && d.time_synced).map(d => String(d.camera_id)));
    const usable = online.filter(cam => calibrations.has(cam)).sort();
    const uncalibrated = online.filter(cam => !calibrations.has(cam)).sort();
    const blockers = [];
    const warnings = [];
    if (!online.length) {
      blockers.push('no camera online');
    } else if (uncalibrated.length) {
      uncalibrated.forEach(cam => blockers.push(`${cam} not calibrated`));
    } else if (usable.length >= 2) {
      usable.forEach(cam => { if (!synced.has(cam)) blockers.push(`${cam} not time-synced`); });
    } else {
      warnings.push(`single-camera session (${usable[0]}); no triangulation`);
    }
    return {
      ready: blockers.length === 0,
      blockers,
      warnings,
      online_cameras: online.sort(),
      calibrated_online_cameras: usable,
      synced_calibrated_online_cameras: usable.filter(cam => synced.has(cam)),
      requires_time_sync: usable.length >= 2,
      mode: usable.length >= 2 ? 'stereo' : (usable.length ? 'single_camera' : 'blocked'),
    };
  }

  function renderSession(state) {
    if (!sessionBox) { /* nav-only render still executes below */ }
    const s = state.session;
    const armed = !!(s && s.armed);
    const readiness = armReadiness(state);
    const canArm = !!(readiness && readiness.ready);
    const blockers = (readiness && readiness.blockers) || [];
    const warnings = (readiness && readiness.warnings) || [];
    currentDefaultPaths = state.default_paths || currentDefaultPaths || ['live'];
    currentLiveSession = state.live_session || currentLiveSession;
    const chip = armed ? `<span class="chip armed">armed</span>` : `<span class="chip idle">idle</span>`;
    const sid = s && s.id ? `<span class="session-id">${esc(s.id)}</span>` : '';
    const clearBtn = (!armed && s && s.id)
      ? `<form class="inline" method="POST" action="/sessions/clear">
           <button class="btn" type="submit">Clear</button>
         </form>`
      : '';
    const gateRow = (!armed && blockers.length)
      ? `<div class="arm-gate"><span class="gate-label">Need:</span> ${esc(blockers.join(', '))}</div>`
      : ((!armed && warnings.length)
        ? `<div class="arm-gate"><span class="gate-label">Mode:</span> ${esc(warnings.join(', '))}</div>`
        : '');
    const sessHtml = `
      <div class="session-head">${chip}${sid}</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm">
          <button class="btn" type="submit" ${armed || !canArm ? 'disabled' : ''}>Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" ${armed ? '' : 'disabled'}>Stop</button>
        </form>
        ${clearBtn}
      </div>
      ${gateRow}
      <div class="card-subtitle">Time Sync</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sync/trigger">
          <button class="btn secondary" type="submit" ${armed ? 'disabled' : ''}>Quick chirp</button>
        </form>
        ${renderSyncLed(state, 'A')}
        ${renderSyncLed(state, 'B')}
      </div>`;
    if (sessionBox) sessionBox.innerHTML = sessHtml;
    renderActiveSession(currentLiveSession);

    // Mirror live state into the shared app-header status strip.
    // Three chips only — devices / cal / sync — matching
    // render_shared.py::_render_nav_status. The editorial badge +
    // headline were redundant with the per-device rows downstream.
    if (navStatus) {
      const online = (state.devices || []).length;
      const usable = (readiness.calibrated_online_cameras || []).length;
      const syncedUsable = (readiness.synced_calibrated_online_cameras || []).length;
      const check = (label, value, ok) =>
        `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
      navStatus.innerHTML = `
        <div class="status-checks">
          ${check('Devices', `${online}`, online >= 1)}
          ${check('Cal', `${usable}`, usable >= 1)}
          ${check('Sync', readiness.requires_time_sync ? `${syncedUsable}/${usable}` : 'single', !readiness.requires_time_sync || syncedUsable >= usable)}
        </div>`;
    }
  }
