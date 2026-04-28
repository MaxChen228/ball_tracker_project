  // === Camera presets =================================================
  // Five fixed views, picked from first principles for what the operator
  // actually analyses in pitch data:
  //
  //   ISO     — isometric overview (3D shape inspection)
  //   CATCH   — from behind catcher looking at pitcher; reads as the
  //             X/Z plane (vertical + horizontal break vs strike zone)
  //   SIDE    — from 1B side looking across; reads as Y/Z plane (arc
  //             shape, vertical drop along travel)
  //   TOP     — bird's-eye looking down; reads as X/Y plane (horizontal
  //             break / tail). up=+Y so pitcher is "north" in the image.
  //   PITCHER — from pitcher looking at catcher; reverse of CATCH for
  //             video-review framing.
  //
  // World frame: X = plate left/right (1B side = -X), Y = depth (pitcher
  // ≈ +Y, catcher ≈ 0), Z = up. Eye coords scale with the data bbox
  // (aspectmode="data" in render_scene.py), so magnitudes ~1.5-2.5
  // produce a comfortable distance regardless of session geometry.
  //
  // Free drag stays as-is — Plotly's native orbit. The first user drag
  // after a snap clears the active pill via plotly_relayouting so the
  // UI honestly reflects "no longer pinned".

  const VIEW_PRESETS = {
    iso:     { eye: {x: 1.5,  y: 1.5,  z: 1.0}, up: {x: 0, y: 0, z: 1}, center: {x: 0, y: 0.2, z: 0.3} },
    catch:   { eye: {x: 0,    y: -1.8, z: 0.4}, up: {x: 0, y: 0, z: 1}, center: {x: 0, y: 0.5, z: 0.4} },
    side:    { eye: {x: -1.8, y: 0.5,  z: 0.4}, up: {x: 0, y: 0, z: 1}, center: {x: 0, y: 0.5, z: 0.4} },
    top:     { eye: {x: 0,    y: 0.5,  z: 2.0}, up: {x: 0, y: 1, z: 0}, center: {x: 0, y: 0.5, z: 0.0} },
    pitcher: { eye: {x: 0,    y: 2.5,  z: 0.5}, up: {x: 0, y: 0, z: 1}, center: {x: 0, y: 0.0, z: 0.3} },
  };

  const viewBtns = Array.from(document.querySelectorAll(".view-preset[data-view]"));

  // Programmatic relayout (button click) also fires plotly_relayouting,
  // which would race with the active-state set. Suppress the auto-clear
  // for one tick around our own snap.
  let suppressClear = false;

  function setActiveView(name) {
    for (const btn of viewBtns) {
      btn.classList.toggle("active", btn.dataset.view === name);
    }
  }
  function clearActiveView() {
    for (const btn of viewBtns) btn.classList.remove("active");
  }

  for (const btn of viewBtns) {
    btn.addEventListener("click", () => {
      const preset = VIEW_PRESETS[btn.dataset.view];
      if (!preset) return;
      suppressClear = true;
      // Deep-clone so Plotly can't mutate our preset table over time.
      const cam = JSON.parse(JSON.stringify(preset));
      Plotly.relayout(sceneDiv, { "scene.camera": cam }).finally(() => {
        setActiveView(btn.dataset.view);
        // One macrotask later release the suppress flag; by then any
        // relayouting events from our snap have already fired.
        setTimeout(() => { suppressClear = false; }, 0);
      });
    });
  }

  // First user drag (or wheel zoom) after a preset snap clears the
  // active pill — the camera is no longer pinned. plotly_relayouting
  // fires throughout the drag, so the chip clears the moment the
  // operator starts moving.
  sceneDiv.on("plotly_relayouting", () => { if (!suppressClear) clearActiveView(); });
