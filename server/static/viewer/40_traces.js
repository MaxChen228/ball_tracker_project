  // === viewer fit-segment helpers (Three.js handoff) ===
  //
  // Phase 3 of the 3D migration: this file used to build Plotly
  // trace dicts (`buildDynamicTraces`, `camMarkerTracesFor`). The
  // Three.js viewer-layers module
  // (`server/static/threejs/viewer_layers.js`) now owns mesh / line
  // construction; what stays here is only the helpers that the
  // legacy IIFE still calls — the speed badge in 50_canvas.js
  // (`updateSpeedBadge`) needs `activeSegmentIndex`.

  function currentSegments() {
    const path = currentPath();
    const segs = SEGMENTS_BY_PATH && Array.isArray(SEGMENTS_BY_PATH[path])
      ? SEGMENTS_BY_PATH[path]
      : [];
    return segs;
  }

  // Pick the segment whose [t_start, t_end] contains `t`, or the
  // nearest one (by midpoint distance) when no segment is active.
  // Returns -1 on empty SEGMENTS. The "nearest" branch is intentional
  // UX: scrubbing in the wind-up portion of the video should still
  // show seg0's release speed in the badge rather than blank out;
  // the scene's active-fit-marker layer separately gates on strict
  // in-range, so the operator always sees the marker only when a
  // segment truly covers `currentT`.
  function activeSegmentIndex(t) {
    const segs = currentSegments();
    if (!segs.length) return -1;
    for (let i = 0; i < segs.length; ++i) {
      const s = segs[i];
      if (t >= s.t_start && t <= s.t_end) return i;
    }
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < segs.length; ++i) {
      const s = segs[i];
      const mid = 0.5 * (s.t_start + s.t_end);
      const d = Math.abs(t - mid);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return best;
  }

  // Plate / projection / virtual-base helpers are still consumed by
  // the cam-view runtime (per-cam canvas overlay above the video) —
  // keep the placeholder substitution targets here so the page
  // build's `str.replace` continues to find them.
  {PLATE_WORLD_JS}
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
