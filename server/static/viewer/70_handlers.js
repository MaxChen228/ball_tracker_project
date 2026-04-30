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
  // Vertical resizer for the bottom timeline panel. Drag to grow / shrink
  // the dock; `--timeline-user-h` drives `.timeline { height: ... }`, the
  // existing ResizeObserver in 99_end.js mirrors it into `--timeline-h`
  // so the main view's bottom padding stays in sync. Persists per page
  // load so the operator's preferred dock height survives reloads.
  (() => {
    const resizer = document.getElementById("tl-resizer");
    if (!resizer) return;
    const timeline = resizer.parentElement;
    const viewerEl = document.querySelector(".viewer");
    // v2 schema bump: pre-PR key (`viewer:timeline-h`) could persist
    // values that no longer fit the current layout (more chips, taller
    // legend, stricter clamp). Reading the v2 key forces a fresh start;
    // legacy v1 entries are wiped here so they can't resurface later.
    const STORE_KEY = "viewer:timeline-h-v2";
    try { localStorage.removeItem("viewer:timeline-h"); } catch (_) {}
    // Escape hatch: `?tl-reset=1` in the URL clears the saved height and
    // returns to default. For when the dock height is so wrong the
    // resizer dblclick is unreachable.
    if (typeof URLSearchParams === "function"
        && new URLSearchParams(window.location.search).get("tl-reset") === "1") {
      try { localStorage.removeItem(STORE_KEY); } catch (_) {}
    }
    function clampHeight(px) {
      const minH = 160;
      const maxH = Math.max(minH + 1, Math.round(window.innerHeight * 0.8));
      return Math.max(minH, Math.min(maxH, Math.round(px)));
    }
    function applyHeight(px) {
      const clamped = clampHeight(px);
      timeline.style.setProperty("--timeline-user-h", `${clamped}px`);
      timeline.classList.add("is-resized");
      // Push `--timeline-h` synchronously so the main viewer's
      // padding-bottom never lags behind the drag — without this the
      // ResizeObserver's microtask gap leaves a visible gap above the
      // dock when the user drags rapidly down. Use the actual rendered
      // offsetHeight (post browser layout reconcile) so CSS min/max
      // safety nets stay authoritative.
      if (viewerEl) {
        viewerEl.style.setProperty("--timeline-h", `${timeline.offsetHeight}px`);
      }
      // Strip canvases are 2D — backing-store px must follow CSS px, else
      // the painted bands stretch / pixelate when the row grows.
      if (window._resizeDetectionCanvas) window._resizeDetectionCanvas();
    }
    try {
      const saved = parseFloat(localStorage.getItem(STORE_KEY));
      if (Number.isFinite(saved)) applyHeight(saved);
    } catch (_) {}
    let dragging = false;
    function onMove(e) {
      if (!dragging) return;
      const y = (e.touches ? e.touches[0].clientY : e.clientY);
      // height = distance from the pointer to viewport bottom
      applyHeight(window.innerHeight - y);
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("tl-resizing");
      const cur = parseFloat(timeline.style.getPropertyValue("--timeline-user-h"));
      if (Number.isFinite(cur)) {
        try { localStorage.setItem(STORE_KEY, String(cur)); } catch (_) {}
      }
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    }
    resizer.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      dragging = true;
      resizer.classList.add("dragging");
      document.body.classList.add("tl-resizing");
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    });
    resizer.addEventListener("dblclick", () => {
      try { localStorage.removeItem(STORE_KEY); } catch (_) {}
      timeline.style.removeProperty("--timeline-user-h");
      timeline.classList.remove("is-resized");
      // Force sync `--timeline-h` to the post-reset offsetHeight so the
      // main viewer's padding-bottom catches up immediately rather than
      // waiting on the next ResizeObserver tick.
      if (viewerEl) {
        viewerEl.style.setProperty("--timeline-h", `${timeline.offsetHeight}px`);
      }
      if (window._resizeDetectionCanvas) window._resizeDetectionCanvas();
    });
    resizer.addEventListener("keydown", (e) => {
      const cur = parseFloat(timeline.style.getPropertyValue("--timeline-user-h"))
        || timeline.getBoundingClientRect().height;
      const step = e.shiftKey ? 32 : 8;
      if (e.key === "ArrowUp") { e.preventDefault(); applyHeight(cur + step); }
      else if (e.key === "ArrowDown") { e.preventDefault(); applyHeight(cur - step); }
    });
    // Viewport resize re-clamps the stored user height — saved 800px on a
    // 1000px viewport would otherwise overflow when the operator shrinks
    // the window to 600px.
    window.addEventListener("resize", () => {
      const cur = parseFloat(timeline.style.getPropertyValue("--timeline-user-h"));
      if (Number.isFinite(cur)) applyHeight(cur);
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
  // Layer-chip popover wiring — chevron buttons toggle the sibling
  // popover. Mirrors `bindLayerPopovers` in fit_curves_layer.js so the
  // chips work even when the 3D scene runtime fails to mount (WebGL
  // context loss, vendor file missing, etc.); without this fallback the
  // entire chip toolbar would be inert because the binding lived solely
  // inside `setupViewerLayers`. Idempotent: each toggle's listener guard
  // prevents double-firing if both setups run.
  (() => {
    const toggles = layerToggles.querySelectorAll("[data-popover-target]");
    if (!toggles.length) return;
    const closeAll = (except) => {
      for (const t of toggles) {
        const pop = document.getElementById(t.getAttribute("data-popover-target"));
        if (!pop || pop === except) continue;
        pop.hidden = true;
        t.setAttribute("aria-expanded", "false");
        t.classList.remove("open");
      }
    };
    for (const toggle of toggles) {
      if (toggle.dataset.popoverBound === "1") continue;
      toggle.dataset.popoverBound = "1";
      toggle.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const pop = document.getElementById(toggle.getAttribute("data-popover-target"));
        if (!pop) return;
        const willOpen = pop.hidden;
        closeAll(willOpen ? pop : null);
        pop.hidden = !willOpen;
        toggle.setAttribute("aria-expanded", String(willOpen));
        toggle.classList.toggle("open", willOpen);
      });
    }
    if (!document._tlPopoverOutsideBound) {
      document._tlPopoverOutsideBound = true;
      document.addEventListener("click", (ev) => {
        if (ev.target.closest("[data-popover]")) return;
        if (ev.target.closest("[data-popover-target]")) return;
        closeAll();
      });
      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") closeAll();
      });
    }
  })();
  // Per-layer enable checkboxes — single handler reads data-layer and
  // dispatches: 3D-scene layers (rays / traj / fit) go through
  // BallTrackerViewerScene.setLayerEnabled; cam-view layers (plate /
  // axes / detection_blobs) fan out to both cam mounts.
  const CAM_VIEW_LAYERS = new Set(["plate", "axes", "detection_blobs"]);
  const SCENE_LAYERS = new Set(["rays", "traj", "fit"]);
  const CAM_IDS = ["A", "B"];
  layerToggles.addEventListener("change", (e) => {
    const cb = e.target.closest(".layer-checkbox[data-layer]");
    if (!cb) return;
    const layer = cb.dataset.layer;
    const on = !!cb.checked;
    if (SCENE_LAYERS.has(layer)) {
      layerVisibility[layer] = on;
      persistLayerVisibility();
      if (window.BallTrackerViewerScene) {
        window.BallTrackerViewerScene.setLayerEnabled(layer, on);
      }
    } else if (CAM_VIEW_LAYERS.has(layer)) {
      if (layer === "detection_blobs") {
        layerVisibility.blobs = on;
        persistLayerVisibility();
      }
      if (window.BallTrackerCamView) {
        for (const c of CAM_IDS) window.BallTrackerCamView.setLayer(c, layer, on);
      }
    }
  });
  // Per-layer popover sliders: opacity (`data-layer-opacity`) + line
  // width (`data-layer-line-width`). Single delegated `input` handler so
  // adding a new chip is just adding the markup; no JS change needed.
  layerToggles.addEventListener("input", (e) => {
    const tgt = e.target;
    if (!tgt || tgt.tagName !== "INPUT") return;
    const opacityLayer = tgt.dataset.layerOpacity;
    const lwLayer = tgt.dataset.layerLineWidth;
    if (opacityLayer) {
      const pct = Math.max(0, Math.min(100, parseFloat(tgt.value) || 0));
      const readout = layerToggles.querySelector(`[data-layer-opacity-readout="${opacityLayer}"]`);
      if (readout) readout.textContent = `${Math.round(pct)}%`;
      _applyLayerOpacity(opacityLayer, pct);
    } else if (lwLayer) {
      const px = parseFloat(tgt.value);
      if (!Number.isFinite(px)) return;
      const readout = layerToggles.querySelector(`[data-layer-line-width-readout="${lwLayer}"]`);
      if (readout) readout.textContent = `${px.toFixed(1)} px`;
      _applyLayerLineWidth(lwLayer, px);
    }
  });
  function _applyLayerOpacity(layer, pct) {
    const v = pct / 100;
    if (layer === "rays") {
      if (window.BallTrackerViewerScene && window.BallTrackerViewerScene.setRaysOpacity) {
        window.BallTrackerViewerScene.setRaysOpacity(v);
      }
    } else if (CAM_VIEW_LAYERS.has(layer)) {
      if (window.BallTrackerCamView && window.BallTrackerCamView.setLayerOpacity) {
        for (const c of CAM_IDS) window.BallTrackerCamView.setLayerOpacity(c, layer, v);
      }
    }
  }
  function _applyLayerLineWidth(layer, px) {
    if (layer === "rays") {
      if (window.BallTrackerViewerScene && window.BallTrackerViewerScene.setRaysLineWidth) {
        window.BallTrackerViewerScene.setRaysLineWidth(px);
      }
    } else if (CAM_VIEW_LAYERS.has(layer)) {
      if (window.BallTrackerCamView && window.BallTrackerCamView.setLayerLineWidth) {
        for (const c of CAM_IDS) window.BallTrackerCamView.setLayerLineWidth(c, layer, px);
      }
    }
  }
  // Mount-time sync: push persisted blobs visibility to cam-view runtime,
  // and seed each cam-view's per-layer state from the chip checkboxes.
  if (window.BallTrackerCamView) {
    const blobsOn = isLayerEnabled("blobs");
    layerVisibility.blobs = blobsOn;
    for (const cb of layerToggles.querySelectorAll(`.layer-checkbox[data-layer]`)) {
      const layer = cb.dataset.layer;
      if (!CAM_VIEW_LAYERS.has(layer)) continue;
      const initialOn = layer === "detection_blobs" ? blobsOn : !!cb.checked;
      cb.checked = initialOn;
      for (const c of CAM_IDS) window.BallTrackerCamView.setLayer(c, layer, initialOn);
    }
  }
  // Seed per-layer opacity / line width from the popover sliders' initial
  // values so the renderer state matches what the slider thumb shows on
  // first paint (otherwise renderers default to opacity 1.0 / base width).
  for (const inp of layerToggles.querySelectorAll("input[data-layer-opacity]")) {
    const pct = parseFloat(inp.value);
    if (Number.isFinite(pct)) _applyLayerOpacity(inp.dataset.layerOpacity, pct);
  }
  for (const inp of layerToggles.querySelectorAll("input[data-layer-line-width]")) {
    const px = parseFloat(inp.value);
    if (Number.isFinite(px)) _applyLayerLineWidth(inp.dataset.layerLineWidth, px);
  }
