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
      if (typeof renderStrikeZone === 'function') renderStrikeZone(s);
    } catch (e) { /* silent retry next tick */ }
  }

  // Skip the per-camera-marker rebuild when the JSON signature of the
  // camera tuple list hasn't changed. Cameras are static after
  // auto-cal until the operator re-runs, so this short-circuits the
  // disposeObject / new-Group churn on every 5 s tick.
  let lastCameraSig = null;
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
      const cams = (payload.scene || {}).cameras || [];
      if (window.BallTrackerCamView) {
        const live = new Set();
        for (const c of cams) {
          window.BallTrackerCamView.setMeta(c.camera_id, c);
          live.add(c.camera_id);
        }
        for (const cam of window.BallTrackerCamView.listCams()) {
          if (!live.has(cam)) window.BallTrackerCamView.setMeta(cam, null);
        }
      }
      // Push camera markers into the Three.js scene if mounted.
      // Re-applying when nothing changed costs ~0 (same Group rebuild
      // for 0-2 cameras) but the signature short-circuit avoids the
      // disposeObject/rebuild every 5 s when nothing's changed.
      if (window.BallTrackerDashboardScene) {
        const sig = JSON.stringify(cams.map(c => [
          c.camera_id, c.center_world, c.axis_forward_world, c.axis_right_world, c.axis_up_world,
        ]));
        if (sig !== lastCameraSig) {
          lastCameraSig = sig;
          window.BallTrackerDashboardScene.applyCameras(cams);
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
