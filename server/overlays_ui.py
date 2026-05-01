"""Shared overlay UI primitives for dashboard `/` and viewer `/viewer/{sid}`.

The dashboard and viewer both render the same Three.js 3D scene.
Anything that toggles a visual layer on top of that scene — currently
only the strike-zone wireframe — needs identical behaviour on both
surfaces (same localStorage key, same scene-layer name). Centralising
the JS runtime here keeps the two pages in lock-step.

Inject ``OVERLAYS_RUNTIME_JS`` as its own ``<script>`` block BEFORE each
page's main script. It exposes ``window.BallTrackerOverlays`` with
helpers consumers can alias locally.

Older versions also shipped client-side ballistic-fit math
(``ballisticFit`` / ``fitTraces``) and per-segment speed colouring
(``speedTraces``). Both are gone — multi-segment fit is now persisted
on ``SessionResult.segments`` and rendered by dashboard / viewer
directly. Don't restore.
"""
from __future__ import annotations

OVERLAYS_RUNTIME_JS: str = r"""
(function () {
  if (window.BallTrackerOverlays) return;
  const NS = {};

  // --- generic localStorage bool/string helpers ---
  function readBool(key, dflt) {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return dflt;
      return raw === "1";
    } catch (_) { return dflt; }
  }
  function writeBool(key, on) {
    try { localStorage.setItem(key, on ? "1" : "0"); } catch (_) {}
  }
  // --- Strike zone ---
  const STRIKE_ZONE_KEY = "ball_tracker_strike_zone_visible";
  NS.strikeZoneVisible = function () { return readBool(STRIKE_ZONE_KEY, true); };
  NS.setStrikeZoneVisible = function (on) { writeBool(STRIKE_ZONE_KEY, on); };
  NS.isStrikeZoneTrace = function (t) {
    return !!(t && t.meta && t.meta.feature === "strike_zone");
  };

  // --- Ballistic segment helpers (canonical client copy) ---
  // Python reference lives in `server/strike_zone.py`. Behavioural parity
  // is enforced by `server/test_strike_judge.py` — keep both sides in
  // lockstep when changing logic, and never silent-fallback the
  // "no plate cross" case to BALL (the operator does research and a
  // mis-labelled pitch poisons strike% / heatmap stats).
  const _GRAVITY_Z = -9.81;
  const _DEFAULT_BALL_RADIUS_M = 0.0366;

  function _ballResult(x, z, t) {
    return { verdict: "ball", crossing_x_m: x, crossing_z_m: z, crossing_t: t };
  }

  // Strike / ball verdict from seg0's ballistic curve, extended both
  // forward AND backward (no clipping to seg0's [t_start, t_end]). The
  // pitch's identity = seg0; segs[1:] (post-bounce, etc.) are ignored
  // by design. Curve through zone box (expanded by ball radius) → STRIKE,
  // otherwise → BALL. Python parity: `judge_pitch_strike` in strike_zone.py.
  NS.judgePitch = function (segments, zone, opts) {
    if (!Array.isArray(segments) || !segments.length || !zone) return null;
    const o = opts || {};
    const ballR = Number.isFinite(o.ball_radius_m) ? o.ball_radius_m : _DEFAULT_BALL_RADIUS_M;
    const N = (o.sample_count | 0) >= 2 ? (o.sample_count | 0) : 64;
    // seg0 = earliest by t_anchor; defensive sort so dict.values() / set
    // callers don't silently flip the verdict.
    const seg0 = segments.slice().sort(
      (a, b) => Number(a.t_anchor) - Number(b.t_anchor)
    )[0];
    if (!seg0 || !Array.isArray(seg0.p0) || seg0.p0.length !== 3) {
      throw new Error("judgePitch: seg0.p0 must be a 3-vector");
    }
    if (!Array.isArray(seg0.v0) || seg0.v0.length !== 3) {
      throw new Error("judgePitch: seg0.v0 must be a 3-vector");
    }
    const p0 = seg0.p0, v0 = seg0.v0;
    const tAnchor = Number(seg0.t_anchor);
    if (!Number.isFinite(tAnchor)) {
      throw new Error("judgePitch: seg0.t_anchor must be finite");
    }
    const vy = v0[1];
    if (Math.abs(vy) < 1e-6) {
      // Curve never reaches the plate y-band → ball.
      return _ballResult(null, null, null);
    }
    const tauFront = (zone.y_front_m - p0[1]) / vy;
    const tauBack = (zone.y_back_m - p0[1]) / vy;
    const tauLo = Math.min(tauFront, tauBack);
    const tauHi = Math.max(tauFront, tauBack);

    const xHalfR = zone.x_half_m + ballR;
    const zMin = zone.z_bottom_m - ballR;
    const zMax = zone.z_top_m + ballR;
    for (let i = 0; i < N; i++) {
      const tau = tauLo + (tauHi - tauLo) * (i / (N - 1));
      const x = p0[0] + v0[0] * tau;
      const z = p0[2] + v0[2] * tau + 0.5 * _GRAVITY_Z * tau * tau;
      if (Math.abs(x) <= xHalfR && z >= zMin && z <= zMax) {
        return { verdict: "strike", crossing_x_m: x, crossing_z_m: z, crossing_t: tAnchor + tau };
      }
    }
    // Front-face crossing for BALL telemetry (analytic, exact at y=y_front).
    const xF = p0[0] + v0[0] * tauFront;
    const zF = p0[2] + v0[2] * tauFront + 0.5 * _GRAVITY_Z * tauFront * tauFront;
    return _ballResult(xF, zF, tAnchor + tauFront);
  };

  // |v(t)| in km/h for a ballistic seg. Caller must pick the right seg
  // (use NS.activeSegmentIndex below); this helper just integrates the
  // ballistic ODE forward from t_anchor.
  NS.instantSpeedKph = function (seg, t) {
    if (!seg || !Array.isArray(seg.v0) || seg.v0.length !== 3) return null;
    const tEval = Number.isFinite(t) ? t : seg.t_start;
    if (!Number.isFinite(tEval) || !Number.isFinite(seg.t_anchor)) return null;
    const tau = tEval - seg.t_anchor;
    const vx = seg.v0[0];
    const vy = seg.v0[1];
    const vz = seg.v0[2] + _GRAVITY_Z * tau;
    return Math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6;
  };

  // Pick the segment whose [t_start, t_end] contains `t`; if no seg is
  // strictly active (currentT in a bounce gap, or 'all' mode at t=t_min),
  // fall back to the seg whose midpoint is nearest. Returns -1 on empty
  // input — callers should hide the badge in that case. Mirrors viewer's
  // local copy in 40_traces.js, lifted to the shared NS so dashboard +
  // viewer both use one canonical helper.
  NS.activeSegmentIndex = function (segments, t) {
    if (!Array.isArray(segments) || !segments.length) return -1;
    if (!Number.isFinite(t)) return 0;
    for (let i = 0; i < segments.length; i++) {
      const s = segments[i];
      if (t >= s.t_start && t <= s.t_end) return i;
    }
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < segments.length; i++) {
      const s = segments[i];
      const mid = 0.5 * (s.t_start + s.t_end);
      const d = Math.abs(t - mid);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return best;
  };

  window.BallTrackerOverlays = NS;
})();
"""


def assert_overlays_present(html: str) -> None:
    """Sanity-check that a rendered page actually injected the runtime.

    Prevents silent regressions where a refactor drops the
    ``overlays_js=`` kwarg or moves the script tag and both pages stop
    sharing strike-zone state without raising any error.
    """
    if "BallTrackerOverlays" not in html:
        raise AssertionError("rendered page missing overlays runtime injection")
