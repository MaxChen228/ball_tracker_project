  function _drawDetectionForPath(ctx, sx, sy, cam, path, color) {
    const framesForThisCam = framesByPath[path] && framesByPath[path][cam.camera_id];
    if (!framesForThisCam) return;
    const ts = framesForThisCam.t_rel_s || [];
    const det = framesForThisCam.detected || [];
    const pxArr = framesForThisCam.px || [];
    const pyArr = framesForThisCam.py || [];
    if (!ts.length) return;
    if (currentT < ts[0]) return;
    // Floor-not-nearest: pick the largest ts[i] ≤ currentT. The video
    // element seeks the same way (it shows the frame whose PTS is the
    // largest one ≤ requested currentTime, not the nearest), so picking
    // `lo` here keeps canvas + video aligned to the same MOV frame.
    // Round-to-nearest used to flip to ts[hi] when currentT sat between
    // two samples, putting the canvas dot one frame ahead of the video
    // (≈ 5 ms × 30-40 px on a fast pitch — visible as residual drift).
    let lo = 0, hi = ts.length - 1;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= currentT) lo = mid; else hi = mid;
    }
    let idx = (ts[hi] <= currentT) ? hi : lo;
    const tol = 0.010;
    // server_post frame gaps leave runs of det=false. Without left-scan
    // the dot blanks across the gap; walk back to the nearest detected
    // frame still within tol so it sticks.
    while (idx >= 0 && !det[idx] && (currentT - ts[idx]) <= tol) idx--;
    if (idx < 0 || !det[idx] || (currentT - ts[idx]) > tol) return;
    const px = pxArr[idx], py = pyArr[idx];
    if (px == null || py == null) return;
    const x = px * sx, y = py * sy;
    // 1 px dark stroke ring on the outer white circle so the dot stays
    // legible against bright video frames (canvas opacity defaults to
    // 65% so the inner white alpha multiplies down to ~0.585). Stroke
    // is opaque black at 50% alpha — adds contrast without darkening
    // the ball when the bg is already dark.
    ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.5)";
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
  }
  // ----- BLOBS overlay (multi-candidate, gated by selector cost ≤ threshold) -----
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
  }
  function _getCostThreshold() { return _costThreshold; }
  // Expose for the header slider's inline `oninput` + the 3D-scene filter
  // hook used by 60_session_tuning.js.
  window._setCostThreshold = _setCostThreshold;
  window._getCostThreshold = _getCostThreshold;

  // A candidate passes the threshold filter when its cost is ≤ the
  // current setting. Legacy JSONs with cost=null pass unconditionally —
  // there's no meaningful selector cost to compare against, so they
  // can't be filtered at view time. Recompute is the path to assign
  // costs to legacy data.
  function _candPassesThreshold(c) {
    if (c.cost == null || !Number.isFinite(c.cost)) return true;
    return c.cost <= _costThreshold;
  }

  // Plain floor lookup (no det back-walk): BLOBS draws every candidate the
  // selector saw on the matched frame. The winner-layer's back-walk to
  // skip det=false frames doesn't apply here.
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
    // canvas-opacity slider (default 65%, often dialled lower) at roughly
    // the same effective contrast as the detection_live winner dot.
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
    window.BallTrackerCamView.registerLayer('detection_live', function (ctx, sx, sy, cam) {
      _drawDetectionForPath(ctx, sx, sy, cam, 'live', colorForCamPath(cam.camera_id, 'live'));
    });
    window.BallTrackerCamView.registerLayer('detection_svr', function (ctx, sx, sy, cam) {
      _drawDetectionForPath(ctx, sx, sy, cam, 'server_post', ACCENT);
    });
    // Two BLOBS layers — one per path — so toolbar's LIVE/SVR path groups
    // can toggle each independently. Color-tier matches the corresponding
    // winner layer (cam color for live, ACCENT for svr) so a frame with
    // both paths on reads as two color-coded ring sets around their
    // respective dots.
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
    // Mount-time slider sync: the `<input type=range>` ships with HTML
    // value="5" hardcoded; pull the persisted K out of localStorage and
    // overwrite each cam's K slider so a previously-set value survives
    // page reload.
    document.querySelectorAll('.cv-blobs-k input[type=range]').forEach(el => {
      el.value = String(_candTopK());
    });
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
