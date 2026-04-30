// Dashboard-specific Three.js layers — what the dashboard's 3D scene
// shows beyond the static ground/plate/strike-zone (which the shared
// scene runtime owns):
//
//   - cameras: per-camera diamond + axis triad, sourced from
//              /calibration/state's `scene.cameras` list
//   - fit:     fit-segment curves + release markers + v0 arrows
//              for the currently-selected session (one at a time —
//              dashboard 3D answers "what's the latest pitch?", not
//              "compare these N pitches"; multi-overlay was retired
//              in the earlier dashboard refactor)
//   - points:  raw triangulated points coloured by segment, behind
//              an operator-toggleable "Show points" switch
//   - live:    in-progress live session — coloured trail + per-cam
//              rays, refreshed on the WS frame stream tick
//
// All public methods are idempotent: calling `applyCameras(list)` a
// second time replaces the previous camera group rather than
// stacking. This mirrors the previous `Plotly.react(scene, ...)`
// rebuild semantics — repaint is a "set" not an "append" except for
// the explicit `appendLivePoint` fast path during live streaming.
//
// Exposed on `window.BallTrackerDashboardScene` for the legacy
// classic-script IIFE bundle (00_boot..99_end.js) to read. ESM
// modules defer until after classic scripts finish parsing, so the
// IIFE will see `undefined` on first reference and gracefully skip
// the redraw call until the next tickCalibration / tickEvents cycle
// fires (1-5 s later, by which time this module has mounted).

import * as THREE from "three";
import {
  SEG_PALETTE,
  POINTS_OUTLIER,
  POINT_SIZE_M_DEFAULT,
  POINT_SIZE_OUTLIER_RATIO,
  classifyPointsBySegment,
  pointsCloud,
  readPersistedPointSizeM,
  writePersistedPointSizeM,
  applyPointSizeToGroup,
} from "./points_layer.js";
import {
  buildFitSegmentLines,
  applyResolution,
  applyLineWidth,
  setupFitHoverTooltip,
  bindLayerPopovers,
  readPersistedFitLineWidth,
  writePersistedFitLineWidth,
  readPersistedFitExtensionSeconds,
  writePersistedFitExtensionSeconds,
} from "./fit_curves_layer.js";

// Visual constants for the dashboard's accent palette. Match the
// previous Plotly-era values in 20_trajectory.js so the on-screen
// colour vocabulary doesn't drift mid-migration.
const FIT_ACCENT = 0xC0392B;
const ARROW_LEN_M = 0.3;

// Build a boolean keep-mask aligned with `points` from per-session
// cost/gap thresholds. Mirrors viewer's `_passCostFilterPoint` semantics:
// null / non-finite threshold = "no mask" (point passes); both null
// candidates on a point also pass (legacy / live-only points may not
// carry per-camera cost yet). `points[i].cost_a/cost_b/residual_m`
// are wire fields persisted on TriangulatedPoint.
function _buildPointKeepMask(points, costThreshold, gapThresholdM) {
  const costMax = (costThreshold == null || !Number.isFinite(costThreshold))
    ? null : Number(costThreshold);
  const gapMax = (gapThresholdM == null || !Number.isFinite(gapThresholdM))
    ? null : Number(gapThresholdM);
  const n = points.length;
  if (costMax === null && gapMax === null) {
    const m = new Array(n);
    for (let i = 0; i < n; ++i) m[i] = true;
    return m;
  }
  const m = new Array(n);
  for (let i = 0; i < n; ++i) {
    const p = points[i];
    let pass = true;
    if (gapMax !== null && Number.isFinite(p.residual_m) && p.residual_m > gapMax) {
      pass = false;
    }
    if (pass && costMax !== null) {
      let mc = -1;
      if (p.cost_a != null && Number.isFinite(p.cost_a)) mc = Math.max(mc, p.cost_a);
      if (p.cost_b != null && Number.isFinite(p.cost_b)) mc = Math.max(mc, p.cost_b);
      if (mc >= 0 && mc > costMax) pass = false;
    }
    m[i] = pass;
  }
  return m;
}

