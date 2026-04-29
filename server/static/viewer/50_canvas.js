  // ----- BLOBS overlay (multi-candidate, gated by selector cost ≤ threshold) -----
  // Sole 2D overlay layer for detection. Pre-fan-out there was also a
  // `detection_live` / `detection_svr` "winner dot" layer drawn from
  // f.px/f.py — that's gone: with fan-out triangulation no candidate
  // is "the winner", every shape-gate-passing candidate gets its own
  // ring + 3D ray + 3D point, gated only by the cost_threshold slider.
  // Operator opens the BLOBS layer to see candidates the selector saw on
  // each frame. The session-level cost_threshold slider in the viewer
  // header (see `session_cost_threshold_strip_html`) controls which
  // candidates are drawn: cost ≤ threshold = show, cost > threshold =
  // hide. The same threshold value is what the recompute endpoint will
  // apply when the operator clicks Apply.
  //
  // Why threshold-based, not rank-based: rank ("top K") is cosmetic —
  // it doesn't change which candidates the selector actually picks.
  // Threshold is the same knob the server uses, so what you see in
  // the overlay is what gets triangulated post-recompute.
  //
  // Initial value is seeded from SessionResult.cost_threshold (server-
  // injected via VIEWER_INITIAL_COST_THRESHOLD), or 1.0 (no filter)
  // when the session was computed before the recompute endpoint
  // landed. Lives on `window` so the header slider's oninput can mutate
  // it and trigger a redraw without the canvas knowing about the DOM
  // input element.
  let _costThreshold = (typeof window.VIEWER_INITIAL_COST_THRESHOLD === "number")
    ? window.VIEWER_INITIAL_COST_THRESHOLD : 1.0;
  function _setCostThreshold(v) {
    const t = Math.max(0, Math.min(1.0, parseFloat(v)));
    _costThreshold = Number.isFinite(t) ? t : 1.0;
    if (window.BallTrackerCamView) window.BallTrackerCamView.redrawAll();
    // Also redraw the 3D scene — `raysAtT` reads `_candPassesThreshold`
    // to decide which fan-out rays to draw at the matched frame, so the
    // slider has visible effect on the 3D point cloud's ray bundle.
    if (typeof scheduleSceneDraw === 'function') scheduleSceneDraw();
  }
  function _getCostThreshold() { return _costThreshold; }
  // Expose for the header slider's inline `oninput` + the 3D-scene filter
  // hook used by 60_session_tuning.js.
  window._setCostThreshold = _setCostThreshold;
  window._getCostThreshold = _getCostThreshold;
  // Expose so 30_frame_index.js's raysAtT can apply the same predicate
  // as the BLOBS canvas overlay — single source of truth for "passing".
  window._candPassesThreshold = _candPassesThreshold;

  // A candidate passes the threshold filter when its cost is ≤ the
  // current setting. Legacy JSONs with cost=null pass unconditionally —
  // there's no meaningful selector cost to compare against, so they
  // can't be filtered at view time. Recompute is the path to assign
  // costs to legacy data.
  function _candPassesThreshold(c) {
    if (c.cost == null || !Number.isFinite(c.cost)) return true;
    return c.cost <= _costThreshold;
  }

  // Plain floor lookup: BLOBS draws every candidate on the matched
  // frame; if the frame has no candidates we just render nothing for
  // that instant. No det back-walk because there is no winner-only
  // layer to keep "alive" across detection gaps anymore.
  function _findClosestFrameIdx(ts, currentT, tol) {
    if (!ts.length || currentT < ts[0] - tol) return -1;
    let lo = 0, hi = ts.length - 1;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= currentT) lo = mid; else hi = mid;
    }
    const idx = (ts[hi] <= currentT) ? hi : lo;
    return (currentT - ts[idx]) <= tol ? idx : -1;
  }

  function _drawBlobsForPath(ctx, sx, sy, cam, path, color) {
    const f = framesByPath[path] && framesByPath[path][cam.camera_id];
    if (!f) return;
    const ts = f.t_rel_s || [], cands = f.candidates || [];
    if (!ts.length || !cands.length) return;
    const idx = _findClosestFrameIdx(ts, currentT, 0.010);
    if (idx < 0) return;
    const frameCands = cands[idx] || [];
    if (!frameCands.length) return;
    // Threshold filter: candidates whose cost ≤ slider value get drawn.
    // Same `_candPassesThreshold` predicate the 3D scene filter (in
    // 60_session_tuning.js) uses, so what the operator sees on the 2D
    // overlay matches what's in the 3D point cloud at this threshold.
    const passing = frameCands.filter(_candPassesThreshold);
    if (!passing.length) return;
    // Solid ring at ~80% alpha so the BLOBS layer reads through the OVL
    // canvas-opacity slider (default 65%, often dialled lower).
    ctx.strokeStyle = (typeof color === 'string' && color.length === 7 && color[0] === '#')
      ? color + 'CC'  // ~80% alpha
      : color;
    ctx.lineWidth = 1.5;
    for (const c of passing) {
      ctx.beginPath();
      ctx.arc(c.px * sx, c.py * sy, 4, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  if (window.BallTrackerCamView) {
    // Two BLOBS layers — one per path — so toolbar's LIVE/SVR path groups
    // can toggle each independently. Color-tier: cam color for live,
    // ACCENT for svr. There used to be a `detection_live` /
    // `detection_svr` "winner dot" layer here too; fan-out triangulation
    // killed the winner concept, so BLOBS is now the only 2D overlay
    // and the cost_threshold slider is the only knob.
    window.BallTrackerCamView.registerLayer('detection_blobs_live', function (ctx, sx, sy, cam) {
      _drawBlobsForPath(ctx, sx, sy, cam, 'live', colorForCamPath(cam.camera_id, 'live'));
    });
    window.BallTrackerCamView.registerLayer('detection_blobs_svr', function (ctx, sx, sy, cam) {
      _drawBlobsForPath(ctx, sx, sy, cam, 'server_post', ACCENT);
    });
    for (const c of (SCENE.cameras || [])) {
      if (c.fx == null || c.R_wc == null || c.t_wc == null
          || c.image_width_px == null || c.image_height_px == null) continue;
      window.BallTrackerCamView.setMeta(c.camera_id, c);
    }
    // Mount-time cost-threshold slider sync: HTML ships with the
    // server-injected SessionResult.cost_threshold (or 1.0 default), so
    // there's nothing extra to pull from localStorage — the value is
    // session-scoped and authoritative on the server.
  }
  function drawVirtuals() {
    if (window.BallTrackerCamView) window.BallTrackerCamView.redrawAll();
  }
  function drawScene() {
    const playback = mode !== "all";
    const cutoff = playback ? currentT : Infinity;
    // Strike-zone toggle: filter the wireframe + fill traces out of
    // STATIC when the user unticks the box. Default ON, persisted in
    // localStorage so the choice survives page reload.
    const showZone = strikeZoneVisible();
    const staticFiltered = showZone
      ? STATIC
      : STATIC.filter(t => !((t.meta || {}).feature === "strike_zone"));
    Plotly.react(sceneDiv, [...staticFiltered, ...buildDynamicTraces(cutoff, playback)], LAYOUT, {displayModeBar: false, responsive: true});
    // Plate overlay is now part of the cam-view 'plate' layer painted
    // onto the canvas overlay above the video — no separate SVG path.
    // virtual canvases are NOT called here on purpose. They schedule on
    // their own RAF (scheduleVirtualDraw) so a heavy Plotly.react redraw
    // can't stall the cheap canvas2D paints — virtual cameras need to
    // stay locked to the video clock during playback even if the 3D
    // scene drops a frame.
  }
  function scheduleVirtualDraw() {
    if (virtualDrawRaf !== null) return;
    virtualDrawRaf = requestAnimationFrame(() => { virtualDrawRaf = null; drawVirtuals(); });
  }
  // Fans out to both paint paths. Each owns its own RAF, so the
  // expensive Plotly.react can't block the cheap canvas2D paints — but
  // every existing `scheduleSceneDraw()` callsite still triggers a
  // full coherent update without rewriting them. In playback mode
  // setFrame() also calls scheduleVirtualDraw directly so the virtual
  // cameras stay locked to the video clock independent of this dedup.
  function scheduleSceneDraw() {
    if (sceneDrawRaf === null) {
      sceneDrawRaf = requestAnimationFrame(() => { sceneDrawRaf = null; drawScene(); });
    }
    scheduleVirtualDraw();
  }
  // BallTrackerCamView's per-cam ResizeObserver handles canvas reflow
  // automatically; no need for a window resize listener here.
