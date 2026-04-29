  function setMode(next) { mode = next; modeAll.classList.toggle("active", next === "all"); modePlayback.classList.toggle("active", next === "playback"); scheduleSceneDraw(); }
  modeAll.addEventListener("click", () => setMode("all"));
  modePlayback.addEventListener("click", () => setMode("playback"));
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
  // --- Strike-zone visibility toggle ---
  const _szToggle = document.getElementById("strike-zone-toggle");
  if (_szToggle) {
    _szToggle.checked = strikeZoneVisible();
    _szToggle.addEventListener("change", () => {
      setStrikeZoneVisible(_szToggle.checked);
      scheduleSceneDraw();
    });
  }
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
