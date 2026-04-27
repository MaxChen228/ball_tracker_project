// === trace builders ===
  // The strike zone is now rendered server-side in render_scene._build_figure
  // so it appears in the dashboard canvas, the per-session viewer, and any
  // other consumer of the shared scene. Visibility is governed by the
  // strike-zone toggle (defaults ON, persisted in localStorage).
  const STRIKE_ZONE_VISIBLE_KEY = 'ball_tracker_strike_zone_visible';
  function strikeZoneVisible() {
    try {
      const raw = localStorage.getItem(STRIKE_ZONE_VISIBLE_KEY);
      if (raw === null) return true;
      return raw === '1';
    } catch (_) { return true; }
  }
  function setStrikeZoneVisible(on) {
    try { localStorage.setItem(STRIKE_ZONE_VISIBLE_KEY, on ? '1' : '0'); } catch (_) {}
  }
  function isStrikeZoneTrace(t) {
    return !!(t && t.meta && t.meta.feature === 'strike_zone');
  }

  function inspectTracesFor(sid, result, color) {
    const raw = result.points || [];
    if (!raw.length) return [];
    return [{
      type: 'scatter3d',
      mode: 'lines+markers',
      x: raw.map(p => p.x_m),
      y: raw.map(p => p.y_m),
      z: raw.map(p => p.z_m),
      line: { color, width: 3, dash: 'dot' },
      marker: { color, size: 2, opacity: 0.6 },
      name: `${sid} · path`,
      hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
      customdata: raw.map(p => p.t_rel_s),
      showlegend: true,
    }];
  }

  function replayTracesFor(sid, result, color) {
    const raw = result.points || [];
    const bounds = trajectoryBounds(raw);
    if (!bounds) return inspectTracesFor(sid, result, color);
    const tActive = bounds.t0 + playheadFrac * (bounds.t1 - bounds.t0);
    const ball = sampleTrajectory(raw, tActive);
    if (!ball) return [];
    const trailWindowS = 0.12;
    const trailPts = raw.filter(p => p.t_rel_s >= (tActive - trailWindowS) && p.t_rel_s <= tActive);
    if (!trailPts.length || trailPts[trailPts.length - 1].t_rel_s < tActive) {
      trailPts.push(ball);
    }
    return [
      {
        type: 'scatter3d', mode: 'lines',
        x: raw.map(p => p.x_m),
        y: raw.map(p => p.y_m),
        z: raw.map(p => p.z_m),
        line: { color, width: 4 },
        name: `${sid} · path`,
        hovertemplate: `${sid}<extra></extra>`,
        showlegend: true,
        opacity: 0.45,
      },
      {
        type: 'scatter3d', mode: 'lines',
        x: trailPts.map(p => p.x_m),
        y: trailPts.map(p => p.y_m),
        z: trailPts.map(p => p.z_m),
        line: { color, width: 6 },
        name: `${sid} · trail`,
        hoverinfo: 'skip',
        showlegend: false,
        opacity: 0.8,
      },
      {
        type: 'scatter3d', mode: 'markers',
        x: [ball.x_m], y: [ball.y_m], z: [ball.z_m],
        marker: {
          color: '#D9A441', size: 9, symbol: 'circle',
          line: { color: '#4A3E24', width: 1.5 },
        },
        name: `${sid} · ball`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
        customdata: [tActive - bounds.t0],
        showlegend: false,
      },
    ];
  }

  function trajTracesFor(sid, result, color) {
    return canvasMode === 'replay'
      ? replayTracesFor(sid, result, color)
      : inspectTracesFor(sid, result, color);
  }

  function ghostTrace(pts, sid) {
    // Rendered before the active-session trace so the active one paints
    // on top. Alpha kept low — this is a "camera framing hasn't moved"
    // visual cue, not a thing to compare against.
    return {
      type: 'scatter3d',
      mode: 'lines',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      line: { color: 'rgba(192,57,43,0.20)', width: 2 },
      name: `${sid} · ghost`,
      hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function liveTraces() {
    const traces = [];
    // Ghost preview of the previous live session — shown BETWEEN arm
    // cycles (no current session armed) so the operator can confirm
    // camera framing still matches the last pitch's trail before
    // throwing again. Suppressed once a new session arms to avoid
    // clutter on the active canvas.
    if (
      (!currentLiveSession || !currentLiveSession.session_id) &&
      lastEndedLiveSid
    ) {
      const ghostPts = livePointStore.get(lastEndedLiveSid) || [];
      if (ghostPts.length) traces.push(ghostTrace(ghostPts, lastEndedLiveSid));
    }
    if (!currentLiveSession || !currentLiveSession.session_id) return traces;
    const sid = currentLiveSession.session_id;
    const rayByCam = liveRayStore.get(sid);
    if (rayByCam) {
      const colors = { A: 'rgba(74,107,140,0.34)', B: 'rgba(211,84,0,0.34)' };
      for (const [cam, rays] of rayByCam.entries()) {
        if (!rays.length) continue;
        const xs = [], ys = [], zs = [];
        for (const r of rays) {
          xs.push(r.origin[0], r.endpoint[0], null);
          ys.push(r.origin[1], r.endpoint[1], null);
          zs.push(r.origin[2], r.endpoint[2], null);
        }
        traces.push({
          type: 'scatter3d',
          mode: 'lines',
          x: xs,
          y: ys,
          z: zs,
          line: { color: colors[cam] || 'rgba(42,37,32,0.28)', width: 2 },
          name: `${sid} · live rays ${cam}`,
          hoverinfo: 'skip',
          showlegend: true,
        });
      }
    }
    const pts = livePointStore.get(sid) || [];
    if (!pts.length) return traces;
    traces.push({
      type: 'scatter3d',
      mode: 'lines+markers',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      marker: {
        size: 4,
        color: pts.map(p => p.t_rel_s),
        colorscale: 'YlOrRd',
        opacity: 0.95,
      },
      line: { color: '#C0392B', width: 4 },
      name: `${sid} · live`,
      hovertemplate: `${sid}<br>t=%{marker.color:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
      showlegend: true,
    });
    return traces;
  }

  function pushLiveRay(sid, cam, ray) {
    let byCam = liveRayStore.get(sid);
    if (!byCam) {
      byCam = new Map();
      liveRayStore.set(sid, byCam);
    }
    const arr = byCam.get(cam) || [];
    arr.push(ray);
    byCam.set(cam, arr);
  }

  function scheduleLiveRayRepaint() {
    if (liveRayPaintPending) return;
    liveRayPaintPending = true;
    requestAnimationFrame(() => {
      liveRayPaintPending = false;
      repaintCanvas();
    });
  }
