  // ----- BLOBS overlay (multi-candidate, gated by selector cost ≤ threshold) -----
  // Sole 2D overlay layer for detection. Pre-fan-out there was also a
  // `detection_live` / `detection_svr` "winner dot" layer drawn from
  // f.px/f.py — that's gone: with fan-out triangulation no candidate
  // is "the winner", every shape-gate-passing candidate gets its own
  // ring + 3D ray + 3D point, gated only by the cost_threshold slider.
  // Operator opens the BLOBS layer to see candidates the selector saw on
  // each frame. The session-level cost_threshold slider in the viewer
  // header (see `session_tuning_strip_html`) controls which
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

  // Sibling of cost: drag preview for the Gap slider in the same header
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

  // A candidate passes the threshold filter when its cost is ≤ the
  // current setting. Legacy JSONs with cost=null pass unconditionally —
  // there's no meaningful selector cost to compare against, so they
  // can't be filtered at view time. Recompute is the path to assign
  // costs to legacy data.
  function _candPassesThreshold(c) {
    if (c.cost == null || !Number.isFinite(c.cost)) return true;
    return c.cost <= _costThreshold;
  }

  // Triangulated-point variant: each persisted point carries `cost_a` and
  // `cost_b` from its source candidate pair (server schema, post-PR
  // pairing-full-emit). The point passes when both ends are ≤ threshold.
  // null / non-numeric on either side means "no cost info" → pass; the
  // canonical case is the synthesized `_frame_candidates` px/py fallback
  // path on legacy fixtures. Once Phase 5 retires that fallback this
  // legacy-pass branch goes away and any null becomes a hard fail.
  function _passCostFilterPoint(p) {
    if (!p) return true;
    const ca = p.cost_a, cb = p.cost_b;
    let m = -1;
    if (ca != null && Number.isFinite(ca)) m = Math.max(m, ca);
    if (cb != null && Number.isFinite(cb)) m = Math.max(m, cb);
    if (m < 0) return true;  // no cost info on either side
    return m <= _costThreshold;
  }
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
  function _drawBlobsForPath(ctx, sx, sy, cam, path, color) {
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
    // Single BLOBS layer; the data path comes from the global PATH
    // selector via currentPath(). Cam-view runtime owns the on/off
    // boolean per cam; toolbar handler flips PATH → calls redrawAll.
    // Ring colour: cam-encoded for live (matches 3D ray cam-colour
    // convention), single accent for svr.
    window.BallTrackerCamView.registerLayer('detection_blobs', function (ctx, sx, sy, cam) {
      const path = currentPath();
      const color = path === 'live' ? colorForCamPath(cam.camera_id, 'live') : ACCENT;
      _drawBlobsForPath(ctx, sx, sy, cam, path, color);
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

  // Reflect the active SegmentRecord's release speed in the speed badge
  // pinned bottom-left of #scene. Active segment is the one whose
  // [t_start, t_end] contains `currentT`; in "all" mode (no scrubber
  // active) we fall back to segment 0. Hidden when SEGMENTS is empty
  // (e.g. session predates segments persistence and migration script
  // hasn't been run, or the segmenter found nothing).
  function updateSpeedBadge() {
    const badge = document.getElementById("viewer-speed-badge");
    if (!badge) return;
    const segs = currentSegments();
    if (!Array.isArray(segs) || !segs.length) {
      badge.hidden = true;
      return;
    }
    const playback = mode !== "all";
    const idx = playback ? activeSegmentIndex(currentT) : 0;
    const seg = segs[idx >= 0 ? idx : 0];
    badge.hidden = false;
    const speedEl = document.getElementById("viewer-lpb-speed");
    const metaEl = document.getElementById("viewer-lpb-meta");
    if (speedEl) speedEl.textContent = seg.speed_kph.toFixed(1);
    const isActiveByTime = playback
      && currentT >= seg.t_start - 1e-3
      && currentT <= seg.t_end + 1e-3;
    const tag = isActiveByTime ? "live" : (playback ? "nearest" : "release");
    const extra = segs.length > 1
      ? `${PATH_LABEL[currentPath()]} seg${idx} · ${tag} · ${segs.length} segs`
      : `${tag} · rmse ${(seg.rmse_m * 100).toFixed(1)}cm`;
    if (metaEl) metaEl.textContent = extra;
  }
  // BallTrackerCamView's per-cam ResizeObserver handles canvas reflow
  // automatically; no need for a window resize listener here.
