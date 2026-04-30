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
      // Three.js scene runtime watches its container via ResizeObserver
      // and re-renders on layout change — no explicit Plotly.Plots.resize
      // call needed.
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
    });
    resizer.addEventListener("keydown", (e) => {
      const rect = work.getBoundingClientRect();
      const current = parseFloat(sceneCol.style.flex);
      const frac = Number.isFinite(current) ? current / (current + (parseFloat(videosCol.style.flex) || 1)) : 0.55;
      const step = e.shiftKey ? 0.08 : 0.02;
      if (e.key === "ArrowLeft") { e.preventDefault(); applyFrac(frac - step); }
      else if (e.key === "ArrowRight") { e.preventDefault(); applyFrac(frac + step); }
    });
  })();
  // Three.js OrbitControls handles wheel zoom natively (smooth dolly
  // along camera-to-target axis); no custom wheel hack needed.
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
  // Paint global PATH segmented control + per-layer enable checkboxes.
  // PATH pills carry data-path; layer checkboxes carry data-layer +
  // boolean state on `aria-checked`. Both are persisted via the v6
  // localStorage map; mount-time call here syncs DOM ←─ persisted state.
  function paintLayerPills() {
    const pathGroup = layerToggles.querySelector("[data-path-group]");
    if (pathGroup) {
      const segsByPath = SEGMENTS_BY_PATH || {};
      for (const pill of pathGroup.querySelectorAll(".layer-pill")) {
        const path = pill.dataset.path;
        const applicable = HAS_PATH[path] || HAS_TRAJ_PATH[path];
        pill.hidden = !applicable;
        pill.setAttribute("aria-checked",
          (applicable && currentPath() === path) ? "true" : "false");
        const countEl = pill.querySelector("[data-path-count]");
        if (countEl) {
          const segs = segsByPath[path];
          countEl.textContent = Array.isArray(segs) ? String(segs.length) : "0";
        }
      }
    }
    for (const cb of layerToggles.querySelectorAll(".layer-checkbox[data-layer]")) {
      const layer = cb.dataset.layer;
      if (layer === "fit" || layer === "rays" || layer === "traj") {
        cb.checked = isLayerEnabled(layer);
      }
    }
  }
  paintLayerPills();
  // --- Strike-zone visibility toggle ---
  // Three.js scene runtime owns the wireframe + fill mesh group.
  // Checkbox flips the shared localStorage flag (so dashboard sees
  // the same default on next mount) AND calls into the scene's
  // setLayerVisible API for an instant in-place toggle.
  const _szToggle = document.getElementById("strike-zone-toggle");
  if (_szToggle) {
    _szToggle.checked = strikeZoneVisible();
    _szToggle.addEventListener("change", () => {
      setStrikeZoneVisible(_szToggle.checked);
      if (window.BallTrackerScene) {
        window.BallTrackerScene.setLayerVisible("strike_zone", _szToggle.checked);
      }
    });
  }
  // Point-size slider seed + binding lives in viewer_layers.js's
  // setupViewerLayers — it runs as a deferred ESM, so by the time it
  // touches the DOM `window.BallTrackerViewerScene` is mounted. Doing
  // the bind here would race (this classic IIFE runs first, layers
  // controller still undefined).
  // Global PATH selector — single click on a path pill flips the active
  // path; every enabled layer (rays/traj/fit/blobs) re-drives from the
  // new data source. Re-clicking the active pill is a no-op (we don't
  // expose a "no path" state — turning a layer off goes through its own
  // checkbox).
  layerToggles.addEventListener("click", (e) => {
    const pill = e.target.closest(".layer-pill[data-path]");
    if (!pill || pill.hidden || pill.disabled) return;
    const path = pill.dataset.path;
    if (currentPath() === path) return;
    // setPath owns the mutation. We must NOT pre-write
    // `layerVisibility.path` here: the IIFE's `layerVisibility` and the
    // ViewerLayers controller's `this.layerVisibility` are the same
    // object reference, so a pre-write would make setPath's
    // `this.layerVisibility.path === path` early-return fire and the
    // dynamic rebuild gets skipped entirely. The next slider-drag
    // rebuild would then "fix" the scene, masking the bug.
    if (window.BallTrackerViewerScene) {
      window.BallTrackerViewerScene.setPath(path);
    } else {
      layerVisibility.path = path;  // no scene yet → just persist
    }
    persistLayerVisibility();
    paintLayerPills();
    if (window.BallTrackerCamView) window.BallTrackerCamView.redrawAll();
    renderDetectionStrip();
  });
  // Per-layer enable checkboxes (rays / traj / fit). One handler reads
  // data-layer and forwards to setLayerEnabled. BLOBS lives on the
  // shared 2D toolbar and goes through its own handler below.
  layerToggles.addEventListener("change", (e) => {
    const cb = e.target.closest(".layer-checkbox[data-layer]");
    if (!cb) return;
    const layer = cb.dataset.layer;
    if (layer !== "rays" && layer !== "traj" && layer !== "fit") return;
    layerVisibility[layer] = !!cb.checked;
    persistLayerVisibility();
    if (window.BallTrackerViewerScene) {
      window.BallTrackerViewerScene.setLayerEnabled(layer, !!cb.checked);
    }
  });
  // --- Shared 2D-overlay toolbar ---
  // Per-cam toolbars retired in v4; the shared bar fans toggle clicks
  // out to every cam-view mount via BallTrackerCamView.setLayer. BLOBS
  // is a single boolean (data path = global PATH selector).
  const sharedBar = document.querySelector("[data-cam-view-shared]");
  if (sharedBar) {
    const camIds = ["A", "B"];
    sharedBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".cv-layer");
      if (!btn || !window.BallTrackerCamView) return;
      const key = btn.dataset.layer;
      const next = !btn.classList.contains("on");
      btn.classList.toggle("on", next);
      btn.setAttribute("aria-checked", next ? "true" : "false");
      for (const c of camIds) window.BallTrackerCamView.setLayer(c, key, next);
      if (key === "detection_blobs") {
        layerVisibility.blobs = next;
        persistLayerVisibility();
      }
    });
    const ovl = sharedBar.querySelector(".cv-opacity input[type=range]");
    if (ovl) {
      ovl.addEventListener("input", () => {
        if (!window.BallTrackerCamView) return;
        for (const c of camIds) window.BallTrackerCamView.setOpacity(c, ovl.value);
      });
    }
    // Mount-time sync: push persisted BLOBS state to runtime + button.
    if (window.BallTrackerCamView) {
      const blobsBtn = sharedBar.querySelector('[data-layer="detection_blobs"]');
      const blobsOn = isLayerEnabled("blobs");
      if (blobsBtn) {
        blobsBtn.classList.toggle("on", blobsOn);
        blobsBtn.setAttribute("aria-checked", blobsOn ? "true" : "false");
      }
      for (const c of camIds) {
        window.BallTrackerCamView.setLayer(c, "detection_blobs", blobsOn);
      }
    }
  }
