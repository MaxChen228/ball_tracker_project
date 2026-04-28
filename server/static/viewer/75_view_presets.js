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

  // Every preset's `center` is locked to the strike-zone centroid so the
  // box stays at the visual middle of the frame regardless of which view
  // the operator picks. Strike zone (server/render_scene_theme.py):
  //   X = 0 (plate centre), Y = (0 + 0.432) / 2 = 0.216,
  //   Z = (0.46 + 1.06) / 2 = 0.76
  // For the four orthogonal views eye is shifted from the centre along
  // exactly one axis so the orthogonal projection reads cleanly; ISO
  // sits diagonally with up=+Z. Eye magnitudes are in data-space (the
  // figure uses aspectmode="data") — ~2 m gives a comfortable orbit
  // distance whether the trajectory is short batting-cage or long
  // pitcher-mound.
  const SZC = { x: 0, y: 0.216, z: 0.76 };
  const VIEW_PRESETS = {
    iso:     { eye: {x: SZC.x + 1.6, y: SZC.y + 1.6, z: SZC.z + 0.8}, up: {x: 0, y: 0, z: 1}, center: SZC },
    catch:   { eye: {x: SZC.x,        y: SZC.y - 2.2, z: SZC.z      }, up: {x: 0, y: 0, z: 1}, center: SZC },
    side:    { eye: {x: SZC.x - 2.2, y: SZC.y,        z: SZC.z      }, up: {x: 0, y: 0, z: 1}, center: SZC },
    top:     { eye: {x: SZC.x,        y: SZC.y,        z: SZC.z + 2.5}, up: {x: 0, y: 1, z: 0}, center: SZC },
    pitcher: { eye: {x: SZC.x,        y: SZC.y + 2.5, z: SZC.z      }, up: {x: 0, y: 0, z: 1}, center: SZC },
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
  //
  // sceneDiv.on is wired by Plotly *after* the first react/newPlot, but
  // this file runs before scheduleSceneDraw() in 99_end.js — calling .on
  // synchronously throws, kills the rest of the IIFE, and STRIP_CAMS in
  // 80_strip.js blows up with TDZ. Retry per-frame until Plotly attaches
  // its event API.
  function _hookRelayouting() {
    if (typeof sceneDiv.on !== "function") {
      requestAnimationFrame(_hookRelayouting);
      return;
    }
    sceneDiv.on("plotly_relayouting", () => { if (!suppressClear) clearActiveView(); });
  }
  _hookRelayouting();
