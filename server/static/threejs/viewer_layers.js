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
import {
  SEG_PALETTE,
  POINTS_OUTLIER,
  POINT_SIZE_M_DEFAULT,
  POINT_SIZE_OUTLIER_RATIO,
  pointsCloud,
  readPersistedPointSizeM,
  writePersistedPointSizeM,
  applyPointSizeToGroup,
} from "./points_layer.js";

const ACCENT_SVR = 0xC0392B;
const TRAJ_LIVE = 0x4A6B8C;
const G_Z = -9.81;
// Same ±tol as raysAtT in the previous implementation — a single decoded
// frame's worth.
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
  let lines;
  if (opts.dashed) {
    lines = new THREE.LineSegments(geom, new THREE.LineDashedMaterial({
      color: new THREE.Color(color),
      transparent: opts.opacity != null,
      opacity: opts.opacity ?? 1.0,
      dashSize: 0.04,
      gapSize: 0.025,
    }));
    // LineDashedMaterial requires per-vertex distance attribute or
    // every segment renders as a solid line — silent visual fallback,
    // exactly the trap to avoid.
    lines.computeLineDistances();
  } else {
    lines = new THREE.LineSegments(geom, new THREE.LineBasicMaterial({
      color: new THREE.Color(color),
      transparent: opts.opacity != null,
      opacity: opts.opacity ?? 1.0,
    }));
  }
  return lines;
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
    // Layer visibility — matches 20_filters.js v4 schema:
    //   { traj: {live, server_post}, rays: {live, server_post}, fit: bool }
    // Cam-pose / ground projection visibility is derived from `rays`
    // (any path on → cam visible) since the operator no longer
    // distinguishes A vs B at the toggle level.
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
    if (layer === "fit") return !!this.layerVisibility.fit;
    return !!(this.layerVisibility[layer] && this.layerVisibility[layer][path]);
  }

  _applyCameraVisibility() {
    if (!this._cameraGroup) return;
    // Cam diamond visible iff any rays path is on. Per-cam toggling was
    // retired in v4 — operator distinguishes A/B by ray colour, not by
    // hiding one cam's pose anchor.
    const anyOn = PATHS.some((p) => this._isVisible("rays", p));
    for (const cg of this._cameraGroup.children) {
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
      const { path } = line.userData || {};
      if (!path) continue;
      line.visible = this._isVisible("rays", path);
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
    // Honour the persisted fit toggle on (re)build — without this, the
    // SSE-driven setSessionData path (which tears down + rebuilds fit
    // curves) would reset visibility to true regardless of the operator's
    // checkbox state.
    this._fitGroup.visible = !!this.layerVisibility.fit;
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
      // BOTH-mode encoding: live rays dashed, svr rays solid. Cam axis
      // (A red / B blue) stays in the colour channel; path axis moves to
      // line style so the two channels don't fight when both pills are on.
      raysGroup.add(lineSegmentsFromPairs(pairs, color, {
        opacity,
        dashed: path === PATH_LIVE,
      }));
    }
    if (raysGroup.children.length) this.scene.addLayer("viewer_rays", raysGroup);

    // Trajectories — points (not lines) so each detected ball position
    // is individually visible and the operator can read clustering /
    // outliers at a glance. Matches dashboard's "Show points" rendering
    // (PointsMaterial + sizeAttenuation true → world-space size that
    // shrinks with camera distance like a real sphere).
    const sizeM = this._pointSize;
    // BOTH-mode α attenuation: when LIVE + SVR traj are both on, push
    // svr to the back (α=0.45) and keep live up front. live is the
    // production pipeline, svr is the debug oracle — operator wants
    // live emphasised when comparing.
    const trajBoth = this._isVisible("traj", PATH_LIVE) && this._isVisible("traj", PATH_SVR);
    if (this._isVisible("traj", PATH_SVR)) {
      // Use scene.triangulated (sorted + render-dist-filtered + each
      // point stamped with `seg_idx` by reconstruct.py) — server is the
      // single source of truth for index→segment classification. We do
      // NOT classify client-side from SegmentRecord.original_indices:
      // those index into the pre-filter sorted points list which is
      // not what TRAJ_BY_PATH.server_post (unsorted!) ships.
      const svrAll = this.SCENE.triangulated || [];
      const buckets = new Map();  // segIdx | "out" -> [points]
      let lastVisible = null;
      for (let i = 0; i < svrAll.length; ++i) {
        const p = svrAll[i];
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
        group.name = "viewer_traj_svr";
        for (const [key, pts] of buckets) {
          const isOut = key === "out";
          const color = isOut ? POINTS_OUTLIER : SEG_PALETTE[Number(key) % SEG_PALETTE.length];
          const baseOpacity = isOut ? 0.55 : 1.0;
          group.add(pointsCloud(pts, color, isOut ? sizeM * POINT_SIZE_OUTLIER_RATIO : sizeM, {
            opacity: trajBoth ? baseOpacity * 0.45 : baseOpacity,
            isOutlier: isOut,
          }));
        }
        if (playback && lastVisible) {
          // Head sphere — "current ball position at t" affordance, sized
          // a bit larger than the cloud so it reads as the active marker.
          group.add(pointMarker([lastVisible.x, lastVisible.y, lastVisible.z], ACCENT_SVR, sizeM * 1.6));
        }
        this.scene.addLayer("viewer_traj_svr", group);
      }
    }
    if (this._isVisible("traj", PATH_LIVE)) {
      const livePts = (this.TRAJ_BY_PATH.live || []).filter(
        (p) => p.t_rel_s <= cutoff && residualPasses(p) && costPassesPoint(p)
      );
      if (livePts.length) {
        const group = new THREE.Group();
        group.name = "viewer_traj_live";
        // Live trail has no per-point segment classification (live
        // pipeline does not run the segmenter). Single accent colour.
        // BOTH-mode: keep live α=0.85 baseline (already lower than the
        // svr cloud's 1.0); svr drops to 0.45 to push it back instead.
        group.add(pointsCloud(livePts, TRAJ_LIVE, sizeM, { opacity: 0.85 }));
        if (playback) {
          const head = livePts[livePts.length - 1];
          group.add(pointMarker([head.x, head.y, head.z], TRAJ_LIVE, sizeM * 1.4));
        }
        this.scene.addLayer("viewer_traj_live", group);
      }
    }

    // Active fit-segment marker (the "predicted ball position at this t").
    if (playback && this._isVisible("fit")) {
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
  // Mutate trajectory point spheres live (PointsMaterial.size in world
  // metres). Walks the two trajectory layers; no geometry rebuild.
  setPointSize(sizeM) {
    if (!Number.isFinite(sizeM)) return;
    this._pointSize = sizeM;
    writePersistedPointSizeM(sizeM);
    for (const name of ["viewer_traj_svr", "viewer_traj_live"]) {
      const layer = this.scene.getLayer && this.scene.getLayer(name);
      applyPointSizeToGroup(layer, sizeM);
    }
  }
  pointSizeM() { return this._pointSize; }

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
  // `layer` is 'traj' or 'rays'. Path is 'live' or 'server_post'. Fit is
  // a boolean toggle on its own surface — see `setFitVisibility`.
  setLayerVisibility(layer, path, visible) {
    if (!this.layerVisibility[layer]) this.layerVisibility[layer] = {};
    this.layerVisibility[layer][path] = !!visible;
    if (layer === "rays") {
      this._applyCameraVisibility();
      this._applyGroundVisibility();
      this._rebuildDynamic();
    } else if (layer === "traj") {
      this._rebuildDynamic();
    }
  }

  setFitVisibility(visible) {
    this.layerVisibility.fit = !!visible;
    if (this._fitGroup) this._fitGroup.visible = !!visible;
    // Active-fit-marker is a dynamic layer rebuilt on setT — flush it
    // now so the operator sees fit hide instantly, not on next scrub.
    this._rebuildDynamic();
  }

  // Sync the entire visibility map at once (used after a localStorage
  // restore in 20_filters.js or a bulk panel update).
  syncVisibility(layerVisibility) {
    this.layerVisibility = layerVisibility;
    this._applyCameraVisibility();
    this._applyGroundVisibility();
    this._rebuildDynamic();
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
    const toSceneDict = (p) => ({
      t_rel_s: p.t_rel_s,
      x: p.x_m,
      y: p.y_m,
      z: p.z_m,
      residual_m: p.residual_m,
      // cost_a / cost_b ride along so _passCostFilterPoint can mask
      // freshly-loaded data the same way it masks server-rendered DATA.scene.
      // seg_idx is stamped client-side from `payload.segments` /
      // `_classifyPointsBySegment`; pass through if a server-rendered
      // payload already has it (legacy SSE path).
      cost_a: (p.cost_a == null ? null : p.cost_a),
      cost_b: (p.cost_b == null ? null : p.cost_b),
      seg_idx: (typeof p.seg_idx === "number" ? p.seg_idx : undefined),
    });
    const points = Array.isArray(payload.points) ? payload.points : [];
    this.SCENE.triangulated = points.map(toSceneDict);
    const tbp = payload.triangulated_by_path || {};
    const newTrajByPath = {};
    for (const key of Object.keys(tbp)) {
      newTrajByPath[key] = (tbp[key] || []).map(toSceneDict);
    }
    this.TRAJ_BY_PATH = newTrajByPath;
    this.SEGMENTS = Array.isArray(payload.segments) ? payload.segments : [];
    this.HAS_TRIANGULATED = this.SCENE.triangulated.length > 0;
    // Fit curves are static-rebuilt from segments; tear down + rebuild.
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
  // Slider seed + bind here, not in the classic IIFE — by the time
  // setupViewerLayers runs the layers controller is mounted, so the
  // initial DOM value can reflect the persisted size loud-and-correct
  // instead of silently displaying the Python-rendered default while
  // the materials use a different size from localStorage.
  bindViewerPointSizeSlider(layers, "#viewer-point-size");
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