// Build a Line geometry from a flat XYZ Float32Array. Used for the
// camera-axis triads and v0 arrows — fit curves themselves now go
// through `buildFitSegmentLines` (Line2 fat lines).
function lineFromBuffer(buf, color, opts = {}) {
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
  const mat = new THREE.LineBasicMaterial({
    color: new THREE.Color(color),
    transparent: opts.transparent ?? false,
    opacity: opts.opacity ?? 1.0,
    depthWrite: opts.depthWrite ?? true,
  });
  return new THREE.Line(geom, mat);
}

// Two-vertex line for the v0 arrow. ArrowHelper would also work but
// adding a separate cone head per arrow doubles object count; a thick
// line is enough for this UI.
function arrowLine(p0, dir, length, color) {
  const buf = new Float32Array([
    p0[0], p0[1], p0[2],
    p0[0] + dir[0] * length, p0[1] + dir[1] * length, p0[2] + dir[2] * length,
  ]);
  return lineFromBuffer(buf, color);
}

// Spherical marker for a release point.
function releaseMarker(p0, color) {
  const geom = new THREE.SphereGeometry(0.020, 16, 12);
  const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(color) });
  const m = new THREE.Mesh(geom, mat);
  m.position.set(p0[0], p0[1], p0[2]);
  return m;
}

class DashboardLayers {
  constructor(scene) {
    this.scene = scene;
    // Mirror of toggle state. Default to scene-runtime's strike-zone
    // localStorage state for first paint; the IIFE wires the change
    // listener via `setShowPoints` afterwards.
    this._showPoints = (() => {
      try { return localStorage.getItem("ball_tracker_dashboard_show_points") === "1"; }
      catch { return false; }
    })();
    this._currentSid = null;
    // Snapshot the latest session result + live session payload so a
    // toggle change can rebuild without the IIFE having to push data
    // again. Keeps `setShowPoints` cheap.
    this._lastResultBySid = new Map();   // sid -> result
    this._lastCameras = [];               // last camera list
    this._lastLiveSession = null;         // { session_id, ... }
    this._lastLivePoints = [];            // current live session's points
    this._lastLiveRays = new Map();       // cam -> [ray, ...]
    // Persistent live-trail BufferGeometry — pre-allocated for the
    // worst-case live session length. iOS streams up to ~240 fps for
    // a few seconds; 2048 points covers >8 s of detection at 240 Hz
    // with headroom. WebGL re-uploads only the dirty range via
    // `setDrawRange` + `attributes.position.needsUpdate`, so we don't
    // GC-thrash a Float32Array per frame at 240 Hz.
    this._LIVE_CAP = 2048;
    this._liveBuf = new Float32Array(this._LIVE_CAP * 3);
    this._liveCount = 0;
    this._liveLineGroup = null;  // lazily created on first applyLive
    // Ghost preview of the *previous* live session — kept alive
    // between arms so the operator can confirm framing matches before
    // throwing again. A separate persistent group; cleared only on
    // an explicit reset (next arm starts streaming fresh trail data).
    this._ghostLineGroup = null;
    // World-space size of fit_points spheres. Restored from the cross-
    // page localStorage key on construction; writes via setPointSize.
    this._pointSize = readPersistedPointSizeM();
    // Fit-curve display tunables (Line2 linewidth in screen-px, dashed
    // extension padding in seconds). Persisted across pages just like
    // pointSize.
    this._fitLineWidth = readPersistedFitLineWidth();
    this._fitExtensionSec = readPersistedFitExtensionSeconds();
    // Cached so applyResolution() can re-push it after a canvas resize.
    this._fitGroup = null;
    // Resize listener — Line2 LineMaterial.resolution must follow the
    // renderer or linewidth uniform reads stale screen px and lines
    // render at default-zero width on next paint.
    this._resizeHandler = () => this._refreshFitResolution();
    window.addEventListener("resize", this._resizeHandler);
  }

  // Push current canvas size into every fit-line LineMaterial.resolution.
  _refreshFitResolution() {
    if (!this._fitGroup) return;
    const dom = this.scene.renderer && this.scene.renderer.domElement;
    if (!dom) return;
    applyResolution(this._fitGroup, new THREE.Vector2(dom.clientWidth, dom.clientHeight));
  }

