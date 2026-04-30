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
      currentPreviewRequested = s.preview_requested || {};
      currentSyncCommands = s.sync_commands || {};
      currentAutoCalibration = s.auto_calibration || { active: {}, last: {} };
      currentCalibrationLastSolves = s.calibration_last_solves || {};
      currentKnownMarkerIds = s.known_marker_ids || { plate: [], extended: [] };
      renderDevices({
        devices: s.devices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs || {},
        auto_calibration: currentAutoCalibration,
        calibration_last_solves: currentCalibrationLastSolves,
        known_marker_ids: currentKnownMarkerIds,
      });
      renderSession(s);
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
        calibration_last_solves: currentCalibrationLastSolves || {},
        known_marker_ids: currentKnownMarkerIds || { plate: [], extended: [] },
      });
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations });
      // Push per-camera reprojection metadata into BallTrackerCamView.
      // The runtime owns paint scheduling + clears absent cams to the
      // uncalibrated badge so the operator sees calibration drop-off
      // immediately on /, /setup, /markers.
      if (window.BallTrackerCamView) {
        const live = new Set();
        for (const c of ((payload.scene || {}).cameras || [])) {
          window.BallTrackerCamView.setMeta(c.camera_id, c);
          live.add(c.camera_id);
        }
        for (const cam of window.BallTrackerCamView.listCams()) {
          if (!live.has(cam)) window.BallTrackerCamView.setMeta(cam, null);
        }
      }
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
      // canvas doesn't keep painting a phantom trajectory whose row no
      // longer exists.
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
