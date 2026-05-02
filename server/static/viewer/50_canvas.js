  // ----- BLOBS overlay (multi-candidate, no client-side cost gate) -----
  // Sole 2D overlay layer for detection. Pre-fan-out there was also a
  // `detection_live` / `detection_svr` "winner dot" layer drawn from
  // f.px/f.py — that's gone: with fan-out triangulation no candidate
  // is "the winner", every shape-gate-passing candidate gets its own
  // ring + 3D ray + 3D point. The cost gate that used to live as an
  // operator slider here is now per-algorithm metadata
  // (`algorithms.cost_threshold_for_algorithm`) — applied server-side
  // before the segmenter consumes points, NOT at view time. The viewer
  // shows everything pairing emitted; predicates below pass-through.

  // Drag preview for the Gap slider in the viewer header strip
  // strip. Mutates `residualCapM` (declared in 10_video_master.js, shared
  // IIFE scope) so `_passResidualFilter` sees the new cap on next redraw.
  // Slider value is centimetres (0–200), converted to metres. 200cm =
  // 2.0m, which is also the route's max — no Infinity special case, the
  // residual cap is always a finite metres value (matches the wire
  // semantics of `gap_threshold_m`).
  function _setGapThreshold(v_cm) {
    const cm = parseFloat(v_cm);
    if (!Number.isFinite(cm)) {
      throw new Error("_setGapThreshold: non-numeric slider value " + v_cm);
    }
    residualCapM = cm / 100;
    if (typeof scheduleSceneDraw === 'function') scheduleSceneDraw();
  }
  function _getGapThresholdM() { return residualCapM; }
  window._setGapThreshold = _setGapThreshold;
  window._getGapThresholdM = _getGapThresholdM;
  // Residual predicate used by the Three.js trajectory rebuild
  // (viewer_layers.js). Closes over `residualCapM` so callers don't need
  // to read it. Points with non-numeric / missing residual_m are treated
  // as residual=0 (legacy fall-through; matches what the Plotly-era
  // 20_filters.js did).
  function _passResidualFilter(p) {
    const r = (p && typeof p.residual_m === "number") ? p.residual_m : 0;
    return r <= residualCapM;
  }
  window._passResidualFilter = _passResidualFilter;
  // Expose so 30_frame_index.js's raysAtT and viewer_layers.js's ray /
  // trajectory rebuilds can apply the same predicate as the BLOBS canvas
  // overlay — single source of truth for "passing".
  window._candPassesThreshold = _candPassesThreshold;

  // No client-side cost gate post cost-absorption refactor. All candidates
  // pass; the per-algorithm cost threshold is enforced server-side at
  // segment-fit time. Function kept as a stable predicate for callers
  // (raysAtT in 30_frame_index.js, viewer_layers.js trajectory rebuild)
  // so future per-algorithm display gates can drop in without touching
  // those call sites.
  function _candPassesThreshold(_c) { return true; }
  function _passCostFilterPoint(_p) { return true; }
  window._passCostFilterPoint = _passCostFilterPoint;

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

  // 4.17 ms = one frame interval at 240 fps. A wider tol pulls blobs
  // from neighbouring frames during scrub / play, producing the visible
  // "blob mask is one frame ahead/behind the video" symptom — most
  // obvious when the ball is moving fast and inter-frame displacement
  // exceeds the ball radius. The previous 10 ms tol covered ±2 frames
  // worth, more than enough to drift visibly. The half-frame buffer
  // (4.17 / 2 ≈ 2.1 ms either side of currentT) is the largest
  // sub-frame quantization we should accept; anything beyond is an
  // off-by-one frame mismatch worth reporting as "no blob this frame".
  const _BLOB_FRAME_TOL_S = 1.0 / 240;
  function _drawBlobsForPath(ctx, sx, sy, cam, path, color, cfg) {
    const f = framesByPath[path] && framesByPath[path][cam.camera_id];
    if (!f) return;
    const ts = f.t_rel_s || [], cands = f.candidates || [];
    if (!ts.length || !cands.length) return;
    const idx = _findClosestFrameIdx(ts, currentT, _BLOB_FRAME_TOL_S);
    if (idx < 0) return;
    const frameCands = cands[idx] || [];
    if (!frameCands.length) return;
    // Threshold filter: candidates whose cost ≤ slider value get drawn.
    // Same `_candPassesThreshold` predicate the 3D scene filter (in
    // 60_session_tuning.js) uses, so what the operator sees on the 2D
    // overlay matches what's in the 3D point cloud at this threshold.
    const passing = frameCands.filter(_candPassesThreshold);
    if (!passing.length) return;
    // Solid ring; opacity comes from cfg.opacity via globalAlpha (so
    // the blobs popover slider drives it directly), line width from
    // cfg.lineWidth. Pre-cfg the renderer baked an 80% alpha hex tail
    // onto the colour string — moved into globalAlpha now.
    ctx.save();
    if (cfg && Number.isFinite(cfg.opacity)) ctx.globalAlpha = cfg.opacity;
    ctx.strokeStyle = color;
    ctx.lineWidth = (cfg && Number.isFinite(cfg.lineWidth)) ? cfg.lineWidth : 1.5;
    for (const c of passing) {
      ctx.beginPath();
      ctx.arc(c.px * sx, c.py * sy, 4, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  if (window.BallTrackerCamView) {
    // Single BLOBS layer; the data path comes from the global PATH
    // selector via currentPath(). Cam-view runtime owns the on/off
    // boolean per cam; toolbar handler flips PATH → calls redrawAll.
    // Ring colour: cam-encoded for live (matches 3D ray cam-colour
    // convention), single accent for svr.
    window.BallTrackerCamView.registerLayer('detection_blobs', function (ctx, sx, sy, cam, _extras, cfg) {
      const path = currentPath();
      const color = path === 'live' ? colorForCamPath(cam.camera_id, 'live') : ACCENT;
      _drawBlobsForPath(ctx, sx, sy, cam, path, color, cfg);
    });
    for (const c of (SCENE.cameras || [])) {
      if (c.fx == null || c.R_wc == null || c.t_wc == null
          || c.image_width_px == null || c.image_height_px == null) continue;
      window.BallTrackerCamView.setMeta(c.camera_id, c);
    }
    // No cost slider after the cost-absorption refactor — the cost
    // gate is per-algorithm and applied server-side at fit time, not
    // a client-side preview knob.
  }
  function drawVirtuals() {
    if (window.BallTrackerCamView) window.BallTrackerCamView.redrawAll();
  }
  function drawScene() {
    // Three.js viewer scene owns the entire 3D pipeline now. Push the
    // current time + mode into the layer module; it rebuilds only the
    // t-dependent layers (rays / traj / fit marker), leaving cameras,
    // ground traces, and fit curves untouched. Strike-zone visibility
    // goes through the scene runtime's `setLayerVisible` (not a trace
    // filter) — see strike-zone toggle handler below.
    if (window.BallTrackerViewerScene) {
      window.BallTrackerViewerScene.setT(currentT, mode);
    }
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
    updateSpeedBadge();
  }

  // Bottom-left badge: |v(t)| at the current playback time + STRIKE / BALL
  // verdict for the whole pitch. Speed reads the segment *active* at
  // currentT (not always seg0 — a bounced 94 km/h pitch frequently has a
  // small detection-noise seg0 ≈ 50 km/h that we'd otherwise display
  // forever); verdict iterates all segs with no extrapolation past seg
  // boundaries (bounces invalidate ballistic continuation). Helpers live
  // on the shared `BallTrackerOverlays` NS so dashboard + viewer use one
  // canonical implementation, parity-tested against Python.
  function updateSpeedBadge() {
    const badge = document.getElementById("viewer-speed-badge");
    if (!badge) return;
    const segs = currentSegments();
    if (!Array.isArray(segs) || !segs.length) {
      badge.hidden = true;
      badge.classList.remove('verdict-strike', 'verdict-ball');
      return;
    }
    badge.hidden = false;
    const speedEl = document.getElementById("viewer-lpb-speed");
    const metaEl = document.getElementById("viewer-lpb-meta");

    const NS = window.BallTrackerOverlays;
    const playback = mode !== "all";
    const tEval = playback ? currentT : segs[0].t_start;
    const idx = NS.activeSegmentIndex(segs, tEval);
    const activeSeg = segs[idx >= 0 ? idx : 0];
    const inst = NS.instantSpeedKph(activeSeg, tEval);
    if (speedEl) speedEl.textContent = Number.isFinite(inst) ? inst.toFixed(1) : '—';

    const zone = window.BallTrackerScene && typeof window.BallTrackerScene.strikeZone === 'function'
      ? window.BallTrackerScene.strikeZone() : null;
    let verdict = 'ball';
    if (zone) {
      const judg = NS.judgePitch(segs, zone);
      verdict = judg ? judg.verdict : 'ball';
    }
    if (metaEl) metaEl.textContent = verdict === 'strike' ? 'STRIKE' : 'BALL';
    badge.classList.toggle('verdict-strike', verdict === 'strike');
    badge.classList.toggle('verdict-ball', verdict === 'ball');
  }
  // BallTrackerCamView's per-cam ResizeObserver handles canvas reflow
  // automatically; no need for a window resize listener here.
