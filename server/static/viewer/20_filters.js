  function passResidualFilter(p) {
    if (!Number.isFinite(residualCapM)) return true;
    const r = (p && typeof p.residual_m === "number") ? p.residual_m : 0;
    return r <= residualCapM;
  }
  // RANSAC ballistic outlier rejection. Spatial-isolation k-NN failed when
  // multiple outliers clustered (each outlier becomes the others' nearest
  // neighbour → looks "non-isolated", inversion bug). RANSAC uses the
  // strong physical prior of g=9.81 instead: sample 4 random points, fit a
  // ballistic, count how many of the rest land within `κ · scale` of the
  // curve, repeat. Outliers can't conspire to land on a parabola of the
  // right curvature, so the inlier-maximising sample wins. ~150 iters at
  // p=0.30 outliers gives >99% chance of hitting an all-clean sample.
  // Scale = median of the smallest 50% of distances (robust to outliers in
  // the unsampled population, won't collapse to zero on synthetic perfect
  // points). Slider κ controls inlier band tightness.
  function applyFitResidualFilter(pts) {
    if (!Number.isFinite(fitResKappa) || pts.length < 6) return pts;
    const ITERS = 150;
    const N = pts.length;
    let bestIdx = null;
    let bestScale = Infinity;
    for (let it = 0; it < ITERS; it++) {
      const idx = new Set();
      while (idx.size < 4) idx.add(Math.floor(Math.random() * N));
      const sample = [...idx].map(i => pts[i]);
      sample.sort((a, b) => a.t_rel_s - b.t_rel_s);
      const fit = _OVL.ballisticFit(sample);
      if (!fit) continue;
      const dists = new Array(N);
      for (let k = 0; k < N; k++) {
        const q = fit.evaluate(pts[k].t_rel_s);
        const dx = pts[k].x - q.x, dy = pts[k].y - q.y, dz = pts[k].z - q.z;
        dists[k] = Math.sqrt(dx*dx + dy*dy + dz*dz);
      }
      const sorted = dists.slice().sort((a, b) => a - b);
      const scale = Math.max(sorted[Math.max(0, Math.floor(N / 2) - 1)], 0.01);
      const thresh = fitResKappa * scale;
      const inliers = [];
      for (let k = 0; k < N; k++) if (dists[k] <= thresh) inliers.push(k);
      if (
        bestIdx === null ||
        inliers.length > bestIdx.length ||
        (inliers.length === bestIdx.length && scale < bestScale)
      ) {
        bestIdx = inliers;
        bestScale = scale;
      }
    }
    if (!bestIdx || bestIdx.length < 4 || bestIdx.length === N) return pts;
    return bestIdx.map(i => pts[i]);
  }
  // Run both filters on a path's full point list; returns the filtered,
  // time-sorted array. Caller passes cutoff for playback clipping.
  function filteredTrajectory(rawPts, cutoff) {
    if (!rawPts || !rawPts.length) return [];
    const residualKept = rawPts
      .filter(p => p.t_rel_s <= cutoff && passResidualFilter(p))
      .slice()
      .sort((a, b) => a.t_rel_s - b.t_rel_s);
    return applyFitResidualFilter(residualKept);
  }
  function hasPathForLayer(layer, path) {
    if (layer === "traj") return HAS_TRAJ_PATH[path];
    const cam = layer.startsWith("cam") ? layer.slice(3) : null;
    if (cam && HAS_PATH_PER_CAM[cam]) return HAS_PATH_PER_CAM[cam][path];
    return HAS_PATH[path];
  }
  // Key is bumped from _layer_visibility → _layer_visibility_v2 because the
  // schema changed: old flat shape is not migrate-able
  // without losing the new `live` axis. Users get the default (all paths on
  // for pipelines that have data) on first post-upgrade load.
  const LAYER_VIS_KEY = "ball_tracker_viewer_layer_visibility_v3";
  const layerVisibility = {
    traj: { live: HAS_TRAJ_PATH.live, server_post: HAS_TRAJ_PATH.server_post },
    camA: { live: HAS_PATH_PER_CAM.A.live, server_post: HAS_PATH_PER_CAM.A.server_post },
    camB: { live: HAS_PATH_PER_CAM.B.live, server_post: HAS_PATH_PER_CAM.B.server_post },
  };
  try {
    const saved = JSON.parse(localStorage.getItem(LAYER_VIS_KEY) || "null");
    if (saved && typeof saved === "object") {
      for (const k of ["traj", "camA", "camB"]) {
        if (saved[k]) {
          for (const path of PATHS) {
            if (typeof saved[k][path] === "boolean") {
              // Respect the saved choice BUT clamp to what's applicable for
              // this session. A stale "traj.live=true" from an old localStorage
              // entry must not resurrect a non-existent toggle.
              const applicable = hasPathForLayer(k, path);
              layerVisibility[k][path] = saved[k][path] && applicable;
            }
          }
        }
      }
    }
  } catch {}
  function persistLayerVisibility() {
    try { localStorage.setItem(LAYER_VIS_KEY, JSON.stringify(layerVisibility)); } catch {}
  }
  function isLayerVisible(layer, path) {
    return !!(layerVisibility[layer] && layerVisibility[layer][path]);
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
