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
  // framesByPath[path][cam] = {t_rel_s, detected, px, py, candidates}. Both
  // entries always present (even if empty) so the rest of the JS can iterate
  // PATHS without null checks.
  const framesByPath = { live: {}, server_post: {} };
  for (const v of VIDEO_META) {
    const f = v.frames || {};
    for (const path of PATHS) {
      const stream = f[path] || { t_rel_s: [], detected: [], px: [], py: [], frame_index: [], filter_status: [], candidates: [] };
      framesByPath[path][v.camera_id] = {
        t_rel_s: stream.t_rel_s || [],
        detected: stream.detected || [],
        px: stream.px || [],
        py: stream.py || [],
        // frame_index: iOS capture-queue index (live) or PyAV decode order
        // (server_post). Distinct from the array idx which is just position
        // in the timestamp-sorted stream — this is the *physical* frame
        // counter and exposes throttle/drop gaps.
        frame_index: stream.frame_index || [],
        // filter_status: chain_filter verdict — "kept" / "rejected_flicker"
        // / "rejected_jump" / null. Live path doesn't run chain_filter so
        // every entry is null there; SVR path is always populated for
        // detection frames.
        filter_status: stream.filter_status || [],
        // candidates[i] = list of {px,py,area,area_score,cost} for frame i.
        // Populated only on the live path (server_post never produces a
        // candidates list). cost may be null on legacy JSONs predating the
        // cost-persistence change — viewer falls back to area-asc sort.
        candidates: stream.candidates || [],
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
  // --- Triangulation residual filter ---
  // Drop points whose ray-midpoint gap exceeds this cap (m). Real ball
  // pairs sit sub-cm; static-target false pairs blow up to metres.
  // Default off (Infinity) and persists to localStorage.
  const RESIDUAL_FILTER_KEY = "ball_tracker_viewer_residual_cap_cm";
  let residualCapM = Infinity;
  try {
    const saved = parseFloat(localStorage.getItem(RESIDUAL_FILTER_KEY));
    if (Number.isFinite(saved) && saved >= 0 && saved < 200) residualCapM = saved / 100;
  } catch (_e) { /* ignore */ }
