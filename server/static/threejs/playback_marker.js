// Shared playback-marker resolver for dashboard + viewer 3D scenes.
//
// This module is intentionally Three.js-free. It answers one question:
// "for playback time t, where should the single current-ball marker be?"
// Page-specific layers own rendering. The marker is the fitted model's
// continuous ball pose; raw triangulated points are observations and never
// drive playback.

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

export function resolvePlaybackMarkerPose({
  mode,
  t,
  fitVisible,
  segments,
}) {
  if (mode !== "playback") return null;
  if (typeof t !== "number" || !Number.isFinite(t)) return null;
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
  return null;
}
