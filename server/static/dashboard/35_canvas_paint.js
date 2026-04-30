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
    const entry = sid ? trajCache.get(sid) : null;
    syncPathPillAvailability(entry);
    const view = entry ? resolvedFitView(entry) : null;
    if (sid && !resultHasRenderableFit(entry)) {
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
    layers.applyFit(sid, view);
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
    const view = entry ? resolvedFitView(entry) : null;
    const segs = view && Array.isArray(view.segments) ? view.segments : [];
    if (!sid || !segs.length) {
      badge.hidden = true;
      return;
    }
    const seg = segs[0];
    badge.hidden = false;
    if (speedEl) speedEl.textContent = seg.speed_kph.toFixed(1);
    const pathTag = view && view.path === 'server_post' ? 'SVR' : 'LIVE';
    const extra = segs.length > 1
      ? `${pathTag} · ${segs.length} segs · rmse ${(seg.rmse_m * 100).toFixed(1)}cm`
      : `${pathTag} · rmse ${(seg.rmse_m * 100).toFixed(1)}cm`;
    if (metaEl) metaEl.textContent = extra;
  }

  // Mirror the selected entry's `paths_completed` set onto the LIVE/SVR
  // pill control. Disables the SVR pill until server_post has run for
  // the selected session, and demotes a stale active selection (e.g.
  // operator was on SVR for an old session, clicks a fresh live-only
  // session — pill flips to LIVE rather than silently rendering nothing).
  function syncPathPillAvailability(entry) {
    const root = document.querySelector('.ff-path-toggle');
    if (!root) return;
    const completed = entry && entry.paths_completed instanceof Set
      ? entry.paths_completed : new Set();
    const segsByPath = (entry && entry.segments_by_path) || {};
    const buttons = root.querySelectorAll('[data-fit-path]');
    for (const btn of buttons) {
      const path = btn.dataset.fitPath;
      const ok = entry ? completed.has(path) : true;
      btn.disabled = !ok;
      const countEl = btn.querySelector('[data-fit-path-count]');
      if (countEl) {
        const segs = segsByPath[path];
        const n = Array.isArray(segs) ? segs.length : 0;
        countEl.textContent = entry ? String(n) : '';
      }
    }
    if (!entry) return;
    let active = fitPathMode();
    if (!completed.has(active)) {
      const fallback = completed.has('live')
        ? 'live'
        : (completed.has('server_post') ? 'server_post' : null);
      if (fallback && fallback !== active) {
        setFitPathMode(fallback);
        active = fallback;
      }
    }
    for (const btn of buttons) {
      btn.classList.toggle('active', btn.dataset.fitPath === active);
    }
  }
