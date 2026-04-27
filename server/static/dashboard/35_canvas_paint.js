// === canvas repaint + wheel zoom ===

  // Layout is effectively static across the dashboard's lifetime (axes,
  // aspect, uirevision never change — only trace data does). Cache the
  // first layout we see and reuse the SAME object reference on every
  // Plotly.react. Passing the identical reference is the most reliable
  // way to tell Plotly "layout hasn't changed, don't touch the camera or
  // recompute anything scene-related" — stronger than relying solely on
  // uirevision heuristics, and cheap.
  let cachedLayout = null;
  let canvasFirstPaintDone = false;
  // Index of the live-trace inside the plot's data array after the most
  // recent Plotly.react. -1 = not painted yet / stale. extendLivePoint()
  // uses Plotly.extendTraces to append a single point without walking the
  // full trace tree — the per-point append cost drops from ~5-20ms
  // (Plotly.react with full trace rebuild) to <1ms. Any structural change
  // (session flip, mode switch, trajectory toggle) must reset this to -1
  // so the next point event falls back to a full repaint and the slot
  // re-anchors.
  let liveTraceIdx = -1;

  function extendLivePoint(pt) {
    if (liveTraceIdx < 0 || !sceneRoot || !window.Plotly) return false;
    try {
      Plotly.extendTraces(
        sceneRoot,
        {
          x: [[pt.x]],
          y: [[pt.y]],
          z: [[pt.z]],
          'marker.color': [[pt.t_rel_s]],
        },
        [liveTraceIdx],
      );
      return true;
    } catch (_) {
      liveTraceIdx = -1;  // slot invalid — force repaint next time
      return false;
    }
  }

  async function repaintCanvas() {
    if (!basePlot || !window.Plotly) return;
    const extraTraces = [];
    // Load any missing trajectories in parallel — checkbox clicks before
    // the first tick should still paint immediately.
    await Promise.all([...selectedTrajIds].map(sid => ensureTrajLoaded(sid)));
    for (const sid of selectedTrajIds) {
      const result = trajCache.get(sid);
      if (!result) continue;
      extraTraces.push(...trajTracesFor(sid, result, trajColorFor(sid)));
    }
    extraTraces.push(...liveTraces());
    // Fit overlay — shared math with viewer via window.BallTrackerOverlays.
    // Source picks which point bucket feeds the fit; mirroring the viewer's
    // semantics so a user toggling on either page sees the same result.
    if (_OVL.fitVisible()) {
      const src = _OVL.fitSource();
      if (src === 'live' && currentLiveSession && currentLiveSession.session_id) {
        const livePts = (livePointStore.get(currentLiveSession.session_id) || [])
          .map(p => ({ x: p.x, y: p.y, z: p.z, t_rel_s: p.t_rel_s }))
          .sort((a, b) => a.t_rel_s - b.t_rel_s);
        if (livePts.length >= 4) {
          const fit = _OVL.ballisticFit(livePts);
          const t0 = livePts[0].t_rel_s;
          const tEnd = livePts[livePts.length - 1].t_rel_s;
          extraTraces.push(..._OVL.fitTraces(fit, t0, tEnd, { nameSuffix: ' · live' }));
        }
      } else if (src === 'server_post') {
        for (const sid of selectedTrajIds) {
          const result = trajCache.get(sid);
          if (!result || !result.points || result.points.length < 4) continue;
          const pts = result.points.map(p => ({
            x: p.x_m, y: p.y_m, z: p.z_m, t_rel_s: p.t_rel_s,
          }));
          const fit = _OVL.ballisticFit(pts);
          const t0 = pts[0].t_rel_s;
          const tEnd = pts[pts.length - 1].t_rel_s;
          extraTraces.push(..._OVL.fitTraces(fit, t0, tEnd, {
            nameSuffix: ` · ${sid}`,
            color: trajColorFor(sid),
          }));
        }
      }
    }
    if (cachedLayout === null) {
      // One-time build from the first basePlot.layout we see. The server
      // sets scene.uirevision='dashboard-canvas' in both SSR and tick
      // responses — matching the value already embedded by fig.to_html
      // means Plotly never sees a uirevision transition and UI state
      // stays under user control from frame zero.
      cachedLayout = JSON.parse(JSON.stringify(basePlot.layout || {}));
      if (!cachedLayout.scene) cachedLayout.scene = {};
      cachedLayout.scene.uirevision = 'dashboard-canvas';
    }
    // Filter the server-rendered scene's strike-zone traces in or out
    // based on the toggle (default ON). Other static traces (plate,
    // ground, axes, cameras) always pass through.
    const showZone = strikeZoneVisible();
    const baseData = (basePlot.data || []).filter(
      t => showZone || !isStrikeZoneTrace(t)
    );
    const finalTraces = [...baseData, ...extraTraces];
    Plotly.react(
      sceneRoot,
      finalTraces,
      cachedLayout,
      // doubleClick:false — Plotly 3D ships a built-in "reset camera on
      // double-click anywhere in the scene" gesture. Users bump into it
      // accidentally (especially on trackpads where a firm tap registers
      // as dblclick) and it overrides uirevision preservation. Kill it.
      // scrollZoom stays true so the native + our wheel handler both
      // work for panning the eye distance.
      { responsive: true, scrollZoom: true, doubleClick: false },
    );
    // Anchor the live-trace slot for subsequent extendTraces calls. The
    // live trace (when present) is the last one liveTraces() appends.
    liveTraceIdx = -1;
    if (currentLiveSession && currentLiveSession.session_id) {
      for (let i = finalTraces.length - 1; i >= 0; i--) {
        const t = finalTraces[i];
        if (t && typeof t.name === 'string' && t.name.endsWith(' · live')) {
          liveTraceIdx = i;
          break;
        }
      }
    }
    canvasFirstPaintDone = true;
  }

  // Plotly's built-in 3D wheel-zoom is tuned for mouse wheels and feels
  // sluggish on trackpads (especially pinch-to-zoom which arrives as
  // ctrl+wheel with tiny deltas). Replace it with a direct camera.eye
  // scale so each wheel tick = ~10 % distance change and trackpad
  // gestures get the same per-event treatment as a mouse wheel click.
  if (sceneRoot) {
    sceneRoot.addEventListener('wheel', (e) => {
      if (!sceneRoot._fullLayout || !sceneRoot._fullLayout.scene) return;
      const cam = sceneRoot._fullLayout.scene.camera;
      if (!cam || !cam.eye) return;
      e.preventDefault();
      // Wheel-down (positive deltaY) = zoom out, wheel-up = zoom in.
      // Magnitude scaled by sqrt so trackpad's many-tiny-events feels
      // continuous instead of jittery; mouse wheel's chunky events
      // still produce a noticeable but bounded jump per click.
      const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
      const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
      Plotly.relayout(sceneRoot, {
        'scene.camera.eye': {
          x: cam.eye.x * factor,
          y: cam.eye.y * factor,
          z: cam.eye.z * factor,
        },
      });
    }, { passive: false });
  }
