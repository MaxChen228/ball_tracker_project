  function pickMasterVideo() {
    if (!vids.length) return null;
    let master = vids[0];
    let masterCount = -1;
    for (const v of vids) {
      // Prefer the video whose own camera has the richest *any-path* detection
      // history — that's the one we want RVFC to drive the scrubber off.
      let n = 0;
      for (const path of PATHS) {
        n += (framesByPath[path][v.dataset.cam]?.t_rel_s || []).length;
      }
      if (n > masterCount) { master = v; masterCount = n; }
    }
    return master;
  }
  // framesByPath[path][cam] = {t_rel_s, detected, px, py}. Three entries
  // always present (even if empty) so the rest of the JS can iterate PATHS
  // without null checks.
  const framesByPath = { live: {}, server_post: {} };
  for (const v of VIDEO_META) {
    const f = v.frames || {};
    for (const path of PATHS) {
      const stream = f[path] || { t_rel_s: [], detected: [], px: [], py: [] };
      framesByPath[path][v.camera_id] = {
        t_rel_s: stream.t_rel_s || [],
        detected: stream.detected || [],
        px: stream.px || [],
        py: stream.py || [],
      };
    }
  }
  const camsWithFramesByPath = {};
  for (const path of PATHS) {
    camsWithFramesByPath[path] = Object.keys(framesByPath[path])
      .filter(c => (framesByPath[path][c].t_rel_s || []).length)
      .sort();
  }
  // Did any camera produce rays / points / frames on this pipeline? Used to
  // hide inapplicable pills (so a live-only session doesn't show dead SVR /
  // POST toggles).
  const HAS_PATH = {
    live: camsWithFramesByPath.live.length > 0
      || (SCENE.rays || []).some(r => sourceToPath(r.source || "server") === "live"),
    server_post: camsWithFramesByPath.server_post.length > 0
      || Object.keys(SCENE.ground_traces || {}).length > 0
      || (SCENE.triangulated || []).length > 0,
  };
  // Per-cam applicability: single-camera sessions must not light up the
  // other cam's pills as dead buttons. Falls back to HAS_PATH for any cam
  // we don't enumerate here.
  const HAS_PATH_PER_CAM = {};
  for (const cam of ["A", "B"]) {
    const raySrc = (p) => (SCENE.rays || []).some(r => r.camera_id === cam && sourceToPath(r.source || "server") === p);
    HAS_PATH_PER_CAM[cam] = {
      live: camsWithFramesByPath.live.includes(cam) || raySrc("live"),
      server_post: camsWithFramesByPath.server_post.includes(cam)
        || !!(SCENE.ground_traces && SCENE.ground_traces[cam])
        || raySrc("server_post"),
    };
  }
  const TRAJ_BY_PATH = SCENE.triangulated_by_path || {};
  const HAS_TRAJ_PATH = {
    live: (TRAJ_BY_PATH.live || []).length > 0,
    server_post: (TRAJ_BY_PATH.server_post || []).length > 0
      || (SCENE.triangulated || []).length > 0,
  };
  // --- Triangulation filters ---
  // Residual: drop points whose ray-midpoint gap exceeds this cap (m).
  //   Real ball pairs sit sub-cm; static-target false pairs blow up to m.
  // FitRes: RANSAC-lite. Run ballistic LSQ on residual-survivors, drop
  //   any point whose 3D distance to the fit curve > k × RMSE, then
  //   re-fit once. k is the slider value. Catches outliers that residual
  //   missed (e.g. two moving false targets triangulating with low gap
  //   but to a physically impossible location). Symmetric in time so
  //   head/tail are not privileged.
  // Both default "off" (residual = Infinity, fitres k = Infinity) and
  // persist to localStorage.
  const RESIDUAL_FILTER_KEY = "ball_tracker_viewer_residual_cap_cm";
  const FITRES_FILTER_KEY = "ball_tracker_viewer_fitres_kappa";
  let residualCapM = Infinity;
  let fitResKappa = Infinity;
  // Fit visibility + source live in shared overlay state (window.BallTrackerOverlays)
  // so dashboard and viewer stay in lock-step. No local copies — read fresh
  // each time so cross-tab edits to localStorage are picked up on next draw.
  try {
    const saved = parseFloat(localStorage.getItem(RESIDUAL_FILTER_KEY));
    if (Number.isFinite(saved) && saved >= 0 && saved < 200) residualCapM = saved / 100;
  } catch (_e) { /* ignore */ }
  try {
    const saved = parseFloat(localStorage.getItem(FITRES_FILTER_KEY));
    if (Number.isFinite(saved) && saved >= 1.0 && saved < 6.0) fitResKappa = saved;
  } catch (_e) { /* ignore */ }
