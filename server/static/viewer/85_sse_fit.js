  // === SSE 'fit' listener — live-apply server-side rebuilds ===
  //
  // Server emits `fit` SSE on three paths: cycle_end (live pitch ends),
  // recompute (operator hit Apply on the tuning strip), and server-post
  // rerun (background detect_pitch finished + state.record rebuilt the
  // SessionResult). Recompute already patches in-place via the inline
  // /recompute response handler in viewer_page.py — but the other two
  // arrive asynchronously while the operator is staring at the viewer,
  // and without a listener here they would only land via 95_autorefresh
  // doing a full location.reload (drops scrubber position, video buffer,
  // layer-visibility map). Mirror dashboard's patchTrajResult pattern:
  // refetch /results, hand it to setSessionData, then suppress the
  // autorefresh reload by signalling that we just patched.
  if (window.EventSource) {
    const SID = (SCENE && SCENE.session_id)
      || location.pathname.split('/').filter(Boolean).pop();
    if (SID) {
      const _es = new EventSource('/stream');
      _es.addEventListener('fit', async (evt) => {
        let payload;
        try { payload = JSON.parse(evt.data); } catch { return; }
        if (!payload || payload.sid !== SID) return;
        // Refetch full SessionResult — fit event ships only segments +
        // thresholds, but cycle_end / server-post both also mutate
        // points (new triangulations). Recompute is the only path where
        // points are invariant; refetching covers all three uniformly
        // at the cost of one extra HTTP per event (cheap, single
        // viewer, LAN).
        let result;
        try {
          const r = await fetch('/results/' + encodeURIComponent(SID), { cache: 'no-store' });
          if (!r.ok) return;
          result = await r.json();
        } catch { return; }
        if (!window.BallTrackerViewerScene) return;
        window.BallTrackerViewerScene.setSessionData({
          points: result.points || [],
          triangulated_by_path: result.triangulated_by_path || {},
          segments: result.segments || [],
        });
        // Patch IIFE-scoped SEGMENTS so activeSegmentIndex /
        // updateSpeedBadge reflect the rebuild. The `let SEGMENTS` in
        // 00_boot.js was declared mutable for exactly this path.
        SEGMENTS = Array.isArray(result.segments) ? result.segments : [];
        if (typeof updateSpeedBadge === 'function') updateSpeedBadge();
        // Tell the autorefresh poller "I just patched, don't reload" —
        // the next /events digest will diff against a freshly-recorded
        // baseline instead of triggering location.reload.
        window.dispatchEvent(new CustomEvent('viewer:fit-applied'));
      });
    }
  }
