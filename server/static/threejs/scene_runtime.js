// Three.js scene runtime — shared by dashboard `/` and viewer
// `/viewer/{sid}`. Owns the renderer, camera, OrbitControls, the
// fixed-set of static layers (ground / plate / strike zone / world
// axes), and a `setView(name)` API for the 5 fixed camera presets.
//
// Designed to be a black box for the page-specific JS: dashboard +
// viewer construct an instance, call `addLayer(name, object3D)` to
// drop in dynamic content (fit curves, rays, points, trajectory),
// call `setLayerVisible(name, bool)` to toggle, call `setView(name)`
// to snap to a preset. Free orbit drag + wheel zoom come from
// OrbitControls — no custom hacks needed.
//
// Why this exists: we previously rendered via Plotly 3D and spent
// session after session fighting its autofit / aspectmode / camera-
// coords interaction. Three.js has none of those layers — the camera
// position is in real-world metres, the scene fills its container,
// and view presets are one `camera.position.set(...)` + `camera.lookAt`
// call away.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// Strike zone centroid (mid-front-back × X=0 × mid-Z) — the focus
// point for every preset. Matches `view_presets_runtime.py`'s SZC
// for the deprecated Plotly path (will be retired in phase 4).
const SZC = { x: 0, y: 0.216, z: 0.76 };

// Five fixed camera presets. Eye is in world metres (no normalisation
// games), `up=+Z` for ISO/CATCH/SIDE/PITCHER reads naturally as "up
// is up"; TOP uses `up=+Y` so the pitcher direction reads "north".
//
// CATCH / PITCHER eye.z lifted +0.4 above SZC.z so the sight line
// tilts ~10° down toward the plate (z=0); without this the plate
// falls below the default vertical FOV.
const PRESETS = {
  iso:     { eye: [SZC.x + 1.6, SZC.y + 1.6, SZC.z + 0.8], up: [0, 0, 1] },
  catch:   { eye: [SZC.x,        SZC.y - 2.2, SZC.z + 0.4], up: [0, 0, 1] },
  side:    { eye: [SZC.x - 2.2, SZC.y,        SZC.z      ], up: [0, 0, 1] },
  top:     { eye: [SZC.x,        SZC.y,        SZC.z + 2.5], up: [0, 1, 0] },
  pitcher: { eye: [SZC.x,        SZC.y + 2.2, SZC.z + 0.4], up: [0, 0, 1] },
};

const DEFAULT_VIEW = "iso";

// strike-zone visibility shares localStorage key with the legacy
// `BallTrackerOverlays` runtime so the dashboard and viewer keep one
// source of truth across the migration. Same semantics — default on,
// flag persists per-browser.
const STRIKE_ZONE_KEY = "ball_tracker_strike_zone_visible";
function strikeZoneVisible() {
  try {
    const raw = localStorage.getItem(STRIKE_ZONE_KEY);
    return raw === null ? true : raw === "1";
  } catch (_) { return true; }
}
function setStrikeZoneVisiblePersist(on) {
  try { localStorage.setItem(STRIKE_ZONE_KEY, on ? "1" : "0"); } catch (_) {}
}

