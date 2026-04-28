  function setMode(next) { mode = next; modeAll.classList.toggle("active", next === "all"); modePlayback.classList.toggle("active", next === "playback"); scheduleSceneDraw(); }
  modeAll.addEventListener("click", () => setMode("all"));
  modePlayback.addEventListener("click", () => setMode("playback"));
  sceneResetBtn.addEventListener("click", () => { Plotly.relayout(sceneDiv, { "scene.camera": DEFAULT_CAMERA }); });
  // Draggable divider between the 3D scene and the 2x2 camera panels.
  // Persists the chosen split so reload keeps the operator's layout.
  (() => {
    const resizer = document.getElementById("col-resizer");
    if (!resizer) return;
    const work = resizer.parentElement;
    const sceneCol = work.querySelector(".scene-col");
    const videosCol = work.querySelector(".videos-col");
    const STORE_KEY = "viewer:col-split-frac";
    function applyFrac(frac) {
      const clamped = Math.max(0.15, Math.min(0.85, frac));
      sceneCol.style.flex = `${clamped} 1 0`;
      videosCol.style.flex = `${1 - clamped} 1 0`;
      try { Plotly.Plots.resize(sceneDiv); } catch (_) {}
    }
    try {
      const saved = parseFloat(localStorage.getItem(STORE_KEY));
      if (Number.isFinite(saved)) applyFrac(saved);
    } catch (_) {}
    let dragging = false;
    function onMove(e) {
      if (!dragging) return;
      const rect = work.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      const frac = x / rect.width;
      applyFrac(frac);
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      const basis = parseFloat(sceneCol.style.flex);
      if (Number.isFinite(basis)) {
        try { localStorage.setItem(STORE_KEY, String(basis)); } catch (_) {}
      }
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    }
    resizer.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      dragging = true;
      resizer.classList.add("dragging");
      document.body.classList.add("col-resizing");
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });
    resizer.addEventListener("dblclick", () => {
      try { localStorage.removeItem(STORE_KEY); } catch (_) {}
      sceneCol.style.flex = "";
      videosCol.style.flex = "";
      try { Plotly.Plots.resize(sceneDiv); } catch (_) {}
    });
    resizer.addEventListener("keydown", (e) => {
      const rect = work.getBoundingClientRect();
      const current = parseFloat(sceneCol.style.flex);
      const frac = Number.isFinite(current) ? current / (current + (parseFloat(videosCol.style.flex) || 1)) : 0.55;
      const step = e.shiftKey ? 0.08 : 0.02;
      if (e.key === "ArrowLeft") { e.preventDefault(); applyFrac(frac - step); }
      else if (e.key === "ArrowRight") { e.preventDefault(); applyFrac(frac + step); }
    });
    window.addEventListener("resize", () => { try { Plotly.Plots.resize(sceneDiv); } catch (_) {} });
  })();
  sceneDiv.addEventListener("wheel", (e) => {
    if (!sceneDiv._fullLayout || !sceneDiv._fullLayout.scene) return;
    const cam = sceneDiv._fullLayout.scene.camera;
    if (!cam || !cam.eye) return;
    e.preventDefault();
    const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
    const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
    Plotly.relayout(sceneDiv, { "scene.camera.eye": { x: cam.eye.x * factor, y: cam.eye.y * factor, z: cam.eye.z * factor } });
  }, { passive: false });
  function setHintOpen(open) { hintOverlay.classList.toggle("open", open); hintBtn.classList.toggle("open", open); hintBtn.setAttribute("aria-expanded", open ? "true" : "false"); }
  hintBtn.addEventListener("click", () => { setHintOpen(!hintOverlay.classList.contains("open")); });
  // One strip-row per pipeline, each hidden until we have data for it. Row
  // id / canvas id pairs are static so the CSS and the JS agree without a
  // parallel config dict.
  const STRIP_ROWS = {
    live: { row: document.getElementById("strip-row-live"), canvas: document.getElementById("detection-canvas-live") },
    server_post: { row: document.getElementById("strip-row-server-post"), canvas: document.getElementById("detection-canvas-server-post") },
  };
  const layerToggles = document.getElementById("layer-toggles");
  const STRIP_MUTED = "rgba(122, 117, 108, 0.35)";
  const STRIP_EMPTY = "rgba(232, 228, 219, 0.6)";
  const STRIP_HEAD = "#2A2520";
  const STRIP_CHIRP = "rgba(230, 179, 0, 0.65)";
  let visibleStripCount = 0;
  for (const path of PATHS) {
    if (HAS_PATH[path]) {
      STRIP_ROWS[path].row.hidden = false;
      visibleStripCount += 1;
    }
  }
  // Surface the multi-pipeline disclaimer only when at least two strips are
  // on screen — otherwise the note is noise.
  const multiNote = document.getElementById("strip-note-multi");
  if (multiNote) multiNote.hidden = visibleStripCount < 2;
  function paintLayerPills() {
    const pills = layerToggles.querySelectorAll(".layer-pill");
    for (const pill of pills) {
      const layer = pill.dataset.layer;
      const path = pill.dataset.path;
      const applicable = hasPathForLayer(layer, path);
      if (!applicable) {
        pill.hidden = true;
        pill.setAttribute("aria-pressed", "false");
        continue;
      }
      pill.hidden = false;
      pill.setAttribute("aria-pressed", isLayerVisible(layer, path) ? "true" : "false");
    }
    for (const sw of layerToggles.querySelectorAll(".layer-name .swatch")) {
      sw.style.background = colorForCamPath(sw.dataset.cam, "server_post");
    }
    // If every pill in a group is hidden, fold the group too — otherwise you
    // get a dangling "Traj" label with nothing under it.
    for (const group of layerToggles.querySelectorAll(".layer-group")) {
      const anyPill = group.querySelector(".layer-pill:not([hidden])");
      group.hidden = !anyPill;
    }
  }
  paintLayerPills();
  // --- Residual filter slider ---
  const residualSlider = document.getElementById("residual-filter-slider");
  const residualReadout = document.getElementById("residual-filter-readout");
  function paintResidualReadout() {
    if (!residualReadout) return;
    if (!Number.isFinite(residualCapM)) residualReadout.textContent = "off";
    else residualReadout.textContent = `≤ ${(residualCapM * 100).toFixed(0)} cm`;
  }
  if (residualSlider) {
    if (Number.isFinite(residualCapM)) {
      residualSlider.value = String(Math.min(200, Math.round(residualCapM * 100)));
    } else {
      residualSlider.value = "200";
    }
    paintResidualReadout();
    residualSlider.addEventListener("input", () => {
      const cm = parseFloat(residualSlider.value);
      if (!Number.isFinite(cm) || cm >= 200) {
        residualCapM = Infinity;
        try { localStorage.removeItem(RESIDUAL_FILTER_KEY); } catch (_e) {}
      } else {
        residualCapM = cm / 100;
        try { localStorage.setItem(RESIDUAL_FILTER_KEY, String(cm)); } catch (_e) {}
      }
      paintResidualReadout();
      scheduleSceneDraw();
    });
  }
  // --- Fit-residual filter slider (k × MAD; slider is 10–60 → 1.0–6.0) ---
  const fitresSlider = document.getElementById("fitres-filter-slider");
  const fitresReadout = document.getElementById("fitres-filter-readout");
  function paintFitresReadout() {
    if (!fitresReadout) return;
    if (!Number.isFinite(fitResKappa)) fitresReadout.textContent = "off";
    else fitresReadout.textContent = `κ ≤ ${fitResKappa.toFixed(1)}`;
  }
  if (fitresSlider) {
    if (Number.isFinite(fitResKappa)) {
      fitresSlider.value = String(Math.round(fitResKappa * 10));
    } else {
      fitresSlider.value = "60";
    }
    paintFitresReadout();
    fitresSlider.addEventListener("input", () => {
      const raw = parseFloat(fitresSlider.value);
      const k = raw / 10.0;
      if (!Number.isFinite(k) || k >= 6.0) {
        fitResKappa = Infinity;
        try { localStorage.removeItem(FITRES_FILTER_KEY); } catch (_e) {}
      } else {
        fitResKappa = k;
        try { localStorage.setItem(FITRES_FILTER_KEY, String(k)); } catch (_e) {}
      }
      paintFitresReadout();
      scheduleSceneDraw();
    });
  }
  // --- Strike-zone visibility toggle ---
  const _szToggle = document.getElementById("strike-zone-toggle");
  if (_szToggle) {
    _szToggle.checked = strikeZoneVisible();
    _szToggle.addEventListener("change", () => {
      setStrikeZoneVisible(_szToggle.checked);
      scheduleSceneDraw();
    });
  }

  // --- Speed overlay toggle + 2D bar chart drawer ---
  // Bar chart mirrors the same per-segment speed array that drives the
  // 3D colours so the operator can read exact m/s values without hovering
  // each tiny segment. When speed is off, the drawer is hidden and the
  // 3D scene falls back to the plain coloured trajectory.
  const _speedToggle = document.getElementById("speed-toggle");
  const _speedBars = document.getElementById("speed-bars");
  function _renderSpeedBars() {
    if (!_speedBars) return;
    if (!_OVL.speedVisible()) { _speedBars.hidden = true; return; }
    // Pick whichever path has more points; ties → server_post.
    const svrPts = (TRAJ_BY_PATH.server_post && TRAJ_BY_PATH.server_post.length)
      ? TRAJ_BY_PATH.server_post : (SCENE.triangulated || []);
    const livePts = TRAJ_BY_PATH.live || [];
    const svrFiltered = filteredTrajectory(svrPts, Infinity);
    const liveFiltered = filteredTrajectory(livePts, Infinity);
    const pick = svrFiltered.length >= liveFiltered.length ? svrFiltered : liveFiltered;
    const sourceLabel = svrFiltered.length >= liveFiltered.length ? "svr" : "live";
    if (pick.length < 2) {
      _speedBars.hidden = false;
      _speedBars.innerHTML = `<div style="font:11px var(--mono); color:var(--sub); padding:8px;">No filtered points to compute speed.</div>`;
      return;
    }
    const speeds = _OVL.computeSpeeds(pick);
    // Bar x = segment MIDPOINT τ (not segment-end) so v reads as
    // "instantaneous speed during this interval" centred where it lives.
    const t0 = pick[0].t_rel_s;
    const taus = [];
    for (let i = 0; i < speeds.length; i++) {
      taus.push(((pick[i].t_rel_s + pick[i + 1].t_rel_s) * 0.5) - t0);
    }
    const validSpeeds = speeds.filter(v => v !== null && Number.isFinite(v));
    const vmax = validSpeeds.reduce((a, b) => Math.max(a, b), 0);
    const colors = speeds.map(v => {
      if (v === null || !Number.isFinite(v)) return "#9C9690";
      return _OVL.viridisColor(vmax > 0 ? v / vmax : 0);
    });
    // Plotly bar accepts null in y → skips that bar entirely. A 0-height
    // bar would look identical to "no data" and (combined with the grey
    // colour) was borderline misleading; null leaves a visible gap in
    // the strip so the user sees the segment exists but has no value.
    const ys = speeds.map(v => (v === null || !Number.isFinite(v)) ? null : v);
    const customdata = speeds.map(v => (v === null || !Number.isFinite(v)) ? null : v * 3.6);
    const trace = {
      type: "bar", x: taus, y: ys,
      marker: { color: colors },
      hovertemplate: `t=%{x:.3f}s<br>v=%{y:.2f} m/s · %{customdata:.1f} km/h<extra></extra>`,
      customdata,
    };
    const layout = {
      margin: {l: 36, r: 8, t: 4, b: 22},
      xaxis: {title: {text: "t (s, anchor-relative)", font: {size: 9}},
               tickfont: {size: 9}, gridcolor: "#E8E4DB"},
      yaxis: {title: {text: `v (m/s) · ${sourceLabel}`, font: {size: 9}},
               tickfont: {size: 9}, gridcolor: "#E8E4DB", rangemode: "tozero"},
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: {family: "JetBrains Mono, monospace", size: 10},
      showlegend: false,
    };
    _speedBars.hidden = false;
    Plotly.react(_speedBars, [trace], layout, {displayModeBar: false, responsive: true});
  }
  if (_speedToggle) {
    _speedToggle.checked = _OVL.speedVisible();
    _speedToggle.addEventListener("change", () => {
      _OVL.setSpeedVisible(_speedToggle.checked);
      // Toggle off → hide drawer immediately, no need for an RAF round-trip.
      if (!_speedToggle.checked && _speedBars) _speedBars.hidden = true;
      scheduleSceneDraw();
    });
  }
  // scheduleSceneDraw() fans out to scheduleSpeedBarsDraw whenever speed
  // is visible — every filter / playback / mode change that touches
  // points already triggers it, so no manual paint hooks are needed.
  // Initial paint:
  if (_OVL.speedVisible()) scheduleSpeedBarsDraw();

  // --- Fit overlay toggle (was modal "fit mode" — now a layer) ---
  const fitToggleBtn = document.getElementById("fit-toggle");
  const fitSourceGroup = document.querySelector(".layer-source-group");
  function syncFitSourceGroupDormant() {
    if (fitSourceGroup) fitSourceGroup.classList.toggle("is-off", !_OVL.fitVisible());
  }
  if (fitToggleBtn) {
    fitToggleBtn.checked = _OVL.fitVisible();
    syncFitSourceGroupDormant();
    fitToggleBtn.addEventListener("change", () => {
      _OVL.setFitVisible(fitToggleBtn.checked);
      syncFitSourceGroupDormant();
      scheduleSceneDraw();
    });
  }
  // --- Fit source selector (svr / live) ---
  const fitSrcPills = Array.from(document.querySelectorAll(".fit-src-pill"));
  function paintFitSourcePills() {
    const cur = _OVL.fitSource();
    for (const btn of fitSrcPills) {
      const src = btn.dataset.src;
      const has = src === "live" ? HAS_TRAJ_PATH.live : HAS_TRAJ_PATH.server_post;
      btn.disabled = !has;
      btn.setAttribute("aria-pressed", (cur === src) ? "true" : "false");
    }
  }
  for (const btn of fitSrcPills) {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      _OVL.setFitSource(btn.dataset.src);
      paintFitSourcePills();
      if (_OVL.fitVisible()) scheduleSceneDraw();
    });
  }
  paintFitSourcePills();
  layerToggles.addEventListener("click", (e) => {
    const pill = e.target.closest(".layer-pill");
    if (!pill || pill.hidden || pill.disabled) return;
    const layer = pill.dataset.layer;
    const path = pill.dataset.path;
    // Refuse to turn off the last visible pipeline *within a cam group* —
    // an all-off group would just remove that camera entirely, which is
    // redundant with hiding the group and confusing as a click result.
    const group = layerVisibility[layer];
    if (!group) return;
    group[path] = !group[path];
    persistLayerVisibility();
    paintLayerPills();
    drawScene();
    renderDetectionStrip();
  });
