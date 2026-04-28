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
      // Trash sessions are still viewable — fall back to the trash
      // bucket so refresh keeps working after the operator deletes.
      let row = await fetchRow('active');
      if (!row) row = await fetchRow('trash');
      if (!row) return;
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
