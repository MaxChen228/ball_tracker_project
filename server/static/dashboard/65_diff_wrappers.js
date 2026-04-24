// === current* state + render diff wrappers ===

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentCaptureMode = 'camera_only';
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
  let _lastSessKey = null;
  let _lastNavKey = null;
  let _lastEvKey = null;

  const _origRenderDevices = renderDevices;
  renderDevices = function(state) {
    const key = JSON.stringify({
      devices: (state.devices || []).map(d => ({
        id: d.camera_id,
        ts: d.time_synced,
        seen: d.last_seen_at,
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

  const _origRenderSession = renderSession;
  renderSession = function(state) {
    const s = state.session;
    const sessKey = JSON.stringify({
      armed: !!(s && s.armed), id: s && s.id, mode: s && s.mode,
      capture_mode: state.capture_mode,
      paths: state.default_paths || [],
      live_session: state.live_session || null,
    });
    const cooldownBucket = Number(state.sync_cooldown_remaining_s || 0) > 0 ? 1 : 0;
    const navKey = JSON.stringify({
      online: (state.devices || []).length,
      cal: (state.calibrations || []).length,
      armed: !!(s && s.armed), id: s && s.id,
      syncing: !!state.sync, cooling: cooldownBucket,
    });
    if (sessKey === _lastSessKey && navKey === _lastNavKey) return;
    _lastSessKey = sessKey;
    _lastNavKey = navKey;
    _origRenderSession(state);
  };

  const _origRenderEvents = renderEvents;
  renderEvents = function(events) {
    const key = JSON.stringify((events || []).map(e => ({
      id: e.session_id, status: e.status, n: e.n_triangulated,
      p: e.processing_state,
      srv: (e.path_status || {}).server_post || '-',
    })));
    if (key === _lastEvKey) return;
    _lastEvKey = key;
    _origRenderEvents(events);
  };
