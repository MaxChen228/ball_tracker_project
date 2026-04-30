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
