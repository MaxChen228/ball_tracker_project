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
    return out;
  }
  {PLATE_WORLD_JS}
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}

  // Phase 6: virtual reprojection is now drawn as canvas layers on
  // BallTrackerCamView, painted on top of the real video. plate +
  // axes come from the runtime; viewer registers two extra layers
  // (detection_blobs_live / detection_blobs_svr) that draw every
  // shape-gate-passing candidate ring on the matched frame, gated by
  // the session-level cost_threshold slider. Currentframe lookup
  // closes over the viewer's `currentT` clock.
