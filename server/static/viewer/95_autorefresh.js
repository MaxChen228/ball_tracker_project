  // === viewer auto-refresh ============================================
  // Poll `/events` every 5 s and reload the page when this session's
  // digest changes. Triggers reflect:
  //   - MOV upload completes        → row.mode flips live_only → camera_only
  //   - Run server detection done   → row.n_triangulated bumps, path_status changes
  //   - reprocess_sessions.py runs  → row.received_at (pitch JSON mtime) bumps
  //   - background processing      → row.processing_state running ↔ idle
  //
  // Field names match server/state_events.py::build_events output. The
  // viewer is otherwise pure SSR — no WS, no other fetch — so this is
  // the only freshness mechanism short of a manual reload.
  //
  // First-seen never reloads (lastSig === null); the first tick records
  // the baseline. Subsequent diffs trigger location.reload(). Yes this
  // interrupts a 3D scene drag — acceptable trade-off for a personal LAN
  // tool, see the plan for rationale.
  (() => {
    const SID = (SCENE && SCENE.session_id)
      || location.pathname.split('/').filter(Boolean).pop();
    if (!SID) return;

    let lastSig = null;
    // Bumped on every viewer:fit-applied. tick() captures it pre-fetch
    // and drops the result if a fit landed mid-fetch — otherwise tick
    // would record a stale (pre-rebuild) sig as the new baseline, and
    // the *next* tick would diff fresh-vs-stale and reload despite
    // 85_sse_fit having already patched the scene.
    let fitGeneration = 0;
    window.addEventListener('viewer:fit-applied', () => { fitGeneration++; });
    async function fetchRow(bucket) {
      try {
        const r = await fetch(`/events?bucket=${bucket}`, { cache: 'no-store' });
        if (!r.ok) return null;
        const events = await r.json();
        return events.find(e => e.session_id === SID) || null;
      } catch (_) {
        return null;
      }
    }
    async function tick() {
      const seenGen = fitGeneration;
      // Trash sessions are still viewable — fall back to the trash
      // bucket so refresh keeps working after the operator deletes.
      let row = await fetchRow('active');
      if (!row) row = await fetchRow('trash');
      if (!row) return;
      // Fit landed during fetch — drop this (potentially pre-rebuild)
      // row. Next tick will see the post-rebuild sig as ground truth.
      if (fitGeneration !== seenGen) return;
      const sig = JSON.stringify({
        mode: row.mode,
        received_at: row.received_at,
        n_triangulated: row.n_triangulated,
        path_status: row.path_status,
        processing_state: row.processing_state,
      });
      if (lastSig !== null && sig !== lastSig) {
        location.reload();
        return;
      }
      lastSig = sig;
    }
    tick();
    setInterval(tick, 5000);
  })();
