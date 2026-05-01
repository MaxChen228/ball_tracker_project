// Shared 3D baseball marker used by viewer + dashboard playback.
//
// The marker is intentionally geometry-first rather than texture-first.
// A UV texture makes the seams wrap into odd rings at small screen sizes;
// explicit front-hemisphere curves keep the visual readable as "baseball"
// while remaining deterministic and asset-free.

import * as THREE from "three";

export const BASEBALL_RADIUS_M = 0.0365;

const BASEBALL_SURFACE = 0xfff7e6;
const BASEBALL_SEAM = 0xb3262f;
const BASEBALL_STITCH = 0x7b1e27;

function _spherePoint(radius, x, y, lift = 1.012) {
  const z = Math.sqrt(Math.max(0, radius * radius - x * x - y * y));
  return new THREE.Vector3(x, y, z * lift);
}

function _frontSeamPoints(radius, side) {
  const points = [];
  const n = 72;
  for (let i = 0; i <= n; ++i) {
    const t = -1.25 + (2.5 * i) / n;
    const y = Math.sin(t) * radius * 0.70;
    const x = side * radius * (0.20 + 0.35 * Math.cos(t));
    points.push(_spherePoint(radius, x, y));
  }
  return points;
}

function _makeLine(points, color, name) {
  const geom = new THREE.BufferGeometry().setFromPoints(points);
  const mat = new THREE.LineBasicMaterial({
    color: new THREE.Color(color),
    transparent: true,
    opacity: 1.0,
  });
  const line = new THREE.Line(geom, mat);
  line.name = name;
  return line;
}

function _makeStitches(radius, seamPoints, name) {
  const verts = [];
  for (let i = 4; i < seamPoints.length - 4; i += 5) {
    const center = seamPoints[i].clone().normalize().multiplyScalar(radius * 1.025);
    const tangent = seamPoints[i + 2].clone().sub(seamPoints[i - 2]).normalize();
    const normal = center.clone().normalize();
    const across = new THREE.Vector3().crossVectors(tangent, normal).normalize();
    const half = radius * 0.085;
    verts.push(
      center.clone().addScaledVector(across, -half),
      center.clone().addScaledVector(across, half),
    );
  }
  const geom = new THREE.BufferGeometry().setFromPoints(verts);
  const mat = new THREE.LineBasicMaterial({
    color: new THREE.Color(BASEBALL_STITCH),
    transparent: true,
    opacity: 0.9,
  });
  const stitches = new THREE.LineSegments(geom, mat);
  stitches.name = name;
  return stitches;
}

function _makeScuffs(radius) {
  const marks = [];
  const seeds = [
    [-0.20, 0.18, 0.025],
    [0.08, -0.30, 0.020],
    [0.26, 0.25, 0.018],
    [-0.31, -0.10, 0.016],
  ];
  for (const [xUnit, yUnit, lenUnit] of seeds) {
    const center = _spherePoint(radius, xUnit * radius, yUnit * radius, 1.018);
    const across = new THREE.Vector3(0.62, -0.38, 0).normalize();
    const half = radius * lenUnit;
    marks.push(
      center.clone().addScaledVector(across, -half),
      center.clone().addScaledVector(across, half),
    );
  }
  const geom = new THREE.BufferGeometry().setFromPoints(marks);
  const mat = new THREE.LineBasicMaterial({
    color: new THREE.Color(0xbfae8c),
    transparent: true,
    opacity: 0.35,
  });
  const scuffs = new THREE.LineSegments(geom, mat);
  scuffs.name = "baseball_surface_scuffs";
  return scuffs;
}

export function createBaseballMarker(opts = {}) {
  const radius = opts.radiusM ?? BASEBALL_RADIUS_M;
  const group = new THREE.Group();
  group.name = opts.name || "baseball_marker";
  group.userData.isBaseballMarker = true;
  group.rotation.set(-0.08, 0.0, -0.15);

  const surfaceColor = opts.surfaceColor ?? BASEBALL_SURFACE;
  const seamColor = opts.seamColor ?? BASEBALL_SEAM;
  const ball = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 56, 36),
    new THREE.MeshStandardMaterial({
      color: new THREE.Color(surfaceColor),
      roughness: 0.82,
      metalness: 0.0,
      emissive: new THREE.Color(0x241b0c),
      emissiveIntensity: 0.045,
    }),
  );
  ball.name = "baseball_surface";
  group.add(ball);

  const left = _frontSeamPoints(radius, -1);
  const right = _frontSeamPoints(radius, 1);
  group.add(_makeLine(left, seamColor, "baseball_seam_left"));
  group.add(_makeLine(right, seamColor, "baseball_seam_right"));
  group.add(_makeStitches(radius, left, "baseball_stitches_left"));
  group.add(_makeStitches(radius, right, "baseball_stitches_right"));
  group.add(_makeScuffs(radius));

  return group;
}

export function setBaseballMarkerPose(marker, position) {
  if (!marker) return;
  if (!Array.isArray(position) || position.length !== 3) {
    throw new Error("setBaseballMarkerPose: position must be [x, y, z]");
  }
  marker.position.set(position[0], position[1], position[2]);
}