  // ---- camera markers (per-camera diamond + axis triad) ----
  applyCameras(cameraList) {
    this._lastCameras = cameraList || [];
    const group = new THREE.Group();
    group.name = "dashboard_cameras";
    for (const cam of this._lastCameras) {
      if (!cam || !cam.center_world) continue;
      const camColor = (this.scene.theme.camera_colors && this.scene.theme.camera_colors[cam.camera_id])
        || this.scene.theme.colors.fallback_camera;
      const colorHex = new THREE.Color(camColor);
      const c = cam.center_world;
      // Diamond: small octahedron at the camera origin.
      const diaGeom = new THREE.OctahedronGeometry(0.04);
      const diaMat = new THREE.MeshBasicMaterial({ color: colorHex });
      const diamond = new THREE.Mesh(diaGeom, diaMat);
      diamond.position.set(c[0], c[1], c[2]);
      diamond.name = `camera_${cam.camera_id}`;
      group.add(diamond);
      // Axis triad: forward (cam color) + right (X red) + up (grey).
      const axes = [
        { dir: cam.axis_forward_world, color: camColor, len: this.scene.theme.axes.camera_forward_len_m },
        { dir: cam.axis_right_world,   color: this.scene.theme.colors.dev,    len: this.scene.theme.axes.camera_axis_len_m },
        { dir: cam.axis_up_world,      color: this.scene.theme.colors.ink_40, len: this.scene.theme.axes.camera_axis_len_m },
      ];
      for (const a of axes) {
        if (!a.dir) continue;
        group.add(arrowLine(c, a.dir, a.len, a.color));
      }
    }
    this.scene.addLayer("cameras", group);
  }

  // ---- fit (selected session) ----
  // `result` is a SessionResult-shaped object: { points, segments,
  // cost_threshold, gap_threshold_m }. `points` is the FULL triangulated
  // set (pairing emits everything; thresholds are operator masks set
  // via the viewer's Apply button). null thresholds → no client-side
  // mask. Pass `null` for either argument to clear (e.g. row deselect).
  applyFit(sid, result) {
    if (!sid || !result) {
      // Drop selection AND cached payload so a subsequent applyFit
      // for the same sid with a fresh result won't fall through to
      // the stale cache.
      if (this._currentSid) this._lastResultBySid.delete(this._currentSid);
      this._currentSid = null;
      this._removeFitLayers();
      return;
    }
    if (sid !== this._currentSid) {
      // Switching sessions — drop the previous layers before drawing.
      this._removeFitLayers();
    }
    this._currentSid = sid;
    this._lastResultBySid.set(sid, result);
    this._rebuildFitLayers();
  }

  setShowPoints(visible) {
    if (this._showPoints === !!visible) return;
    this._showPoints = !!visible;
    try { localStorage.setItem("ball_tracker_dashboard_show_points", this._showPoints ? "1" : "0"); }
    catch {}
    this._rebuildFitLayers();
  }
  showPointsEnabled() { return this._showPoints; }

  // Mutate fit_points sphere size live (PointsMaterial.size in world
  // metres). No geometry rebuild — slider drag stays smooth.
  setPointSize(sizeM) {
    if (!Number.isFinite(sizeM)) return;
    this._pointSize = sizeM;
    writePersistedPointSizeM(sizeM);
    const layer = this.scene.getLayer && this.scene.getLayer("fit_points");
    applyPointSizeToGroup(layer, sizeM);
  }
  pointSizeM() { return this._pointSize; }

  // Mutate fit-curve line width live (LineMaterial.linewidth in screen-
  // space px). O(N) over fit_curves children — no geometry rebuild.
  setFitLineWidth(px) {
    if (!Number.isFinite(px)) return;
    this._fitLineWidth = px;
    writePersistedFitLineWidth(px);
    if (this._fitGroup) applyLineWidth(this._fitGroup, px);
  }
  fitLineWidthPx() { return this._fitLineWidth; }

  // Dashed extension padding (seconds) — geometry depends on this so a
  // change triggers a fit-curves rebuild.
  setFitExtensionSeconds(sec) {
    if (!Number.isFinite(sec)) return;
    this._fitExtensionSec = sec;
    writePersistedFitExtensionSeconds(sec);
    this._rebuildFitLayers();
  }
  fitExtensionSeconds() { return this._fitExtensionSec; }

  _removeFitLayers() {
    this.scene.removeLayer("fit_curves");
    this.scene.removeLayer("fit_release");
    this.scene.removeLayer("fit_arrows");
    this.scene.removeLayer("fit_points");
    this._fitGroup = null;
  }

