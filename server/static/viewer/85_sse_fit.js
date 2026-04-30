  // === SSE 'fit' listener ===
  //
  // Without this, cycle_end / server-post `fit` events would only land
  // via 95_autorefresh's location.reload — losing scrubber position,
  // video buffer, layer-visibility map. Recompute already patches
  // in-place via the inline /recompute response handler in
  // viewer_page.py, so we filter `cause === "recompute"` to avoid a
  // double-write and a wasted /results refetch.
  {
    const SID = (SCENE && SCENE.session_id)
      || location.pathname.split('/').filter(Boolean).pop();
    if (!SID) throw new Error("viewer SSE init: SID missing from SCENE / pathname");
    // Server '/stream' is unfiltered; we filter by sid client-side.
    const es = new EventSource('/stream');
    es.addEventListener('fit', async (evt) => {
      const payload = JSON.parse(evt.data);
      if (payload.sid !== SID) return;
      // Tell autorefresh "I just patched" regardless of cause — every
      // fit event mutates the row's signature on the server side, so
      // suppressing the would-be reload is needed even when we skip
      // the refetch path below.
      window.dispatchEvent(new CustomEvent('viewer:fit-applied'));
      if (payload.cause === "recompute") return;
      // Refetch full SessionResult — fit event ships only segments +
      // thresholds, but cycle_end / server-post both also mutate
      // points (new triangulations).
      const r = await fetch('/results/' + encodeURIComponent(SID), { cache: 'no-store' });
      if (!r.ok) throw new Error("viewer SSE: /results fetch failed " + r.status);
      const result = await r.json();
      if (!window.BallTrackerViewerScene) {
        throw new Error("viewer SSE: BallTrackerViewerScene not mounted");
      }
      if (!Array.isArray(result.points) || !result.triangulated_by_path
          || !Array.isArray(result.segments)) {
        throw new Error("viewer SSE: /results payload missing required fields");
      }
      window.BallTrackerViewerScene.setSessionData({
        points: result.points,
        triangulated_by_path: result.triangulated_by_path,
        segments: result.segments,
      });
      // `let SEGMENTS` in 00_boot.js was declared mutable for this path.
      SEGMENTS = result.segments;
      updateSpeedBadge();
    });
  }
