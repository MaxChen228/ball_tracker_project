  // === Camera presets =================================================
  // Five fixed views (ISO/CATCH/SIDE/TOP/PITCHER) shared with the
  // dashboard via window.BallTrackerViewPresets (server/view_presets_runtime.py).
  // Eye/up/center tables, click→Plotly.relayout wiring, and the
  // plotly_relayouting active-pill clear all live in the runtime so
  // dashboard + viewer stay in lockstep — pre-extraction the dashboard
  // had no presets at all and this file held its own copy.
  if (window.BallTrackerViewPresets && typeof sceneDiv !== "undefined") {
    const _toolbar = document.querySelector(".scene-col .scene-views");
    if (_toolbar) window.BallTrackerViewPresets.bind(sceneDiv, _toolbar);
  }