  _rebuildFitLayers() {
    if (!this._currentSid) return;
    const result = this._lastResultBySid.get(this._currentSid);
    if (!result) {
      this._removeFitLayers();
      return;
    }
    // Clear all fit layers before rebuilding. Without this the
    // conditional `fit_points` group leaks across rebuilds when its
    // predicate flips ON → OFF — e.g. operator unticks "Show points"
    // but the cached group stays mounted because addLayer is never
    // called for it this pass.
    this._removeFitLayers();
    const segments = Array.isArray(result.segments) ? result.segments : [];
    const points = result.points || [];
    // Pairing emits the full triangulated set; cost/gap on the result
    // are the operator's per-session mask (set via the viewer's Apply
    // button → POST /sessions/<sid>/recompute → SSE `fit`). Build a
    // boolean keep-mask aligned with `points` so the segment-bucket
    // pass below stays correct (`SegmentRecord.original_indices`
    // indexes into the full `points` list — pre-filtering would break
    // that contract). null threshold → all-true mask.
    const keep = _buildPointKeepMask(points, result.cost_threshold, result.gap_threshold_m);

    // --- fit curves ---
    const dom = this.scene.renderer && this.scene.renderer.domElement;
    const resolution = new THREE.Vector2(
      dom ? dom.clientWidth : 1,
      dom ? dom.clientHeight : 1,
    );
    const curveGroup = buildFitSegmentLines(segments, {
      groupName: "fit_curves",
      palette: (i) => SEG_PALETTE[i % SEG_PALETTE.length],
      lineWidthPx: this._fitLineWidth,
      prePadSec: this._fitExtensionSec,
      postPadSec: this._fitExtensionSec,
      resolution,
    });
    this._fitGroup = curveGroup;
    const releaseGroup = new THREE.Group();
    releaseGroup.name = "fit_release";
    const arrowGroup = new THREE.Group();
    arrowGroup.name = "fit_arrows";
    for (let i = 0; i < segments.length; ++i) {
      const seg = segments[i];
      const color = SEG_PALETTE[i % SEG_PALETTE.length];
      // Release marker at p0.
      releaseGroup.add(releaseMarker(seg.p0, color));
      // v0 arrow if magnitude is non-zero (degenerate segments
      // shouldn't survive the segmenter's MIN_DISP/MIN_SPEED gates,
      // but guard anyway — drawing a NaN arrow would crash WebGL).
      const vmag = Math.hypot(seg.v0[0], seg.v0[1], seg.v0[2]);
      if (vmag > 0) {
        arrowGroup.add(arrowLine(
          seg.p0,
          [seg.v0[0] / vmag, seg.v0[1] / vmag, seg.v0[2] / vmag],
          ARROW_LEN_M,
          color,
        ));
      }
    }
    this.scene.addLayer("fit_curves", curveGroup);
    this.scene.addLayer("fit_release", releaseGroup);
    this.scene.addLayer("fit_arrows", arrowGroup);

    // --- raw points (Show points toggle) ---
    if (this._showPoints && points.length) {
      const pointsGroup = new THREE.Group();
      pointsGroup.name = "fit_points";
      // Normalise dashboard's `{x_m,y_m,z_m}` shape to `{x,y,z}` once,
      // so the shared pointsCloud helper has a single contract with
      // viewer (which already speaks `{x,y,z}`).
      const xyz = points.map((p) => ({ x: p.x_m, y: p.y_m, z: p.z_m }));
      // Classify on the FULL list — `SegmentRecord.original_indices`
      // are full-list indices. Then skip masked-out points in the
      // bucketing pass below.
      const byPoint = classifyPointsBySegment(xyz, segments);
      const buckets = new Map();
      for (let i = 0; i < xyz.length; ++i) {
        if (!keep[i]) continue;
        const k = byPoint[i];
        const key = k === -1 ? "out" : String(k);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(xyz[i]);
      }
      const sizeM = this._pointSize;
      for (const [key, pts] of buckets) {
        const isOut = key === "out";
        const color = isOut ? POINTS_OUTLIER : SEG_PALETTE[Number(key) % SEG_PALETTE.length];
        pointsGroup.add(pointsCloud(pts, color, isOut ? sizeM * POINT_SIZE_OUTLIER_RATIO : sizeM, {
          opacity: isOut ? 0.55 : 1.0,
          isOutlier: isOut,
        }));
      }
      this.scene.addLayer("fit_points", pointsGroup);
    }

  }

