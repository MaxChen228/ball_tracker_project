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

const SEG_PALETTE = [
  0xE45756, 0x4C78A8, 0x54A24B, 0xF58518,
  0xB279A2, 0x72B7B2, 0xFF9DA6, 0x9D755D,
];

// Visual constants for the dashboard's accent palette. Match the
// previous Plotly-era values in 20_trajectory.js so the on-screen
// colour vocabulary doesn't drift mid-migration.
const FIT_ACCENT = 0xC0392B;
const POINTS_OUTLIER = new THREE.Color(0x4A3E24);
const G_Z = -9.81;
const ARROW_LEN_M = 0.3;

// Sample a parabolic fit segment into N world-space points.
// `seg` is a SegmentRecord: { p0, v0, t_anchor, t_start, t_end }.
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

// Bucket points by which segment claimed them (segments[i].original_indices
// indexes into the points list). Returns parallel arrays. Out-of-segment
// points get `byPoint[i] === -1`.
function classifyPointsBySegment(points, segments) {
  const byPoint = new Array(points.length).fill(-1);
  for (let i = 0; i < segments.length; ++i) {
    const oi = segments[i].original_indices || [];
    for (const k of oi) {
      if (k >= 0 && k < byPoint.length) byPoint[k] = i;
    }
  }
  return byPoint;
}

// Build a Line geometry from a flat XYZ Float32Array.
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
  // `result` is a SessionResult-shaped object: { points: [{x_m,y_m,z_m,t_rel_s}, ...], segments: [SegmentRecord, ...] }.
  // Pass `null` to clear.
  applyFit(sid, result) {
    if (sid !== this._currentSid) {
      // Drop the previous session's layers before drawing the new one.
      this._removeFitLayers();
    }
    this._currentSid = sid;
    if (sid && result) this._lastResultBySid.set(sid, result);
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

  _removeFitLayers() {
    this.scene.removeLayer("fit_curves");
    this.scene.removeLayer("fit_release");
    this.scene.removeLayer("fit_arrows");
    this.scene.removeLayer("fit_points");
    this.scene.removeLayer("fit_raw_path");
  }

  _rebuildFitLayers() {
    if (!this._currentSid) return;
    const result = this._lastResultBySid.get(this._currentSid);
    if (!result) {
      this._removeFitLayers();
      return;
    }
    const segments = Array.isArray(result.segments) ? result.segments : [];
    const points = result.points || [];

    // --- fit curves ---
    const curveGroup = new THREE.Group();
    curveGroup.name = "fit_curves";
    const releaseGroup = new THREE.Group();
    releaseGroup.name = "fit_release";
    const arrowGroup = new THREE.Group();
    arrowGroup.name = "fit_arrows";
    for (let i = 0; i < segments.length; ++i) {
      const seg = segments[i];
      const color = SEG_PALETTE[i % SEG_PALETTE.length];
      const buf = sampleSegmentCurve(seg, 80);
      curveGroup.add(lineFromBuffer(buf, color));
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
      const byPoint = classifyPointsBySegment(points, segments);
      // Bucket points by segment so each bucket renders as one
      // Points object — fewer draw calls than per-point Mesh.
      const buckets = new Map();
      for (let i = 0; i < points.length; ++i) {
        const k = byPoint[i];
        const key = k === -1 ? "out" : String(k);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(points[i]);
      }
      for (const [key, pts] of buckets) {
        const isOut = key === "out";
        const color = isOut ? POINTS_OUTLIER : new THREE.Color(SEG_PALETTE[Number(key) % SEG_PALETTE.length]);
        const buf = new Float32Array(pts.length * 3);
        for (let i = 0; i < pts.length; ++i) {
          buf[i * 3 + 0] = pts[i].x_m;
          buf[i * 3 + 1] = pts[i].y_m;
          buf[i * 3 + 2] = pts[i].z_m;
        }
        const geom = new THREE.BufferGeometry();
        geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
        const mat = new THREE.PointsMaterial({
          color,
          size: isOut ? 0.012 : 0.018,
          sizeAttenuation: true,
          transparent: isOut,
          opacity: isOut ? 0.55 : 1.0,
        });
        pointsGroup.add(new THREE.Points(geom, mat));
      }
      this.scene.addLayer("fit_points", pointsGroup);
    }

    // --- raw path fallback (no segments) ---
    // When the segmenter found nothing usable, surface the raw path as
    // a thin dashed line so the operator sees the shape rather than an
    // empty scene. Same intent as the Plotly-era branch.
    if (!segments.length && !this._showPoints && points.length) {
      const buf = new Float32Array(points.length * 3);
      for (let i = 0; i < points.length; ++i) {
        buf[i * 3 + 0] = points[i].x_m;
        buf[i * 3 + 1] = points[i].y_m;
        buf[i * 3 + 2] = points[i].z_m;
      }
      const line = lineFromBuffer(buf, POINTS_OUTLIER, { transparent: true, opacity: 0.55 });
      const group = new THREE.Group();
      group.name = "fit_raw_path";
      group.add(line);
      this.scene.addLayer("fit_raw_path", group);
    }
  }

  // ---- live in-progress session ----
  applyLive({ session, points, raysByCam }) {
    this._lastLiveSession = session || null;
    this._lastLivePoints = Array.isArray(points) ? points.slice() : [];
    this._lastLiveRays = raysByCam || new Map();
    this._rebuildLiveLayers();
  }

  // Fast path: append a single new point to the live trail without
  // rebuilding the whole BufferGeometry. Falls back to a full rebuild
  // when the underlying geometry slot is stale (e.g. session flipped).
  appendLivePoint(pt) {
    if (!this._lastLiveSession) return;
    this._lastLivePoints.push(pt);
    // Rebuild the live trail layer. The append is cheap (one geometry,
    // one material) — no need for the buffer-extension trick that
    // Plotly.extendTraces required.
    this._rebuildLiveLayers();
  }

  _rebuildLiveLayers() {
    this.scene.removeLayer("live_trail");
    this.scene.removeLayer("live_rays");
    if (!this._lastLiveSession) return;
    if (this._lastLivePoints.length) {
      const pts = this._lastLivePoints;
      const buf = new Float32Array(pts.length * 3);
      for (let i = 0; i < pts.length; ++i) {
        buf[i * 3 + 0] = pts[i].x;
        buf[i * 3 + 1] = pts[i].y;
        buf[i * 3 + 2] = pts[i].z;
      }
      const trail = lineFromBuffer(buf, FIT_ACCENT);
      const group = new THREE.Group();
      group.name = "live_trail";
      group.add(trail);
      this.scene.addLayer("live_trail", group);
    }
    if (this._lastLiveRays.size) {
      const raysGroup = new THREE.Group();
      raysGroup.name = "live_rays";
      const colors = { A: 0x4A6B8C, B: 0xD35400 };
      for (const [cam, rays] of this._lastLiveRays) {
        if (!rays.length) continue;
        // 2 vertices per ray + null-segment trick is Plotly-specific;
        // in Three.js use LineSegments where every pair is one segment.
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
  }

  clearLive() {
    this._lastLiveSession = null;
    this._lastLivePoints = [];
    this._lastLiveRays = new Map();
    this.scene.removeLayer("live_trail");
    this.scene.removeLayer("live_rays");
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
  // Strike-zone toggle: classic IIFE wires the checkbox listener; here
  // we just sync the initial visibility from localStorage (the scene
  // already does this on construction, but the .selected checkbox
  // state may differ — IIFE's 40_traj_handlers.js re-sets it on mount).
  return layers;
}
