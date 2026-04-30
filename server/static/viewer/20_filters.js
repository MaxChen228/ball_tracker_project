  // (Plotly-era passResidualFilter / filteredTrajectory removed — the
  // Three.js viewer reads `window._passResidualFilter` exposed from
  // 50_canvas.js and applies it inline in viewer_layers.js's trajectory
  // rebuild. Single source of truth, no per-IIFE function copy.)
  function hasPathForLayer(layer, path) {
    if (layer === "traj") return HAS_TRAJ_PATH[path];
    return HAS_PATH[path];
  }
  // v6 schema: a single global `path` (live / server_post) drives the
  // data source for every layer the operator turns on. Each layer has
  // its own boolean enable flag. Mental model: "I'm looking at the
  // SVR pipeline, show me rays + fit on top of blobs." Path is one
  // exclusive choice; layer toggles compose freely on top.
  const LAYER_VIS_KEY = "ball_tracker_viewer_layer_visibility_v6";
  function _defaultPath() {
    if (HAS_PATH.server_post || HAS_TRAJ_PATH.server_post) return "server_post";
    if (HAS_PATH.live || HAS_TRAJ_PATH.live) return "live";
    return "server_post";
  }
  const layerVisibility = {
    path: _defaultPath(),
    rays: true,
    traj: true,
    fit: true,
    blobs: true,
  };
  try {
    const saved = JSON.parse(localStorage.getItem(LAYER_VIS_KEY) || "null");
    if (saved && typeof saved === "object") {
      if (saved.path === "live" || saved.path === "server_post") {
        layerVisibility.path = saved.path;
      }
      for (const k of ["rays", "traj", "fit", "blobs"]) {
        if (typeof saved[k] === "boolean") layerVisibility[k] = saved[k];
      }
    }
  } catch {}
  function persistLayerVisibility() {
    try { localStorage.setItem(LAYER_VIS_KEY, JSON.stringify(layerVisibility)); } catch {}
  }
  function currentPath() { return layerVisibility.path; }
  function isLayerEnabled(layer) {
    if (!(layer in layerVisibility)) {
      throw new Error("isLayerEnabled: unknown layer " + layer);
    }
    if (layer === "path") {
      throw new Error("isLayerEnabled: 'path' is not a boolean layer; use currentPath()");
    }
    return !!layerVisibility[layer];
  }
  // Flat cams-present views used by the frame scrubber / label renderer.
  // Build the scrubber's discrete time positions per camera, preferring the
  // live path's timestamps as the canonical clock. server_post for the same
  // cam represents the SAME physical frames re-decoded from the MOV — its
  // PTS drifts by up to ±1ms from the iOS sample PTS due to MOV time_base
  // quantization (30000-tick container vs iOS variable-rate sensor clock).
  // Adding both as independent scrubber positions almost doubles
  // TOTAL_FRAMES (e.g. 2000 → 3700 for s_a1cc0233 after running server
  // detection). Live is the upper-bound on physical frames we have any
  // detection for; server_post only adds positions for cams that have NO
  // live data (rare — usually missing-upload sessions). Per-frame
  // server_post overlays still read from framesByPath[server_post][cam] via
  // tol-based lookup in 50_canvas.js / 30_frame_index.js — those don't
  // require dedicated scrubber positions.
  const MASTER_FPS = Math.max(60, ...Object.values(fpsByCam).filter(f => isFinite(f) && f > 0));
  const QUANT = 10000;
  const timeMap = new Map();
  const _scrubberCams = new Set([
    ...camsWithFramesByPath.live,
    ...camsWithFramesByPath.server_post,
  ]);
  for (const cam of _scrubberCams) {
    const liveTs = framesByPath.live[cam]?.t_rel_s;
    const fallbackTs = framesByPath.server_post[cam]?.t_rel_s;
    const tsList = (liveTs && liveTs.length) ? liveTs : (fallbackTs || []);
    for (const t of tsList) {
      const q = Math.round(t * QUANT);
      if (!timeMap.has(q)) timeMap.set(q, t);
    }
  }
  if (timeMap.size === 0) {
    for (const r of SCENE.rays || []) timeMap.set(Math.round(r.t_rel_s * QUANT), r.t_rel_s);
    for (const p of SCENE.triangulated || []) timeMap.set(Math.round(p.t_rel_s * QUANT), p.t_rel_s);
    for (const path of Object.keys(TRAJ_BY_PATH)) {
      for (const p of TRAJ_BY_PATH[path] || []) timeMap.set(Math.round(p.t_rel_s * QUANT), p.t_rel_s);
    }
  }
  const unionTimes = Array.from(timeMap.values()).sort((a, b) => a - b);
  if (unionTimes.length === 0) { unionTimes.push(0); unionTimes.push(0.05); }
  const TOTAL_FRAMES = unionTimes.length;
  let tMin = unionTimes[0];
  let tMax = unionTimes[TOTAL_FRAMES - 1];
  // Window used to pick "current" rays in playback mode. We want the
  // near-frame match so the 3D view shows an instantaneous ray pair, not
  // a cumulative fan. 0.75 of the nominal inter-frame gap gives a bit of
  // slack for A/B jitter without pulling in neighbouring frames.
  const PLAYBACK_RAY_TOL = TOTAL_FRAMES > 1
    ? Math.max(0.004, (tMax - tMin) / (TOTAL_FRAMES - 1) * 0.75)
    : 0.010;