  // ---- live in-progress session ----
  // Bulk push: replaces the trail with `points`, replaces the per-cam
  // rays. Used by tickEvents / SSE handlers that ship full snapshots.
  applyLive({ session, points, raysByCam }) {
    const newSid = session && session.session_id;
    const prevSid = this._lastLiveSession && this._lastLiveSession.session_id;
    if (newSid && newSid !== prevSid) {
      // New arm cycle — drop ghost and re-anchor live buffer.
      this.clearGhost();
      this._liveCount = 0;
    }
    this._lastLiveSession = session || null;
    this._lastLiveRays = raysByCam || new Map();
    this._lastLivePoints = Array.isArray(points) ? points.slice() : [];
    // Repack persistent buffer from the snapshot (cap at _LIVE_CAP).
    const cap = this._LIVE_CAP;
    const n = Math.min(this._lastLivePoints.length, cap);
    for (let i = 0; i < n; ++i) {
      const p = this._lastLivePoints[i];
      this._liveBuf[i * 3 + 0] = p.x;
      this._liveBuf[i * 3 + 1] = p.y;
      this._liveBuf[i * 3 + 2] = p.z;
    }
    this._liveCount = n;
    this._refreshLiveTrail();
    this._rebuildLiveRays();
  }

  // Fast-path append: copy XYZ into the persistent buffer's next slot
  // and bump draw range. No allocation, no GC churn — safe at 240 Hz.
  appendLivePoint(pt) {
    if (!this._lastLiveSession) return;
    if (this._liveCount >= this._LIVE_CAP) return;  // capped; drop silently is intentional
    const i = this._liveCount;
    this._liveBuf[i * 3 + 0] = pt.x;
    this._liveBuf[i * 3 + 1] = pt.y;
    this._liveBuf[i * 3 + 2] = pt.z;
    this._liveCount++;
    this._lastLivePoints.push(pt);
    this._refreshLiveTrail();
  }

  // Lazy-construct + update the persistent live-trail line. No
  // BufferGeometry rebuild on append — only `setDrawRange` +
  // `needsUpdate` flag flip, which WebGL re-uploads only the dirty
  // range.
  _refreshLiveTrail() {
    if (!this._lastLiveSession) {
      this.scene.removeLayer("live_trail");
      this._liveLineGroup = null;
      return;
    }
    if (!this._liveLineGroup) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.BufferAttribute(this._liveBuf, 3));
      const mat = new THREE.LineBasicMaterial({ color: new THREE.Color(FIT_ACCENT) });
      const line = new THREE.Line(geom, mat);
      const group = new THREE.Group();
      group.name = "live_trail";
      group.add(line);
      this.scene.addLayer("live_trail", group);
      this._liveLineGroup = group;
    }
    const line = this._liveLineGroup.children[0];
    line.geometry.setDrawRange(0, this._liveCount);
    line.geometry.attributes.position.needsUpdate = true;
    // Bound box invalidation so OrbitControls' raycasting (we don't
    // use it but Three.js may anyway) sees the updated extent.
    line.geometry.computeBoundingSphere();
  }

  _rebuildLiveRays() {
    this.scene.removeLayer("live_rays");
    if (!this._lastLiveSession || !this._lastLiveRays.size) return;
    const raysGroup = new THREE.Group();
    raysGroup.name = "live_rays";
    const colors = { A: 0x4A6B8C, B: 0xD35400 };
    for (const [cam, rays] of this._lastLiveRays) {
      if (!rays.length) continue;
      const buf = new Float32Array(rays.length * 6);
      for (let i = 0; i < rays.length; ++i) {
        const r = rays[i];
        buf[i * 6 + 0] = r.origin[0];
        buf[i * 6 + 1] = r.origin[1];
        buf[i * 6 + 2] = r.origin[2];
        buf[i * 6 + 3] = r.endpoint[0];
        buf[i * 6 + 4] = r.endpoint[1];
        buf[i * 6 + 5] = r.endpoint[2];
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
      const mat = new THREE.LineBasicMaterial({
        color: new THREE.Color(colors[cam] || 0x2A2520),
        transparent: true,
        opacity: 0.34,
      });
      raysGroup.add(new THREE.LineSegments(geom, mat));
    }
    this.scene.addLayer("live_rays", raysGroup);
  }

  // Promote the current live trail to a "ghost" layer (between-arm
  // preview) and clear the live state. The ghost stays visible until
  // the next arm cycle starts streaming fresh data — clearGhost is
  // called from applyLive on a session change.
  promoteLiveToGhost() {
    if (!this._liveCount || !this._lastLivePoints.length) {
      this.clearLive();
      return;
    }
    // Snapshot the buffer into a fresh sized Float32Array — the
    // persistent _liveBuf gets reused for the next session.
    const n = this._liveCount;
    const ghostBuf = new Float32Array(n * 3);
    ghostBuf.set(this._liveBuf.subarray(0, n * 3));
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(ghostBuf, 3));
    const mat = new THREE.LineBasicMaterial({
      color: new THREE.Color(FIT_ACCENT),
      transparent: true,
      opacity: 0.20,
    });
    const group = new THREE.Group();
    group.name = "live_ghost";
    group.add(new THREE.Line(geom, mat));
    this.scene.addLayer("live_ghost", group);
    this._ghostLineGroup = group;
    this.clearLive();
  }

  clearGhost() {
    this.scene.removeLayer("live_ghost");
    this._ghostLineGroup = null;
  }

  clearLive() {
    this._lastLiveSession = null;
    this._lastLiveRays = new Map();
    this._lastLivePoints = [];
    this._liveCount = 0;
    if (this._liveLineGroup) {
      // Reset draw range so the persistent buffer's stale tail isn't
      // accidentally rendered on next applyLive.
      const line = this._liveLineGroup.children[0];
      line.geometry.setDrawRange(0, 0);
      line.geometry.attributes.position.needsUpdate = true;
    }
    this.scene.removeLayer("live_trail");
    this.scene.removeLayer("live_rays");
    this._liveLineGroup = null;
  }
}

