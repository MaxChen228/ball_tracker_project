  // === viewer fit-segment helpers (Three.js handoff) ===
  //
  // Phase 3 of the 3D migration: this file used to build Plotly
  // trace dicts. The Three.js viewer-layers module owns mesh / line
  // construction now; this file only keeps `currentSegments()` because
  // the legacy IIFE consumers (50_canvas.js / 80_strip.js) still need
  // a path-aware accessor. `activeSegmentIndex` was lifted to the
  // shared `BallTrackerOverlays` NS so dashboard + viewer share one
  // implementation — call `NS.activeSegmentIndex(segs, t)` instead.

  function currentSegments() {
    const path = currentPath();
    const segs = SEGMENTS_BY_PATH && Array.isArray(SEGMENTS_BY_PATH[path])
      ? SEGMENTS_BY_PATH[path]
      : [];
    return segs;
  }

  // Plate / projection / virtual-base helpers are still consumed by
  // the cam-view runtime (per-cam canvas overlay above the video) —
  // keep the placeholder substitution targets here so the page
  // build's `str.replace` continues to find them.
  {PLATE_WORLD_JS}
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
