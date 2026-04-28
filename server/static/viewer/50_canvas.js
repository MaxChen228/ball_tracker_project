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
    // chain_filter rejected_jump / server_post frame gaps leave runs of
    // det=false. Without left-scan the dot blanks across the gap; walk
    // back to the nearest detected frame still within tol so it sticks.
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
  if (window.BallTrackerCamView) {
    window.BallTrackerCamView.registerLayer('detection_live', function (ctx, sx, sy, cam) {
      _drawDetectionForPath(ctx, sx, sy, cam, 'live', colorForCamPath(cam.camera_id, 'live'));
    });
    window.BallTrackerCamView.registerLayer('detection_svr', function (ctx, sx, sy, cam) {
      _drawDetectionForPath(ctx, sx, sy, cam, 'server_post', ACCENT);
    });
    for (const c of (SCENE.cameras || [])) {
      if (c.fx == null || c.R_wc == null || c.t_wc == null
          || c.image_width_px == null || c.image_height_px == null) continue;
      window.BallTrackerCamView.setMeta(c.camera_id, c);
    }
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
    // virtual canvases + speed-bars are NOT called here on purpose. Both
    // are scheduled on their own RAF (scheduleVirtualDraw /
    // scheduleSpeedBarsDraw) so a heavy Plotly.react redraw can't stall
    // the cheap canvas2D paints. During playback the virtual cameras
    // need to stay locked to the video clock, even when the 3D scene
    // drops a frame.
  }
  let speedBarsRaf = null;
  function scheduleVirtualDraw() {
    if (virtualDrawRaf !== null) return;
    virtualDrawRaf = requestAnimationFrame(() => { virtualDrawRaf = null; drawVirtuals(); });
  }
  function scheduleSpeedBarsDraw() {
    if (speedBarsRaf !== null) return;
    if (typeof _renderSpeedBars !== "function") return;
    speedBarsRaf = requestAnimationFrame(() => { speedBarsRaf = null; _renderSpeedBars(); });
  }
  // Fans out to all three paint paths. Each owns its own RAF, so the
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
    if (_OVL.speedVisible()) scheduleSpeedBarsDraw();
  }
  // BallTrackerCamView's per-cam ResizeObserver handles canvas reflow
  // automatically; no need for a window resize listener here.
