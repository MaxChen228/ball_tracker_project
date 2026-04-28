  function camMarkerTracesFor(c) {
    const color = CAM_COLOR[c.camera_id] || FALLBACK;
    const [cx, cy, cz] = c.center_world;
    const mkLine = (axis, axisColor, length) => ({
      type: "scatter3d",
      x: [cx, cx + length * axis[0]],
      y: [cy, cy + length * axis[1]],
      z: [cz, cz + length * axis[2]],
      mode: "lines",
      line: {color: axisColor, width: 4},
      hoverinfo: "skip",
      showlegend: false,
    });
    return [
      {
        type: "scatter3d",
        x: [cx], y: [cy], z: [cz],
        mode: "markers+text",
        marker: {size: 8, color: color, symbol: "diamond"},
        text: [`Cam ${c.camera_id}`],
        textposition: "top center",
        textfont: {family: "JetBrains Mono, monospace", size: 11, color: "#2A2520"},
        showlegend: false,
        hovertemplate: `Camera ${c.camera_id}<br>x=%{x:.2f} m<br>y=%{y:.2f} m<br>z=%{z:.2f} m<extra></extra>`,
      },
      mkLine(c.axis_forward_world, color, SCENE_THEME.cam_fwd_len_m),
      mkLine(c.axis_right_world, SCENE_THEME.axis_color_right, SCENE_THEME.cam_axis_len_m),
      mkLine(c.axis_up_world, SCENE_THEME.axis_color_up, SCENE_THEME.cam_axis_len_m),
    ];
  }
  function cameraIsAnyPathVisible(camera_id) {
    const group = layerVisibility[`cam${camera_id}`];
    if (!group) return false;
    return PATHS.some(p => group[p] && HAS_PATH[p]);
  }
  function buildDynamicTraces(cutoff, playback) {
    const out = [];
    // --- cameras (diamond + axis triad), gated on the per-cam pipeline pills ---
    for (const c of (SCENE.cameras || [])) {
      if (!cameraIsAnyPathVisible(c.camera_id)) continue;
      for (const t of camMarkerTracesFor(c)) out.push(t);
    }
    // --- rays: one trace per (camera × path), each with its own visibility ---
    const raysByKey = {};
    for (const r of (SCENE.rays || [])) {
      const path = sourceToPath(r.source || "server");
      const camKey = `cam${r.camera_id}`;
      if (!isLayerVisible(camKey, path)) continue;
      const key = `${r.camera_id}|${path}`;
      (raysByKey[key] = raysByKey[key] || []).push(r);
    }
    for (const [key, rays] of Object.entries(raysByKey)) {
      const [cam, path] = key.split("|");
      const color = colorForCamPath(cam, path);
      const {xs, ys, zs} = playback
        ? raysAtT(rays, currentT, PLAYBACK_RAY_TOL)
        : ballDetectedRaysUpTo(rays, cutoff);
      if (!xs.length) continue;
      out.push({ type: "scatter3d", x: xs, y: ys, z: zs, mode: "lines",
        line: {color: color, width: playback ? 3 : 2, dash: PATH_DASH[path]},
        opacity: playback ? 0.95 : PATH_OPACITY[path],
        name: `Rays ${cam} (${PATH_LABEL[path]}, ${Math.floor(xs.length / 3)})`,
        hoverinfo: "skip", showlegend: false });
    }
    // --- ground traces: each scene bucket → exactly one path ---
    const GROUND_BUCKETS = [
      { path: "server_post", traces: SCENE.ground_traces || {} },
      { path: "live", traces: SCENE.ground_traces_live || {} },
    ];
    for (const {path, traces} of GROUND_BUCKETS) {
      for (const [cam, trace] of Object.entries(traces)) {
        if (!isLayerVisible(`cam${cam}`, path)) continue;
        const filtered = trace.filter(p => p.t_rel_s <= cutoff);
        if (!filtered.length) continue;
        const color = colorForCamPath(cam, path);
        // When ANY triangulation path has produced 3D points, de-emphasise
        // ground traces so the trajectory reads as the primary result.
        const dimmed = HAS_TRIANGULATED;
        out.push({ type: "scatter3d",
          x: filtered.map(p => p.x), y: filtered.map(p => p.y), z: filtered.map(p => p.z),
          mode: "lines+markers",
          line: {color: color, width: path === "live" ? 2 : 3, dash: PATH_DASH[path]},
          marker: {size: 3, color: color, symbol: PATH_MARKER_SYMBOL[path]},
          opacity: dimmed ? 0.40 : PATH_OPACITY[path],
          name: `Ground trace ${cam} (${PATH_LABEL[path]}, ${filtered.length} pts)`,
          showlegend: false });
      }
    }
    // --- 3D trajectory: server_post ---
    if (isLayerVisible("traj", "server_post")) {
      const svrPts = (TRAJ_BY_PATH.server_post && TRAJ_BY_PATH.server_post.length)
        ? TRAJ_BY_PATH.server_post : (SCENE.triangulated || []);
      const triPts = filteredTrajectory(svrPts, cutoff);
      if (triPts.length) {
        out.push({ type: "scatter3d", x: triPts.map(p => p.x), y: triPts.map(p => p.y), z: triPts.map(p => p.z),
          mode: "lines+markers", line: {color: ACCENT, width: 4},
          marker: {size: 4, color: ACCENT},
          name: `3D trajectory (svr, ${triPts.length} pts)` });
        if (playback) {
          const head = triPts[triPts.length - 1];
          out.push({ type: "scatter3d", x: [head.x], y: [head.y], z: [head.z],
            mode: "markers", marker: {size: 9, color: ACCENT, symbol: "circle",
              line: {color: "#2A2520", width: 1}},
            hoverinfo: "skip", showlegend: false });
        }
      }
    }
    // --- 3D trajectory: live ---
    if (isLayerVisible("traj", "live")) {
      const livePts = filteredTrajectory(TRAJ_BY_PATH.live || [], cutoff);
      if (livePts.length) {
        out.push({ type: "scatter3d", x: livePts.map(p => p.x), y: livePts.map(p => p.y), z: livePts.map(p => p.z),
          mode: "lines+markers",
          line: {color: "#4A6B8C", width: 3, dash: "dot"},
          marker: {size: 3, color: "#4A6B8C", opacity: 0.7},
          name: `3D trajectory (live, ${livePts.length} pts)` });
        if (playback) {
          const head = livePts[livePts.length - 1];
          out.push({ type: "scatter3d", x: [head.x], y: [head.y], z: [head.z],
            mode: "markers", marker: {size: 7, color: "#4A6B8C", symbol: "diamond",
              line: {color: "#2A2520", width: 1}},
            hoverinfo: "skip", showlegend: false });
        }
      }
    }
    // --- Speed overlay (per-segment colour by instantaneous speed) ---
    // Drawn AFTER regular trajectories so the colourful segments sit on
    // top of the plain coloured line, but BEFORE the fit overlay so the
    // fit dashed curve stays the visual focus when both are on.
    if (_OVL.speedVisible()) {
      const svrPts = (TRAJ_BY_PATH.server_post && TRAJ_BY_PATH.server_post.length)
        ? TRAJ_BY_PATH.server_post : (SCENE.triangulated || []);
      const livePts = TRAJ_BY_PATH.live || [];
      const buckets = [
        { pts: filteredTrajectory(svrPts, cutoff), tag: " · svr", layerOn: isLayerVisible("traj", "server_post") },
        { pts: filteredTrajectory(livePts, cutoff), tag: " · live", layerOn: isLayerVisible("traj", "live") },
      ];
      // Global vmax across all visible buckets so segments are comparable
      // and we only emit one colorbar (stacked colorbars are unreadable).
      let vmaxGlobal = 0;
      for (const b of buckets) {
        if (!b.layerOn || b.pts.length < 2) continue;
        for (const v of _OVL.computeSpeeds(b.pts)) {
          if (Number.isFinite(v) && v > vmaxGlobal) vmaxGlobal = v;
        }
      }
      let firstBucket = true;
      for (const b of buckets) {
        if (!b.layerOn || b.pts.length < 2) continue;
        for (const tr of _OVL.speedTraces(b.pts, {
          tag: b.tag,
          vmaxOverride: vmaxGlobal,
          includeColorbar: firstBucket,
        })) {
          out.push(tr);
        }
        firstBucket = false;
      }
    }
    // --- Fit overlay (drawn on top of regular trajectories) ---
    // Fit is a layer, not a mode: the user picks Residual / Outlier
    // filters, the surviving points are the fit input, and the curve sits
    // on top of the existing trajectory so the viewer can see fit vs raw
    // without flipping modes.
    if (_OVL.fitVisible()) {
      const src = _OVL.fitSource();
      const raw = (src === "live")
        ? (TRAJ_BY_PATH.live || [])
        : ((TRAJ_BY_PATH.server_post && TRAJ_BY_PATH.server_post.length)
            ? TRAJ_BY_PATH.server_post : (SCENE.triangulated || []));
      const source = filteredTrajectory(raw, Infinity);
      if (source.length >= 4) {
        const fit = _OVL.ballisticFit(source);
        const tStart = source[0].t_rel_s;
        const tEnd = playback ? Math.min(cutoff, source[source.length - 1].t_rel_s)
                              : source[source.length - 1].t_rel_s;
        for (const tr of _OVL.fitTraces(fit, tStart, tEnd, {nameSuffix: ` · ${src}`})) {
          out.push(tr);
        }
        updateFitInfoPanel(fit, source.length, src);
      } else {
        updateFitInfoPanel(null, source.length, src);
      }
    } else {
      updateFitInfoPanel(null, 0, null);
    }
    return out;
  }

  // ballisticFit lives in window.BallTrackerOverlays (see overlays_ui.py)
  // — same math powers dashboard + viewer + outlier RANSAC above.

  function updateFitInfoPanel(fit, sampleCount, sourceLabel) {
    const box = document.getElementById("fit-info");
    if (!box) return;
    if (!_OVL.fitVisible()) { box.hidden = true; return; }
    if (!fit) {
      box.hidden = false;
      box.innerHTML = sampleCount
        ? `<h4>Ballistic fit</h4><div class="fit-warn">Need ≥4 filtered points; have ${sampleCount}.</div>`
        : `<h4>Ballistic fit</h4><div class="fit-warn">No triangulated points pass the current filters.</div>`;
      return;
    }
    const v = fit.v0;
    const rows = [
      ["samples", `${sampleCount} (${sourceLabel})`],
      ["flight t", `${fit.flight_time_s.toFixed(3)} s`],
      ["|v₀|", `${fit.speed_mps.toFixed(1)} m/s · ${fit.speed_kmph.toFixed(1)} km/h`],
      ["v₀ (x,y,z)", `(${v.x.toFixed(2)}, ${v.y.toFixed(2)}, ${v.z.toFixed(2)})`],
      ["elevation", `${fit.elevation_deg.toFixed(1)}°`],
      ["azimuth", `${fit.azimuth_deg.toFixed(1)}° from +Y`],
      ["apex z", `${fit.apex_height_m.toFixed(2)} m @ t+${fit.apex_time_s.toFixed(3)}s`],
      ["g_fit", `${fit.g_fit.toFixed(2)} m/s² (free; 9.81 = pure gravity)`],
      ["a (x,y,z)", `(${fit.a.x.toFixed(2)}, ${fit.a.y.toFixed(2)}, ${fit.a.z.toFixed(2)})`],
      ["RMSE (fit)", `${(fit.rmse_m*100).toFixed(1)} cm`],
      ["RMSE (z only)", `${(fit.rmse_by_axis_m.z*100).toFixed(1)} cm`],
    ];
    let html = `<h4>Ballistic fit · g free, per-axis quadratic</h4>`;
    for (const [k, v] of rows) {
      html += `<div class="fit-row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
    }
    // Sanity warning: if RMSE > 10 cm the ballistic assumption (no drag,
    // no spin, pure gravity) is probably wrong — or filters still leak
    // outliers. Tell the user instead of silently fitting garbage.
    if (fit.rmse_m > 0.10) {
      html += `<div class="fit-warn">RMSE ${(fit.rmse_m*100).toFixed(1)} cm — outliers leaked past filters OR motion is non-quadratic (significant jerk). Tighten Residual / Outlier.</div>`;
    }
    box.hidden = false;
    box.innerHTML = html;
  }
  {PLATE_WORLD_JS}
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}

  // Phase 6: virtual reprojection is now drawn as canvas layers on
  // BallTrackerCamView, painted on top of the real video. plate +
  // axes come from the runtime; viewer registers two extra layers
  // (detection_live / detection_svr) that draw the per-frame ball
  // detection blob from each pipeline. Currentframe lookup closes
  // over the viewer's `currentT` clock.
  //
  // Algorithm mirrors cam_view_math.find_detection_index — keep both
  // halves in sync (binary search + left-scan-on-gap + tol gate).
