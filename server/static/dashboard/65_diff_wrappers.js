// === current* state + render diff wrappers ===

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentPreviewRequested = {};
  let currentSyncCommands = {};
  let currentCalibrationLastTs = {};
  let currentAutoCalibration = { active: {}, last: {} };
  let currentCalibrationLastSolves = {};
  let currentKnownMarkerIds = { plate: [], extended: [] };
  let currentEventsBucket = 'active';
  const pendingPreviewMutations = new Set();

  // renderDevices runs on every state tick — phase 3 made it surgical
  // (per-row field patching that preserves button DOM nodes), so the
  // diff-key short-circuit is no longer needed: redundant patches are
  // idempotent and dirt cheap. Removing the key also drops the
  // _lastDevKey = null invalidation noise across the live-stream / form
  // / tick callers. renderEvents still uses its own diff cache.
  let _lastEvKey = null;

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
