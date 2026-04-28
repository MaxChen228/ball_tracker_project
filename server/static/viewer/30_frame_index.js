  function buildCamIndexFor(frameMap, cam) {
    const f = frameMap[cam];
    const ts = f.t_rel_s, det = f.detected;
    const fidx = f.frame_index || [];
    const fstat = f.filter_status || [];
    const out = new Array(TOTAL_FRAMES).fill(null);
    if (!ts.length) return out;
    const tol = 0.010;
    let j = 0;
    for (let i = 0; i < TOTAL_FRAMES; ++i) {
      const t = unionTimes[i];
      if (t < ts[0] - tol || t > ts[ts.length - 1] + tol) continue;
      // Floor: pick largest ts[j] ≤ t. Matches the HTML5 video element
      // seek behaviour (displays PTS ≤ currentTime) and the canvas overlay's
      // _drawDetectionForPath floor, so timeline label / canvas dot /
      // video frame all reflect the same source frame.
      // tol allowance: when t < ts[0] within 10ms, j stays at 0 even
      // though ts[0] > t — preserves the prior "show closest valid frame
      // in pre-roll slack" behaviour rather than going strict-floor.
      while (j + 1 < ts.length && ts[j + 1] <= t) j++;
      out[i] = {
        idx: j,
        t: ts[j],
        detected: !!det[j],
        frame_index: fidx[j] ?? null,
        filter_status: fstat[j] ?? null,
      };
    }
    return out;
  }
  // One (cam → frameIndex → {idx, t, detected}) table per pipeline.
  // Three fully-independent tables so a missed detection in SVR does not
  // suppress LIVE's head-indicator, etc.
  const camAtFrameByPath = { live: {}, server_post: {} };
  for (const path of PATHS) {
    for (const cam of camsWithFramesByPath[path]) {
      camAtFrameByPath[path][cam] = buildCamIndexFor(framesByPath[path], cam);
    }
  }
  let mode = "all";
  let currentFrame = 0;
  let currentT = tMin;
  let rvfcEnabled = false;
  let seekRafPending = false;
  let sceneDrawRaf = null;
  let virtualDrawRaf = null;
  let isScrubbing = false;
  let suppressVideoFeedbackUntilMs = 0;
  const masterVideo = pickMasterVideo();
  const HARD_SYNC_THRESHOLD_S = 0.040;
  const SOFT_SYNC_THRESHOLD_S = 0.008;
  const MAX_RATE_NUDGE = 0.12;
  scrubber.max = String(TOTAL_FRAMES - 1);
  scrubber.step = "1";
  frameInput.max = String(TOTAL_FRAMES - 1);
  frameTotal.textContent = String(TOTAL_FRAMES - 1);
  function ballDetectedRaysUpTo(rays, t) {
    const xs = [], ys = [], zs = [];
    for (const r of rays) {
      if (r.t_rel_s > t) continue;
      xs.push(r.origin[0], r.endpoint[0], null);
      ys.push(r.origin[1], r.endpoint[1], null);
      zs.push(r.origin[2], r.endpoint[2], null);
    }
    return {xs, ys, zs};
  }
  // Playback: pick the single ray closest to currentT (within tol) rather
  // than the cumulative fan. Keeps the scene readable as an instantaneous
  // snapshot tied to the bottom player.
  function raysAtT(rays, t, tol) {
    let best = null, bestDt = Infinity;
    for (const r of rays) {
      const dt = Math.abs(r.t_rel_s - t);
      if (dt <= tol && dt < bestDt) { best = r; bestDt = dt; }
    }
    if (!best) return {xs: [], ys: [], zs: []};
    return {
      xs: [best.origin[0], best.endpoint[0], null],
      ys: [best.origin[1], best.endpoint[1], null],
      zs: [best.origin[2], best.endpoint[2], null],
    };
  }
  // Camera diamond + 3-axis triad is data the user should be able to hide
  // in lock-step with that camera's ray pills. When every path for a given
  // camera is off, the camera itself disappears too — no orphaned diamonds.
  // Emitted BEFORE rays so Plotly's autoscale sees the camera centre up
  // front and the initial viewport always frames the rig rather than just
  // the plate.
