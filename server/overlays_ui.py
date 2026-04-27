"""Shared overlay UI primitives for dashboard `/` and viewer `/viewer/{sid}`.

The dashboard and viewer both render the same Plotly 3D scene. Anything
that toggles a visual layer on top of that scene — strike zone, ballistic
fit, per-segment speed colouring — needs identical behaviour on both
surfaces (same localStorage keys, same trace meta predicates, same math).
Centralising the JS runtime here keeps the two pages in lock-step without
each duplicating logic.

Inject ``OVERLAYS_RUNTIME_JS`` as its own ``<script>`` block BEFORE each
page's main script. It exposes ``window.BallTrackerOverlays`` with
helpers consumers can alias locally.
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
  function readStr(key, dflt, allowed) {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return dflt;
      if (allowed && allowed.indexOf(raw) < 0) return dflt;
      return raw;
    } catch (_) { return dflt; }
  }
  function writeStr(key, val) {
    try { localStorage.setItem(key, val); } catch (_) {}
  }

  // --- Strike zone ---
  const STRIKE_ZONE_KEY = "ball_tracker_strike_zone_visible";
  NS.strikeZoneVisible = function () { return readBool(STRIKE_ZONE_KEY, true); };
  NS.setStrikeZoneVisible = function (on) { writeBool(STRIKE_ZONE_KEY, on); };
  NS.isStrikeZoneTrace = function (t) {
    return !!(t && t.meta && t.meta.feature === "strike_zone");
  };

  // --- Fit overlay ---
  // Fit is a layer, not a mode: drawn ON TOP of the regular trajectory.
  // Source picks which triangulation pipeline feeds the fit when both
  // exist (viewer has both paths; dashboard maps "live" to the active
  // session's live points and "server_post" to the loaded event points).
  const FIT_VISIBLE_KEY = "ball_tracker_overlay_fit_enabled";
  const FIT_SOURCE_KEY = "ball_tracker_overlay_fit_source";
  const FIT_SOURCES = ["server_post", "live"];
  NS.fitVisible = function () { return readBool(FIT_VISIBLE_KEY, false); };
  NS.setFitVisible = function (on) { writeBool(FIT_VISIBLE_KEY, on); };
  NS.fitSource = function () { return readStr(FIT_SOURCE_KEY, "server_post", FIT_SOURCES); };
  NS.setFitSource = function (src) {
    if (FIT_SOURCES.indexOf(src) >= 0) writeStr(FIT_SOURCE_KEY, src);
  };

  // --- Ballistic fit math ---
  // Per-axis quadratic LSQ: p(τ) = p0 + v0·τ + 0.5·a·τ² (3 params/axis,
  // 9 total, gravity NOT pinned). Acceleration free on every axis; the
  // fitted -a_z is reported as g_fit so the operator reads it as "the
  // gravity-equivalent acceleration this pitch's data shows".
  // Input: array of {x, y, z, t_rel_s}. Returns null on <4 pts or
  // singular normal equations (e.g. all τ identical).
  function solve3x3(M, b) {
    const A = [
      [M[0][0], M[0][1], M[0][2], b[0]],
      [M[1][0], M[1][1], M[1][2], b[1]],
      [M[2][0], M[2][1], M[2][2], b[2]],
    ];
    for (let i = 0; i < 3; i++) {
      let pivot = i;
      for (let r = i + 1; r < 3; r++) {
        if (Math.abs(A[r][i]) > Math.abs(A[pivot][i])) pivot = r;
      }
      if (pivot !== i) { const tmp = A[i]; A[i] = A[pivot]; A[pivot] = tmp; }
      if (Math.abs(A[i][i]) < 1e-12) return null;
      for (let r = i + 1; r < 3; r++) {
        const f = A[r][i] / A[i][i];
        for (let c = i; c <= 3; c++) A[r][c] -= f * A[i][c];
      }
    }
    const x = [0, 0, 0];
    for (let i = 2; i >= 0; i--) {
      let s = A[i][3];
      for (let c = i + 1; c < 3; c++) s -= A[i][c] * x[c];
      x[i] = s / A[i][i];
    }
    return x;
  }
  NS.ballisticFit = function (pts) {
    if (!pts || pts.length < 4) return null;
    const t0 = pts[0].t_rel_s;
    const taus = pts.map(p => p.t_rel_s - t0);
    const n = pts.length;
    const sT = [n, 0, 0, 0, 0];
    for (const tau of taus) {
      let p = 1;
      for (let k = 1; k <= 4; k++) { p *= tau; sT[k] += p; }
    }
    const M = [
      [sT[0], sT[1], sT[2]],
      [sT[1], sT[2], sT[3]],
      [sT[2], sT[3], sT[4]],
    ];
    function fitAxis(getVal) {
      const r = [0, 0, 0];
      for (let i = 0; i < n; i++) {
        const v = getVal(pts[i]);
        r[0] += v;
        r[1] += taus[i] * v;
        r[2] += taus[i] * taus[i] * v;
      }
      const c = solve3x3(M, r);
      if (!c) return null;
      return { p0: c[0], v0: c[1], a: 2 * c[2] };
    }
    const fx = fitAxis(p => p.x);
    const fy = fitAxis(p => p.y);
    const fz = fitAxis(p => p.z);
    if (!fx || !fy || !fz) return null;
    const out = {
      p0: { x: fx.p0, y: fy.p0, z: fz.p0 },
      v0: { x: fx.v0, y: fy.v0, z: fz.v0 },
      a:  { x: fx.a,  y: fy.a,  z: fz.a  },
      g_fit: -fz.a,
      t0,
      flight_time_s: pts[n - 1].t_rel_s - t0,
    };
    out.evaluate = function (t) {
      const tau = t - t0;
      const half = 0.5 * tau * tau;
      return {
        x: fx.p0 + fx.v0 * tau + fx.a * half,
        y: fy.p0 + fy.v0 * tau + fy.a * half,
        z: fz.p0 + fz.v0 * tau + fz.a * half,
      };
    };
    out.speed_mps = Math.hypot(out.v0.x, out.v0.y, out.v0.z);
    out.speed_kmph = out.speed_mps * 3.6;
    const horiz = Math.hypot(out.v0.x, out.v0.y);
    out.elevation_deg = Math.atan2(out.v0.z, horiz) * 180 / Math.PI;
    out.azimuth_deg = Math.atan2(out.v0.x, out.v0.y) * 180 / Math.PI;
    let rss = 0, rssZ = 0;
    for (const p of pts) {
      const q = out.evaluate(p.t_rel_s);
      const dx = p.x - q.x, dy = p.y - q.y, dz = p.z - q.z;
      rss += dx * dx + dy * dy + dz * dz;
      rssZ += dz * dz;
    }
    out.rmse_m = Math.sqrt(rss / n);
    out.rmse_by_axis_m = { z: Math.sqrt(rssZ / n) };
    out.apex_time_s = 0;
    out.apex_height_m = fz.p0;
    if (Math.abs(fz.a) > 1e-9) {
      const tauApex = -fz.v0 / fz.a;
      if (tauApex > 0 && tauApex < out.flight_time_s) {
        const z = out.evaluate(t0 + tauApex).z;
        if (z > out.apex_height_m) {
          out.apex_time_s = tauApex;
          out.apex_height_m = z;
        }
      }
    }
    return out;
  };

  // Build the Plotly traces for a fit overlay. Single shared style so
  // dashboard + viewer look identical.
  //   fit:        result of ballisticFit()
  //   tStart, tEnd: τ-window to draw (pass full inlier window for "all"
  //                 mode; trim to playback cutoff in playback mode)
  //   color:      curve color (default deep red)
  //   nameSuffix: appended to legend name (e.g. " · live", "(svr)")
  // Returns 0–2 traces (curve + v0 arrow), empty if fit is null.
  NS.fitTraces = function (fit, tStart, tEnd, opts) {
    if (!fit) return [];
    const o = opts || {};
    const color = o.color || "#A7372A";
    const nameSuffix = o.nameSuffix || "";
    const nCurve = 80;
    const xs = [], ys = [], zs = [];
    for (let i = 0; i <= nCurve; i++) {
      const t = tStart + (tEnd - tStart) * (i / nCurve);
      const p = fit.evaluate(t);
      xs.push(p.x); ys.push(p.y); zs.push(p.z);
    }
    const traces = [{
      type: "scatter3d", mode: "lines",
      x: xs, y: ys, z: zs,
      line: { color, width: 5, dash: "dash" },
      name: `Ballistic fit${nameSuffix} · RMSE ${(fit.rmse_m * 100).toFixed(1)} cm`,
      hovertemplate: `t=%{customdata:.3f}s<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
      customdata: xs.map((_, i) => tStart + (tEnd - tStart) * (i / nCurve) - fit.t0),
      legendgroup: "ballistic",
    }];
    // v0 direction arrow at release point — caps total length so a 90 mph
    // pitch doesn't shoot off-canvas; floor it so a slow lob still reads.
    const origin = fit.evaluate(fit.t0);
    const arrowLen = Math.min(2.0, Math.max(0.3, fit.speed_mps * 0.05));
    const vn = Math.max(1e-9, Math.hypot(fit.v0.x, fit.v0.y, fit.v0.z));
    traces.push({
      type: "scatter3d", mode: "lines+markers",
      x: [origin.x, origin.x + fit.v0.x / vn * arrowLen],
      y: [origin.y, origin.y + fit.v0.y / vn * arrowLen],
      z: [origin.z, origin.z + fit.v0.z / vn * arrowLen],
      line: { color, width: 4, dash: "dash" },
      marker: { size: [0, 6], color, symbol: "diamond" },
      hoverinfo: "skip", showlegend: false,
      legendgroup: "ballistic",
    });
    return traces;
  };

  // --- Speed overlay ---
  // Per-segment instantaneous speed (m/s) coloured along the trajectory.
  // Plotly Scatter3d does not support per-segment line colour in a single
  // trace, so we emit one line trace per segment plus a hidden marker
  // trace that carries the shared colorbar. Bar chart (viewer-only) is
  // built off the same speeds[] array so the two views are consistent.
  const SPEED_VISIBLE_KEY = "ball_tracker_overlay_speed_enabled";
  NS.speedVisible = function () { return readBool(SPEED_VISIBLE_KEY, false); };
  NS.setSpeedVisible = function (on) { writeBool(SPEED_VISIBLE_KEY, on); };

  // Per-segment speed: ||Δp|| / Δt. Δt ≤ 0 → null (duplicated rows or
  // out-of-order timestamps; emitting 0 would silently masquerade as
  // "ball stationary" — flag-bearing repo policy is no silent fallback,
  // so consumers see null and decide). Returns array length pts.length-1.
  NS.computeSpeeds = function (pts) {
    if (!pts || pts.length < 2) return [];
    const out = new Array(pts.length - 1);
    for (let i = 1; i < pts.length; i++) {
      const dt = pts[i].t_rel_s - pts[i - 1].t_rel_s;
      if (dt <= 0) { out[i - 1] = null; continue; }
      const dx = pts[i].x - pts[i - 1].x;
      const dy = pts[i].y - pts[i - 1].y;
      const dz = pts[i].z - pts[i - 1].z;
      out[i - 1] = Math.sqrt(dx * dx + dy * dy + dz * dz) / dt;
    }
    return out;
  };

  // Viridis-ish 5-stop ramp. Plotly accepts named "Viridis" colorscales
  // for the colorbar trace, but we need a JS-callable lookup for the
  // per-segment line colours, so do it locally. t ∈ [0, 1].
  const VIRIDIS = [
    [0.0, [68, 1, 84]],
    [0.25, [59, 82, 139]],
    [0.5, [33, 145, 140]],
    [0.75, [94, 201, 98]],
    [1.0, [253, 231, 37]],
  ];
  function viridisColor(t) {
    if (!Number.isFinite(t)) t = 0;
    if (t < 0) t = 0; if (t > 1) t = 1;
    let lo = VIRIDIS[0], hi = VIRIDIS[VIRIDIS.length - 1];
    for (let i = 1; i < VIRIDIS.length; i++) {
      if (t <= VIRIDIS[i][0]) { hi = VIRIDIS[i]; lo = VIRIDIS[i - 1]; break; }
    }
    const span = Math.max(1e-9, hi[0] - lo[0]);
    const f = (t - lo[0]) / span;
    const r = Math.round(lo[1][0] + (hi[1][0] - lo[1][0]) * f);
    const g = Math.round(lo[1][1] + (hi[1][1] - lo[1][1]) * f);
    const b = Math.round(lo[1][2] + (hi[1][2] - lo[1][2]) * f);
    return `rgb(${r},${g},${b})`;
  }
  NS.viridisColor = viridisColor;

  // Build the speed-coloured 3D trajectory traces.
  //
  // *** Why this is bucketed instead of one-trace-per-segment ***
  // Plotly's Scatter3d does NOT accept an array for `line.color` — line
  // colour is per-trace, not per-vertex. Earlier the overlay emitted N-1
  // tiny line traces (one per segment) so each could have its own colour.
  // For a 1.5 s pitch at 240 fps that's 80–150 segments × 2 paths =
  // 160–300 Plotly traces per render. During playback the cutoff trims
  // the tail so the trace COUNT changes every frame, which forces
  // Plotly.react to do a full structural rebuild instead of a cheap data
  // diff. Measured: 30–50 ms react cost, dropping ~10 fps of video sync.
  //
  // Bucketing the speed range into N_BUCKETS quantised stops collapses
  // every same-bucket segment into ONE line trace (with NaN separators
  // so segments stay visually disjoint). Trace count becomes O(1) and
  // stable across playback frames — Plotly can diff data arrays without
  // rebuilding, dropping react cost back to ~5 ms. The visual quantisation
  // of the colour ramp at 8 buckets is below human perceptual resolution
  // for typical speed ranges (a 30 m/s pitch quantised in 3.75 m/s steps
  // reads as a smooth gradient).
  //
  // Per-segment hover precision is preserved by a separate per-vertex
  // marker trace whose `marker.color` IS allowed to be an array — that
  // trace also carries the shared colorbar.
  //
  // `opts.includeColorbar` defaults true; callers building multiple
  // trajectories pass false on all but one. `opts.vmaxOverride` shares
  // a global vmax across trajectories so segments are comparable.
  const SPEED_N_BUCKETS = 8;
  const SPEED_NEUTRAL_GREY = "#9C9690";
  NS.SPEED_N_BUCKETS = SPEED_N_BUCKETS;
  NS.speedTraces = function (pts, opts) {
    const o = opts || {};
    if (!pts || pts.length < 2) return [];
    const cutoff = (o.cutoff === undefined) ? Infinity : o.cutoff;
    const speeds = NS.computeSpeeds(pts);
    const valid = speeds.filter(v => v !== null && Number.isFinite(v));
    const vmaxLocal = valid.reduce((a, b) => Math.max(a, b), 0);
    const vmax = (o.vmaxOverride && o.vmaxOverride > 0) ? o.vmaxOverride : vmaxLocal;
    const vmin = 0;
    const span = Math.max(1e-3, vmax - vmin);
    const lineWidth = o.lineWidth || 6;
    const tag = o.tag || "";

    // Pre-allocate buckets. Each holds a flat NaN-separated coordinate
    // stream so segments don't connect across the bucket.
    const buckets = [];
    for (let b = 0; b < SPEED_N_BUCKETS; b++) {
      const tCenter = (b + 0.5) / SPEED_N_BUCKETS;
      buckets.push({
        x: [], y: [], z: [],
        color: viridisColor(tCenter),
        vLo: vmin + (b / SPEED_N_BUCKETS) * span,
        vHi: vmin + ((b + 1) / SPEED_N_BUCKETS) * span,
      });
    }
    const nullBucket = { x: [], y: [], z: [] };
    // Hover-marker arrays — one entry per segment END vertex with the
    // segment's speed value; lets the user read exact m/s without
    // hitting the bucket-quantised line.
    const markerX = [], markerY = [], markerZ = [];
    const markerC = [], markerHover = [];

    for (let i = 0; i < speeds.length; i++) {
      const a = pts[i], b = pts[i + 1];
      if (a.t_rel_s > cutoff) break;
      const v = speeds[i];
      if (v === null || !Number.isFinite(v)) {
        nullBucket.x.push(a.x, b.x, NaN);
        nullBucket.y.push(a.y, b.y, NaN);
        nullBucket.z.push(a.z, b.z, NaN);
        continue;
      }
      let bIdx = Math.floor(((v - vmin) / span) * SPEED_N_BUCKETS);
      if (bIdx < 0) bIdx = 0;
      if (bIdx >= SPEED_N_BUCKETS) bIdx = SPEED_N_BUCKETS - 1;
      buckets[bIdx].x.push(a.x, b.x, NaN);
      buckets[bIdx].y.push(a.y, b.y, NaN);
      buckets[bIdx].z.push(a.z, b.z, NaN);
      // Marker at midpoint of the segment so it sits on the coloured line.
      markerX.push((a.x + b.x) * 0.5);
      markerY.push((a.y + b.y) * 0.5);
      markerZ.push((a.z + b.z) * 0.5);
      markerC.push(v);
      markerHover.push(
        `seg ${i + 1}/${speeds.length}<br>` +
        `t=${a.t_rel_s.toFixed(3)}–${b.t_rel_s.toFixed(3)} s<br>` +
        `v=${v.toFixed(2)} m/s · ${(v * 3.6).toFixed(1)} km/h`
      );
    }

    const traces = [];
    for (let b = 0; b < SPEED_N_BUCKETS; b++) {
      if (!buckets[b].x.length) continue;
      traces.push({
        type: "scatter3d", mode: "lines",
        x: buckets[b].x, y: buckets[b].y, z: buckets[b].z,
        line: { color: buckets[b].color, width: lineWidth },
        hovertemplate:
          `v ∈ [${buckets[b].vLo.toFixed(1)}, ${buckets[b].vHi.toFixed(1)}] m/s` +
          `<extra></extra>`,
        showlegend: false,
        legendgroup: "speed" + tag,
      });
    }
    if (nullBucket.x.length) {
      traces.push({
        type: "scatter3d", mode: "lines",
        x: nullBucket.x, y: nullBucket.y, z: nullBucket.z,
        line: { color: SPEED_NEUTRAL_GREY, width: lineWidth, dash: "dot" },
        hovertemplate: "Δt ≤ 0 — speed undefined<extra></extra>",
        showlegend: false,
        legendgroup: "speed" + tag,
      });
    }
    // Per-vertex hover markers + (optionally) the shared colorbar.
    // If there's nothing to colour, skip — markers with empty arrays are
    // a Plotly footgun.
    if (markerX.length) {
      traces.push({
        type: "scatter3d", mode: "markers",
        x: markerX, y: markerY, z: markerZ,
        marker: {
          size: 3,
          color: markerC,
          colorscale: "Viridis",
          cmin: vmin, cmax: vmax,
          showscale: o.includeColorbar !== false,
          colorbar: o.includeColorbar !== false ? {
            title: { text: "v (m/s)", side: "right" },
            thickness: 10, len: 0.4, x: 1.02, y: 0.5,
            tickfont: { size: 10 },
          } : undefined,
        },
        customdata: markerHover,
        hovertemplate: "%{customdata}<extra></extra>",
        name: `Speed${tag} · max ${vmax.toFixed(1)} m/s`,
        showlegend: o.includeColorbar !== false,
        legendgroup: "speed" + tag,
      });
    } else if (o.includeColorbar !== false) {
      // No segments yet (playback cutoff at 0) but the user still wants
      // to see the colorbar — emit a hidden carrier so the legend isn't
      // empty during the first few frames.
      traces.push({
        type: "scatter3d", mode: "markers",
        x: [pts[0].x], y: [pts[0].y], z: [pts[0].z],
        marker: {
          size: 0.1, opacity: 0,
          color: [vmin], colorscale: "Viridis",
          cmin: vmin, cmax: vmax, showscale: true,
          colorbar: {
            title: { text: "v (m/s)", side: "right" },
            thickness: 10, len: 0.4, x: 1.02, y: 0.5,
            tickfont: { size: 10 },
          },
        },
        name: `Speed${tag} · max ${vmax.toFixed(1)} m/s`,
        hoverinfo: "skip", showlegend: true,
        legendgroup: "speed" + tag,
      });
    }
    return traces;
  };

  window.BallTrackerOverlays = NS;
})();
"""


def assert_overlays_present(html: str) -> None:
    """Sanity-check that a rendered page actually injected the runtime.

    Prevents silent regressions where a refactor drops the
    ``overlays_js=`` kwarg or moves the script tag and both pages stop
    sharing strike-zone / fit / speed state without raising any error.
    """
    if "BallTrackerOverlays" not in html:
        raise AssertionError("rendered page missing overlays runtime injection")
