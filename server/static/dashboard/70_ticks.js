// === tickStatus / tickCalibration / tickEvents ===

  async function tickStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      // /status does not include calibrations; merge the last-known set so
      // the devices card shows "calibrated" chips between calibration ticks.
      s.calibrations = currentCalibrations || [];
      currentDevices = s.devices || [];
      currentSession = s.session || null;
      currentCaptureMode = s.capture_mode || 'camera_only';
      currentPreviewRequested = s.preview_requested || {};
      currentSyncCommands = s.sync_commands || {};
      currentAutoCalibration = s.auto_calibration || { active: {}, last: {} };
      renderDevices({
        devices: s.devices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs || {},
        auto_calibration: currentAutoCalibration,
      });
      renderSession(s);
      // Telemetry: record per-cam WS latency sampled from /status.
      // Server-side ws_latency_ms reflects the last heartbeat round-trip
      // per the DeviceSocketManager snapshot.
      const nowMs = Date.now();
      for (const dev of (s.devices || [])) {
        if (!dev || !dev.camera_id) continue;
        const lat = dev.ws_latency_ms;
        if (typeof lat !== 'number') continue;
        const arr = latencySamples[dev.camera_id] = latencySamples[dev.camera_id] || [];
        arr.push({ t_ms: nowMs, latency: lat });
        while (arr.length && nowMs - arr[0].t_ms > TELEMETRY_WINDOW_MS) arr.shift();
      }
    } catch (e) { /* silent retry next tick */ }
  }

  // ETag of the last basePlot we repainted from. Server computes a
  // sha1[:16] of the plot subtree in /calibration/state; we short-circuit
  // the client-side JSON.stringify over full Plotly trace data. Falls
  // back to the (inline) full-JSON digest when the server response
  // lacks plot_etag (older server build).
  let lastBasePlotEtag = null;
  async function tickCalibration() {
    try {
      const r = await fetch('/calibration/state', { cache: 'no-store' });
      if (!r.ok) return;
      const payload = await r.json();
      currentCalibrations = (payload.calibrations || []).map(c => c.camera_id);
      currentCalibrationLastTs = {};
      for (const c of (payload.calibrations || [])) {
        if (c.last_ts != null) currentCalibrationLastTs[c.camera_id] = c.last_ts;
      }
      renderDevices({
        devices: currentDevices || [],
        calibrations: currentCalibrations,
        preview_requested: currentPreviewRequested,
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs,
        auto_calibration: currentAutoCalibration,
      });
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations, capture_mode: currentCaptureMode });
      // Update per-camera virt reprojection metadata from scene.cameras
      // (carries fx/fy/cx/cy/R_wc/t_wc/distortion/dims). Push to both
      // the legacy virtCamMeta (still consumed by setup/markers until
      // those phases land) AND the new shared BallTrackerCamView so
      // the dashboard's merged cam-view picks up calibration changes.
      virtCamMeta.clear();
      for (const c of ((payload.scene || {}).cameras || [])) {
        virtCamMeta.set(c.camera_id, c);
        if (window.BallTrackerCamView) window.BallTrackerCamView.setMeta(c.camera_id, c);
      }
      // Cameras that were previously set but are absent now — clear so
      // the cam-view drops back to "uncalibrated" badge.
      if (window.BallTrackerCamView) {
        const live = new Set(((payload.scene || {}).cameras || []).map(c => c.camera_id));
        for (const cam of (window.BallTrackerCamView._internal.camMeta.keys())) {
          if (!live.has(cam)) window.BallTrackerCamView.setMeta(cam, null);
        }
      }
      redrawAllVirtCanvases();
      redrawAllPreviewPlateOverlays();
      // Main 3D canvas lives only on `/`. Don't gate the metadata update
      // above on sceneRoot — `/setup` still needs virt canvases drawn.
      if (payload.plot && sceneRoot && window.Plotly) {
        const etag = payload.plot_etag
          || ('inline:' + JSON.stringify(payload.plot).length);
        if (etag !== lastBasePlotEtag || basePlot === null) {
          lastBasePlotEtag = etag;
          basePlot = payload.plot;
          repaintCanvas();
        }
      }
    } catch (e) { /* silent */ }
  }

  let currentEvents = [];
  async function tickEvents() {
    try {
      const r = await fetch(`/events?bucket=${encodeURIComponent(currentEventsBucket)}`, { cache: 'no-store' });
      if (!r.ok) return;
      const events = await r.json();
      currentEvents = events;
      // Prune selection for sessions the user deleted server-side so the
      // canvas doesn't keep painting a phantom trajectory whose checkbox
      // no longer exists.
      const liveIds = new Set(events.map(e => e.session_id));
      let pruned = false;
      for (const sid of [...selectedTrajIds]) {
        if (!liveIds.has(sid)) {
          selectedTrajIds.delete(sid);
          trajCache.delete(sid);
          pruned = true;
        }
      }
      if (pruned) { persistTrajSelection(); repaintCanvas(); }
      renderEvents(events);
    } catch (e) { /* silent */ }
  }
