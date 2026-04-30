// Shared point-cloud primitives for dashboard + viewer 3D scenes.
// Both surfaces render raw triangulated points the same way (THREE.Points
// with sizeAttenuation=true so size is world-units, scaling with camera
// distance like a physical sphere). Centralising the palette + classifier
// + builder here prevents drift between the two layer modules.
//
// `setPointSize(sizeM)` callers walk PointsMaterial children and mutate
// `material.size` directly — no geometry rebuild, slider feels instant.

import * as THREE from "three";

// Per-segment palette. Position N maps to SegmentRecord index N. Out-of-
// segment points use POINTS_OUTLIER instead.
export const SEG_PALETTE = [
  0xE45756, 0x4C78A8, 0x54A24B, 0xF58518,
  0xB279A2, 0x72B7B2, 0xFF9DA6, 0x9D755D,
];

export const POINTS_OUTLIER = 0x4A3E24;

// Default world-space size (metres) for trajectory points. Both layer
// modules seed from this; user-driven slider mutates per-instance.
export const POINT_SIZE_M_DEFAULT = 0.018;
// Slider bounds — covers "barely visible at 5m camera distance" up to
// "obviously a baseball at 1m". Step 0.001 keeps the slider tactile.
export const POINT_SIZE_M_MIN = 0.005;
export const POINT_SIZE_M_MAX = 0.040;
export const POINT_SIZE_M_STEP = 0.001;
// Outlier points render smaller + more transparent so the eye reads
// in-segment points as primary. 0.67 = dashboard's 0.012/0.018 ratio.
export const POINT_SIZE_OUTLIER_RATIO = 0.67;

// localStorage key for the cross-page persisted size. Same string on
// both surfaces so a tweak in dashboard carries to viewer and back.
export const POINT_SIZE_LS_KEY = "ball_tracker_point_size_m";

// Bucket points by which segment claimed them (segments[i].original_indices
// indexes into the points list). Returns parallel array; out-of-segment
// points get -1.
export function classifyPointsBySegment(points, segments) {
  const byPoint = new Array(points.length).fill(-1);
  for (let i = 0; i < (segments || []).length; ++i) {
    const oi = segments[i].original_indices || [];
    for (const k of oi) {
      if (k >= 0 && k < byPoint.length) byPoint[k] = i;
    }
  }
  return byPoint;
}

// Build one THREE.Points object from `pts` (each {x,y,z}). One Points per
// colour bucket = one draw call per bucket. `sizeM` is world-space radius.
// Pass `opts.isOutlier = true` so applyPointSizeToGroup can re-shrink
// outlier buckets when the slider mutates size, without colour-equality
// hacks (which would break if a SEG_PALETTE entry ever collided with
// POINTS_OUTLIER).
export function pointsCloud(pts, color, sizeM, opts = {}) {
  const buf = new Float32Array(pts.length * 3);
  for (let i = 0; i < pts.length; ++i) {
    buf[i * 3 + 0] = pts[i].x;
    buf[i * 3 + 1] = pts[i].y;
    buf[i * 3 + 2] = pts[i].z;
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(buf, 3));
  const opacity = opts.opacity ?? 1.0;
  const mat = new THREE.PointsMaterial({
    color: new THREE.Color(color),
    size: sizeM,
    sizeAttenuation: true,
    transparent: opacity < 1.0,
    opacity,
  });
  const node = new THREE.Points(geom, mat);
  node.userData.isOutlier = !!opts.isOutlier;
  return node;
}

// Read the persisted size, clamped to slider bounds. Falls through to
// the default on any read error or out-of-band value (loud-fail isn't
// useful here — first paint must succeed even with a fresh localStorage).
export function readPersistedPointSizeM() {
  try {
    const raw = localStorage.getItem(POINT_SIZE_LS_KEY);
    if (raw == null) return POINT_SIZE_M_DEFAULT;
    const v = Number(raw);
    if (!Number.isFinite(v)) return POINT_SIZE_M_DEFAULT;
    if (v < POINT_SIZE_M_MIN) return POINT_SIZE_M_MIN;
    if (v > POINT_SIZE_M_MAX) return POINT_SIZE_M_MAX;
    return v;
  } catch {
    return POINT_SIZE_M_DEFAULT;
  }
}

export function writePersistedPointSizeM(sizeM) {
  try { localStorage.setItem(POINT_SIZE_LS_KEY, String(sizeM)); }
  catch {}
}

// Walk a Group and mutate every PointsMaterial.size in place. Cheap —
// no geometry rebuild, slider feels instant while dragging. Reads the
// `userData.isOutlier` tag set by `pointsCloud` to apply the reduced
// ratio (vs colour-equality, which would silently mis-shrink in-segment
// points if a future SEG_PALETTE entry collided with POINTS_OUTLIER).
export function applyPointSizeToGroup(group, sizeM) {
  if (!group) return;
  group.traverse((node) => {
    if (!node || !node.isPoints) return;
    const mat = node.material;
    if (!mat || !mat.isPointsMaterial) return;
    const isOut = !!(node.userData && node.userData.isOutlier);
    mat.size = isOut ? sizeM * POINT_SIZE_OUTLIER_RATIO : sizeM;
    mat.needsUpdate = true;
  });
}