// Module-load entry point: invoked from the inline boot script after
// `mountScene()` returns. Exposes the layers controller via
// `window.BallTrackerDashboardScene` so the legacy IIFE can call into
// it. Returns the controller in case the caller wants direct access.
export function setupDashboardLayers(scene) {
  const layers = new DashboardLayers(scene);
  window.BallTrackerDashboardScene = layers;
  // Bind the view-preset toolbar that ships in the page HTML so chip
  // clicks drive `scene.setView(name)` directly. Toolbar selector is
  // shared with the viewer (`.scene-views`).
  const toolbar = document.querySelector(".scene-views");
  if (toolbar) scene.bindViewToolbar(toolbar);
  // Layer-chip popover wiring: chevron buttons toggle the sibling
  // popover; outside-click + Escape close. Done once; idempotent.
  bindLayerPopovers(document);
  // Bind sliders here, not in the classic IIFE. Module mount order:
  // classic <script> runs first (sliders are no-ops then because
  // window.BallTrackerDashboardScene is undefined), THEN this ESM
  // mounts and reads the persisted values, pushes them to the slider
  // values + readouts, and binds `input` -> setX. Push, not pull —
  // eliminates the boot race where sliders showed Python defaults
  // regardless of the persisted localStorage value.
  bindPointSizeSlider(layers, "#dash-point-size");
  bindFitLineWidthSlider(layers, "#dash-fit-line-width");
  bindFitExtensionSlider(layers, "#dash-fit-extension");
  // Hover tooltip — Raycaster on the fit_curves group; shows
  // instantaneous |v(t)| in km/h. Tooltip parent is the renderer's
  // CSS-positioned ancestor (`#scene-root`), which is already
  // position-relative-or-absolute by virtue of OrbitControls' filling.
  const tooltipParent = scene.renderer.domElement.parentNode;
  if (tooltipParent) {
    setupFitHoverTooltip({
      scene,
      fitGroupGetter: () => layers._fitGroup,
      segmentsFn: () => {
        const r = layers._lastResultBySid.get(layers._currentSid);
        return r && Array.isArray(r.segments) ? r.segments : [];
      },
      tooltipParent,
    });
  }
  return layers;
}

function bindPointSizeSlider(layers, containerSel) {
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

function bindFitLineWidthSlider(layers, containerSel) {
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

function bindFitExtensionSlider(layers, containerSel) {
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
