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
  function paintLayerPills() {
    const pills = layerToggles.querySelectorAll(".layer-pill");
    for (const pill of pills) {
      const layer = pill.dataset.layer;
      const path = pill.dataset.path;
      const applicable = hasPathForLayer(layer, path);
      if (!applicable) {
        pill.hidden = true;
        pill.setAttribute("aria-checked", "false");
        continue;
      }
      pill.hidden = false;
      pill.setAttribute("aria-checked", isLayerVisible(layer, path) ? "true" : "false");
    }
    // Fold groups whose pills are all hidden. Skip checkbox-style
    // groups (fit, strike-zone) which have no .layer-pill children.
    for (const group of layerToggles.querySelectorAll(".layer-group")) {
      if (group.querySelector(".layer-checkbox")) continue;
      const anyPill = group.querySelector(".layer-pill:not([hidden])");
      group.hidden = !anyPill;
    }
  }
  paintLayerPills();
  // --- Fit-curve visibility toggle ---
  // Sibling of strike-zone: a top-level boolean (no live/svr split, since
  // the segmenter currently runs on a single authoritative path per
  // session). When dual-segmenter lands, `layerVisibility.fit` flips to
  // a {live, server_post} pair like rays/traj.
  const _fitToggle = document.getElementById("fit-layer-toggle");
  if (_fitToggle) {
    _fitToggle.checked = !!layerVisibility.fit;
    _fitToggle.addEventListener("change", () => {
      layerVisibility.fit = !!_fitToggle.checked;
      persistLayerVisibility();
      if (window.BallTrackerViewerScene) {
        window.BallTrackerViewerScene.setFitVisibility(layerVisibility.fit);
      }
    });
  }
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
  layerToggles.addEventListener("click", (e) => {
    const pill = e.target.closest(".layer-pill");
    if (!pill || pill.hidden || pill.disabled) return;
    const layer = pill.dataset.layer;
    const path = pill.dataset.path;
    // Single-select: clicking the active pill is a no-op (we don't
    // expose a "no path" state here — operator who wants to hide rays
    // entirely uses the FIT checkbox / scene mode toggles, not this
    // group). Clicking the inactive pill switches selection.
    if (layerVisibility[layer] === path) return;
    layerVisibility[layer] = path;
    persistLayerVisibility();
    paintLayerPills();
    if (window.BallTrackerViewerScene) {
      window.BallTrackerViewerScene.setLayerSelection(layer, path);
    }
    renderDetectionStrip();
  });
  // --- Shared 2D-overlay toolbar ---
  // Per-cam toolbars retired in v4. The shared bar fans toggle clicks
  // out to every cam-view mount via BallTrackerCamView.setLayer; the
  // runtime keeps per-cam layer state internally but operator never
  // wants A and B set differently here (left/right placement already
  // encodes the cam axis).
  const sharedBar = document.querySelector("[data-cam-view-shared]");
  if (sharedBar) {
    const camIds = ["A", "B"];
    const blobsGroup = sharedBar.querySelector("[data-blobs-group]");
    sharedBar.addEventListener("click", (e) => {
      const btn = e.target.closest(".cv-layer");
      if (!btn || !window.BallTrackerCamView) return;
      const key = btn.dataset.layer;
      // BLOBS is single-select (live ⇆ svr): clicking active = no-op,
      // clicking inactive switches selection AND turns the sibling
      // pill off. PLATE / AXES are independent on/off pills.
      if (blobsGroup && blobsGroup.contains(btn)) {
        if (btn.classList.contains("on")) return;
        for (const sibling of blobsGroup.querySelectorAll(".cv-layer")) {
          const sKey = sibling.dataset.layer;
          const isPicked = sibling === btn;
          sibling.classList.toggle("on", isPicked);
          sibling.setAttribute("aria-checked", isPicked ? "true" : "false");
          for (const c of camIds) window.BallTrackerCamView.setLayer(c, sKey, isPicked);
        }
        layerVisibility.blobs = key === "detection_blobs_live" ? "live" : "server_post";
        persistLayerVisibility();
        return;
      }
      const next = !btn.classList.contains("on");
      btn.classList.toggle("on", next);
      for (const c of camIds) window.BallTrackerCamView.setLayer(c, key, next);
    });
    const ovl = sharedBar.querySelector(".cv-opacity input[type=range]");
    if (ovl) {
      ovl.addEventListener("input", () => {
        if (!window.BallTrackerCamView) return;
        for (const c of camIds) window.BallTrackerCamView.setOpacity(c, ovl.value);
      });
    }
    // Sync persisted blobs selection on mount: HTML seeds live=on, but
    // localStorage may say svr. Push both states down to the cam-view
    // runtime so its per-cam layer map matches what the shared bar
    // shows — without this, an svr-persisted session boots with the
    // svr pill highlighted but the runtime still drawing live blobs.
    if (blobsGroup && window.BallTrackerCamView) {
      const liveOn = layerVisibility.blobs === "live";
      const liveBtn = blobsGroup.querySelector('[data-layer="detection_blobs_live"]');
      const svrBtn = blobsGroup.querySelector('[data-layer="detection_blobs_svr"]');
      if (liveBtn && svrBtn) {
        liveBtn.classList.toggle("on", liveOn);
        liveBtn.setAttribute("aria-checked", liveOn ? "true" : "false");
        svrBtn.classList.toggle("on", !liveOn);
        svrBtn.setAttribute("aria-checked", !liveOn ? "true" : "false");
      }
      for (const c of camIds) {
        window.BallTrackerCamView.setLayer(c, "detection_blobs_live", liveOn);
        window.BallTrackerCamView.setLayer(c, "detection_blobs_svr", !liveOn);
      }
    }
  }
