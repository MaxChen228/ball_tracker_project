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
      if (typeof syncDashboardPlayback === 'function') syncDashboardPlayback(null, null);
      if (typeof renderEvents === 'function' && Array.isArray(currentEvents)) {
        _lastEvKey = null;
        renderEvents(currentEvents);
      }
      updateLatestPitchBadge();
      return;
    }
    layers.applyFit(sid, view);
    if (typeof syncDashboardPlayback === 'function') syncDashboardPlayback(sid, view);
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

  // Refresh the speed badge overlay above the 3D canvas.
  //
  // Speed: instantaneous |v(t)| at the current dashboard playback time,
  // computed against the segment that's *active* at that t (via
  // `BallTrackerOverlays.activeSegmentIndex`). Releases of small noisy
  // pre-segs would mis-display as the "pitch speed" if we always used
  // seg0 — a bounced 94 km/h fastball produces seg0 ≈ a 50 km/h
  // detection-noise sliver before the real seg, and the badge needs to
  // follow the scrubber to seg1 where the actual physics live.
  //
  // Verdict: whole-pitch judgment via `judgePitch(segs, zone)` — iterates
  // segments, no extrapolation past any seg's [t_start, t_end] (bounces
  // invalidate ballistic continuation). NO_PLATE_CROSS surfaces as "—"
  // rather than silently collapsing into BALL.
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
      badge.classList.remove('verdict-strike', 'verdict-ball');
      return;
    }
    badge.hidden = false;
    const NS = window.BallTrackerOverlays;
    const tEval = (typeof dashPlayback !== 'undefined' && dashPlayback && Number.isFinite(dashPlayback.t))
      ? dashPlayback.t : segs[0].t_start;
    const idx = NS.activeSegmentIndex(segs, tEval);
    const activeSeg = segs[idx >= 0 ? idx : 0];
    const inst = NS.instantSpeedKph(activeSeg, tEval);
    if (speedEl) speedEl.textContent = Number.isFinite(inst) ? inst.toFixed(1) : '—';

    const zone = window.BallTrackerScene && typeof window.BallTrackerScene.strikeZone === 'function'
      ? window.BallTrackerScene.strikeZone() : null;
    let verdict = 'ball';
    if (zone) {
      const judg = NS.judgePitch(segs, zone);
      verdict = judg ? judg.verdict : 'ball';
    }
    if (metaEl) metaEl.textContent = verdict === 'strike' ? 'STRIKE' : 'BALL';
    badge.classList.toggle('verdict-strike', verdict === 'strike');
    badge.classList.toggle('verdict-ball', verdict === 'ball');
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
