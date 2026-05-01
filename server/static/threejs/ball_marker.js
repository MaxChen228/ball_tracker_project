// Shared 3D baseball marker used by viewer + dashboard playback.
//
// Procedural mesh, no external texture: an off-white sphere plus two
// raised red seam curves. The real baseball radius is about 36-37 mm;
// callers may scale via radiusM, but should keep one marker primitive
// everywhere so the current-ball visual stays consistent.

import * as THREE from "three";

export const BASEBALL_RADIUS_M = 0.0365;

const BASEBALL_SURFACE = 0xf8f4e8;
const BASEBALL_SEAM = 0xb3262f;
const SEAM_LIFT = 1.025;

function seamCurve(radius, phase) {
  const pts = [];
  const n = 160;
  for (let i = 0; i <= n; ++i) {
    const u = (i / n) * Math.PI * 2;
    const x = Math.cos(u);
    const y = 0.42 * Math.sin(2 * u + phase);
    const z = Math.sin(u);
    const v = new THREE.Vector3(x, y, z).normalize().multiplyScalar(radius * SEAM_LIFT);
    pts.push(v);
  }
  return new THREE.CatmullRomCurve3(pts, true, "centripetal");
}

function stitchMarks(radius, phase, color) {
  const verts = [];
  const material = new THREE.LineBasicMaterial({
    color: new THREE.Color(color),
    transparent: true,
    opacity: 0.95,
  });
  const n = 28;
  const half = radius * 0.055;
  for (let i = 0; i < n; ++i) {
    const u = (i / n) * Math.PI * 2;
    const base = new THREE.Vector3(
      Math.cos(u),
      0.42 * Math.sin(2 * u + phase),
      Math.sin(u),
    ).normalize();
    const tangent = new THREE.Vector3(
      -Math.sin(u),
      0.84 * Math.cos(2 * u + phase),
      Math.cos(u),
    ).normalize();
    const binormal = new THREE.Vector3().crossVectors(base, tangent).normalize();
    const center = base.clone().multiplyScalar(radius * 1.035);
    const p0 = center.clone().add(binormal.clone().multiplyScalar(-half));
    const p1 = center.clone().add(binormal.clone().multiplyScalar(half));
    verts.push(p0, p1);
  }
  const geom = new THREE.BufferGeometry().setFromPoints(verts);
  const line = new THREE.LineSegments(geom, material);
  line.name = "baseball_stitches";
  return line;
}

export function createBaseballMarker(opts = {}) {
  const radius = opts.radiusM ?? BASEBALL_RADIUS_M;
  const group = new THREE.Group();
  group.name = opts.name || "baseball_marker";

  const ball = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 32, 20),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(opts.surfaceColor ?? BASEBALL_SURFACE),
      roughness: 0.82,
      metalness: 0.0,
    }),
  );
  ball.name = "baseball_surface";
  group.add(ball);

  for (const [idx, phase] of [0, Math.PI].entries()) {
    const geom = new THREE.TubeGeometry(seamCurve(radius, phase), 160, radius * 0.014, 6, true);
    const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(opts.seamColor ?? BASEBALL_SEAM) });
    const seam = new THREE.Mesh(geom, mat);
    seam.name = `baseball_seam_${idx}`;
    group.add(seam);
    group.add(stitchMarks(radius, phase, opts.seamColor ?? BASEBALL_SEAM));
  }

  return group;
}

export function setBaseballMarkerPose(marker, position) {
  if (!marker) return;
  if (!Array.isArray(position) || position.length !== 3) {
    throw new Error("setBaseballMarkerPose: position must be [x, y, z]");
  }
  marker.position.set(position[0], position[1], position[2]);
}
