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
//   - traj:         one authority trajectory cloud + head marker
//   - fit_<i>:      one fit-segment parabola per SegmentRecord, with
//                   active-segment highlight + a marker on the curve at
//                   `currentT` during playback
//
// The legacy Plotly viewer rebuilt EVERY trace on every Plotly.react.
// Three.js is dispatch-style: `setT(t)` rebuilds only the dynamic
// layers that depend on t, leaving cameras / fit curves / ground
// traces alone. Static-vs-dynamic split per layer keeps scrub
// responsiveness above 60 fps even on the iPad.
//
// Colour semantics are intentionally role-based, not path-based:
// PATH chooses which detection surface to render, while hue still
// encodes the scene role:
//   - authority trajectory / fit curves: segment palette
//   - active head marker: dashboard accent
//   - rays / ground traces: stable per-camera colours

import * as THREE from "three";
import {
  SEG_PALETTE,
  POINTS_OUTLIER,
  POINT_SIZE_M_DEFAULT,
  POINT_SIZE_OUTLIER_RATIO,
  pointsCloud,
  readPersistedPointSizeM,
  writePersistedPointSizeM,
  applyPointSizeToGroup,
  classifyPointsBySegment,
} from "./points_layer.js";
import {
  buildFitSegmentLines,
  applyResolution,
  applyLineWidth,
  applyActiveHighlight,
  setupFitHoverTooltip,
  bindLayerPopovers,
  readPersistedFitLineWidth,
  writePersistedFitLineWidth,
  readPersistedFitExtensionSeconds,
  writePersistedFitExtensionSeconds,
} from "./fit_curves_layer.js";

const FIT_ACCENT = 0xC0392B;
const G_Z = -9.81;
// Same ±tol as raysAtT in the previous implementation — a single decoded
// frame's worth.
const PLAYBACK_RAY_TOL = 0.010;


const PATH_LIVE = "live";
const PATH_SVR = "server_post";
const PATHS = [PATH_LIVE, PATH_SVR];

