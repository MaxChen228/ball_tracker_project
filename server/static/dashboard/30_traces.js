// === pitch trace assemblers (Three.js handoff) ===
//
// Phase 2 of the 3D migration: this file used to build Plotly trace
// dicts (`pitchTracesFor`, `liveTraces`, `ghostTrace`). The Three.js
// scene now owns mesh/line construction — see
// `server/static/threejs/dashboard_layers.js`. What remains here:
//
//   - constants used elsewhere in the IIFE (palette names referenced
//     by other dashboard JS files for the events row swatches)
//   - thin wrappers that hand the result of the legacy `trajCache`
//     Map straight to the Three.js layer module without trace-dict
//     translation
//
// `BallTrackerDashboardScene` is the new ESM-loaded layer controller;
// when the IIFE runs it may be `undefined` for the first ~1 s while
// the module loads, so every call goes through `_layers()` which
// returns `null` when the runtime isn't mounted yet.

  const _OVL = window.BallTrackerOverlays;
  const strikeZoneVisible = _OVL.strikeZoneVisible;
  const setStrikeZoneVisible = _OVL.setStrikeZoneVisible;
  // isStrikeZoneTrace is unused under Three.js — the strike-zone
  // visibility toggle goes through `scene.setLayerVisible('strike_zone', ...)`
  // which the runtime owns, not via filtering Plotly traces.

  // Per-segment palette referenced by 60_events_render.js's swatch
  // colour picker for the trajectory list — keeps event-row swatch
  // colour in lockstep with the in-scene fit curve colour.
  const _SEG_PALETTE = [
    '#E45756', '#4C78A8', '#54A24B', '#F58518',
    '#B279A2', '#72B7B2', '#FF9DA6', '#9D755D',
  ];

  function _layers() {
    return window.BallTrackerDashboardScene || null;
  }

  // Legacy IIFE callsites still reference `pitchTracesFor` for
  // backwards compatibility within the bundle's own code paths;
  // route them through the Three.js layer module. Returns nothing
  // useful (no traces to compose) — the side effect is the layer
  // update.
  function pitchTracesFor(sid, result) {
    const layers = _layers();
    if (!layers) return [];
    layers.applyFit(sid, result);
    return [];
  }

  function liveTraces() {
    const layers = _layers();
    if (!layers) return [];
    if (currentLiveSession && currentLiveSession.session_id) {
      const sid = currentLiveSession.session_id;
      const points = livePointStore.get(sid) || [];
      const raysByCam = liveRayStore.get(sid) || new Map();
      layers.applyLive({ session: currentLiveSession, points, raysByCam });
    } else {
      layers.clearLive();
    }
    return [];
  }

  function pushLiveRay(sid, cam, ray) {
    let byCam = liveRayStore.get(sid);
    if (!byCam) {
      byCam = new Map();
      liveRayStore.set(sid, byCam);
    }
    const arr = byCam.get(cam) || [];
    arr.push(ray);
    byCam.set(cam, arr);
  }

  function scheduleLiveRayRepaint() {
    if (liveRayPaintPending) return;
    liveRayPaintPending = true;
    requestAnimationFrame(() => {
      liveRayPaintPending = false;
      repaintCanvas();
    });
  }
