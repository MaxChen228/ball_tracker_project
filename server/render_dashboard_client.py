from __future__ import annotations

from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    DRAW_PLATE_OVERLAY_JS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
)


def _resolve_js_template() -> str:
    """Substitute the shared virt-canvas helpers into the dashboard JS.
    The template uses `{PLATE_WORLD_JS}` / `{PROJECTION_JS}` / etc. as
    literal placeholders (NOT Python f-string fields — the rest of the
    template is full of JS braces that would explode `.format()`), so
    resolve them with plain `str.replace` before embedding."""
    js = _JS_TEMPLATE_RAW
    js = js.replace("{PLATE_WORLD_JS}", PLATE_WORLD_JS)
    js = js.replace("{PROJECTION_JS}", PROJECTION_JS)
    js = js.replace("{DRAW_VIRTUAL_BASE_JS}", DRAW_VIRTUAL_BASE_JS)
    js = js.replace("{DRAW_PLATE_OVERLAY_JS}", DRAW_PLATE_OVERLAY_JS)
    return js


_JS_TEMPLATE_RAW = r"""
(function () {
  console.info('[ball_tracker] dashboard JS boot', { build: 'preview-refactor-v2' });
  const navEl = document.querySelector('.nav');
  const rootStyle = document.documentElement && document.documentElement.style;
  function syncNavOffset() {
    if (!navEl || !rootStyle) return;
    const h = Math.ceil(navEl.getBoundingClientRect().height || 0);
    if (h > 0) rootStyle.setProperty('--nav-offset', `${h}px`);
  }
  syncNavOffset();
  if (typeof ResizeObserver !== 'undefined' && navEl) {
    const navObserver = new ResizeObserver(() => syncNavOffset());
    navObserver.observe(navEl);
  } else {
    window.addEventListener('resize', syncNavOffset, { passive: true });
  }

  const EXPECTED = ['A', 'B'];
  const pageMode = document.body?.dataset.page || '';
  const setupCompareMode = pageMode === 'setup';

  const sceneRoot = document.getElementById('scene-root');
  const devicesBox = document.getElementById('devices-body');
  const activeBox = document.getElementById('active-body');
  const sessionBox = document.getElementById('session-body');
  const eventsBox = document.getElementById('events-body');
  const navStatus = document.getElementById('nav-status');
  let currentDefaultPaths = ['server_post'];
  let currentLiveSession = null;
  const livePointStore = new Map();   // sid -> [{x,y,z,t_rel_s}]
  let lastEndedLiveSid = null;        // For ghost-preview on the next arm
  // Per-cam WS connection state from SSE device_status events. Keyed by
  // camera id; value shape: {connected: bool, since_ms: number}. The
  // degraded banner fires when an armed session has any cam that's been
  // disconnected for more than the grace window.
  const WS_GRACE_MS = 10_000;
  const wsStatus = new Map();
  // Telemetry panel state. All arrays are rolling windows; entries get
  // timestamped with Date.now() so the 60s window can be filtered by
  // wall-clock rather than insertion order. `pairTimestamps` holds the
  // arrival ms of each `point` SSE event; pair rate (pts/s) is the count
  // of entries within the trailing 1s window. `latencySamples` tracks
  // per-cam ws_latency_ms pulls from /status (1Hz).
  const TELEMETRY_WINDOW_MS = 60_000;
  const pairTimestamps = [];
  const latencySamples = { A: [], B: [] };
  const errorLog = [];  // {t_ms, kind, message}
  function recordError(kind, message) {
    errorLog.unshift({ t_ms: Date.now(), kind, message });
    if (errorLog.length > 10) errorLog.pop();
  }

  // --- Trajectory overlay state --------------------------------------------
  // Persisted set of session_ids whose triangulated trajectory is currently
  // painted on top of the calibration canvas. Cached /results/{sid} payloads
  // avoid refetching across ticks; basePlot is the last /calibration/state
  // fig spec so checkbox toggles can repaint without waiting for the 5 s
  // tick. Palette is deliberately disjoint from the A/B camera colours in
  // render_scene.py so the trajectory lines don't get confused with cams.
  const TRAJ_STORAGE_KEY = 'ball_tracker_dashboard_selected_trajectories';
  const TRAJ_PALETTE = ['#256246', '#9B6B16', '#A7372A', '#4A6B8C', '#5A5550', '#C97A2B'];
  const selectedTrajIds = (() => {
    try {
      const raw = localStorage.getItem(TRAJ_STORAGE_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch { return new Set(); }
  })();
  const trajCache = new Map();       // sid -> {points_on_device, fit_on_device}
  let basePlot = null;               // last /calibration/state .plot payload

  function persistTrajSelection() {
    try { localStorage.setItem(TRAJ_STORAGE_KEY, JSON.stringify([...selectedTrajIds])); }
    catch { /* storage full / private mode — ignore, selection stays in-memory */ }
  }

  // Stable hash → palette index so the same session always gets the same
  // colour across reloads even though the Set iteration order is random.
  function trajColorFor(sid) {
    let h = 0;
    for (let i = 0; i < sid.length; ++i) h = ((h << 5) - h + sid.charCodeAt(i)) | 0;
    return TRAJ_PALETTE[Math.abs(h) % TRAJ_PALETTE.length];
  }

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      // Dashboard displays on-device (mode-two) only. Server (mode-one) data
      // is forensic-only — it's still in the SessionResult payload but
      // intentionally ignored here.
      const entry = {
        points_on_device: data.points_on_device || [],
        fit_on_device: data.fit_on_device || null,
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  function evalQuadratic(coeffs, t) {
    return coeffs[0] * t * t + coeffs[1] * t + coeffs[2];
  }

  function densifyFit(fit, n) {
    const t0 = fit.t_min_s;
    const t1 = (fit.plate_t_s !== null && fit.plate_t_s !== undefined) ? fit.plate_t_s : fit.t_max_s;
    const xs = new Array(n), ys = new Array(n), zs = new Array(n);
    for (let i = 0; i < n; ++i) {
      const t = t0 + (t1 - t0) * (i / (n - 1));
      xs[i] = evalQuadratic(fit.coeffs_x, t);
      ys[i] = evalQuadratic(fit.coeffs_y, t);
      zs[i] = evalQuadratic(fit.coeffs_z, t);
    }
    return { xs, ys, zs };
  }

  // --- Canvas mode + playback state ---------------------------------------
  const CANVAS_MODE_KEY = 'ball_tracker_canvas_mode';
  let canvasMode = (() => {
    try { return localStorage.getItem(CANVAS_MODE_KEY) === 'replay' ? 'replay' : 'inspect'; }
    catch { return 'inspect'; }
  })();
  // Playback state — single global progress in [0,1] mapped to each selected
  // session's own [t_min, t_max]. This lets the scrubber stay coherent when
  // multiple sessions are overlaid without caring that their durations
  // differ; the UX reads as "show me all selected pitches synchronized to
  // the same fraction of their flight".
  let playheadFrac = 0.0;
  let playbackSpeed = 1.0;
  let isPlaying = false;
  let lastFrameTs = null;

  const playbackBar = document.getElementById('playback-bar');
  const playpauseBtn = document.getElementById('playpause');
  const scrubSlider = document.getElementById('scrub');
  const timeReadout = document.getElementById('time-readout');

  function activeReplaySid() {
    // Most recently added selected session is the "active" one — its
    // absolute time drives the readout while others animate at the same
    // fraction of their own flight.
    const arr = [...selectedTrajIds];
    return arr.length ? arr[arr.length - 1] : null;
  }

  function activeFitDuration() {
    const sid = activeReplaySid();
    if (!sid) return 0;
    const r = trajCache.get(sid);
    if (!r || !r.fit_on_device) return 0;
    return r.fit_on_device.t_max_s - r.fit_on_device.t_min_s;
  }

  function updateTimeReadout() {
    if (!timeReadout || !scrubSlider) return;
    const dur = activeFitDuration();
    const now = dur * playheadFrac;
    timeReadout.textContent = `${now.toFixed(2)} / ${dur.toFixed(2)} s`;
    scrubSlider.value = Math.round(playheadFrac * 1000);
  }

  // --- Strike zone geometry: MLB-standard 17" wide at plate, Z in 0.5-1.2 m
  // for a demo rig (no batter present). Drawn as a dashed wireframe so it
  // reads as reference grid, not a solid obstacle.
  const STRIKE_ZONE_HALF_W = 0.216;  // 17" / 2
  const STRIKE_ZONE_Z_LO = 0.5;
  const STRIKE_ZONE_Z_HI = 1.2;
  function strikeZoneTrace() {
    const hw = STRIKE_ZONE_HALF_W;
    return {
      type: 'scatter3d', mode: 'lines',
      x: [-hw, +hw, +hw, -hw, -hw],
      y: [0, 0, 0, 0, 0],
      z: [STRIKE_ZONE_Z_LO, STRIKE_ZONE_Z_LO, STRIKE_ZONE_Z_HI, STRIKE_ZONE_Z_HI, STRIKE_ZONE_Z_LO],
      line: { color: 'rgba(80,80,80,0.55)', width: 3, dash: 'dash' },
      name: 'strike zone',
      hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function inspectTracesFor(sid, result, color) {
    // Inspect mode: dense fitted quadratic + inlier dots + outlier X markers.
    // Lets operator judge RANSAC decisions at a glance and spot sessions
    // where the fit chose the wrong cluster.
    const fit = result.fit_on_device;
    const raw = result.points_on_device || [];
    if (fit) {
      const { xs, ys, zs } = densifyFit(fit, 64);
      const inlierSet = new Set(fit.inlier_indices);
      const inliers = raw.filter((_, i) => inlierSet.has(i));
      const outliers = raw.filter((_, i) => !inlierSet.has(i));
      const traces = [{
        type: 'scatter3d',
        mode: 'lines',
        x: xs, y: ys, z: zs,
        line: { color, width: 5 },
        name: `${sid} · fit`,
        hovertemplate: `${sid}<br>rms=${fit.rms_m.toFixed(3)}m<extra></extra>`,
        showlegend: true,
      }, {
        type: 'scatter3d',
        mode: 'markers',
        x: inliers.map(p => p.x_m),
        y: inliers.map(p => p.y_m),
        z: inliers.map(p => p.z_m),
        marker: { color, size: 3, opacity: 0.55 },
        name: `${sid} · inliers`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
        customdata: inliers.map(p => p.t_rel_s),
        showlegend: false,
      }];
      if (outliers.length) {
        traces.push({
          type: 'scatter3d',
          mode: 'markers',
          x: outliers.map(p => p.x_m),
          y: outliers.map(p => p.y_m),
          z: outliers.map(p => p.z_m),
          marker: { color: '#C03A2B', size: 5, symbol: 'x', opacity: 0.9 },
          name: `${sid} · outliers`,
          hovertemplate: `${sid} OUTLIER<br>t=%{customdata:.3f}s<extra></extra>`,
          customdata: outliers.map(p => p.t_rel_s),
          showlegend: false,
        });
      }
      return traces;
    }
    if (!raw.length) return [];
    return [{
      type: 'scatter3d',
      mode: 'lines+markers',
      x: raw.map(p => p.x_m),
      y: raw.map(p => p.y_m),
      z: raw.map(p => p.z_m),
      line: { color, width: 3, dash: 'dot' },
      marker: { color, size: 2, opacity: 0.6 },
      name: `${sid} · raw`,
      hovertemplate: `${sid} (unfit)<br>t=%{customdata:.3f}s<extra></extra>`,
      customdata: raw.map(p => p.t_rel_s),
      showlegend: true,
    }];
  }

  function replayTracesFor(sid, result, color) {
    // Replay mode: clean trajectory line + animated ball sphere + short
    // motion trail. Inlier/outlier markers are suppressed — those are an
    // inspect-mode concern, not a broadcast/demo concern.
    const fit = result.fit_on_device;
    if (!fit) return [];
    const { xs, ys, zs } = densifyFit(fit, 80);
    const tActive = fit.t_min_s + playheadFrac * (fit.t_max_s - fit.t_min_s);
    const bx = evalQuadratic(fit.coeffs_x, tActive);
    const by = evalQuadratic(fit.coeffs_y, tActive);
    const bz = evalQuadratic(fit.coeffs_z, tActive);
    // Short fading trail: 12 samples behind the ball, ~0.1 s worth.
    const trailN = 12;
    const trailDt = 0.01;
    const trailX = [], trailY = [], trailZ = [];
    for (let i = trailN; i >= 1; --i) {
      const tt = tActive - i * trailDt;
      if (tt < fit.t_min_s) continue;
      trailX.push(evalQuadratic(fit.coeffs_x, tt));
      trailY.push(evalQuadratic(fit.coeffs_y, tt));
      trailZ.push(evalQuadratic(fit.coeffs_z, tt));
    }
    return [
      {
        type: 'scatter3d', mode: 'lines',
        x: xs, y: ys, z: zs,
        line: { color, width: 4 },
        name: `${sid} · path`,
        hovertemplate: `${sid}<br>rms=${fit.rms_m.toFixed(3)}m<extra></extra>`,
        showlegend: true,
        opacity: 0.45,
      },
      {
        type: 'scatter3d', mode: 'lines',
        x: trailX, y: trailY, z: trailZ,
        line: { color, width: 6 },
        name: `${sid} · trail`,
        hoverinfo: 'skip',
        showlegend: false,
        opacity: 0.8,
      },
      {
        type: 'scatter3d', mode: 'markers',
        x: [bx], y: [by], z: [bz],
        marker: {
          color: '#D9A441', size: 9, symbol: 'circle',
          line: { color: '#4A3E24', width: 1.5 },
        },
        name: `${sid} · ball`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
        customdata: [tActive - fit.t_min_s],
        showlegend: false,
      },
    ];
  }

  function trajTracesFor(sid, result, color) {
    return canvasMode === 'replay'
      ? replayTracesFor(sid, result, color)
      : inspectTracesFor(sid, result, color);
  }

  function ghostTrace(pts, sid) {
    // Rendered before the active-session trace so the active one paints
    // on top. Alpha kept low — this is a "camera framing hasn't moved"
    // visual cue, not a thing to compare against.
    return {
      type: 'scatter3d',
      mode: 'lines',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      line: { color: 'rgba(192,57,43,0.20)', width: 2 },
      name: `${sid} · ghost`,
      hoverinfo: 'skip',
      showlegend: false,
    };
  }

  function liveTraces() {
    const traces = [];
    // Ghost preview of the previous live session — shown BETWEEN arm
    // cycles (no current session armed) so the operator can confirm
    // camera framing still matches the last pitch's trail before
    // throwing again. Suppressed once a new session arms to avoid
    // clutter on the active canvas.
    if (
      (!currentLiveSession || !currentLiveSession.session_id) &&
      lastEndedLiveSid
    ) {
      const ghostPts = livePointStore.get(lastEndedLiveSid) || [];
      if (ghostPts.length) traces.push(ghostTrace(ghostPts, lastEndedLiveSid));
    }
    if (!currentLiveSession || !currentLiveSession.session_id) return traces;
    const sid = currentLiveSession.session_id;
    const pts = livePointStore.get(sid) || [];
    if (!pts.length) return traces;
    traces.push({
      type: 'scatter3d',
      mode: 'lines+markers',
      x: pts.map(p => p.x),
      y: pts.map(p => p.y),
      z: pts.map(p => p.z),
      marker: {
        size: 4,
        color: pts.map(p => p.t_rel_s),
        colorscale: 'YlOrRd',
        opacity: 0.95,
      },
      line: { color: '#C0392B', width: 4 },
      name: `${sid} · live`,
      hovertemplate: `${sid}<br>t=%{marker.color:.3f}s<br>x=%{x:.2f} y=%{y:.2f} z=%{z:.2f}<extra></extra>`,
      showlegend: true,
    });
    return traces;
  }

  // Layout is effectively static across the dashboard's lifetime (axes,
  // aspect, uirevision never change — only trace data does). Cache the
  // first layout we see and reuse the SAME object reference on every
  // Plotly.react. Passing the identical reference is the most reliable
  // way to tell Plotly "layout hasn't changed, don't touch the camera or
  // recompute anything scene-related" — stronger than relying solely on
  // uirevision heuristics, and cheap.
  let cachedLayout = null;
  let canvasFirstPaintDone = false;
  // Index of the live-trace inside the plot's data array after the most
  // recent Plotly.react. -1 = not painted yet / stale. extendLivePoint()
  // uses Plotly.extendTraces to append a single point without walking the
  // full trace tree — the per-point append cost drops from ~5-20ms
  // (Plotly.react with full trace rebuild) to <1ms. Any structural change
  // (session flip, mode switch, trajectory toggle) must reset this to -1
  // so the next point event falls back to a full repaint and the slot
  // re-anchors.
  let liveTraceIdx = -1;

  function extendLivePoint(pt) {
    if (liveTraceIdx < 0 || !sceneRoot || !window.Plotly) return false;
    try {
      Plotly.extendTraces(
        sceneRoot,
        {
          x: [[pt.x]],
          y: [[pt.y]],
          z: [[pt.z]],
          'marker.color': [[pt.t_rel_s]],
        },
        [liveTraceIdx],
      );
      return true;
    } catch (_) {
      liveTraceIdx = -1;  // slot invalid — force repaint next time
      return false;
    }
  }

  async function repaintCanvas() {
    if (!basePlot || !window.Plotly) return;
    const extraTraces = [];
    // Load any missing trajectories in parallel — checkbox clicks before
    // the first tick should still paint immediately.
    await Promise.all([...selectedTrajIds].map(sid => ensureTrajLoaded(sid)));
    // Strike zone shown only in replay mode — serves as a reference target
    // for where the pitch is going, irrelevant for outlier-inspection.
    if (canvasMode === 'replay' && selectedTrajIds.size > 0) {
      extraTraces.push(strikeZoneTrace());
    }
    for (const sid of selectedTrajIds) {
      const result = trajCache.get(sid);
      if (!result) continue;
      extraTraces.push(...trajTracesFor(sid, result, trajColorFor(sid)));
    }
    extraTraces.push(...liveTraces());
    if (cachedLayout === null) {
      // One-time build from the first basePlot.layout we see. The server
      // sets scene.uirevision='dashboard-canvas' in both SSR and tick
      // responses — matching the value already embedded by fig.to_html
      // means Plotly never sees a uirevision transition and UI state
      // stays under user control from frame zero.
      cachedLayout = JSON.parse(JSON.stringify(basePlot.layout || {}));
      if (!cachedLayout.scene) cachedLayout.scene = {};
      cachedLayout.scene.uirevision = 'dashboard-canvas';
    }
    const finalTraces = [...(basePlot.data || []), ...extraTraces];
    Plotly.react(
      sceneRoot,
      finalTraces,
      cachedLayout,
      // doubleClick:false — Plotly 3D ships a built-in "reset camera on
      // double-click anywhere in the scene" gesture. Users bump into it
      // accidentally (especially on trackpads where a firm tap registers
      // as dblclick) and it overrides uirevision preservation. Kill it.
      // scrollZoom stays true so the native + our wheel handler both
      // work for panning the eye distance.
      { responsive: true, scrollZoom: true, doubleClick: false },
    );
    // Anchor the live-trace slot for subsequent extendTraces calls. The
    // live trace (when present) is the last one liveTraces() appends.
    liveTraceIdx = -1;
    if (currentLiveSession && currentLiveSession.session_id) {
      for (let i = finalTraces.length - 1; i >= 0; i--) {
        const t = finalTraces[i];
        if (t && typeof t.name === 'string' && t.name.endsWith(' · live')) {
          liveTraceIdx = i;
          break;
        }
      }
    }
    canvasFirstPaintDone = true;
  }

  // Plotly's built-in 3D wheel-zoom is tuned for mouse wheels and feels
  // sluggish on trackpads (especially pinch-to-zoom which arrives as
  // ctrl+wheel with tiny deltas). Replace it with a direct camera.eye
  // scale so each wheel tick = ~10 % distance change and trackpad
  // gestures get the same per-event treatment as a mouse wheel click.
  if (sceneRoot) {
    sceneRoot.addEventListener('wheel', (e) => {
      if (!sceneRoot._fullLayout || !sceneRoot._fullLayout.scene) return;
      const cam = sceneRoot._fullLayout.scene.camera;
      if (!cam || !cam.eye) return;
      e.preventDefault();
      // Wheel-down (positive deltaY) = zoom out, wheel-up = zoom in.
      // Magnitude scaled by sqrt so trackpad's many-tiny-events feels
      // continuous instead of jittery; mouse wheel's chunky events
      // still produce a noticeable but bounded jump per click.
      const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
      const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
      Plotly.relayout(sceneRoot, {
        'scene.camera.eye': {
          x: cam.eye.x * factor,
          y: cam.eye.y * factor,
          z: cam.eye.z * factor,
        },
      });
    }, { passive: false });
  }

  // Delegated change handler — event list re-renders on every tick, so we
  // can't rebind per-checkbox. Capture click on the wrapping <label> to
  // prevent the event-row <a> from swallowing the toggle.
  if (eventsBox) eventsBox.addEventListener('click', (e) => {
    if (e.target.closest('.traj-toggle')) e.stopPropagation();
  });
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-events-bucket]');
    if (!btn) return;
    e.preventDefault();
    currentEventsBucket = btn.dataset.eventsBucket === 'trash' ? 'trash' : 'active';
    document.querySelectorAll('[data-events-bucket]').forEach(node => {
      node.classList.toggle('active', node.dataset.eventsBucket === currentEventsBucket);
    });
    tickEvents();
  });
  if (eventsBox) eventsBox.addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-traj-sid]');
    if (!cb) return;
    const sid = cb.dataset.trajSid;
    // Single-select preview: clicking one row always replaces the
    // selection (clicking again on the same row deselects). Multi-select
    // was confusing when replays had different durations + made the fit
    // outlier inspector busy when several sessions overlapped in space.
    if (cb.checked) {
      selectedTrajIds.clear();
      selectedTrajIds.add(sid);
      // Uncheck every other checkbox in the events list so the DOM
      // reflects the one-at-a-time invariant without waiting for the
      // next events tick to re-render.
      eventsBox.querySelectorAll('input[data-traj-sid]').forEach(other => {
        if (other !== cb) other.checked = false;
      });
      // Reset playhead so the new selection starts from t=0 rather
      // than wherever the previous pitch was mid-animation.
      playheadFrac = 0.0;
    } else {
      selectedTrajIds.delete(sid);
    }
    persistTrajSelection();
    if (canvasMode === 'replay') updateTimeReadout();
    repaintCanvas();
  });

  // --- Canvas mode toggle: INSPECT vs REPLAY -------------------------------
  function applyCanvasMode(nextMode) {
    if (nextMode !== 'inspect' && nextMode !== 'replay') return;
    canvasMode = nextMode;
    try { localStorage.setItem(CANVAS_MODE_KEY, canvasMode); } catch {}
    document.querySelectorAll('.canvas-mode-toggle button').forEach(b => {
      b.classList.toggle('active', b.dataset.canvasMode === canvasMode);
    });
    // Playback bar only makes sense in replay mode; pause + reset the
    // scrubber when leaving so we don't keep the animation loop running
    // invisibly (wasted frames + broken readout on return).
    if (canvasMode === 'replay') {
      if (playbackBar) playbackBar.classList.add('show');
      updateTimeReadout();
    } else {
      if (playbackBar) playbackBar.classList.remove('show');
      setPlaying(false);
    }
    repaintCanvas();
  }
  document.querySelectorAll('.canvas-mode-toggle button').forEach(btn => {
    btn.addEventListener('click', () => applyCanvasMode(btn.dataset.canvasMode));
  });
  // Initial mode sync (localStorage value may already be 'replay').
  applyCanvasMode(canvasMode);

  // --- Playback controls ---------------------------------------------------
  // Track whether the user is currently mid-drag on the canvas. Plotly
  // 3D orbit/pan rely on a continuous pointer-down gesture with no
  // DOM-level repaint interruptions between mousedown and mouseup —
  // every Plotly.react during that window wipes the drag state before
  // the next mousemove can extend it. During replay playback we issue
  // Plotly.react every frame for the ball's new position, which stomps
  // on any orbit attempt and manifests as "only wheel zoom works".
  // Suppress visual repaints (not the playhead logic) while dragging;
  // the ball will catch up on mouseup.
  let isUserInteracting = false;
  if (sceneRoot) {
    sceneRoot.addEventListener('pointerdown', () => { isUserInteracting = true; });
    // mouseup/pointerup can fire OUTSIDE the canvas if the user releases
    // after dragging away — bind to window, not sceneRoot, so we never
    // miss the release and leave the flag stuck true.
    window.addEventListener('pointerup', () => { isUserInteracting = false; });
    window.addEventListener('pointercancel', () => { isUserInteracting = false; });
  }

  function setPlaying(flag) {
    isPlaying = !!flag;
    if (playpauseBtn) playpauseBtn.textContent = isPlaying ? '❚❚' : '▶';
    if (isPlaying) {
      lastFrameTs = null;
      requestAnimationFrame(animationTick);
    }
  }
  function animationTick(ts) {
    if (!isPlaying) return;
    if (lastFrameTs !== null) {
      const dur = activeFitDuration();
      if (dur > 0) {
        const dt = (ts - lastFrameTs) / 1000.0;
        playheadFrac += (dt * playbackSpeed) / dur;
        if (playheadFrac >= 1.0) {
          // Loop back to start so the operator can keep playing without
          // clicking ▶ after every pitch. If single-shot is ever desired,
          // gate on a `loop` flag from a future UI element.
          playheadFrac = 0.0;
        }
        updateTimeReadout();
        // Skip the heavy repaint while the user is mid-drag — playhead
        // still advances silently so playback resumes at the correct
        // time on pointerup.
        if (!isUserInteracting) repaintCanvas();
      }
    }
    lastFrameTs = ts;
    if (isPlaying) requestAnimationFrame(animationTick);
  }
  if (playpauseBtn) playpauseBtn.addEventListener('click', () => {
    if (activeFitDuration() <= 0) return;  // nothing to play
    setPlaying(!isPlaying);
  });
  if (scrubSlider) scrubSlider.addEventListener('input', () => {
    playheadFrac = Math.max(0, Math.min(1, parseInt(scrubSlider.value, 10) / 1000.0));
    setPlaying(false);  // user scrub pauses playback
    updateTimeReadout();
    repaintCanvas();
  });
  document.querySelectorAll('.playback-bar .speed button').forEach(btn => {
    btn.addEventListener('click', () => {
      playbackSpeed = parseFloat(btn.dataset.speed);
      document.querySelectorAll('.playback-bar .speed button').forEach(b =>
        b.classList.toggle('active', b === btn)
      );
    });
  });
  // Spacebar: play/pause when replay visible and user isn't typing in a form.
  window.addEventListener('keydown', (e) => {
    if (canvasMode !== 'replay') return;
    if (e.target.matches('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); playpauseBtn.click(); }
  });

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c])); }

  function statusChip(cam, online, calibrated) {
    if (calibrated) return `<span class="chip calibrated">calibrated</span>`;
    if (online)     return `<span class="chip online">online</span>`;
    return `<span class="chip idle">offline</span>`;
  }

  function autoCalLabel(autoRun, autoLast, online) {
    if (autoRun) {
      return autoRun.summary || autoRun.status || 'running';
    }
    if (autoLast) {
      if (autoLast.status === 'completed') {
        const reproj = autoLast.result && autoLast.result.reprojection_px != null
          ? (' · ' + Number(autoLast.result.reprojection_px).toFixed(1) + 'px')
          : '';
        return `${autoLast.summary || 'Applied'}${reproj}`;
      }
      return autoLast.summary || autoLast.status || 'failed';
    }
    return online ? 'idle' : 'offline';
  }

  function autoCalButtonLabel(autoRun) {
    if (!autoRun) return 'Run auto-cal';
    switch (autoRun.status) {
      case 'searching': return 'Capturing…';
      case 'tracking': return 'Tracking…';
      case 'stabilizing': return 'Stabilizing…';
      case 'solving': return 'Solving…';
      default: return 'Auto-cal…';
    }
  }

  function renderDevices(state) {
    if (!devicesBox) return;
    const devByCam = new Map((state.devices || []).map(d => [d.camera_id, d]));
    const calibrated = new Set(state.calibrations || []);
    const syncPending = state.sync_commands || {};
    const previewReq = state.preview_requested || {};
    const previewPending = new Set(state.preview_pending || []);
    const calLastTs = state.calibration_last_ts || {};
    const autoCalActive = (state.auto_calibration && state.auto_calibration.active) || {};
    const autoCalLast = (state.auto_calibration && state.auto_calibration.last) || {};
    function hhmm(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000);
      return d.toTimeString().slice(0, 5);
    }

    function row(cam, deviceRecord) {
      const online = !!deviceRecord;
      const timeSynced = !!(deviceRecord && deviceRecord.time_synced);
      const pending = !!syncPending[cam];
      const isCal = calibrated.has(cam);
      const previewOn = !!previewReq[cam];
      const previewBusy = previewPending.has(cam);
      const lastTs = calLastTs[cam];
      const autoRun = autoCalActive[cam] || null;
      const autoLast = autoCalLast[cam] || null;
      const calDot = isCal ? 'ok' : (online ? 'warn' : 'bad');
      const syncDot = !online ? 'bad' : (pending ? 'warn' : (timeSynced ? 'ok' : 'warn'));
      const autoDot = autoRun ? 'warn'
                    : (autoLast && autoLast.status === 'completed' ? 'ok'
                    : (autoLast && autoLast.status === 'failed' ? 'bad' : (online ? 'warn' : 'bad')));
      const syncLabel = !online ? 'offline' : (pending ? 'pending…' : (timeSynced ? 'synced' : 'not synced'));
      const calLabel = (isCal && lastTs) ? ('last ' + hhmm(lastTs))
                     : (!online ? 'offline' : (isCal ? 'calibrated' : 'pending'));
      const autoLabel = autoCalLabel(autoRun, autoLast, online);
      const previewDisabled = previewBusy || !online;
      const autoCalDisabled = !!autoRun || !online;
      const previewBtn = (`<button type="button" class="btn small preview-btn${previewOn ? ' active' : ''}" ` +
        `data-preview-cam="${esc(cam)}" data-preview-enabled="${previewOn ? 1 : 0}" ` +
        `${previewDisabled ? 'disabled' : ''}>` +
        `${previewBusy ? (previewOn ? 'PREVIEW ON…' : 'PREVIEW…') : (previewOn ? 'PREVIEW ON' : 'PREVIEW')}</button>`);
      const autoCalBtn = `<button type="button" class="btn small" data-auto-cal="${esc(cam)}" ${autoCalDisabled ? 'disabled' : ''}>` +
        `${autoCalButtonLabel(autoRun)}</button>`;
      // Always render the panel so the row height stays stable; off
      // state shows a black placeholder. When on, the tickPreviewImages
      // loop (see below) cache-busts the <img src>.
      // Only hit the preview endpoint when actually watching — otherwise
      // the browser eagerly fetches the <img> src on every render and
      // spams 404s for cams with preview off.
      const initialSrc = previewOn
        ? ('/camera/' + encodeURIComponent(cam) + '/preview?t=' + Date.now())
        : '';
      const previewPanel = `<div class="preview-panel${previewOn ? '' : ' off'}" data-preview-panel="${esc(cam)}">` +
        `<img data-preview-img="${esc(cam)}" src="${initialSrc}" alt="preview ${esc(cam)}">` +
        `<svg class="plate-overlay" data-preview-overlay="${esc(cam)}" aria-hidden="true"><polygon></polygon></svg>` +
        `<div class="placeholder">${previewOn ? '…' : 'Preview off'}</div>` +
        `</div>`;
      const virtCell = `<div class="virt-cell" data-virt-cell="${esc(cam)}">` +
        `<canvas data-virt-canvas="${esc(cam)}"></canvas>` +
        `<div class="virt-label">VIRT · ${esc(cam)}</div>` +
        `<div class="placeholder">${isCal ? 'loading…' : 'not calibrated'}</div>` +
        `</div>`;
      const syncLedCls = !online ? 'offline'
                        : pending ? 'listening'
                        : timeSynced ? 'synced'
                        : 'waiting';
      const syncId = deviceRecord && deviceRecord.time_sync_id;
      const shortSid = syncId ? (syncId.length > 8 ? syncId.slice(-6) : syncId.replace(/^sy_/, '')) : '';
      const syncIdTxt = (timeSynced && syncId)
        ? `<span class="sync-id-chip" title="${esc(syncId)}">·${esc(shortSid)}</span>`
        : '';
      return `
        <div class="device">
          <div class="device-head">
            <span class="sync-led ${syncLedCls}" title="time sync · ${esc(syncLabel)}"></span>
            <div class="id">${esc(cam)}</div>
            <div class="sub">
              <span class="item ${syncDot}"><span class="dot ${syncDot}"></span>time sync · ${esc(syncLabel)}${syncIdTxt}</span>
              <span class="item ${calDot}"><span class="dot ${calDot}"></span>pose · ${esc(calLabel)}</span>
              <span class="item ${autoDot}"><span class="dot ${autoDot}"></span>auto-cal · ${esc(autoLabel)}</span>
            </div>
            <div class="chip-col">${statusChip(cam, online, isCal)}</div>
          </div>
          <div class="device-actions">${previewBtn}${autoCalBtn}</div>
          ${previewPanel}
          ${virtCell}
        </div>`;
    }

    const rows = EXPECTED.map(cam => row(cam, devByCam.get(cam))).join('');
    const extras = (state.devices || [])
      .filter(d => !EXPECTED.includes(d.camera_id))
      .map(d => row(d.camera_id, d)).join('');
    devicesBox.innerHTML = `<div class="devices-grid">${rows + extras}</div>`;
    // The innerHTML rebuild above destroys any existing canvases inside
    // the virt cells and preview overlays — redraw them on the fresh DOM.
    if (typeof redrawAllVirtCanvases === 'function') redrawAllVirtCanvases();
    if (typeof redrawAllPreviewPlateOverlays === 'function') redrawAllPreviewPlateOverlays();
  }

  const MODE_LABELS = { camera_only: 'Camera-only', on_device: 'On-device', dual: 'Dual' };
  const PATH_LABELS = {
    live: ['Live stream', 'iOS → WS'],
    ios_post: ['iOS post-pass', 'on-device analyzer'],
    server_post: ['Server post-pass', 'PyAV + OpenCV'],
  };

  // Instantaneous fps derived from the most recent pair of frame_count
  // samples. Returns 0 when <2 samples or the window is too short to be
  // meaningful. Keeps the sparkline-per-cam history bounded to 60 entries
  // (~60s at 1Hz frame_count emission) so arbitrary-long sessions don't
  // grow unbounded.
  const FPS_HISTORY_CAP = 60;
  function pushFrameSample(liveSession, cam, count) {
    liveSession.frame_samples = liveSession.frame_samples || { A: [], B: [] };
    const arr = liveSession.frame_samples[cam] = liveSession.frame_samples[cam] || [];
    const now = Date.now();
    const prev = arr.length ? arr[arr.length - 1] : null;
    arr.push({ t: now, count });
    if (arr.length > FPS_HISTORY_CAP) arr.shift();
    // fps from most recent two samples
    if (arr.length >= 2) {
      const a = arr[arr.length - 2];
      const b = arr[arr.length - 1];
      const dtS = Math.max(0.001, (b.t - a.t) / 1000);
      liveSession.frame_fps = liveSession.frame_fps || {};
      liveSession.frame_fps[cam] = Math.max(0, (b.count - a.count) / dtS);
    }
    return prev;
  }

  function drawSparkline(canvas, samples) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!samples || samples.length < 2) return;
    // Derive per-sample fps on the fly
    const fps = [];
    for (let i = 1; i < samples.length; i++) {
      const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
      fps.push((samples[i].count - samples[i - 1].count) / dtS);
    }
    const maxFps = Math.max(240, ...fps);  // keep 240 as visual cap
    ctx.strokeStyle = '#C0392B';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    fps.forEach((f, i) => {
      const x = (i / (fps.length - 1 || 1)) * W;
      const y = H - (f / maxFps) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function fmtElapsed(ms) {
    if (!ms || ms < 0) return '00:00.0';
    const total = ms / 1000;
    const m = Math.floor(total / 60);
    const s = Math.floor(total % 60);
    const ds = Math.floor((total * 10) % 10);
    return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}.${ds}`;
  }

  function renderActiveSession(liveSession) {
    if (!activeBox) return;
    if (!liveSession || !liveSession.session_id) {
      activeBox.innerHTML = `<div class="active-empty">No active live stream.</div>`;
      return;
    }
    const sid = esc(liveSession.session_id);
    const frameCounts = liveSession.frame_counts || {};
    const fps = liveSession.frame_fps || {};
    const armed = liveSession.armed !== false;
    const chips = (liveSession.paths || []).map(path =>
      `<span class="path-chip on">${esc((PATH_LABELS[path] || [path])[0])}</span>`
    ).join('') || `<span class="path-chip">none</span>`;
    const elapsedMs = liveSession.armed_at_ms
      ? (armed ? Date.now() : (liveSession.ended_at_ms || Date.now())) - liveSession.armed_at_ms
      : 0;
    // Last-point-age flips red after 200ms of silence during an armed
    // session — operator signal that triangulation has stalled (lost
    // sync, ball left frame, or a stream is dropping frames).
    const lastPtMs = liveSession.last_point_at_ms
      ? Date.now() - liveSession.last_point_at_ms
      : null;
    const lastPtClass = (armed && lastPtMs !== null && lastPtMs > 200) ? 'stale' : '';
    const lastPtTxt = lastPtMs === null
      ? '—'
      : (lastPtMs < 1000 ? `${lastPtMs}ms ago` : `${(lastPtMs/1000).toFixed(1)}s ago`);
    const depths = liveSession.point_depths || [];
    let depthTxt = '—';
    if (depths.length) {
      const mean = depths.reduce((a,b)=>a+b,0) / depths.length;
      const variance = depths.reduce((a,b)=>a+(b-mean)*(b-mean),0) / depths.length;
      const std = Math.sqrt(variance);
      depthTxt = `${mean.toFixed(2)}m ± ${std.toFixed(2)}`;
    }
    // Post-pass rows: which paths are part of the session and their status
    const pathsOn = new Set(liveSession.paths || []);
    const completed = new Set(liveSession.paths_completed || []);
    const postPassRow = (path, label) => {
      if (!pathsOn.has(path)) return '';
      const state = completed.has(path) ? 'done' : (armed ? 'pending' : 'running');
      return `<span class="postpass-chip ${state}">${esc(label)}: ${state}</span>`;
    };
    const postPassChips = [
      postPassRow('ios_post', 'iOS'),
      postPassRow('server_post', 'srv'),
    ].filter(Boolean).join('');
    activeBox.innerHTML = `
      <div class="active-head">
        <span class="chip armed ${armed ? 'pulse' : ''}">${armed ? '●REC' : 'ended'}</span>
        <span class="session-id">${sid}</span>
        <span class="elapsed" data-elapsed>${fmtElapsed(elapsedMs)}</span>
      </div>
      <div class="path-chip-row">${chips}</div>
      <div class="cam-row" data-cam="A">
        <canvas class="spark" data-spark="A"></canvas>
        <span class="k">A</span>
        <span class="v">${(fps.A || 0).toFixed(0)} fps</span>
        <span class="vsub">${Number(frameCounts.A || 0)} frames</span>
      </div>
      <div class="cam-row" data-cam="B">
        <canvas class="spark" data-spark="B"></canvas>
        <span class="k">B</span>
        <span class="v">${(fps.B || 0).toFixed(0)} fps</span>
        <span class="vsub">${Number(frameCounts.B || 0)} frames</span>
      </div>
      <div class="live-pairs ${lastPtClass}">
        <span class="k">Live pairs</span>
        <span class="v">${Number(liveSession.point_count || 0)} pts</span>
        <span class="vsub">last ${lastPtTxt} · ${depthTxt}</span>
      </div>
      ${postPassChips ? `<div class="postpass-row">${postPassChips}</div>` : ''}
      <div class="active-actions">
        <button type="button" class="btn-reset" data-reset-trail>Reset trail</button>
      </div>`;
    // Redraw sparklines after DOM replacement (canvas clears on innerHTML).
    ['A','B'].forEach(cam => {
      const canvas = activeBox.querySelector(`[data-spark="${cam}"]`);
      const samples = ((liveSession.frame_samples || {})[cam]) || [];
      drawSparkline(canvas, samples);
    });
    const resetBtn = activeBox.querySelector('[data-reset-trail]');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        if (!currentLiveSession) return;
        livePointStore.set(currentLiveSession.session_id, []);
        currentLiveSession.point_count = 0;
        currentLiveSession.point_depths = [];
        currentLiveSession.last_point_at_ms = null;
        liveTraceIdx = -1;
        renderActiveSession(currentLiveSession);
        repaintCanvas();
      });
    }
  }

  function renderDetectionPaths(session) {
    const armed = !!(session && session.armed);
    const active = new Set(armed ? (session.paths || currentDefaultPaths || []) : (currentDefaultPaths || []));
    if (armed) {
      const chips = [...active].map(path =>
        `<span class="path-chip on">${esc((PATH_LABELS[path] || [path])[0])}</span>`
      ).join('') || `<span class="path-chip">none</span>`;
      return `<div class="path-lock"><span class="mode-label">Paths</span><div class="path-chip-row">${chips}</div></div>`;
    }
    const options = ['live', 'ios_post', 'server_post'].map(path => {
      const [title, sub] = PATH_LABELS[path] || [path, ''];
      return `<label class="path-option">
          <input type="checkbox" name="paths" value="${path}" ${active.has(path) ? 'checked' : ''}>
          <span class="copy">
            <span class="title">${esc(title)}</span>
            <span class="sub">${esc(sub)}</span>
          </span>
        </label>`;
    }).join('');
    return `<form method="POST" action="/detection/paths" id="paths-form">
      <div class="paths-stack">${options}</div>
      <div class="paths-actions"><button class="btn" type="submit">Apply</button></div>
    </form>`;
  }

  // Per-cam sync indicator shown next to Quick chirp. States:
  //   off     → device not in registry (no heartbeat recently).
  //   waiting → device online but no valid time-sync anchor yet.
  //   synced  → cam is holding an anchor from a recent successful sync.
  // Reads directly off `state.devices[*].time_synced` since the server
  // owns that truth. `time_sync_age_s` tooltip so operator can tell how
  // fresh "synced" is.
  function renderSyncLed(state, cam) {
    const devs = (state && state.devices) || [];
    const dev = devs.find(d => d.camera_id === cam);
    let cls = 'off';
    let tip = cam + ': offline';
    if (dev) {
      if (dev.time_synced) {
        cls = 'synced';
        const age = (typeof dev.time_sync_age_s === 'number')
          ? ' · ' + dev.time_sync_age_s.toFixed(0) + 's ago' : '';
        tip = cam + ': synced' + age;
      } else {
        cls = 'waiting';
        tip = cam + ': waiting';
      }
    }
    return `<span class="sync-led ${cls}" title="${esc(tip)}">${esc(cam)}</span>`;
  }

  function renderSession(state) {
    if (!sessionBox) { /* nav-only render still executes below */ }
    const s = state.session;
    const armed = !!(s && s.armed);
    currentDefaultPaths = state.default_paths || currentDefaultPaths || ['server_post'];
    currentLiveSession = state.live_session || currentLiveSession;
    const chip = armed ? `<span class="chip armed">armed</span>` : `<span class="chip idle">idle</span>`;
    const sid = s && s.id ? `<span class="session-id">${esc(s.id)}</span>` : '';
    const clearBtn = (!armed && s && s.id)
      ? `<form class="inline" method="POST" action="/sessions/clear">
           <button class="btn" type="submit">Clear</button>
         </form>`
      : '';
    const sessHtml = `
      <div class="session-head">${chip}${sid}</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm">
          <button class="btn" type="submit" ${armed ? 'disabled' : ''}>Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" ${armed ? '' : 'disabled'}>Stop</button>
        </form>
        ${clearBtn}
      </div>
      <div class="card-subtitle">Time Sync</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sync/trigger">
          <button class="btn secondary" type="submit" ${armed ? 'disabled' : ''}>Quick chirp</button>
        </form>
        ${renderSyncLed(state, 'A')}
        ${renderSyncLed(state, 'B')}
      </div>
      ${renderDetectionPaths(s)}`;
    if (sessionBox) sessionBox.innerHTML = sessHtml;
    renderActiveSession(currentLiveSession);

    // Mirror live state into the shared app-header status strip.
    if (navStatus) {
      const online = (state.devices || []).length;
      const cal = (state.calibrations || []).length;
      const synced = (state.devices || []).filter(d => d && d.time_synced).length;
      const expected = 2;
      const cooldown = Number(state.sync_cooldown_remaining_s || 0);
      let badgeCls = 'ready';
      let badge = 'Ready';
      let headline = 'ready to arm';
      let context = 'all prerequisites satisfied';
      if (armed) {
        badgeCls = 'recording';
        badge = 'Recording';
        headline = esc(s.id || '—');
        context = 'session active';
      } else if (state.sync) {
        badgeCls = 'syncing';
        badge = 'Sync';
        headline = 'sync in progress';
        context = 'complete on /sync';
      } else if (online < expected) {
        badgeCls = 'blocked';
        badge = 'Blocked';
        headline = 'bring both devices online';
        context = `${online}/${expected} devices available`;
      } else if (cal < expected) {
        badgeCls = 'blocked';
        badge = 'Blocked';
        headline = 'finish calibration';
        context = `${cal}/${expected} cameras calibrated`;
      } else if (synced < expected) {
        badgeCls = 'blocked';
        badge = 'Blocked';
        headline = 'run time sync';
        context = `${synced}/${expected} cameras synced`;
      } else if (cooldown > 0) {
        badgeCls = 'cooldown';
        badge = 'Cooldown';
        headline = 'sync settling';
        context = `${cooldown.toFixed(0)}s remaining`;
      }
      const check = (label, value, ok) =>
        `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
      const navHtml = `
        <div class="status-main">
          <span class="status-badge ${badgeCls}">${badge}</span>
          <span class="status-headline">${headline}</span>
          <span class="status-context">${context}</span>
        </div>
        <div class="status-checks">
          ${check('Devices', `${online}/${expected}`, online >= expected)}
          ${check('Cal', `${cal}/${expected}`, cal >= expected)}
          ${check('Sync', `${synced}/${expected}`, synced >= expected)}
        </div>`;
      navStatus.innerHTML = navHtml;
    }
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(digits);
  }

  // Full time-sync controls live on /sync. The dashboard only mirrors
  // current sync state in the shared header.

  function renderEvents(events) {
    if (!eventsBox) return;
    let evHtml;
    if (!events || events.length === 0) {
      eventsBox.innerHTML = `<div class="events-empty">No sessions received yet.</div>`;
      return;
    }
    evHtml = events.map(e => {
      const sid = esc(e.session_id);
      const stat = (e.status || '').replace(/_/g, ' ');
      const speedKmh = e.speed_mps != null ? (e.speed_mps * 3.6).toFixed(1) : null;
      const duration = fmtNum(e.fit_duration_s != null ? e.fit_duration_s : e.duration_s, 2);
      const rms = fmtNum(e.rms_m, 3);
      const plateX = e.plate_xz_m ? e.plate_xz_m[0].toFixed(2) : null;
      const plateZ = e.plate_xz_m ? e.plate_xz_m[1].toFixed(2) : null;
      const pathStatus = e.path_status || {};
      const pathChips = [['live', 'L'], ['ios_post', 'I'], ['server_post', 'S']]
        .map(([path, label]) => `<span class="path-chip${pathStatus[path] === 'done' ? ' on' : ''}">${label}</span>`)
        .join('');
      // Quality chip from fit RMS: <10mm excellent, <30mm good, <80mm fair, else poor.
      // Sessions without a fit get a neutral `no-fit` chip — they still list
      // (the operator may want to forensic them) but signal loudly.
      let qualityClass = 'no-fit', qualityLabel = 'no fit';
      if (e.rms_m != null) {
        if (e.rms_m < 0.010)      { qualityClass = 'excellent'; qualityLabel = 'excellent'; }
        else if (e.rms_m < 0.030) { qualityClass = 'good';      qualityLabel = 'good'; }
        else if (e.rms_m < 0.080) { qualityClass = 'fair';      qualityLabel = 'fair'; }
        else                      { qualityClass = 'poor';      qualityLabel = 'poor'; }
      }
      const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
      const trashMsg = `移動 session ${e.session_id} 到垃圾桶？`;
      // Trajectory overlay toggle: only sessions with on-device points qualify.
      // Mode-one (camera_only) sessions are intentionally not overlayable on
      // the LIVE dashboard — use the forensic viewer for those.
      const hasTraj = (e.n_triangulated_on_device || 0) > 0;
      const color = hasTraj ? trajColorFor(e.session_id) : '';
      const checked = selectedTrajIds.has(e.session_id) ? 'checked' : '';
      const toggle = hasTraj
        ? `<label class="traj-toggle" title="Overlay trajectory on canvas">
             <input type="checkbox" data-traj-sid="${sid}" ${checked}>
             <span class="swatch" style="background:${color}"></span>
           </label>`
        : `<span class="traj-toggle-placeholder" aria-hidden="true"></span>`;
      const metricsRow = e.has_fit ? `
          <div class="event-stats">
            ${speedKmh != null ? `<span><span class="k">Speed</span><span class="v">${speedKmh} km/h</span></span>` : ''}
            ${plateX != null ? `<span><span class="k">Plate (x,z)</span><span class="v">${plateX}, ${plateZ} m</span></span>` : ''}
            <span><span class="k">Dur</span><span class="v">${duration} s</span></span>
            <span><span class="k">RMS</span><span class="v">${rms} m</span></span>
          </div>` : '';
      const processingState = e.processing_state ? `<span class="chip ${esc(e.processing_state)}">${esc(e.processing_state)}</span>` : '';
      const processingAction = e.processing_state === 'queued' || e.processing_state === 'processing'
        ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/cancel_processing">
             <button class="event-action warn" type="submit">Cancel Proc</button>
           </form>`
        : (e.processing_state === 'canceled' && e.processing_resumable)
          ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/resume_processing">
               <button class="event-action ok" type="submit">Resume</button>
             </form>`
          : '';
      const lifecycleAction = currentEventsBucket === 'trash'
        ? `
            <form class="event-action-form" method="POST" action="/sessions/${sid}/restore">
              <button class="event-action ok" type="submit">Restore</button>
            </form>
            <form class="event-action-form" method="POST"
                  action="/sessions/${sid}/delete"
                  onsubmit="return confirm(${JSON.stringify(confirmMsg)});">
              <button class="event-action dev" type="submit">Delete</button>
            </form>`
        : `
            <form class="event-action-form" method="POST"
                  action="/sessions/${sid}/trash"
                  onsubmit="return confirm(${JSON.stringify(trashMsg)});">
              <button class="event-action dev" type="submit">Trash</button>
            </form>`;
      return `
        <div class="event-item">
          ${toggle}
          <a class="event-row" href="/viewer/${sid}">
            <div class="event-top">
              <span class="sid">${sid}</span>
              <span class="event-paths">${pathChips}</span>
              <span class="quality chip ${qualityClass}" title="fit RMS quality">${qualityLabel}</span>
              ${processingState}
              <span class="chip ${esc(e.status || '')}">${esc(stat)}</span>
            </div>
            ${metricsRow}
          </a>
          <div class="event-actions">
            ${processingAction}
            ${lifecycleAction}
          </div>
        </div>`;
    }).join('');
    eventsBox.innerHTML = evHtml;
  }

  let currentDevices = null;
  let currentSession = null;
  let currentCalibrations = null;
  let currentCaptureMode = 'camera_only';
  let currentPreviewRequested = {};
  let currentSyncCommands = {};
  let currentCalibrationLastTs = {};
  let currentAutoCalibration = { active: {}, last: {} };
  let currentEventsBucket = 'active';
  const pendingPreviewMutations = new Set();

  // Keys used to skip re-renders when nothing changed. We compare serialised
  // state data rather than innerHTML strings because the browser re-serialises
  // HTML differently from the raw template literals we build.
  let _lastDevKey = null;
  let _lastSessKey = null;
  let _lastNavKey = null;
  let _lastEvKey = null;

  const _origRenderDevices = renderDevices;
  renderDevices = function(state) {
    const key = JSON.stringify({
      devices: (state.devices || []).map(d => ({
        id: d.camera_id,
        ts: d.time_synced,
        seen: d.last_seen_at,
        ws: d.ws_connected,
      })),
      calibrations: (state.calibrations || []).slice().sort(),
      preview: state.preview_requested || {},
      preview_pending: [...(state.preview_pending || [])].sort(),
      last_ts: state.calibration_last_ts || {},
      sync_pending: Object.keys(state.sync_commands || {}).sort(),
      auto_calibration: state.auto_calibration || { active: {}, last: {} },
    });
    if (key === _lastDevKey) return;
    _lastDevKey = key;
    _origRenderDevices(state);
  };

  const _origRenderSession = renderSession;
  renderSession = function(state) {
    const s = state.session;
    const sessKey = JSON.stringify({
      armed: !!(s && s.armed), id: s && s.id, mode: s && s.mode,
      capture_mode: state.capture_mode,
      paths: state.default_paths || [],
      live_session: state.live_session || null,
    });
    const cooldownBucket = Number(state.sync_cooldown_remaining_s || 0) > 0 ? 1 : 0;
    const navKey = JSON.stringify({
      online: (state.devices || []).length,
      cal: (state.calibrations || []).length,
      armed: !!(s && s.armed), id: s && s.id,
      syncing: !!state.sync, cooling: cooldownBucket,
    });
    if (sessKey === _lastSessKey && navKey === _lastNavKey) return;
    _lastSessKey = sessKey;
    _lastNavKey = navKey;
    _origRenderSession(state);
  };

  const _origRenderEvents = renderEvents;
  renderEvents = function(events) {
    const key = JSON.stringify((events || []).map(e => ({
      id: e.session_id, status: e.status, n: e.n_triangulated, p: e.processing_state,
    })));
    if (key === _lastEvKey) return;
    _lastEvKey = key;
    _origRenderEvents(events);
  };

  async function tickStatus() {
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (!r.ok) return;
      const s = await r.json();
      // /status does not include calibrations; merge the last-known set so
      // the devices card shows "calibrated" chips between calibration ticks.
      s.calibrations = currentCalibrations || [];
      currentDevices = s.devices || [];
      currentSession = s.session || null;
      currentCaptureMode = s.capture_mode || 'camera_only';
      currentPreviewRequested = s.preview_requested || {};
      currentSyncCommands = s.sync_commands || {};
      currentAutoCalibration = s.auto_calibration || { active: {}, last: {} };
      renderDevices({
        devices: s.devices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs || {},
        auto_calibration: currentAutoCalibration,
      });
      renderSession(s);
      // Telemetry: record per-cam WS latency sampled from /status.
      // Server-side ws_latency_ms reflects the last heartbeat round-trip
      // per the DeviceSocketManager snapshot.
      const nowMs = Date.now();
      for (const dev of (s.devices || [])) {
        if (!dev || !dev.camera_id) continue;
        const lat = dev.ws_latency_ms;
        if (typeof lat !== 'number') continue;
        const arr = latencySamples[dev.camera_id] = latencySamples[dev.camera_id] || [];
        arr.push({ t_ms: nowMs, latency: lat });
        while (arr.length && nowMs - arr[0].t_ms > TELEMETRY_WINDOW_MS) arr.shift();
      }
    } catch (e) { /* silent retry next tick */ }
  }

  // Digest of the last basePlot we actually repainted from. Calibrations
  // rarely change between 5 s ticks; skipping the Plotly.react call when
  // the payload is identical (same cameras, same poses) eliminates the
  // most-frequent opportunity for an accidental camera snap-back and
  // avoids ~ms of churn per tick.
  let lastBasePlotDigest = null;
  async function tickCalibration() {
    try {
      const r = await fetch('/calibration/state', { cache: 'no-store' });
      if (!r.ok) return;
      const payload = await r.json();
      currentCalibrations = (payload.calibrations || []).map(c => c.camera_id);
      currentCalibrationLastTs = {};
      for (const c of (payload.calibrations || [])) {
        if (c.last_ts != null) currentCalibrationLastTs[c.camera_id] = c.last_ts;
      }
      renderDevices({
        devices: currentDevices || [],
        calibrations: currentCalibrations,
        preview_requested: currentPreviewRequested,
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs,
        auto_calibration: currentAutoCalibration,
      });
      renderSession({ devices: currentDevices || [], session: currentSession, calibrations: currentCalibrations, capture_mode: currentCaptureMode });
      // Update per-camera virt reprojection metadata from scene.cameras
      // (carries fx/fy/cx/cy/R_wc/t_wc/distortion/dims).
      virtCamMeta.clear();
      for (const c of ((payload.scene || {}).cameras || [])) {
        virtCamMeta.set(c.camera_id, c);
      }
      redrawAllVirtCanvases();
      redrawAllPreviewPlateOverlays();
      // Main 3D canvas lives only on `/`. Don't gate the metadata update
      // above on sceneRoot — `/setup` still needs virt canvases drawn.
      if (payload.plot && sceneRoot && window.Plotly) {
        const digest = JSON.stringify(payload.plot);
        if (digest !== lastBasePlotDigest || basePlot === null) {
          lastBasePlotDigest = digest;
          basePlot = payload.plot;
          repaintCanvas();
        }
      }
    } catch (e) { /* silent */ }
  }

  let currentEvents = [];
  async function tickEvents() {
    try {
      const r = await fetch(`/events?bucket=${encodeURIComponent(currentEventsBucket)}`, { cache: 'no-store' });
      if (!r.ok) return;
      const events = await r.json();
      currentEvents = events;
      // Prune selection for sessions the user deleted server-side so the
      // canvas doesn't keep painting a phantom trajectory whose checkbox
      // no longer exists.
      const liveIds = new Set(events.map(e => e.session_id));
      let pruned = false;
      for (const sid of [...selectedTrajIds]) {
        if (!liveIds.has(sid)) {
          selectedTrajIds.delete(sid);
          trajCache.delete(sid);
          pruned = true;
        }
      }
      if (pruned) { persistTrajSelection(); repaintCanvas(); }
      renderEvents(events);
    } catch (e) { /* silent */ }
  }

  // Mode toggle: intercept form submit via fetch + optimistic update so
  // the button state never bounces back to the previous value between the
  // POST and the next tickStatus round-trip.
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (form.action && form.action.endsWith('/sessions/set_mode')) {
      e.preventDefault();
      const mode = (form.querySelector('input[name="mode"]') || {}).value;
      if (!mode) return;
      currentCaptureMode = mode;
      // Invalidate key so the next renderSession call repaints.
      _lastSessKey = null;
      renderSession({ devices: currentDevices || [], session: currentSession,
                      calibrations: currentCalibrations || [], capture_mode: currentCaptureMode });
      try { await fetch('/sessions/set_mode', { method: 'POST', body: new FormData(form) }); }
      catch (_) {}
      return;
    }
    if (form.action && /\/sessions\/[^/]+\/(trash|restore|delete|cancel_processing|resume_processing)$/.test(form.action)) {
      e.preventDefault();
      try {
        await fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'Accept': 'application/json' } });
      } catch (_) {}
      await tickEvents();
      return;
    }
    if (form.action && form.action.endsWith('/sync/trigger')) {
      // Quick chirp: dispatch the WS sync_command, then auto-play
      // /chirp.wav through this browser tab 500 ms later so the
      // operator doesn't have to fumble with a separate third device.
      // The Audio element MUST be constructed inside the gesture
      // (this submit handler) so Safari/Chrome count the later
      // setTimeout .play() as user-initiated.
      e.preventDefault();
      const btn = form.querySelector('button');
      if (btn) btn.disabled = true;
      const chirpAudio = new Audio('/chirp.wav');
      try {
        const resp = await fetch(form.action, {
          method: 'POST',
          headers: { 'Accept': 'application/json' },
        });
        if (resp.ok) {
          // 500 ms lets iOS receive the WS sync_command and spin up
          // the mic detector before the sweep starts; combined with
          // the WAV's 500 ms leading silence, there's ~1 s of slack
          // before the actual chirp sweep begins.
          setTimeout(() => {
            chirpAudio.play().catch(() => { /* autoplay blocked — silent */ });
          }, 500);
        }
      } catch (_) {}
      finally {
        // Re-enable shortly after; /status tick will reconcile real state.
        setTimeout(() => { if (btn) btn.disabled = false; }, 600);
      }
      return;
    }
    // (Mutual-sync kickoff lives on /sync now.)
  });

  // Live-preview toggle. Server is authoritative — click POSTs the
  // intent, the next /status tick reconciles. Previously we awaited
  // tickStatus inline which, under connection-pool saturation (preview
  // img poll + status poll + SSE), would hang the
  // finally block and leave the cam in pendingPreviewMutations — then
  // every subsequent click hit the `pendingPreviewMutations.has(cam)`
  // early-return and felt "stuck". Now: fire-and-forget. 4 s watchdog
  // guarantees pending clears even if the POST hangs.
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-preview-cam]');
    if (!btn) return;
    if (btn.disabled) return;
    const cam = btn.dataset.previewCam;
    if (!cam || pendingPreviewMutations.has(cam)) return;
    const enabled = btn.dataset.previewEnabled !== '1';
    pendingPreviewMutations.add(cam);
    // Optimistic: flip currentPreviewRequested immediately so the next
    // renderDevices paints the final state. /status tick will reconcile.
    if (enabled) currentPreviewRequested[cam] = true;
    else delete currentPreviewRequested[cam];
    _lastDevKey = null;
    if (currentDevices !== null || currentCalibrations !== null) {
      renderDevices({
        devices: currentDevices || [],
        calibrations: currentCalibrations || [],
        preview_requested: currentPreviewRequested,
        preview_pending: [...pendingPreviewMutations],
        sync_commands: currentSyncCommands,
        calibration_last_ts: currentCalibrationLastTs || {},
        auto_calibration: currentAutoCalibration,
      });
    }
    const clearPending = () => {
      pendingPreviewMutations.delete(cam);
      _lastDevKey = null;
      if (currentDevices !== null || currentCalibrations !== null) {
        renderDevices({
          devices: currentDevices || [],
          calibrations: currentCalibrations || [],
          preview_requested: currentPreviewRequested,
          preview_pending: [...pendingPreviewMutations],
          sync_commands: currentSyncCommands,
          calibration_last_ts: currentCalibrationLastTs || {},
          auto_calibration: currentAutoCalibration,
        });
      }
    };
    const watchdog = setTimeout(clearPending, 4000);
    fetch('/camera/' + encodeURIComponent(cam) + '/preview_request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
      .catch(() => {})
      .finally(() => {
        clearTimeout(watchdog);
        clearPending();
        tickStatus();
      });
  });

  // Preview is a simple server-owned flag: click flips it, server pushes
  // the new state to the phone over WS, WS drop flips it back to false.
  // No client-side keep-alive or TTL refresh — those created race
  // conditions where toggle-off was silently re-armed by a stale beat.

  // Preview image polling. MJPEG streaming via <img> is flaky across
  // browsers (Chrome silently aborts when the server's first multipart
  // boundary doesn't land within a short window), so we bump a
  // cache-busting query-string on every <img data-preview-img> every
  // 200 ms — ~5 fps preview, trivial to debug via the Network tab, and
  // each frame is a normal GET /camera/{id}/preview that returns a
  // single JPEG or 404.
  function tickPreviewImages() {
    const t = Date.now();
    for (const img of document.querySelectorAll('img[data-preview-img]')) {
      const cam = img.dataset.previewImg;
      if (!cam) continue;
      const panel = img.closest('.preview-panel');
      if (!panel || panel.classList.contains('off')) continue;
      img.src = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + t;
      img.style.opacity = 1;
    }
  }
  setInterval(tickPreviewImages, 200);

  // Per-camera mini 3D pose canvas — renders beside each preview panel.
  // Reuses `basePlot` (from /calibration/state) by keeping traces with
  // meta.camera_id == this cam PLUS shared world traces (no meta/camera_id).
  // Tiny Plotly react on each calibration tick; layout cached per host.
  // Per-camera 2D reprojection (K·[R|t]·P). Ported from the viewer's
  // drawVirtCanvas: project the home-plate pentagon through THIS camera's
  // own calibration so the dashed outline lands where the camera sees the
  // plate. If the reprojected outline doesn't sit on top of the plate in
  // the real preview above, calibration is off.
  {PLATE_WORLD_JS}
  // Populated by tickCalibration from /calibration/state `scene.cameras`.
  const virtCamMeta = new Map();
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
  {DRAW_PLATE_OVERLAY_JS}
  function drawVirtCanvas(canvas, cam) {
    return !!drawVirtualBase(canvas, cam);
  }
  function redrawAllVirtCanvases() {
    for (const canvas of document.querySelectorAll('[data-virt-canvas]')) {
      const cam = canvas.dataset.virtCanvas;
      const meta = virtCamMeta.get(cam);
      const cell = canvas.closest('.virt-cell');
      const ok = drawVirtCanvas(canvas, meta);
      if (cell) cell.classList.toggle('ready', ok);
    }
  }
  function redrawAllPreviewPlateOverlays() {
    for (const svg of document.querySelectorAll('[data-preview-overlay]')) {
      const cam = svg.dataset.previewOverlay;
      const meta = virtCamMeta.get(cam);
      redrawPlateOverlay(svg, meta);
    }
  }
  window.addEventListener('resize', () => {
    redrawAllVirtCanvases();
    redrawAllPreviewPlateOverlays();
  });

  // Prime both immediately, then stagger polling so the UI stays
  // current without hammering the server. Status carries arming state
  // --- CALIBRATION card (Phase 5) -------------------------------------
  // Click "Auto calibrate" → POST /calibration/auto/start/<cam>.
  // Optimistic:
  // button disables while in flight; toast on failure.
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-auto-cal]');
    if (!btn) return;
    if (btn.disabled) return;
    const cam = btn.dataset.autoCal;
    btn.disabled = true;
    const originalLabel = btn.textContent;
    btn.textContent = 'Starting…';
    try {
      const r = await fetch('/calibration/auto/start/' + encodeURIComponent(cam),
                            { method: 'POST' });
      if (!r.ok) {
        let msg = 'Calibration failed';
        try { const body = await r.json(); if (body.detail) msg = body.detail; } catch (_) {}
        alert(msg);
        return;
      }
      tickStatus();
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
    }
  });

  // Register extended markers from the picked camera.
  document.addEventListener('click', async (e) => {
    if (e.target && e.target.id === 'marker-register-btn') {
      const sel = document.getElementById('marker-register-cam');
      const cam = sel && sel.value;
      if (!cam) return;
      e.target.disabled = true;
      try {
        const r = await fetch('/calibration/markers/register/' + encodeURIComponent(cam),
                              { method: 'POST' });
        if (!r.ok) {
          let msg = 'Register failed';
          try { const body = await r.json(); if (body.detail) msg = body.detail; } catch (_) {}
          alert(msg);
        }
        tickExtendedMarkers();
      } finally {
        e.target.disabled = false;
      }
      return;
    }
    if (e.target && e.target.id === 'marker-clear-btn') {
      if (!confirm('Clear all extended markers?')) return;
      try {
        await fetch('/calibration/markers/clear', { method: 'POST',
          headers: { 'Content-Type': 'application/json' } });
      } catch (_) {}
      tickExtendedMarkers();
      return;
    }
    const remBtn = e.target.closest('[data-marker-remove]');
    if (remBtn) {
      const mid = remBtn.dataset.markerRemove;
      try {
        await fetch('/calibration/markers/' + encodeURIComponent(mid),
                    { method: 'DELETE' });
      } catch (_) {}
      tickExtendedMarkers();
    }
  });

  function renderExtendedMarkers(markers) {
    const listEl = document.getElementById('marker-list');
    if (!listEl) return;
    if (!markers || markers.length === 0) {
      listEl.innerHTML = '<div class="marker-list-empty">No extended markers registered.</div>';
      return;
    }
    const rows = markers.map(row => {
      const id = Number(row.id);
      const wx = Number(row.wx);
      const wy = Number(row.wy);
      const fmt = v => (v >= 0 ? '+' : '') + v.toFixed(3);
      return '<div class="marker-row">' +
             '<span class="mid">#' + id + '</span>' +
             '<span class="mxy">(' + fmt(wx) + ', ' + fmt(wy) + ') m</span>' +
             '<button type="button" data-marker-remove="' + id +
             '" title="Remove marker ' + id + '">&times;</button>' +
             '</div>';
    }).join('');
    listEl.innerHTML = '<div class="marker-list">' + rows + '</div>';
  }

  async function tickExtendedMarkers() {
    try {
      const r = await fetch('/calibration/markers', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderExtendedMarkers(body.markers || []);
    } catch (e) { /* silent */ }
  }

  function initLiveStream() {
    if (!window.EventSource) return;
    const es = new EventSource('/stream');
    es.addEventListener('session_armed', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        currentLiveSession = {
          session_id: data.sid,
          armed: true,
          paths: data.paths || [],
          frame_counts: {},
          frame_samples: { A: [], B: [] },
          frame_fps: {},
          point_count: 0,
          point_depths: [],
          paths_completed: [],
          armed_at_ms: Date.now(),
        };
        livePointStore.set(data.sid, []);
        liveTraceIdx = -1;
        // Ghost trail is deliberately preserved across arm — it'll stay
        // rendered until a real point for the new session lands, at which
        // point liveTraces() stops emitting it (the new session trace
        // takes over visually). lastEndedLiveSid is not cleared here so
        // the operator can still see framing drift even on the first
        // moments of the new cycle.
        renderActiveSession(currentLiveSession);
        repaintCanvas();
        playCue('armed');
      } catch (_) {}
    });
    es.addEventListener('frame_count', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!currentLiveSession || currentLiveSession.session_id !== data.sid) return;
        currentLiveSession.frame_counts = currentLiveSession.frame_counts || {};
        currentLiveSession.frame_counts[data.cam] = Number(data.count || 0);
        pushFrameSample(currentLiveSession, data.cam, Number(data.count || 0));
        renderActiveSession(currentLiveSession);
      } catch (_) {}
    });
    es.addEventListener('path_completed', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!currentLiveSession || currentLiveSession.session_id !== data.sid) return;
        const done = new Set(currentLiveSession.paths_completed || []);
        done.add(data.path);
        currentLiveSession.paths_completed = [...done];
        renderActiveSession(currentLiveSession);
      } catch (_) {}
    });
    es.addEventListener('calibration_changed', () => {
      // Skip the 5s polling tick — repaint canvas immediately so the new
      // pose lands on screen. tickCalibration() still runs on schedule as
      // a safety net if the SSE event arrives before the dashboard has
      // its first paint done.
      if (typeof tickCalibration === 'function') tickCalibration();
    });
    es.addEventListener('point', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const sid = data.sid;
        const pt = {
          x: Number(data.x),
          y: Number(data.y),
          z: Number(data.z),
          t_rel_s: Number(data.t_rel_s || 0),
        };
        const arr = livePointStore.get(sid) || [];
        arr.push(pt);
        livePointStore.set(sid, arr);
        if (currentLiveSession && currentLiveSession.session_id === sid) {
          currentLiveSession.point_count = arr.length;
          currentLiveSession.last_point_at_ms = Date.now();
          if (!currentLiveSession.point_depths) currentLiveSession.point_depths = [];
          currentLiveSession.point_depths.push(pt.z);
          if (currentLiveSession.point_depths.length > 20) {
            currentLiveSession.point_depths.shift();
          }
          renderActiveSession(currentLiveSession);
          // Fast path: append to the already-anchored live trace slot.
          // Falls back to a full repaint if the slot is stale (e.g. first
          // point after an arm, or after a structural change invalidated
          // the cached index).
          if (!extendLivePoint(pt)) repaintCanvas();
        } else {
          repaintCanvas();
        }
        // Telemetry: each `point` SSE arrival is one triangulated pair.
        // Drop samples older than the window so the rolling stats stay
        // bounded regardless of session count or length.
        const nowMs = Date.now();
        pairTimestamps.push(nowMs);
        while (pairTimestamps.length && nowMs - pairTimestamps[0] > TELEMETRY_WINDOW_MS) {
          pairTimestamps.shift();
        }
      } catch (_) {}
    });
    es.addEventListener('session_ended', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (currentLiveSession && currentLiveSession.session_id === data.sid) {
          currentLiveSession.armed = false;
          currentLiveSession.ended_at_ms = Date.now();
          if (Array.isArray(data.paths_completed)) {
            currentLiveSession.paths_completed = data.paths_completed;
          }
          renderActiveSession(currentLiveSession);
          // Retain the trail reference for ghost preview on the next arm.
          // Clear currentLiveSession after a short delay so the active card
          // stays visible briefly with its final counters.
          lastEndedLiveSid = data.sid;
          setTimeout(() => {
            if (currentLiveSession && currentLiveSession.session_id === data.sid && !currentLiveSession.armed) {
              currentLiveSession = null;
              liveTraceIdx = -1;
              renderActiveSession(null);
              repaintCanvas();
            }
          }, 3000);
          playCue('ended');
        }
      } catch (_) {}
    });
    es.addEventListener('device_status', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (!data || !data.cam) return;
        const prev = wsStatus.get(data.cam);
        const connected = !!data.ws_connected;
        if (!prev || prev.connected !== connected) {
          wsStatus.set(data.cam, { connected, since_ms: Date.now() });
          if (!connected) recordError('ws_disconnect', `Cam ${data.cam} WebSocket dropped`);
          // Device came online or went offline — refresh the Devices panel
          // immediately rather than waiting for the 1 s tickStatus cadence.
          _lastDevKey = null;
          tickStatus();
        }
        updateDegradedBanner();
      } catch (_) {}
    });
  }

  // ------ Audio cues (opt-in via localStorage toggle) --------------------
  let audioCtx = null;
  function audioEnabled() {
    try { return localStorage.getItem('ball_tracker_audio_cues') === '1'; } catch { return false; }
  }
  function playCue(kind) {
    if (!audioEnabled()) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const freq = kind === 'armed' ? 220 : kind === 'ended' ? 440 : 150;
      const durS = kind === 'degraded' ? 0.2 : 0.08;
      osc.frequency.value = freq;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0.12, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + durS);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + durS);
    } catch (_) {}
  }

  // ------ Degraded banner: WS lost > grace window on an armed cam ---------
  let lastDegradedState = false;
  function updateDegradedBanner() {
    const banner = document.getElementById('degraded-banner');
    if (!banner) return;
    const now = Date.now();
    const armed = currentLiveSession && currentLiveSession.armed;
    const stale = [];
    for (const [cam, st] of wsStatus) {
      if (!st.connected && now - st.since_ms > WS_GRACE_MS) stale.push(cam);
    }
    const degraded = armed && stale.length > 0;
    if (degraded) {
      banner.style.display = 'flex';
      banner.querySelector('[data-degraded-body]').textContent =
        `Cam ${stale.join(', ')} WebSocket lost — falling back to post-pass. Next session will be 2-8s latency.`;
    } else {
      banner.style.display = 'none';
    }
    if (degraded && !lastDegradedState) playCue('degraded');
    lastDegradedState = degraded;
  }

  // ------ Telemetry panel -------------------------------------------------
  // Collapsible debug overlay bottom-left of canvas. Operator rarely looks
  // at it — it's a diagnostic when "feels slow" needs an evidence trail.
  // All metrics are derived client-side from existing SSE + /status signals;
  // no new server endpoints required.
  function percentile(arr, p) {
    if (!arr.length) return null;
    const sorted = [...arr].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(sorted.length * p)));
    return sorted[idx];
  }

  function drawTelemetrySpark(canvas, values, maxVal) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!values || values.length < 2) return;
    const maxY = maxVal !== undefined ? maxVal : Math.max(1, ...values);
    ctx.strokeStyle = '#4A6B8C';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - (Math.max(0, v) / maxY) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function sessionPathMatrix() {
    // Derived from the events list (most recent 10). Each cell shows
    // whether a given path completed for that session.
    const cells = [];
    const list = (currentEvents || []).slice(0, 10);
    for (const ev of list) {
      const paths = new Set(ev.paths_completed || []);
      cells.push({
        sid: ev.session_id,
        live: paths.has('live'),
        ios: paths.has('ios_post'),
        srv: paths.has('server_post'),
      });
    }
    return cells;
  }

  function renderTelemetry() {
    const box = document.getElementById('telemetry-body');
    if (!box) return;
    // Per-cam fps sparkline — reuse frame_samples on currentLiveSession
    const camRow = (cam) => {
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      const avg = fps.length ? fps.reduce((a,b)=>a+b,0) / fps.length : 0;
      const min = fps.length ? Math.min(...fps) : 0;
      return `
        <div class="tel-row">
          <span class="k">${cam} fps</span>
          <canvas class="tel-spark" data-tel-spark="${cam}"></canvas>
          <span class="v">avg ${avg.toFixed(0)} · min ${min.toFixed(0)}</span>
        </div>`;
    };
    // Pair rate: trailing-window count of pair timestamps over 1s
    const nowMs = Date.now();
    const pairsLast1s = pairTimestamps.filter(t => nowMs - t <= 1000).length;
    const pairsAvg = pairTimestamps.length / Math.max(1, TELEMETRY_WINDOW_MS / 1000);
    // Latency stats aggregated across cams
    const allLat = [];
    for (const cam of ['A','B']) {
      for (const s of (latencySamples[cam] || [])) allLat.push(s.latency);
    }
    const p50 = percentile(allLat, 0.50);
    const p95 = percentile(allLat, 0.95);
    const maxLat = allLat.length ? Math.max(...allLat) : null;
    const latTxt = p50 === null
      ? '—'
      : `p50 ${p50.toFixed(0)}ms · p95 ${p95.toFixed(0)}ms · max ${maxLat.toFixed(0)}ms`;
    // Path completion matrix
    const matrix = sessionPathMatrix();
    const matrixHtml = matrix.length
      ? matrix.map(c => `<span class="tel-cell" title="${esc(c.sid)}">${c.live?'L':'·'}${c.ios?'i':'·'}${c.srv?'s':'·'}</span>`).join('')
      : '<span class="tel-none">no sessions yet</span>';
    // Errors
    const errHtml = errorLog.length
      ? errorLog.map(e => {
          const ts = new Date(e.t_ms).toLocaleTimeString();
          return `<div class="tel-err"><span class="t">${ts}</span> <span class="msg">${esc(e.message)}</span></div>`;
        }).join('')
      : '<span class="tel-none">none</span>';
    box.innerHTML = `
      ${camRow('A')}
      ${camRow('B')}
      <div class="tel-row">
        <span class="k">Pairs</span>
        <span class="v">${pairsLast1s}/s · avg ${pairsAvg.toFixed(1)}/s</span>
      </div>
      <div class="tel-row">
        <span class="k">WS latency</span>
        <span class="v">${latTxt}</span>
      </div>
      <div class="tel-block">
        <span class="k">Last 10 sessions (L/i/s)</span>
        <div class="tel-matrix">${matrixHtml}</div>
      </div>
      <div class="tel-block">
        <span class="k">Errors</span>
        <div class="tel-errors">${errHtml}</div>
      </div>`;
    // Draw sparklines after DOM replacement
    ['A','B'].forEach(cam => {
      const canvas = box.querySelector(`[data-tel-spark="${cam}"]`);
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      drawTelemetrySpark(canvas, fps, 240);
    });
  }

  // 10 Hz tick for the time-sensitive active-session fields (elapsed
  // counter + last-point-age). Cheaper than re-rendering the whole card
  // on every SSE event, and ensures the "stale" flag trips within 100 ms
  // of the 200 ms threshold being crossed.
  function tickActiveSession() {
    if (!currentLiveSession || !currentLiveSession.armed) return;
    const elapsedEl = activeBox && activeBox.querySelector('[data-elapsed]');
    if (elapsedEl && currentLiveSession.armed_at_ms) {
      elapsedEl.textContent = fmtElapsed(Date.now() - currentLiveSession.armed_at_ms);
    }
    // Re-evaluate stale flag without a full re-render
    const pairsEl = activeBox && activeBox.querySelector('.live-pairs');
    if (pairsEl && currentLiveSession.last_point_at_ms) {
      const age = Date.now() - currentLiveSession.last_point_at_ms;
      pairsEl.classList.toggle('stale', age > 200);
    }
  }

  // (1 s) and is the only high-frequency tick.
  initLiveStream();
  tickStatus();
  tickCalibration();
  tickEvents();
  tickExtendedMarkers();
  setInterval(tickStatus, 1000);
  setInterval(tickCalibration, 5000);
  setInterval(tickEvents, 5000);
  setInterval(tickExtendedMarkers, 5000);
  setInterval(tickActiveSession, 100);
  // Re-check the degraded banner without waiting for a new device_status
  // event — the grace window ticks forward even when no events arrive,
  // so the banner needs its own cadence to flip on at the right moment.
  setInterval(updateDegradedBanner, 1000);
  // Telemetry panel re-renders at 1Hz when open; closed <details> gets
  // display:none for its body so the innerHTML rewrite is a no-op visually.
  setInterval(() => {
    const panel = document.getElementById('telemetry-panel');
    if (panel && panel.open) renderTelemetry();
  }, 1000);

  // ------ Keyboard shortcuts --------------------------------------------
  // Deliberately NOT including Space for Arm/Stop — operator typically
  // has a ball in-hand when near the phone and accidentally hitting
  // Space on a tablet keyboard while moving is a real footgun. Space
  // stays bound to replay play/pause (existing behavior).
  document.addEventListener('keydown', (e) => {
    // Ignore when user is typing in an input / textarea
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'r' || e.key === 'R') {
      const btn = activeBox && activeBox.querySelector('[data-reset-trail]');
      if (btn) { e.preventDefault(); btn.click(); }
    } else if (e.key === 'c' || e.key === 'C') {
      // Scroll devices sidebar card into view — closest we have to
      // "open calibration panel" since auto-cal is per-device inline.
      const devices = document.getElementById('devices-body');
      if (devices) { e.preventDefault(); devices.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    } else if (e.key === 'm' || e.key === 'M') {
      // Toggle audio cues. Shown in the nav strip when enabled.
      try {
        const cur = localStorage.getItem('ball_tracker_audio_cues') === '1';
        localStorage.setItem('ball_tracker_audio_cues', cur ? '0' : '1');
      } catch (_) {}
    }
  });
})();
"""


_JS_TEMPLATE = _resolve_js_template()


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