class BallTrackerScene {
  constructor(container, opts = {}) {
    if (!container) throw new Error("BallTrackerScene needs a container element");
    this.container = container;
    this.theme = opts.theme || readThemeFromDOM();
    this._activeView = DEFAULT_VIEW;
    this._dynamicLayers = new Map(); // name -> { group, visible }
    this._activePillSetter = null;   // optional caller-supplied callback

    // --- renderer ---
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,  // let CSS bg show through (matches --bg)
      powerPreference: "default",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(container.clientWidth || 600, container.clientHeight || 400, false);
    this.renderer.domElement.style.display = "block";
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    container.appendChild(this.renderer.domElement);

    // --- scene ---
    this.scene = new THREE.Scene();
    // No fog. Background is null so the canvas stays transparent
    // and CSS bg paints through (cheap dark-mode swap later).
    this.scene.background = null;
    // Soft ambient so the strike-zone fill mesh reads as translucent
    // rather than flat-shaded black on the back faces.
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.0));

    // --- camera ---
    // FOV 35° gives a mild perspective without strong barrel; the
    // working scene is ~3 m across and we want roughly orthographic
    // feel for the orthogonal presets. Far=50 m is overkill for
    // pitch geometry but cheap.
    this.camera = new THREE.PerspectiveCamera(
      35,
      container.clientWidth / Math.max(1, container.clientHeight),
      0.05,
      50,
    );
    const iso = PRESETS.iso;
    this.camera.position.set(iso.eye[0], iso.eye[1], iso.eye[2]);
    this.camera.up.set(iso.up[0], iso.up[1], iso.up[2]);
    this.camera.lookAt(SZC.x, SZC.y, SZC.z);

    // --- orbit controls ---
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(SZC.x, SZC.y, SZC.z);
    this.controls.enablePan = true;
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.1;
    this.controls.minDistance = 0.4;
    this.controls.maxDistance = 12;
    this.controls.update();
    // First user-drag clears the active preset pill. plotly_relayouting
    // analogue from the old runtime — same UX contract.
    this.controls.addEventListener("start", () => this._onUserInteract());

    // --- static layers ---
    this._buildStaticLayers();

    // --- resize handling ---
    this._ro = new ResizeObserver(() => this._onResize());
    this._ro.observe(container);

    // --- render loop ---
    this._renderLoop = () => {
      this._raf = requestAnimationFrame(this._renderLoop);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    };
    this._raf = requestAnimationFrame(this._renderLoop);

    // --- initial visibility from localStorage ---
    this.setLayerVisible("strike_zone", strikeZoneVisible());
  }

  // ---- static layer construction ----
  _buildStaticLayers() {
    const t = this.theme;
    const root = new THREE.Group();
    root.name = "static";
    this._staticRoot = root;
    this.scene.add(root);

    // ground plane (Z=0)
    const g = t.ground.half_extent_m;
    const groundGeom = new THREE.PlaneGeometry(g * 2, g * 2);
    const groundMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(t.colors.border_l),
      transparent: true,
      opacity: 0.18,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const ground = new THREE.Mesh(groundGeom, groundMat);
    ground.name = "ground";
    root.add(ground);

    // home plate — pentagon mesh + outline
    const plateShape = new THREE.Shape();
    plateShape.moveTo(t.plate.x[0], t.plate.y[0]);
    for (let i = 1; i < t.plate.x.length; ++i) {
      plateShape.lineTo(t.plate.x[i], t.plate.y[i]);
    }
    plateShape.lineTo(t.plate.x[0], t.plate.y[0]);
    const plateGeom = new THREE.ShapeGeometry(plateShape);
    const plateMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(t.colors.surface),
      transparent: true,
      opacity: 0.95,
      side: THREE.DoubleSide,
    });
    const plate = new THREE.Mesh(plateGeom, plateMat);
    plate.name = "plate";
    root.add(plate);
    // outline — closed loop
    const plateOutlinePts = [];
    for (let i = 0; i < t.plate.x.length; ++i) {
      plateOutlinePts.push(new THREE.Vector3(t.plate.x[i], t.plate.y[i], 0.001));
    }
    plateOutlinePts.push(plateOutlinePts[0].clone());
    const plateOutlineGeom = new THREE.BufferGeometry().setFromPoints(plateOutlinePts);
    const plateOutlineMat = new THREE.LineBasicMaterial({
      color: new THREE.Color(t.colors.ink),
      linewidth: 1, // browsers ignore values >1; the visual width comes from antialiasing
    });
    const plateOutline = new THREE.Line(plateOutlineGeom, plateOutlineMat);
    plateOutline.name = "plate_outline";
    root.add(plateOutline);

    // strike zone — wireframe + translucent fill
    const sz = t.strike_zone;
    const szWidth = sz.x_half_m * 2;
    const szDepth = sz.y_back_m - sz.y_front_m;
    const szHeight = sz.z_top_m - sz.z_bottom_m;
    const szGeom = new THREE.BoxGeometry(szWidth, szDepth, szHeight);
    const szFillMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(sz.line_width ? t.colors.strike_zone : t.colors.strike_zone),
      transparent: true,
      opacity: sz.fill_opacity,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const szFill = new THREE.Mesh(szGeom, szFillMat);
    szFill.position.set(0, (sz.y_front_m + sz.y_back_m) / 2, (sz.z_bottom_m + sz.z_top_m) / 2);
    szFill.name = "strike_zone_fill";
    // Wireframe overlay (12 edges of the box) on top of the fill.
    const szEdges = new THREE.EdgesGeometry(szGeom);
    const szEdgeMat = new THREE.LineBasicMaterial({
      color: new THREE.Color(t.colors.strike_zone),
    });
    const szWire = new THREE.LineSegments(szEdges, szEdgeMat);
    szWire.position.copy(szFill.position);
    szWire.name = "strike_zone_wire";
    const szGroup = new THREE.Group();
    szGroup.name = "strike_zone";
    szGroup.add(szFill);
    szGroup.add(szWire);
    root.add(szGroup);
    this._strikeZoneGroup = szGroup;

    // world axes — three short lines from origin (X red, Y blue, Z grey)
    const axesGroup = new THREE.Group();
    axesGroup.name = "world_axes";
    const axisLen = t.axes.world_len_m;
    const axisMaterials = [
      new THREE.LineBasicMaterial({ color: new THREE.Color(t.colors.dev) }),       // X
      new THREE.LineBasicMaterial({ color: new THREE.Color(t.colors.contra) }),    // Y
      new THREE.LineBasicMaterial({ color: new THREE.Color(t.colors.ink_40) }),    // Z
    ];
    const axisDirs = [
      [axisLen, 0, 0],
      [0, axisLen, 0],
      [0, 0, axisLen],
    ];
    for (let i = 0; i < 3; ++i) {
      const geom = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0, 0, 0),
        new THREE.Vector3(axisDirs[i][0], axisDirs[i][1], axisDirs[i][2]),
      ]);
      axesGroup.add(new THREE.Line(geom, axisMaterials[i]));
    }
    root.add(axesGroup);
  }

  // ---- dynamic layer API ----
  addLayer(name, object3D) {
    if (this._dynamicLayers.has(name)) this.removeLayer(name);
    this.scene.add(object3D);
    this._dynamicLayers.set(name, { group: object3D, visible: true });
  }

  removeLayer(name) {
    const entry = this._dynamicLayers.get(name);
    if (!entry) return;
    this.scene.remove(entry.group);
    disposeObject(entry.group);
    this._dynamicLayers.delete(name);
  }

  setLayerVisible(name, visible) {
    // built-in static layers: strike_zone, ground, plate, plate_outline, world_axes
    const staticLayer = this._staticRoot.getObjectByName(name);
    if (staticLayer) {
      staticLayer.visible = !!visible;
      if (name === "strike_zone") setStrikeZoneVisiblePersist(visible);
      return;
    }
    const entry = this._dynamicLayers.get(name);
    if (entry) {
      entry.visible = !!visible;
      entry.group.visible = !!visible;
    }
  }

  hasLayer(name) {
    return this._dynamicLayers.has(name)
      || !!this._staticRoot.getObjectByName(name);
  }

  // ---- view preset API ----
  setView(name) {
    const preset = PRESETS[name];
    if (!preset) return;
    this._activeView = name;
    this.camera.up.set(preset.up[0], preset.up[1], preset.up[2]);
    this.camera.position.set(preset.eye[0], preset.eye[1], preset.eye[2]);
    this.controls.target.set(SZC.x, SZC.y, SZC.z);
    this.controls.update();
    if (this._activePillSetter) this._activePillSetter(name);
  }

  // The dashboard / viewer toolbar wires the click handlers via this
  // hook. Click → setView(name) → setActivePill(name); user-drag fires
  // OrbitControls 'start' → _onUserInteract → setActivePill(null).
  bindViewToolbar(toolbarEl, opts = {}) {
    if (!toolbarEl) return;
    const buttons = Array.from(toolbarEl.querySelectorAll(".view-preset[data-view]"));
    if (!buttons.length) return;
    const setActive = (name) => {
      for (const b of buttons) b.classList.toggle("active", b.dataset.view === name);
    };
    this._activePillSetter = setActive;
    for (const b of buttons) {
      b.addEventListener("click", () => this.setView(b.dataset.view));
    }
    setActive(this._activeView);
  }

  _onUserInteract() {
    // First drag/zoom after a snap clears the active pill — view is
    // no longer "pinned to preset". Same UX as the previous Plotly
    // plotly_relayouting hook.
    if (this._activePillSetter) this._activePillSetter(null);
  }

  _onResize() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (w === 0 || h === 0) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  // ---- tear-down ----
  dispose() {
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    this.controls.dispose();
    for (const [, entry] of this._dynamicLayers) disposeObject(entry.group);
    disposeObject(this._staticRoot);
    this.renderer.dispose();
    if (this.renderer.domElement.parentNode === this.container) {
      this.container.removeChild(this.renderer.domElement);
    }
  }
}

function disposeObject(obj) {
  obj.traverse((node) => {
    if (node.geometry) node.geometry.dispose();
    if (node.material) {
      if (Array.isArray(node.material)) node.material.forEach((m) => m.dispose());
      else node.material.dispose();
    }
  });
}

function readThemeFromDOM() {
  const el = document.getElementById("bt-scene-theme");
  if (!el) throw new Error("BallTrackerScene: missing #bt-scene-theme JSON payload");
  return JSON.parse(el.textContent);
}

// Page-renderer entry point. Mounts a new scene onto an element with
// the given id; exposes the instance on `window.BallTrackerScene` for
// the page-specific JS bundles to read.
export function mountScene(containerId) {
  const container = document.getElementById(containerId);
  if (!container) {
    console.warn(`mountScene: no element with id "${containerId}" — scene not mounted`);
    return null;
  }
  const inst = new BallTrackerScene(container);
  window.BallTrackerScene = inst;
  return inst;
}

export { BallTrackerScene, PRESETS, SZC };
