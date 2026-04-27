// === sync LED + arm readiness + renderSession ===
//
// Surgical updates only. Earlier revisions rebuilt sessionBox.innerHTML on
// every tick — fine functionally, but each SSE device_heartbeat (1 Hz)
// invalidated the diff key in 86_live_stream.js, recreating the Arm /
// Stop button DOM nodes once a second. Anything the user was hovering
// flickered as the :hover state was reapplied to a fresh element. The
// skeleton below is built once; later calls only patch attributes /
// textContent so hover and focus survive.

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

  // Per-cam sync indicator. State derives from `state.devices[*]`:
  //   off     → no entry / offline
  //   waiting → online but no valid time-sync anchor
  //   synced  → holding a fresh anchor
  function syncLedState(state, cam) {
    const dev = ((state && state.devices) || []).find(d => d.camera_id === cam);
    if (!dev) return { cls: 'off', tip: cam + ': offline' };
    if (dev.time_synced) {
      const age = (typeof dev.time_sync_age_s === 'number') ? ' · ' + dev.time_sync_age_s.toFixed(0) + 's ago' : '';
      return { cls: 'synced', tip: cam + ': synced' + age };
    }
    return { cls: 'waiting', tip: cam + ': waiting' };
  }

  let sessionDom = null;
  // Slot for inline error feedback from a failed Arm POST. Cleared on
  // the next successful state change so it doesn't linger forever.
  let sessionArmError = '';

  function buildSessionSkeleton() {
    if (!sessionBox) return null;
    sessionBox.innerHTML = `
      <div class="session-head">
        <span class="chip" data-role="chip">idle</span>
        <span class="session-id" data-role="sid"></span>
      </div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm" data-role="arm-form">
          <button class="btn" type="submit" data-role="arm-btn">Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" data-role="stop-btn">Stop</button>
        </form>
        <span data-role="clear-slot"></span>
      </div>
      <div class="arm-gate" data-role="gate" hidden>
        <span class="gate-label" data-role="gate-label"></span>
        <span data-role="gate-text"></span>
      </div>
      <div class="arm-error" data-role="arm-error" hidden></div>
      <div class="card-subtitle">Time Sync</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sync/trigger">
          <button class="btn secondary" type="submit" data-role="sync-btn">Quick chirp</button>
        </form>
        <span class="sync-led" data-role="led-A">A</span>
        <span class="sync-led" data-role="led-B">B</span>
      </div>`;
    const $ = sel => sessionBox.querySelector(sel);
    sessionDom = {
      chip: $('[data-role=chip]'),
      sid: $('[data-role=sid]'),
      armForm: $('[data-role=arm-form]'),
      armBtn: $('[data-role=arm-btn]'),
      stopBtn: $('[data-role=stop-btn]'),
      clearSlot: $('[data-role=clear-slot]'),
      gate: $('[data-role=gate]'),
      gateLabel: $('[data-role=gate-label]'),
      gateText: $('[data-role=gate-text]'),
      armError: $('[data-role=arm-error]'),
      syncBtn: $('[data-role=sync-btn]'),
      ledA: $('[data-role=led-A]'),
      ledB: $('[data-role=led-B]'),
    };
    sessionDom.armForm.addEventListener('submit', onArmSubmit);
    return sessionDom;
  }

  // Intercept the Arm form submit so a 409 (not_ready_to_arm) renders
  // an inline blocker message instead of silently 303-ing back to /
  // and looking like the click did nothing.
  async function onArmSubmit(evt) {
    evt.preventDefault();
    sessionArmError = '';
    if (sessionDom && sessionDom.armBtn) sessionDom.armBtn.disabled = true;
    try {
      const r = await fetch('/sessions/arm', {
        method: 'POST',
        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
        body: '{}',
      });
      if (!r.ok) {
        let detail = null;
        try { detail = (await r.json()).detail; } catch (_) {}
        const blockers = (detail && detail.blockers) || [];
        sessionArmError = blockers.length
          ? `Arm blocked: ${blockers.join(', ')}`
          : `Arm failed (${r.status})`;
      }
    } catch (e) {
      sessionArmError = `Arm failed: ${e.message || 'network error'}`;
    }
    // Force a repaint so the error slot updates immediately; SSE
    // device_status will subsequently flip the chip if armed.
    if (typeof tickStatus === 'function') tickStatus();
    else if (sessionDom) renderSessionPatch(_lastSessionState || {});
  }

  // Last state passed to renderSession — used to repaint after an inline
  // arm error without waiting for the next /status tick.
  let _lastSessionState = null;

  function renderSessionPatch(state) {
    _lastSessionState = state;
    if (sessionBox && !sessionDom) buildSessionSkeleton();
    const dom = sessionDom;
    const s = state.session;
    const armed = !!(s && s.armed);
    const readiness = armReadiness(state);
    const canArm = !!(readiness && readiness.ready);
    const blockers = (readiness && readiness.blockers) || [];
    const warnings = (readiness && readiness.warnings) || [];
    currentDefaultPaths = state.default_paths || currentDefaultPaths || ['live'];
    currentLiveSession = state.live_session || currentLiveSession;

    if (dom) {
      // chip
      dom.chip.className = 'chip ' + (armed ? 'armed' : 'idle');
      dom.chip.textContent = armed ? 'armed' : 'idle';
      // session-id
      const sidText = (s && s.id) ? s.id : '';
      if (dom.sid.textContent !== sidText) dom.sid.textContent = sidText;
      // arm button
      const armDisabled = armed || !canArm;
      const armTitle = blockers.length
        ? blockers.join(', ')
        : (warnings.length ? warnings.join(', ') : 'Ready to record');
      if (dom.armBtn.disabled !== armDisabled) dom.armBtn.disabled = armDisabled;
      if (dom.armBtn.title !== armTitle) dom.armBtn.title = armTitle;
      // stop button
      if (dom.stopBtn.disabled !== !armed) dom.stopBtn.disabled = !armed;
      // clear button (only shown when an ended session is still cached)
      const wantClear = !armed && !!(s && s.id);
      const hasClear = !!dom.clearSlot.firstChild;
      if (wantClear && !hasClear) {
        dom.clearSlot.innerHTML = '<form class="inline" method="POST" action="/sessions/clear">'
          + '<button class="btn" type="submit">Clear</button></form>';
      } else if (!wantClear && hasClear) {
        dom.clearSlot.innerHTML = '';
      }
      // gate row (blockers > warnings > hidden)
      if (!armed && blockers.length) {
        dom.gate.hidden = false;
        if (dom.gateLabel.textContent !== 'Need:') dom.gateLabel.textContent = 'Need:';
        const txt = ' ' + blockers.join(', ');
        if (dom.gateText.textContent !== txt) dom.gateText.textContent = txt;
      } else if (!armed && warnings.length) {
        dom.gate.hidden = false;
        if (dom.gateLabel.textContent !== 'Mode:') dom.gateLabel.textContent = 'Mode:';
        const txt = ' ' + warnings.join(', ');
        if (dom.gateText.textContent !== txt) dom.gateText.textContent = txt;
      } else {
        if (!dom.gate.hidden) dom.gate.hidden = true;
      }
      // arm error slot
      if (sessionArmError) {
        dom.armError.hidden = false;
        if (dom.armError.textContent !== sessionArmError) dom.armError.textContent = sessionArmError;
      } else if (!dom.armError.hidden) {
        dom.armError.hidden = true;
        dom.armError.textContent = '';
      }
      // quick chirp button
      if (dom.syncBtn.disabled !== armed) dom.syncBtn.disabled = armed;
      // sync LEDs
      for (const cam of ['A', 'B']) {
        const led = cam === 'A' ? dom.ledA : dom.ledB;
        const { cls, tip } = syncLedState(state, cam);
        const klass = 'sync-led ' + cls;
        if (led.className !== klass) led.className = klass;
        if (led.title !== tip) led.title = tip;
      }
    }
    renderActiveSession(currentLiveSession);

    // Mirror live state into the shared app-header status strip.
    // /sync ships its own nav renderer (render_sync_client.py::renderNav)
    // with a fraction-format ("Sync 1/2"); both bundles load on /sync,
    // so writing here too means the two ticks fight every cycle and the
    // operator sees the strip flicker between formats. Skip on /sync —
    // dashboard / setup keep the single-value format we own.
    if (navStatus && pageMode !== 'sync') {
      const online = (state.devices || []).length;
      const usable = (readiness.calibrated_online_cameras || []).length;
      const syncedUsable = (readiness.synced_calibrated_online_cameras || []).length;
      const check = (label, value, ok) =>
        `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
      const html = `<div class="status-checks">${check('Devices', `${online}`, online >= 1)}${check('Cal', `${usable}`, usable >= 1)}${check('Sync', readiness.requires_time_sync ? `${syncedUsable}/${usable}` : 'single', !readiness.requires_time_sync || syncedUsable >= usable)}</div>`;
      if (navStatus.innerHTML !== html) navStatus.innerHTML = html;
    }

    // Successful arm — clear any stale inline error.
    if (armed && sessionArmError) {
      sessionArmError = '';
      if (dom && !dom.armError.hidden) {
        dom.armError.hidden = true;
        dom.armError.textContent = '';
      }
    }
  }

  function renderSession(state) {
    renderSessionPatch(state);
  }
