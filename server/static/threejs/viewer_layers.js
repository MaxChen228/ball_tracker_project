// Viewer-specific Three.js layers — what /viewer/{sid} shows beyond
// the static ground/plate/strike-zone:
//
//   - cameras:      per-cam diamond + axis triad, gated by the per-cam
//                   pipeline pills (visible iff at least one of that
//                   cam's paths is enabled)
//   - rays:         per-(cam × path) ray bundle. In "all" mode show all
//                   rays up to `cutoff`; in "playback" mode show only
//                   rays at `currentT` (within tol).
//   - ground:       per-cam ground projection trace per path
//   - traj_svr:     3D server_post trajectory line + head marker
//   - traj_live:    3D live trajectory line + head marker
//   - fit_<i>:      one fit-segment parabola per SegmentRecord, with
//                   active-segment highlight + a marker on the curve at
//                   `currentT` during playback
//
// The legacy Plotly viewer rebuilt EVERY trace on every Plotly.react.
// Three.js is dispatch-style: `setT(t)` rebuilds only the dynamic
// layers that depend on t, leaving cameras / fit curves / ground
// traces alone. Static-vs-dynamic split per layer keeps scrub
// responsiveness above 60 fps even on the iPad.

import * as THREE from "three";

const SEG_PALETTE = [
  0xE45756, 0x4C78A8, 0x54A24B, 0xF58518,
  0xB279A2, 0x72B7B2, 0xFF9DA6, 0x9D755D,
];

const ACCENT_SVR = 0xC0392B;
const TRAJ_LIVE = 0x4A6B8C;
const G_Z = -9.81;
// Same ±tol as Plotly-era raysAtT — a single decoded frame's worth.
const PLAYBACK_RAY_TOL = 0.010;

const PATH_LIVE = "live";
const PATH_SVR = "server_post";
const PATHS = [PATH_LIVE, PATH_SVR];

// Per-(path, cam) colour scheme, mirrors viewer 00_boot.js PATH_COLORS.
const PATH_COLORS = {
  [PATH_LIVE]: { A: 0xB8451F, B: 0xE08B5F },
  [PATH_SVR]:  { A: 0x4A6B8C, B: 0x89A5BD },
};
function colorForCamPath(cam, path, fallback) {
  return (PATH_COLORS[path] && PATH_COLORS[path][cam]) || fallback;
}

function lineFromBuffer(buf, color, opts = {}) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
  return new THREE.Line(geom, new THREE.LineBasicMaterial({
    color: new THREE.Color(color),
    transparent: opts.opacity != null,
    opacity: opts.opacity ?? 1.0,
    depthWrite: opts.depthWrite ?? true,
  }));
}

function lineSegmentsFromPairs(pairs, color, opts = {}) {
  // pairs: array of [x0,y0,z0,x1,y1,z1]
  const buf = new Float32Array(pairs.length * 6);
  for (let i = 0; i < pairs.length; ++i) {
    const p = pairs[i];
    buf[i * 6 + 0] = p[0]; buf[i * 6 + 1] = p[1]; buf[i * 6 + 2] = p[2];
    buf[i * 6 + 3] = p[3]; buf[i * 6 + 4] = p[4]; buf[i * 6 + 5] = p[5];
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
  return new THREE.LineSegments(geom, new THREE.LineBasicMaterial({
    color: new THREE.Color(color),
    transparent: opts.opacity != null,
    opacity: opts.opacity ?? 1.0,
  }));
}

function pointMarker(p, color, radius = 0.030) {
  const geom = new THREE.SphereGeometry(radius, 16, 12);
  const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(color) });
  const m = new THREE.Mesh(geom, mat);
  m.position.set(p[0], p[1], p[2]);
  return m;
}

function sampleSegmentCurve(seg, n) {
  const out = new Float32Array(n * 3);
  const t0 = seg.t_start, t1 = seg.t_end, ta = seg.t_anchor;
  const p0 = seg.p0, v0 = seg.v0;
  for (let i = 0; i < n; ++i) {
    const t = t0 + (t1 - t0) * (i / (n - 1));
    const tau = t - ta;
    out[i * 3 + 0] = p0[0] + v0[0] * tau;
    out[i * 3 + 1] = p0[1] + v0[1] * tau;
    out[i * 3 + 2] = p0[2] + v0[2] * tau + 0.5 * G_Z * tau * tau;
  }
  return out;
}

