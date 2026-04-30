// === trace builders ===
  // The strike zone is rendered server-side in render_scene._build_figure
  // so it appears in dashboard, viewer, and any other shared-scene
  // consumer. Visibility helpers come from window.BallTrackerOverlays
  // (server/overlays_ui.py) — keep dashboard + viewer in lock-step.
  const _OVL = window.BallTrackerOverlays;
  const strikeZoneVisible = _OVL.strikeZoneVisible;
  const setStrikeZoneVisible = _OVL.setStrikeZoneVisible;
  const isStrikeZoneTrace = _OVL.isStrikeZoneTrace;

  // Per-segment palette — distinct from camera A/B colours so coloured
  // points don't camouflage with rays. Shared with viewer
  // _VIEWER_SEG_PALETTE so seg0 is the same red on every page.
  const _SEG_PALETTE = [
    '#E45756', '#4C78A8', '#54A24B', '#F58518',
    '#B279A2', '#72B7B2', '#FF9DA6', '#9D755D',
  ];
  const _G = -9.81;  // m/s², z axis only

  // Sample a SegmentRecord into N points along the parabolic curve.
  // p0 + v0·τ + ½·G·τ² with τ = t - t_anchor. Pure compute, kept in JS
  // so result.json doesn't carry the (regenerable) sample array.
  function _sampleSegmentCurve(seg, n) {
    const xs = [], ys = [], zs = [];
    const t0 = seg.t_start, t1 = seg.t_end, t_a = seg.t_anchor;
    const p0 = seg.p0, v0 = seg.v0;
    for (let i = 0; i < n; ++i) {
      const t = t0 + (t1 - t0) * (i / (n - 1));
      const tau = t - t_a;
      xs.push(p0[0] + v0[0] * tau);
      ys.push(p0[1] + v0[1] * tau);
      // gravity pinned to z only (matches segmenter G=(0,0,-9.81)).
      zs.push(p0[2] + v0[2] * tau + 0.5 * _G * tau * tau);
    }
    return { xs, ys, zs };
  }

  function _segmentsFromResult(result) {
    return Array.isArray(result.segments) ? result.segments : [];
  }

  function _classifyPointsBySegment(points, segments) {
    // Return {seg_idx_for_point: int[], any_in_segment: bool}.
    // segment.original_indices index into `points` (server keeps the
    // original-order list authoritative). Point not in any segment ⇒ -1.
    const byPoint = new Array(points.length).fill(-1);
    let any = false;
    for (let i = 0; i < segments.length; ++i) {
      const oi = segments[i].original_indices || [];
      for (const k of oi) {
        if (k >= 0 && k < byPoint.length) {
          byPoint[k] = i;
          any = true;
        }
      }
    }
    return { byPoint, any };
  }

  // Build the per-pitch traces: fit curves + release-point + v0 arrows
  // (always on), and the raw triangulated points (only when Show points
  // toggle is on). Dashed-line ghost overlay for between-arm preview is
  // built separately by `liveTraces` so it stays out of this hot path.
  function pitchTracesFor(sid, result) {
    const traces = [];
    const segments = _segmentsFromResult(result);
    const points = result.points || [];
    const showPts = typeof showPointsEnabled === 'function' && showPointsEnabled();

    if (showPts && points.length) {
      const cls = _classifyPointsBySegment(points, segments);
      // Bucket points by segment so a single Scatter3d trace per
      // colour keeps the legend compact.
      const buckets = new Map();
      for (let i = 0; i < points.length; ++i) {
        const segIdx = cls.byPoint[i];
        const key = segIdx === -1 ? 'none' : String(segIdx);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(points[i]);
      }
      for (const [key, pts] of buckets.entries()) {
        const isOutlier = key === 'none';
        const color = isOutlier ? _PITCH_POINTS_COLOR : _SEG_PALETTE[Number(key) % _SEG_PALETTE.length];
        traces.push({
          type: 'scatter3d',
          mode: 'markers',
          x: pts.map(p => p.x_m),
          y: pts.map(p => p.y_m),
          z: pts.map(p => p.z_m),
          marker: { size: isOutlier ? 3 : 4, color, opacity: isOutlier ? 0.5 : 0.9 },
          name: isOutlier
            ? `${sid} · outliers (${pts.length})`
            : `${sid} · seg${key} pts (${pts.length})`,
          hovertemplate: `t=%{customdata:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
          customdata: pts.map(p => p.t_rel_s),
          showlegend: true,
        });
      }
    }

    if (!segments.length && !showPts && points.length) {
      // Fallback when the segmenter found nothing usable — show the raw
      // path as a thin line so the operator still sees the shape rather
      // than an empty scene. Comment lives here because this is the
      // only "show something rather than nothing" branch in the
      // refactored dashboard.
      traces.push({
        type: 'scatter3d',
        mode: 'lines',
        x: points.map(p => p.x_m),
        y: points.map(p => p.y_m),
        z: points.map(p => p.z_m),
        line: { color: _PITCH_POINTS_COLOR, width: 2, dash: 'dot' },
        name: `${sid} · raw path (no fit)`,
        hovertemplate: `${sid}<extra></extra>`,
        showlegend: true,
      });
    }

    for (let i = 0; i < segments.length; ++i) {
      const seg = segments[i];
      const color = _SEG_PALETTE[i % _SEG_PALETTE.length];
      const samp = _sampleSegmentCurve(seg, 80);
      // Fit curve.
      traces.push({
        type: 'scatter3d',
        mode: 'lines',
        x: samp.xs, y: samp.ys, z: samp.zs,
        line: { color, width: 5, dash: 'dash' },
        name: `${sid} · seg${i} fit (${seg.speed_kph.toFixed(1)} kph, rmse ${(seg.rmse_m * 100).toFixed(1)}cm)`,
        hovertemplate: 'fit<extra></extra>',
        showlegend: true,
      });
      // Release point + v0 unit arrow (length = 0.3 m). Single arrow per
      // segment so multi-bounce events read as "release here, going
      // this way". A degenerate |v0|=0 segment shouldn't survive
      // segmenter.MIN_DISP / MIN_SPEED gates, so a zero magnitude is a
      // bug — skip the arrow but keep the release marker so the operator
      // sees something is wrong rather than the segment vanishing.
      const vmag = Math.hypot(seg.v0[0], seg.v0[1], seg.v0[2]);
      traces.push({
        type: 'scatter3d',
        mode: 'markers',
        x: [seg.p0[0]], y: [seg.p0[1]], z: [seg.p0[2]],
        marker: { size: 7, color, symbol: 'circle', line: { color: '#2A2520', width: 1.2 } },
        name: `${sid} · seg${i} release`,
        hoverinfo: 'skip',
        showlegend: false,
      });
      if (vmag > 0) {
        const arrowLen = 0.3;
        traces.push({
          type: 'scatter3d',
          mode: 'lines',
          x: [seg.p0[0], seg.p0[0] + seg.v0[0] / vmag * arrowLen],
          y: [seg.p0[1], seg.p0[1] + seg.v0[1] / vmag * arrowLen],
          z: [seg.p0[2], seg.p0[2] + seg.v0[2] / vmag * arrowLen],
          line: { color, width: 8 },
          name: `${sid} · seg${i} v0`,
          hoverinfo: 'skip',
          showlegend: false,
        });
      }
    }

    return traces;
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
      line: { color: _PITCH_GHOST_COLOR, width: 2 },
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
      line: { color: _PITCH_FIT_COLOR, width: 4 },
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