function colorForCamera(cam, theme, fallback) {
  return (theme.camera_colors && theme.camera_colors[cam]) || fallback;
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
    this.SEGMENTS_BY_PATH = opts.SEGMENTS_BY_PATH || {};
    this.TRAJ_BY_PATH = opts.TRAJ_BY_PATH || { server_post: [], live: [] };
    this.HAS_TRIANGULATED = opts.HAS_TRIANGULATED || false;
    this.fallbackColor = opts.fallbackColor || 0x999999;
    this.t = opts.tInitial ?? 0;
    this.mode = opts.mode || "all";  // "all" | "playback"
    // Layer visibility — matches 20_filters.js v6 schema:
    //   { path: "live"|"server_post", rays: bool, traj: bool, fit: bool, blobs: bool }
    // PATH drives the whole viewer data surface: rays / ground / 2D
    // overlays / 3D trajectory / fit all pivot together.
    // Caller MUST provide it (the IIFE's `window.VIEWER_DATA.layerVisibility`
    // ships the localStorage-restored map). Falling back to a default
    // here would mask an init-order regression — fail loud per CLAUDE.md.
    if (!opts.layerVisibility) {
      throw new Error("setupViewerLayers: opts.layerVisibility is required");
    }
    this.layerVisibility = opts.layerVisibility;
    // Restored from the cross-page localStorage key on construction;
    // dashboard writes the same key, so a slider tweak on either page
    // carries to the other on next load.
    this._pointSize = readPersistedPointSizeM();
    // Fit-curve display tunables (Line2 linewidth in screen-px, dashed
    // extension padding in seconds). Persisted across pages.
    this._fitLineWidth = readPersistedFitLineWidth();
    this._fitExtensionSec = readPersistedFitExtensionSeconds();

    // --- one-time cameras + ground traces + fit curves ---
    this._buildCameras();
    this._buildGroundTraces();
    this._buildFitCurves();
    // --- t-dependent layers (rays / traj / fit marker) ---
    this._rebuildDynamic();

    // Resize listener — Line2 LineMaterial.resolution must follow the
    // renderer canvas size, otherwise linewidth uniform reads stale
    // px and the lines render at the wrong width on next paint.
    this._resizeHandler = () => this._refreshFitResolution();
    window.addEventListener("resize", this._resizeHandler);
  }

  _refreshFitResolution() {
    if (!this._fitGroup) return;
    const dom = this.scene.renderer && this.scene.renderer.domElement;
    if (!dom) return;
    applyResolution(this._fitGroup, new THREE.Vector2(dom.clientWidth, dom.clientHeight));
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

  _currentPath() { return this.layerVisibility.path; }
  _currentSegments() {
    const segs = this.SEGMENTS_BY_PATH[this._currentPath()];
    return Array.isArray(segs) ? segs : [];
  }
  _currentTrajectory() {
    const pts = this.TRAJ_BY_PATH[this._currentPath()];
    return Array.isArray(pts) ? pts : [];
  }
  _layerOn(layer) { return !!this.layerVisibility[layer]; }
  // True iff the layer's enable flag is on AND its data subset matches the
  // global path.
  _isVisible(layer, path) {
    if (!this._layerOn(layer)) return false;
    if (path == null) return true;
    return path === this._currentPath();
  }

  _applyCameraVisibility() {
    if (!this._cameraGroup) return;
    // Cam diamonds are static reference markers — always visible.
    for (const cg of this._cameraGroup.children) {
      cg.visible = true;
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
        const color = colorForCamera(cam, this.scene.theme, this.fallbackColor);
        const opacity = this.HAS_TRIANGULATED ? 0.40 : 0.55;
        const line = lineFromBuffer(buf, color, { opacity });
        line.userData = { cam, path };
        line.name = `ground_${cam}_${path}`;
        group.add(line);
      }
    }
    this.scene.addLayer("viewer_ground", group);
    this._groundGroup = group;
    this._applyGroundVisibility();
  }

  // Visibility for the ground projection layer.
  //
  // Mode-dependent semantics — *not* a simple rays-toggle proxy:
  //
  //   "all" mode: show the cumulative monocular ground trail per (cam,
  //     path), gated by the rays toggle + path. This is the "where has
  //     each camera's line-of-sight been pointing" reference plane.
  //
  //   "playback" mode: hide entirely. Each ray's `endpoint` is already
  //     clamped to the ray↔z=0 intersection (server: reconstruct.py
  //     `endpoint = ground` when within MAX_RENDER_DIST), so the rays
  //     layer at currentT already paints the per-frame ground hit. A
  //     separate ground polyline would just reconnect the *same* points
  //     across all frames as a chained trail — which visually reads as
  //     "thin filament trajectory on z=0", indistinguishable from a
  //     genuine ball-on-ground path. Operators keep mistaking it for a
  //     bug. Drop the redundancy in playback; the cumulative trail is
  //     what "all" mode is for.
  //
  // Cheap on every setT — only mutates `.visible` flags, no geometry
  // touch.
  _applyGroundVisibility() {
    if (!this._groundGroup) return;
    const playback = this.mode === "playback";
    for (const line of this._groundGroup.children) {
      const { path } = line.userData || {};
      if (!path) continue;
      line.visible = !playback && this._isVisible("rays", path);
    }
  }

  // ---- fit curves ----
  _buildFitCurves() {
    const segs = this._currentSegments();
    const dom = this.scene.renderer && this.scene.renderer.domElement;
    const resolution = new THREE.Vector2(
      dom ? dom.clientWidth : 1,
      dom ? dom.clientHeight : 1,
    );
    const group = buildFitSegmentLines(segs, {
      groupName: "viewer_fit_curves",
      palette: (i) => SEG_PALETTE[i % SEG_PALETTE.length],
      lineWidthPx: this._fitLineWidth,
      prePadSec: this._fitExtensionSec,
      postPadSec: this._fitExtensionSec,
      activeHighlight: true,
      resolution,
    });
    this.scene.addLayer("viewer_fit_curves", group);
    this._fitGroup = group;
    // Honour the persisted fit toggle on (re)build — without this, the
    // SSE-driven setSessionData path (which tears down + rebuilds fit
    // curves) would reset visibility to true regardless of the operator's
    // checkbox state.
    this._fitGroup.visible = this._layerOn("fit");
    this._applyFitActiveHighlight();
  }

  _applyFitActiveHighlight() {
    if (!this._fitGroup) return;
    // Line2 supports real `linewidth` (the previous LineBasicMaterial
    // hack of encoding active via opacity is gone); active segment
    // gets ×1.6 width over the operator-tuned base width.
    applyActiveHighlight(
      this._fitGroup,
      this._currentSegments(),
      this.t,
      this.mode,
      this._fitLineWidth,
    );
  }

  // ---- t-dependent rays / traj / fit marker ----
  _rebuildDynamic() {
    this.scene.removeLayer("viewer_rays");
    this.scene.removeLayer("viewer_traj");
    this.scene.removeLayer("viewer_fit_marker");

    const playback = this.mode === "playback";
    const cutoff = playback ? this.t : Infinity;

    // Drag-preview predicates from the per-session tuning sliders. Both
    // are owned by the viewer's IIFE in 50_canvas.js and exposed via
    // `window` so layer rebuild here can stay decoupled from the slider's
    // DOM. Loud-fail rather than silent fallback (CLAUDE.md): if either
    // is missing, viewer init order is broken and the cost/gap sliders
    // would silently no-op — exactly the regression that motivated this
    // wiring. Read once per rebuild so a slider mutation between calls
    // is picked up on the next rebuild but not mid-loop.
    const candPasses = window._candPassesThreshold;
    const residualPasses = window._passResidualFilter;
    const costPassesPoint = window._passCostFilterPoint;
    if (typeof candPasses !== "function" || typeof residualPasses !== "function"
        || typeof costPassesPoint !== "function") {
      throw new Error("viewer init order broken: _candPassesThreshold / _passResidualFilter / _passCostFilterPoint not on window");
    }

    // Rays — group by (cam, path). All rays at currentT (within tol)
    // during playback; all rays up to cutoff in "all" mode.
    const raysByKey = new Map();
    for (const r of (this.SCENE.rays || [])) {
      // Explicit null/undefined guard: `r.t_rel_s > Infinity` is false
      // in JS even for `undefined`, so an unstamped ray would slip
      // through the all-mode filter while playback's `Math.abs(NaN)`
      // would silently drop it — paths disagreeing on bad data is
      // exactly the silent-fallback class CLAUDE.md forbids.
      if (typeof r.t_rel_s !== "number" || !Number.isFinite(r.t_rel_s)) continue;
      const path = r.source === "live" ? PATH_LIVE : PATH_SVR;
      if (!this._isVisible("rays", path)) continue;
      const key = `${r.camera_id}|${path}`;
      let arr = raysByKey.get(key);
      if (!arr) { arr = []; raysByKey.set(key, arr); }
      arr.push(r);
    }
    const raysGroup = new THREE.Group();
    raysGroup.name = "viewer_rays";
    for (const [key, rays] of raysByKey) {
      const [cam, path] = key.split("|");
      const color = colorForCamera(cam, this.scene.theme, this.fallbackColor);
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
            if (!candPasses({ cost: r.cost })) continue;
            pairs.push([r.origin[0], r.origin[1], r.origin[2],
                        r.endpoint[0], r.endpoint[1], r.endpoint[2]]);
          }
        }
      } else {
        for (const r of rays) {
          if (r.t_rel_s > cutoff) continue;
          if (!candPasses({ cost: r.cost })) continue;
          pairs.push([r.origin[0], r.origin[1], r.origin[2],
                      r.endpoint[0], r.endpoint[1], r.endpoint[2]]);
        }
      }
      if (!pairs.length) continue;
      const opacity = playback ? 0.95 : 0.55;
      raysGroup.add(lineSegmentsFromPairs(pairs, color, { opacity }));
    }
    if (raysGroup.children.length) this.scene.addLayer("viewer_rays", raysGroup);

    // Trajectories — points (not lines) so each detected ball position
    // is individually visible and the operator can read clustering /
    // outliers at a glance. Matches dashboard's "Show points" rendering
    // (PointsMaterial + sizeAttenuation true → world-space size that
    // shrinks with camera distance like a real sphere).
    const sizeM = this._pointSize;
    if (this._isVisible("traj")) {
      const pathPts = this._currentTrajectory();
      const buckets = new Map();  // segIdx | "out" -> [points]
      let lastVisible = null;
      for (let i = 0; i < pathPts.length; ++i) {
        const p = pathPts[i];
        if (p.t_rel_s > cutoff) continue;
        if (!residualPasses(p)) continue;
        if (!costPassesPoint(p)) continue;
        const k = (typeof p.seg_idx === "number") ? p.seg_idx : -1;
        const key = k === -1 ? "out" : String(k);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(p);
        lastVisible = p;
      }
      if (buckets.size) {
        const group = new THREE.Group();
        group.name = "viewer_traj";
        for (const [key, pts] of buckets) {
          const isOut = key === "out";
          const color = isOut ? POINTS_OUTLIER : SEG_PALETTE[Number(key) % SEG_PALETTE.length];
          group.add(pointsCloud(pts, color, isOut ? sizeM * POINT_SIZE_OUTLIER_RATIO : sizeM, {
            opacity: isOut ? 0.55 : 1.0,
            isOutlier: isOut,
          }));
        }
        if (playback && lastVisible) {
          group.add(pointMarker([lastVisible.x, lastVisible.y, lastVisible.z], FIT_ACCENT, sizeM * 1.6));
        }
        this.scene.addLayer("viewer_traj", group);
      }
    }

    // Active fit-segment marker (the "predicted ball position at this t").
    if (playback && this._isVisible("fit")) {
      const segs = this._currentSegments();
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
  // Mutate trajectory point spheres live (PointsMaterial.size in world
  // metres). Walks the two trajectory layers; no geometry rebuild.
  setPointSize(sizeM) {
    if (!Number.isFinite(sizeM)) return;
    this._pointSize = sizeM;
    writePersistedPointSizeM(sizeM);
    for (const name of ["viewer_traj"]) {
      const layer = this.scene.getLayer && this.scene.getLayer(name);
      applyPointSizeToGroup(layer, sizeM);
    }
  }
  pointSizeM() { return this._pointSize; }

  // Fit-curve display tunables. `setFitLineWidth` mutates LineMaterial
  // in place (cheap); `setFitExtensionSeconds` triggers a fit-curves
  // rebuild because the dashed extension geometry depends on it.
  setFitLineWidth(px) {
    if (!Number.isFinite(px)) return;
    this._fitLineWidth = px;
    writePersistedFitLineWidth(px);
    if (this._fitGroup) {
      applyLineWidth(this._fitGroup, px);
      this._applyFitActiveHighlight();
    }
  }
  fitLineWidthPx() { return this._fitLineWidth; }

  setFitExtensionSeconds(sec) {
    if (!Number.isFinite(sec)) return;
    this._fitExtensionSec = sec;
    writePersistedFitExtensionSeconds(sec);
    this.scene.removeLayer("viewer_fit_curves");
    this._buildFitCurves();
  }
  fitExtensionSeconds() { return this._fitExtensionSec; }

  setT(t, mode) {
    this.t = t;
    if (mode != null && mode !== this.mode) {
      this.mode = mode;
    }
    this._rebuildDynamic();
    this._applyFitActiveHighlight();
    this._applyGroundVisibility();
  }

  setMode(mode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this._rebuildDynamic();
    this._applyFitActiveHighlight();
    this._applyGroundVisibility();
  }

  // Switch the global PATH (live / server_post). The whole viewer data
  // surface re-drives from the new source, including 3D traj + fit.
  // No same-path early-return: the IIFE's `layerVisibility` map is the
  // SAME object reference as `this.layerVisibility`, so a caller pre-
  // writing the field (legacy pattern) would make the guard fire and
  // skip the rebuild. Caller-side dedup belongs in the click handler,
  // not here. Idempotent rebuild is cheap.
  setPath(path) {
    if (path !== "live" && path !== "server_post") {
      throw new Error(`setPath: invalid path '${path}'`);
    }
    this.layerVisibility.path = path;
    this.scene.removeLayer("viewer_fit_curves");
    this._buildFitCurves();
    this._applyGroundVisibility();
    this._rebuildDynamic();
  }

  // Toggle a single boolean layer (rays / traj / fit / blobs).
  setLayerEnabled(layer, enabled) {
    if (!(layer in this.layerVisibility) || layer === "path") {
      throw new Error(`setLayerEnabled: unknown boolean layer '${layer}'`);
    }
    this.layerVisibility[layer] = !!enabled;
    if (layer === "fit") {
      if (this._fitGroup) this._fitGroup.visible = this._layerOn("fit");
      // Active-fit-marker is dynamic; flush so toggle takes effect now.
      this._rebuildDynamic();
    } else if (layer === "rays") {
      this._applyGroundVisibility();
      this._rebuildDynamic();
    } else if (layer === "traj") {
      this._rebuildDynamic();
    }
    // `blobs` is owned by the 2D canvas (50_canvas.js); it reads
    // currentPath() / isLayerEnabled('blobs') from the IIFE itself.
  }


  // Patch in the freshly-recomputed SessionResult after the operator
  // hit Apply on the per-session tuning strip. Avoids a full page
  // reload (which re-buffers video, resets scrubber, drops localStorage
  // layer-visibility state). Caller is `_applyTuning` in viewer_page.py.
  //
  // `payload` carries SessionResult.model_dump() shape — `points` /
  // `triangulated_by_path` are lists of TriangulatedPoint dicts with
  // `{x_m, y_m, z_m, ...}` keys, but the viewer's scene/TRAJ_BY_PATH
  // expects `{x, y, z, ...}` (matches what reconstruct._pts_to_dicts
  // emits at first-load time). Convert here so the rest of the viewer
  // sees identical shapes regardless of how it learned the data.
  setSessionData(payload) {
    if (!payload || typeof payload !== "object") {
      throw new Error("setSessionData: missing payload");
    }
    // First-load (page render) ships scene dicts with `seg_idx` already
    // stamped by `reconstruct._pts_to_dicts`. Recompute / refetch ships
    // raw `SessionResult.model_dump()` where the field doesn't exist —
    // `seg_idx` lives on the scene-dict transport, not on the persisted
    // model. Reclassify here against the just-supplied segments so
    // `_rebuildDynamic`'s seg_idx → palette bucketing stays correct
    // across the recompute roundtrip. Without this every point falls
    // into the `"out"` bucket and renders POINTS_OUTLIER (gray).
    const toSceneDict = (p) => ({
      t_rel_s: p.t_rel_s,
      x: p.x_m,
      y: p.y_m,
      z: p.z_m,
      residual_m: p.residual_m,
      cost_a: (p.cost_a == null ? null : p.cost_a),
      cost_b: (p.cost_b == null ? null : p.cost_b),
      seg_idx: undefined,  // filled in below
    });
    const stampSegIdx = (dicts, segments) => {
      const idx = classifyPointsBySegment(dicts, segments || []);
      for (let i = 0; i < dicts.length; ++i) {
        if (idx[i] !== -1) dicts[i].seg_idx = idx[i];
      }
    };
    const segments = Array.isArray(payload.segments) ? payload.segments : [];
    const segmentsByPath = payload.segments_by_path || {};
    const points = Array.isArray(payload.points) ? payload.points : [];
    const sceneDicts = points.map(toSceneDict);
    stampSegIdx(sceneDicts, segments);
    this.SCENE.triangulated = sceneDicts;
    const tbp = payload.triangulated_by_path || {};
    const newTrajByPath = {};
    for (const key of Object.keys(tbp)) {
      const pathDicts = (tbp[key] || []).map(toSceneDict);
      stampSegIdx(pathDicts, segmentsByPath[key] || []);
      newTrajByPath[key] = pathDicts;
    }
    this.TRAJ_BY_PATH = newTrajByPath;
    this.SEGMENTS = segments;
    this.SEGMENTS_BY_PATH = segmentsByPath;
    this.HAS_TRIANGULATED = this.SCENE.triangulated.length > 0;
    this.scene.removeLayer("viewer_fit_curves");
    this._buildFitCurves();
    this._rebuildDynamic();
  }
}