function evalSegmentAt(seg, t) {
  const tau = t - seg.t_anchor;
  return [
    seg.p0[0] + seg.v0[0] * tau,
    seg.p0[1] + seg.v0[1] * tau,
    seg.p0[2] + seg.v0[2] * tau + 0.5 * G_Z * tau * tau,
  ];
}

class ViewerLayers {
  constructor(scene, opts) {
    this.scene = scene;
    this.SCENE = opts.SCENE;
    this.SEGMENTS = opts.SEGMENTS;
    this.TRAJ_BY_PATH = opts.TRAJ_BY_PATH || { server_post: [], live: [] };
    this.HAS_TRIANGULATED = opts.HAS_TRIANGULATED || false;
    this.fallbackColor = opts.fallbackColor || 0x999999;
    this.t = opts.tInitial ?? 0;
    this.mode = opts.mode || "all";  // "all" | "playback"
    // Per-(cam | traj, path) visibility — nested shape matching the
    // legacy IIFE's 20_filters.js `layerVisibility`:
    //   { camA: { live, server_post }, camB: { ... }, traj: { ... } }
    // Defaults follow SSR `<button aria-pressed>` initial state baked
    // into viewer_page.py.
    this.layerVisibility = opts.layerVisibility || {
      camA: { live: true, server_post: true },
      camB: { live: true, server_post: true },
      traj: { live: false, server_post: true },
    };

    // --- one-time cameras + ground traces + fit curves ---
    this._buildCameras();
    this._buildGroundTraces();
    this._buildFitCurves();
    // --- t-dependent layers (rays / traj / fit marker) ---
    this._rebuildDynamic();
  }

