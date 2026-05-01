// Shared playback-marker resolver for dashboard + viewer 3D scenes.
//
// This module is intentionally Three.js-free. It answers one question:
// "for playback time t, where should the single current-ball marker be?"
// Page-specific layers own rendering; this keeps FIT and TRAJ from each
// independently drawing their own marker.

const EPS_S = 1e-3;

export const G_Z = -9.81;

export function evalSegmentAt(seg, t) {
  const tau = t - seg.t_anchor;
  return [
    seg.p0[0] + seg.v0[0] * tau,
    seg.p0[1] + seg.v0[1] * tau,
    seg.p0[2] + seg.v0[2] * tau + 0.5 * G_Z * tau * tau,
  ];
}

export function activeSegmentIndex(segments, t) {
  for (let i = 0; i < (segments || []).length; ++i) {
    const seg = segments[i];
    if (t >= seg.t_start - EPS_S && t <= seg.t_end + EPS_S) return i;
  }
  return -1;
}

export function lastVisiblePoint(points, cutoff, residualPasses, costPassesPoint) {
  let last = null;
  for (const p of points || []) {
    if (typeof p.t_rel_s !== "number" || !Number.isFinite(p.t_rel_s)) continue;
    if (p.t_rel_s > cutoff) continue;
    if (!residualPasses(p)) continue;
    if (!costPassesPoint(p)) continue;
    last = p;
  }
  return last;
}

export function resolvePlaybackMarkerPose({
  mode,
  t,
  fitVisible,
  trajVisible,
  segments,
  points,
  residualPasses,
  costPassesPoint,
}) {
  if (mode !== "playback") return null;
  if (typeof t !== "number" || !Number.isFinite(t)) return null;
  if (typeof residualPasses !== "function" || typeof costPassesPoint !== "function") {
    throw new Error("resolvePlaybackMarkerPose: residualPasses and costPassesPoint are required");
  }
  if (fitVisible) {
    const idx = activeSegmentIndex(segments || [], t);
    if (idx !== -1) {
      return {
        source: "fit",
        segIdx: idx,
        position: evalSegmentAt(segments[idx], t),
      };
    }
  }
  if (trajVisible) {
    const p = lastVisiblePoint(points || [], t, residualPasses, costPassesPoint);
    if (p) {
      return {
        source: "traj",
        segIdx: typeof p.seg_idx === "number" ? p.seg_idx : -1,
        position: [p.x, p.y, p.z],
      };
    }
  }
  return null;
}
