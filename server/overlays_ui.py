"""Shared overlay UI primitives for dashboard `/` and viewer `/viewer/{sid}`.

The dashboard and viewer both render the same Plotly 3D scene. Anything
that toggles a visual layer on top of that scene — strike zone today,
ballistic fit and per-segment speed colouring next — needs identical
behaviour on both surfaces (same localStorage keys, same trace meta
predicates, same defaults). Centralising the JS runtime here keeps the
two pages in lock-step without each duplicating logic.

Inject ``OVERLAYS_RUNTIME_JS`` as its own ``<script>`` block BEFORE each
page's main script. It exposes ``window.BallTrackerOverlays`` with
helpers consumers can alias locally.
"""
from __future__ import annotations

OVERLAYS_RUNTIME_JS: str = r"""
(function () {
  if (window.BallTrackerOverlays) return;
  const NS = {};
  // Strike zone — shared visibility flag across dashboard + viewer.
  const STRIKE_ZONE_KEY = "ball_tracker_strike_zone_visible";
  NS.strikeZoneVisible = function () {
    try {
      const raw = localStorage.getItem(STRIKE_ZONE_KEY);
      if (raw === null) return true;
      return raw === "1";
    } catch (_) { return true; }
  };
  NS.setStrikeZoneVisible = function (on) {
    try { localStorage.setItem(STRIKE_ZONE_KEY, on ? "1" : "0"); } catch (_) {}
  };
  NS.isStrikeZoneTrace = function (t) {
    return !!(t && t.meta && t.meta.feature === "strike_zone");
  };
  window.BallTrackerOverlays = NS;
})();
"""