  // ---- camera markers ----
  _buildCameras() {
    const group = new THREE.Group();
    group.name = "viewer_cameras";
    const t = this.scene.theme;
    for (const c of (this.SCENE.cameras || [])) {
      if (!c || !c.center_world) continue;
      const camColor = (t.camera_colors && t.camera_colors[c.camera_id]) || this.fallbackColor;
      const cg = new THREE.Group();
      cg.name = `cam_${c.camera_id}`;
      const center = c.center_world;
      // Diamond
      const dia = new THREE.Mesh(
        new THREE.OctahedronGeometry(0.04),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(camColor) }),
      );
      dia.position.set(center[0], center[1], center[2]);
      cg.add(dia);
      // Axis triad
      for (const [axis, color, len] of [
        [c.axis_forward_world, camColor, t.axes.camera_forward_len_m],
        [c.axis_right_world, t.colors.dev, t.axes.camera_axis_len_m],
        [c.axis_up_world, t.colors.ink_40, t.axes.camera_axis_len_m],
      ]) {
        if (!axis) continue;
        const buf = new Float32Array([
          center[0], center[1], center[2],
          center[0] + axis[0] * len, center[1] + axis[1] * len, center[2] + axis[2] * len,
        ]);
        cg.add(lineFromBuffer(buf, color));
      }
      group.add(cg);
    }
    this.scene.addLayer("viewer_cameras", group);
    this._cameraGroup = group;
    this._applyCameraVisibility();
  }

  _isVisible(layer, path) {
    return !!(this.layerVisibility[layer] && this.layerVisibility[layer][path]);
  }

  _applyCameraVisibility() {
    if (!this._cameraGroup) return;
    for (const cg of this._cameraGroup.children) {
      const camId = cg.name.replace(/^cam_/, "");
      // Visible iff any of this cam's pipeline pills is on.
      const anyOn = PATHS.some((p) => this._isVisible(`cam${camId}`, p));
      cg.visible = anyOn;
    }
  }

  // ---- ground traces ----
  _buildGroundTraces() {
    const group = new THREE.Group();
    group.name = "viewer_ground";
    const buckets = [
      { path: PATH_SVR, traces: this.SCENE.ground_traces || {} },
      { path: PATH_LIVE, traces: this.SCENE.ground_traces_live || {} },
    ];
    for (const { path, traces } of buckets) {
      for (const [cam, trace] of Object.entries(traces)) {
        if (!trace || !trace.length) continue;
        const buf = new Float32Array(trace.length * 3);
        for (let i = 0; i < trace.length; ++i) {
          buf[i * 3 + 0] = trace[i].x;
          buf[i * 3 + 1] = trace[i].y;
          buf[i * 3 + 2] = trace[i].z;
        }
        const color = colorForCamPath(cam, path, this.fallbackColor);
        const opacity = this.HAS_TRIANGULATED ? 0.40 : 0.55;
        const line = lineFromBuffer(buf, color, { opacity });
        line.userData = { cam, path, ts: trace.map((p) => p.t_rel_s) };
        line.name = `ground_${cam}_${path}`;
        group.add(line);
      }
    }
    this.scene.addLayer("viewer_ground", group);
    this._groundGroup = group;
    this._applyGroundVisibility();
  }

  _applyGroundVisibility() {
    if (!this._groundGroup) return;
    for (const line of this._groundGroup.children) {
      const { cam, path } = line.userData || {};
      if (!cam || !path) continue;
      line.visible = this._isVisible(`cam${cam}`, path);
    }
  }

  // ---- fit curves ----
  _buildFitCurves() {
    const group = new THREE.Group();
    group.name = "viewer_fit_curves";
    const segs = Array.isArray(this.SEGMENTS) ? this.SEGMENTS : [];
    for (let i = 0; i < segs.length; ++i) {
      const buf = sampleSegmentCurve(segs[i], 64);
      const color = SEG_PALETTE[i % SEG_PALETTE.length];
      const line = lineFromBuffer(buf, color, { opacity: 0.55 });
      line.userData = { segIdx: i };
      line.name = `fit_seg_${i}`;
      group.add(line);
    }
    this.scene.addLayer("viewer_fit_curves", group);
    this._fitGroup = group;
    this._applyFitActiveHighlight();
  }

  _applyFitActiveHighlight() {
    if (!this._fitGroup) return;
    const segs = Array.isArray(this.SEGMENTS) ? this.SEGMENTS : [];
    const playback = this.mode === "playback";
    for (const line of this._fitGroup.children) {
      const i = line.userData.segIdx;
      const seg = segs[i];
      if (!seg) continue;
      const isActive = playback
        && this.t >= seg.t_start - 1e-3
        && this.t <= seg.t_end + 1e-3;
      // Three.js LineBasicMaterial doesn't support `linewidth > 1` on
      // most browsers; we encode the active state via opacity instead.
      line.material.opacity = isActive ? 1.0 : 0.55;
      line.material.transparent = !isActive;
      line.material.needsUpdate = true;
    }
  }

  // ---- t-dependent rays / traj / fit marker ----
  _rebuildDynamic() {
    this.scene.removeLayer("viewer_rays");
    this.scene.removeLayer("viewer_traj_svr");
    this.scene.removeLayer("viewer_traj_live");
    this.scene.removeLayer("viewer_fit_marker");

    const playback = this.mode === "playback";
    const cutoff = playback ? this.t : Infinity;

    // Rays — group by (cam, path). All rays at currentT (within tol)
    // during playback; all rays up to cutoff in "all" mode.
    const raysByKey = new Map();
    for (const r of (this.SCENE.rays || [])) {
      const path = r.source === "live" ? PATH_LIVE : PATH_SVR;
      if (!this._isVisible(`cam${r.camera_id}`, path)) continue;
      const key = `${r.camera_id}|${path}`;
      let arr = raysByKey.get(key);
      if (!arr) { arr = []; raysByKey.set(key, arr); }
      arr.push(r);
    }
    const raysGroup = new THREE.Group();
    raysGroup.name = "viewer_rays";
    for (const [key, rays] of raysByKey) {
      const [cam, path] = key.split("|");
      const color = colorForCamPath(cam, path, this.fallbackColor);
      let pairs = [];
      if (playback) {
        // Pick rays whose t_rel_s is within tol of currentT — same
        // contract as Plotly-era raysAtT.
        let bestT = null, bestDt = Infinity;
        for (const r of rays) {
          const dt = Math.abs(r.t_rel_s - this.t);
          if (dt <= PLAYBACK_RAY_TOL && dt < bestDt) { bestT = r.t_rel_s; bestDt = dt; }
        }
        if (bestT !== null) {
          for (const r of rays) {
            if (r.t_rel_s !== bestT) continue;
            pairs.push([r.origin[0], r.origin[1], r.origin[2],
                        r.endpoint[0], r.endpoint[1], r.endpoint[2]]);
          }
        }
      } else {
        for (const r of rays) {
          if (r.t_rel_s > cutoff) continue;
          pairs.push([r.origin[0], r.origin[1], r.origin[2],
                      r.endpoint[0], r.endpoint[1], r.endpoint[2]]);
        }
      }
      if (!pairs.length) continue;
      const opacity = playback ? 0.95 : 0.55;
      raysGroup.add(lineSegmentsFromPairs(pairs, color, { opacity }));
    }
    if (raysGroup.children.length) this.scene.addLayer("viewer_rays", raysGroup);

    // Trajectories
    if (this._isVisible("traj", PATH_SVR)) {
      const svrPts = (this.TRAJ_BY_PATH.server_post && this.TRAJ_BY_PATH.server_post.length)
        ? this.TRAJ_BY_PATH.server_post : (this.SCENE.triangulated || []);
      const filtered = svrPts.filter((p) => p.t_rel_s <= cutoff);
      if (filtered.length) {
        const buf = new Float32Array(filtered.length * 3);
        for (let i = 0; i < filtered.length; ++i) {
          buf[i * 3 + 0] = filtered[i].x;
          buf[i * 3 + 1] = filtered[i].y;
          buf[i * 3 + 2] = filtered[i].z;
        }
        const group = new THREE.Group();
        group.name = "viewer_traj_svr";
        group.add(lineFromBuffer(buf, ACCENT_SVR));
        if (playback) {
          const head = filtered[filtered.length - 1];
          group.add(pointMarker([head.x, head.y, head.z], ACCENT_SVR, 0.030));
        }
        this.scene.addLayer("viewer_traj_svr", group);
      }
    }
    if (this._isVisible("traj", PATH_LIVE)) {
      const livePts = (this.TRAJ_BY_PATH.live || []).filter((p) => p.t_rel_s <= cutoff);
      if (livePts.length) {
        const buf = new Float32Array(livePts.length * 3);
        for (let i = 0; i < livePts.length; ++i) {
          buf[i * 3 + 0] = livePts[i].x;
          buf[i * 3 + 1] = livePts[i].y;
          buf[i * 3 + 2] = livePts[i].z;
        }
        const group = new THREE.Group();
        group.name = "viewer_traj_live";
        group.add(lineFromBuffer(buf, TRAJ_LIVE, { opacity: 0.7 }));
        if (playback) {
          const head = livePts[livePts.length - 1];
          group.add(pointMarker([head.x, head.y, head.z], TRAJ_LIVE, 0.024));
        }
        this.scene.addLayer("viewer_traj_live", group);
      }
    }

    // Active fit-segment marker (the "predicted ball position at this t").
    if (playback) {
      const segs = Array.isArray(this.SEGMENTS) ? this.SEGMENTS : [];
      for (let i = 0; i < segs.length; ++i) {
        const seg = segs[i];
        if (this.t < seg.t_start - 1e-3 || this.t > seg.t_end + 1e-3) continue;
        const color = SEG_PALETTE[i % SEG_PALETTE.length];
        const p = evalSegmentAt(seg, this.t);
        const group = new THREE.Group();
        group.name = "viewer_fit_marker";
        group.add(pointMarker(p, color, 0.030));
        this.scene.addLayer("viewer_fit_marker", group);
        break;  // only one segment can be active at any t
      }
    }
  }

  // ---- public API ----
  setT(t, mode) {
    this.t = t;
    if (mode != null && mode !== this.mode) {
      this.mode = mode;
    }
    this._rebuildDynamic();
    this._applyFitActiveHighlight();
  }

  setMode(mode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this._rebuildDynamic();
    this._applyFitActiveHighlight();
  }

  // Toggle a single (layer, path) flag and refresh affected layers.
  // `layer` matches the IIFE's nested visibility shape: 'camA' / 'camB'
  // / 'traj'. Path is 'live' or 'server_post'.
  setLayerVisibility(layer, path, visible) {
    if (!this.layerVisibility[layer]) this.layerVisibility[layer] = {};
    this.layerVisibility[layer][path] = !!visible;
    if (layer.startsWith("cam")) {
      this._applyCameraVisibility();
      this._applyGroundVisibility();
      this._rebuildDynamic();
    } else if (layer === "traj") {
      this._rebuildDynamic();
    }
  }

  // Sync the entire visibility map at once (used after a localStorage
  // restore in 20_filters.js or a bulk panel update).
  syncVisibility(layerVisibility) {
    this.layerVisibility = layerVisibility;
    this._applyCameraVisibility();
    this._applyGroundVisibility();
    this._rebuildDynamic();
  }
}

export function setupViewerLayers(scene, opts) {
  const layers = new ViewerLayers(scene, opts);
  window.BallTrackerViewerScene = layers;
  const toolbar = document.querySelector(".scene-views");
  if (toolbar) scene.bindViewToolbar(toolbar);
  return layers;
}