export function setupViewerLayers(scene, opts) {
  const layers = new ViewerLayers(scene, opts);
  window.BallTrackerViewerScene = layers;
  const toolbar = document.querySelector(".scene-views");
  if (toolbar) scene.bindViewToolbar(toolbar);
  // Layer-chip popover wiring: chevron buttons toggle the sibling
  // popover; outside-click + Escape close.
  bindLayerPopovers(document);
  // Slider seed + bind here, not in the classic IIFE — by the time
  // setupViewerLayers runs the layers controller is mounted, so the
  // initial DOM value can reflect the persisted size loud-and-correct
  // instead of silently displaying the Python-rendered default while
  // the materials use a different size from localStorage.
  bindViewerPointSizeSlider(layers, "#viewer-point-size");
  bindViewerFitLineWidthSlider(layers, "#viewer-fit-line-width");
  bindViewerFitExtensionSlider(layers, "#viewer-fit-extension");
  // Hover tooltip — Raycaster on the fit_curves group, shows
  // instantaneous |v(t)| in km/h.
  const tooltipParent = scene.renderer.domElement.parentNode;
  if (tooltipParent) {
    setupFitHoverTooltip({
      scene,
      fitGroupGetter: () => layers._fitGroup,
      segmentsFn: () => layers._currentSegments(),
      tooltipParent,
    });
  }
  return layers;
}

