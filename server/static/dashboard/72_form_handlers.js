// === submit + preview click handlers ===

  // Event-row actions intercept: fetch + single events-tick refresh so
  // the button state never bounces back to the previous value between
  // the POST and the next tickEvents round-trip.
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    const m = form.action && form.action.match(/\/sessions\/([^/]+)\/(trash|restore|delete|cancel_processing|resume_processing|run_server_post)$/);
    if (m) {
      e.preventDefault();
      const sid = m[1];
      const op = m[2];
      // Optimistic: flip the row's processing_state in-memory and
      // re-render BEFORE the fetch resolves, so the operator sees Cancel
      // (or Run srv reappear) within ~1 frame instead of waiting for the
      // polling tick. tickEvents() at the end reconciles real state, so
      // a fetch reject naturally rolls the optimistic mutation back.
      if (op === 'run_server_post' || op === 'cancel_processing') {
        const target = (currentEvents || []).find(ev => ev && ev.session_id === sid);
        if (target) {
          // Cancel mirrors what the server settles on (state_processing
          // emits 'canceled', not null). Setting null briefly would drop
          // the chip until the next polling tick and then flash 'canceled'
          // back in 5 s later; 'canceled' keeps the chip in place and lets
          // 60_events_render's button branch swap Cancel → Run srv since
          // its hide-condition only fires on {queued,processing}.
          target.processing_state = (op === 'run_server_post') ? 'queued' : 'canceled';
          _lastEvKey = null;
          renderEvents(currentEvents);
        }
      }
      try {
        await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' } });
      } catch (_) {}
      await tickEvents();
      return;
    }
    if (form.action && form.action.endsWith('/sync/trigger')) {
      // Quick chirp: dispatch the WS sync_command, then auto-play
      // /chirp.wav through this browser tab 500 ms later so the
      // operator doesn't have to fumble with a separate third device.
      // The Audio element MUST be constructed inside the gesture
      // (this submit handler) so Safari/Chrome count the later
      // setTimeout .play() as user-initiated.
      e.preventDefault();
      const btn = form.querySelector('button');
      if (btn) btn.disabled = true;
      const chirpAudio = new Audio('/chirp.wav');
      try {
        const resp = await fetch(form.action, {
          method: 'POST',
          headers: { 'Accept': 'application/json' },
        });
        if (resp.ok) {
          // 500 ms lets iOS receive the WS sync_command and spin up
          // the mic detector before the sweep starts; combined with
          // the WAV's 500 ms leading silence, there's ~1 s of slack
          // before the actual chirp sweep begins.
          setTimeout(() => {
            chirpAudio.play().catch(() => { /* autoplay blocked — silent */ });
          }, 500);
        }
      } catch (_) {}
      finally {
        // Re-enable shortly after; /status tick will reconcile real state.
        setTimeout(() => { if (btn) btn.disabled = false; }, 600);
      }
      return;
    }
    // (Mutual-sync kickoff lives on /sync now.)
  });

  // Live-preview toggle. Server is authoritative — click POSTs the
  // intent, the next /status tick reconciles. Previously we awaited
  // tickStatus inline which, under connection-pool saturation (preview
  // img poll + status poll + SSE), would hang the
  // finally block and leave the cam in pendingPreviewMutations — then
  // every subsequent click hit the `pendingPreviewMutations.has(cam)`
  // early-return and felt "stuck". Now: fire-and-forget. 4 s watchdog
  // guarantees pending clears even if the POST hangs.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-preview-cam]');
    if (!btn) return;
    if (btn.disabled) return;
    const cam = btn.dataset.previewCam;
    if (!cam || pendingPreviewMutations.has(cam)) return;
    const enabled = btn.dataset.previewEnabled !== '1';
    pendingPreviewMutations.add(cam);
    // Optimistic: flip currentPreviewRequested immediately so the next
    // renderDevices paints the final state. /status tick will reconcile.
    if (enabled) currentPreviewRequested[cam] = true;
    else delete currentPreviewRequested[cam];
    _lastDevKey = null;
    if (currentDevices !== null || currentCalibrations !== null) {
      renderDevices({
        devices: currentDevices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        preview_pending: [...pendingPreviewMutations],
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs || {},
        auto_calibration: currentAutoCalibration,
      });
    }
    const clearPending = () => {
      pendingPreviewMutations.delete(cam);
      _lastDevKey = null;
      if (currentDevices !== null || currentCalibrations !== null) {
        renderDevices({
          devices: currentDevices || [],
          calibrations: currentCalibrations || [],
          preview_requested: currentPreviewRequested,
          preview_pending: [...pendingPreviewMutations],
          sync_commands: currentSyncCommands,
          calibration_last_ts: currentCalibrationLastTs || {},
          auto_calibration: currentAutoCalibration,
        });
      }
    };
    const watchdog = setTimeout(clearPending, 4000);
    fetch('/camera/' + encodeURIComponent(cam) + '/preview_request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
      .catch(() => {})
      .finally(() => {
        clearTimeout(watchdog);
        clearPending();
        tickStatus();
      });
  });
