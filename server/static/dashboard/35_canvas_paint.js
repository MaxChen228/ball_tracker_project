// === canvas repaint (Three.js dispatcher) ===
//
// Phase 2 of the 3D migration: this file used to drive a full
// `Plotly.react(scene, traces, layout)` rebuild on every selection
// change / events tick / fit-SSE event. Under Three.js the scene
// runtime + per-layer modules manage their own state; `repaintCanvas`
// becomes a thin dispatcher that pushes the latest selection / live
// session data into the layer controller.
//
// The Plotly-era helpers `cachedLayout`, `liveTraceIdx`, the
// `Plotly.extendTraces` fast-path, the wheel-zoom hack, and
// `doubleClick:false` all retired with this commit — the Three.js
// scene's OrbitControls handles wheel + drag natively without any
// uirevision bookkeeping, and BufferGeometry rebuild on append is
// cheap enough that we don't need an extend-style fast path.

  let canvasFirstPaintDone = false;

  function _layers() {
    return window.BallTrackerDashboardScene || null;
  }

  function extendLivePoint(pt) {
    // BufferGeometry rebuild per append is fast in Three.js — no
    // extendTraces analogue needed. Returns true on success so the
    // caller's "fall back to repaintCanvas" branch stays a no-op
    // when the live trail is healthy.
    const layers = _layers();
    if (!layers) return false;
    layers.appendLivePoint(pt);
    return true;
  }

  async function repaintCanvas() {
    const layers = _layers();
    if (!layers) return;  // module hasn't mounted yet; next tick will catch up
    // Load any missing trajectories in parallel — selection changes
    // before the first /events tick should still paint immediately.
    await Promise.all([...selectedTrajIds].map(sid => ensureTrajLoaded(sid)));
    const sid = [...selectedTrajIds][0] || null;
    const result = sid ? trajCache.get(sid) : null;
    if (sid && !resultHasRenderableFit(result)) {
      selectedTrajIds.delete(sid);
      persistTrajSelection();
      layers.applyFit(null, null);
      if (typeof renderEvents === 'function' && Array.isArray(currentEvents)) {
        _lastEvKey = null;
        renderEvents(currentEvents);
      }
      updateLatestPitchBadge();
      return;
    }
    layers.applyFit(sid, result);
    // Live session — pulls live trail + per-cam rays from the
    // `livePointStore` / `liveRayStore` maps that the WS frame
    // listener (86_live_stream.js) pushes into.
    if (currentLiveSession && currentLiveSession.session_id) {
      const lsid = currentLiveSession.session_id;
      layers.applyLive({
        session: currentLiveSession,
        points: livePointStore.get(lsid) || [],
        raysByCam: liveRayStore.get(lsid) || new Map(),
      });
    } else {
      layers.clearLive();
    }
    canvasFirstPaintDone = true;
    updateLatestPitchBadge();
  }

  // Expose a stable hook for the module-side scene boot script.
  // `repaintCanvas` itself lives inside the dashboard IIFE, so a
  // `type="module"` script cannot see it by lexical scope. The boot
  // script calls this once after `setupDashboardLayers(...)` so a
  // selection restored from localStorage is rehydrated immediately.
  window.BallTrackerDashboardRepaint = repaintCanvas;

  // Refresh the speed badge overlay above the 3D canvas. Reads the
  // currently-selected sid's segments from `trajCache` and shows the
  // first segment's speed (operators throw single-segment pitches; a
  // bouncing ball produces multi-segment but the release speed lives
  // on segment 0). Hidden when no fit data is available.
  function updateLatestPitchBadge() {
    const badge = document.getElementById('latest-pitch-badge');
    if (!badge) return;
    const speedEl = document.getElementById('lpb-speed');
    const metaEl = document.getElementById('lpb-meta');
    const sid = [...selectedTrajIds][0] || null;
    const entry = sid ? trajCache.get(sid) : null;
    const segs = entry && Array.isArray(entry.segments) ? entry.segments : [];
    if (!sid || !segs.length) {
      badge.hidden = true;
      return;
    }
    const seg = segs[0];
    badge.hidden = false;
    if (speedEl) speedEl.textContent = seg.speed_kph.toFixed(1);
    const extra = segs.length > 1
      ? `${segs.length} segs · rmse ${(seg.rmse_m * 100).toFixed(1)}cm`
      : `rmse ${(seg.rmse_m * 100).toFixed(1)}cm`;
    if (metaEl) metaEl.textContent = extra;
  }
