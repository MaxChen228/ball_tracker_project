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
      const stream = f[path] || { t_rel_s: [], detected: [], px: [], py: [], frame_index: [], candidates: [] };
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
  const TRAJ_BY_PATH = SCENE.triangulated_by_path || {};
  const HAS_TRAJ_PATH = {
    live: (TRAJ_BY_PATH.live || []).length > 0,
    server_post: (TRAJ_BY_PATH.server_post || []).length > 0
      || (SCENE.triangulated || []).length > 0,
  };
  // --- Triangulation residual filter (client-side preview) ---
  // Sibling of cost_threshold: drops points whose ray-midpoint gap
  // exceeds this cap (m) at draw time. Authoritative knob is the
  // per-session SessionResult.gap_threshold_m, server-injected as
  // VIEWER_INITIAL_GAP_THRESHOLD_M (metres). The header strip's Gap
  // slider drives `_setGapThreshold` (50_canvas.js) which mutates this
  // var; viewer_layers.js's trajectory rebuild reads it via
  // `window._passResidualFilter`. Always a finite metres value — 2.0m
  // is just the slider's max, not "Infinity / off".
  let residualCapM = (typeof window.VIEWER_INITIAL_GAP_THRESHOLD_M === "number")
    ? window.VIEWER_INITIAL_GAP_THRESHOLD_M
    : 2.0;
