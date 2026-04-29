// === current* state + render diff wrappers ===

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentPreviewRequested = {};
  let currentSyncCommands = {};
  let currentCalibrationLastTs = {};
  let currentAutoCalibration = { active: {}, last: {} };
  let currentEventsBucket = 'active';
  const pendingPreviewMutations = new Set();

  // Keys used to skip re-renders when nothing changed. We compare serialised
  // state data rather than innerHTML strings because the browser re-serialises
  // HTML differently from the raw template literals we build.
  let _lastDevKey = null;
  let _lastEvKey = null;

  const _origRenderDevices = renderDevices;
  renderDevices = function(state) {
    const key = JSON.stringify({
      devices: (state.devices || []).map(d => ({
        id: d.camera_id,
        ts: d.time_synced,
        // last_seen_at is NOT in the key. It changes on every 1 Hz
        // heartbeat but doesn't affect anything renderDevices paints
        // (no "X s ago" label here). Including it forced a full
        // innerHTML rebuild every second, flickering hover state on
        // the PREVIEW / Auto-cal buttons.
        ws: d.ws_connected,
        // Round battery to 5% buckets so tiny-wobble heartbeats don't
        // repaint the whole devices card every second.
        batt: (typeof d.battery_level === 'number') ? Math.round(d.battery_level * 20) : null,
        bstate: d.battery_state || null,
      })),
      calibrations: (state.calibrations || []).slice().sort(),
      preview: state.preview_requested || {},
      preview_pending: [...(state.preview_pending || [])].sort(),
      last_ts: state.calibration_last_ts || {},
      sync_pending: Object.keys(state.sync_commands || {}).sort(),
      auto_calibration: state.auto_calibration || { active: {}, last: {} },
    });
    if (key === _lastDevKey) return;
    _lastDevKey = key;
    _origRenderDevices(state);
  };

  // renderSession now does surgical DOM patches that are idempotent and
  // cheap; no need for a coarse JSON-key short-circuit in front of it.
  // The previous wrapper's sessKey also lacked readiness fields, so a
  // device flipping time_synced left the Arm button stale until something
  // unrelated invalidated the key.

  const _origRenderEvents = renderEvents;
  renderEvents = function(events) {
    // Full hash — every field renderEvents reads must contribute,
    // otherwise the skip-path leaves the row stale (e.g. n_ball_frames_
    // by_path flipping from empty to full on a live-session complete
    // would've been invisible under the previous 4-key digest).
    const key = JSON.stringify((events || []).map(e => {
      const pbc = e.n_ball_frames_by_path || {};
      const pbcStr = Object.keys(pbc).sort().map(p => {
        const cams = pbc[p] || {};
        return p + ':' + Object.keys(cams).sort().map(c => c + '=' + cams[c]).join(',');
      }).join('|');
      const ps = e.path_status || {};
      return {
        id: e.session_id, status: e.status, n: e.n_triangulated,
        p: e.processing_state,
        pl: ps.live || '-', srv: ps.server_post || '-',
        st: e.server_post_ts || null,
        pc: pbcStr,
        d: e.duration_s != null ? Number(e.duration_s).toFixed(2) : null,
      };
    }));
    if (key === _lastEvKey) return;
    _lastEvKey = key;
    _origRenderEvents(events);
  };