function bindViewerPointSizeSlider(layers, containerSel) {
  const slider = document.querySelector(`${containerSel} [data-point-size-slider]`);
  const readout = document.querySelector(`${containerSel} [data-point-size-readout]`);
  if (!slider) return;
  const seed = layers.pointSizeM();
  slider.value = String(seed);
  if (readout) readout.textContent = `${Math.round(seed * 1000)} mm`;
  slider.addEventListener("input", () => {
    const v = parseFloat(slider.value);
    if (!Number.isFinite(v)) return;
    if (readout) readout.textContent = `${Math.round(v * 1000)} mm`;
    layers.setPointSize(v);
  });
}

function bindViewerFitLineWidthSlider(layers, containerSel) {
  const slider = document.querySelector(`${containerSel} [data-fit-line-width-slider]`);
  const readout = document.querySelector(`${containerSel} [data-fit-line-width-readout]`);
  if (!slider) return;
  const seed = layers.fitLineWidthPx();
  slider.value = String(seed);
  if (readout) readout.textContent = `${seed.toFixed(1)} px`;
  slider.addEventListener("input", () => {
    const v = parseFloat(slider.value);
    if (!Number.isFinite(v)) return;
    if (readout) readout.textContent = `${v.toFixed(1)} px`;
    layers.setFitLineWidth(v);
  });
}

function bindViewerFitExtensionSlider(layers, containerSel) {
  const slider = document.querySelector(`${containerSel} [data-fit-extension-slider]`);
  const readout = document.querySelector(`${containerSel} [data-fit-extension-readout]`);
  if (!slider) return;
  const seed = layers.fitExtensionSeconds();
  slider.value = String(seed);
  if (readout) readout.textContent = `${Math.round(seed * 1000)} ms`;
  slider.addEventListener("input", () => {
    const v = parseFloat(slider.value);
    if (!Number.isFinite(v)) return;
    if (readout) readout.textContent = `${Math.round(v * 1000)} ms`;
    layers.setFitExtensionSeconds(v);
  });
}
