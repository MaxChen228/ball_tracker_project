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
  // server's default_paths now always contains just "live" (server_post
  // is triggered post-hoc per session). Kept as a fallback for the rare
  // bootstrap before /status returns.
  let currentDefaultPaths = ['live'];
  let currentLiveSession = null;
  const livePointStore = new Map();   // sid -> [{x,y,z,t_rel_s}]
  const liveRayStore = new Map();     // sid -> Map(cam -> [{origin,endpoint,t_rel_s,frame_index}])
  let lastEndedLiveSid = null;        // For ghost-preview on the next arm
  let liveRayPaintPending = false;
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

  function initHSVControls() {
    const form = document.getElementById('hsv-form');
    if (!form) return;
    const syncField = (key, value) => {
      const range = form.querySelector(`[data-hsv-range="${key}"]`);
      const number = form.querySelector(`[data-hsv-number="${key}"]`);
      if (range) range.value = String(value);
      if (number) number.value = String(value);
    };
    form.querySelectorAll('[data-hsv-range]').forEach((input) => {
      input.addEventListener('input', () => syncField(input.dataset.hsvRange, input.value));
    });
    form.querySelectorAll('[data-hsv-number]').forEach((input) => {
      input.addEventListener('input', () => syncField(input.dataset.hsvNumber, input.value));
    });
    form.querySelectorAll('[data-hsv-preset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        syncField('h_min', btn.dataset.hMin);
        syncField('h_max', btn.dataset.hMax);
        syncField('s_min', btn.dataset.sMin);
        syncField('s_max', btn.dataset.sMax);
        syncField('v_min', btn.dataset.vMin);
        syncField('v_max', btn.dataset.vMax);
      });
    });
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
  const trajCache = new Map();       // sid -> {points}
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
      // Read paths INDEPENDENTLY — `data.points` has a server-side fallback
      // (server_post → live) baked in which silently swaps sources. Use
      // `triangulated_by_path` so live and server_post stay strictly siblings.
      const byPath = data.triangulated_by_path || {};
      const sortByT = arr => (arr || []).slice().sort((a, b) => a.t_rel_s - b.t_rel_s);
      const entry = {
        pointsByPath: {
          live: sortByT(byPath.live),
          server_post: sortByT(byPath.server_post),
        },
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  function trajectoryBounds(points) {
    if (!points || points.length < 2) return null;
    return { t0: points[0].t_rel_s, t1: points[points.length - 1].t_rel_s };
  }

  function sampleTrajectory(points, t) {
    if (!points || !points.length) return null;
    if (points.length === 1) return points[0];
    if (t <= points[0].t_rel_s) return points[0];
    if (t >= points[points.length - 1].t_rel_s) return points[points.length - 1];
    for (let i = 1; i < points.length; ++i) {
      const a = points[i - 1];
      const b = points[i];
      if (t > b.t_rel_s) continue;
      const span = Math.max(1e-6, b.t_rel_s - a.t_rel_s);
      const alpha = (t - a.t_rel_s) / span;
      return {
        x_m: a.x_m + (b.x_m - a.x_m) * alpha,
        y_m: a.y_m + (b.y_m - a.y_m) * alpha,
        z_m: a.z_m + (b.z_m - a.z_m) * alpha,
        t_rel_s: t,
      };
    }
    return points[points.length - 1];
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

  function activeReplayDuration() {
    const sid = activeReplaySid();
    if (!sid) return 0;
    const r = trajCache.get(sid);
    const bounds = r ? trajectoryBounds(pointsForEntry(r)) : null;
    if (!bounds) return 0;
    return bounds.t1 - bounds.t0;
  }

  function updateTimeReadout() {
    if (!timeReadout || !scrubSlider) return;
    const dur = activeReplayDuration();
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

    // Fit filter state — mirrors the viewer sliders. Persists per-browser.
  const DASH_RES_KEY = 'ball_tracker_dash_residual_cm';
  const DASH_OUT_KEY = 'ball_tracker_dash_outlier_kappa';
  const DASH_SRC_KEY = 'ball_tracker_dash_fit_source';
  let dashResidualCapM = 0.20;     // default 20 cm (matches viewer)
  let dashOutlierKappa = 3.0;      // default κ = 3.0
  // live and server_post are strict siblings, no priority. Default
  // server_post because it's typically more accurate; user picks
  // explicitly via the toggle. NEVER fall back across sources.
  let dashFitSource = 'server_post';
  try {
    const s = localStorage.getItem(DASH_SRC_KEY);
    if (s === 'live' || s === 'server_post') dashFitSource = s;
  } catch {}
  // Single chokepoint for source resolution. If the chosen source has no
  // points, return [] — do NOT silently switch. Caller renders nothing.
  function pointsForEntry(entry) {
    if (!entry || !entry.pointsByPath) return [];
    return entry.pointsByPath[dashFitSource] || [];
  }
  try {
    const r = parseFloat(localStorage.getItem(DASH_RES_KEY));
    if (Number.isFinite(r) && r >= 0 && r <= 200) dashResidualCapM = r / 100;
    else if (localStorage.getItem(DASH_RES_KEY) === 'off') dashResidualCapM = Infinity;
  } catch {}
  try {
    const k = parseFloat(localStorage.getItem(DASH_OUT_KEY));
    if (Number.isFinite(k) && k >= 1.0 && k < 6.0) dashOutlierKappa = k;
    else if (localStorage.getItem(DASH_OUT_KEY) === 'off') dashOutlierKappa = Infinity;
  } catch {}

  // Spatial-isolation outlier filter — mean distance from each point to
  // its 3 nearest 3D neighbours, reject those > median + κ·1.4826·MAD.
  // Same logic as viewer's `applyFitResidualFilter`. Robust to LSQ
  // leverage (one bad point can't masquerade as inlier because we don't
  // fit first). Pure on `{x_m, y_m, z_m}`; returns the same shape.
  function spatialIsolationFilterDash(pts, kappa) {
    if (kappa === undefined) kappa = dashOutlierKappa;
    if (!Number.isFinite(kappa) || !pts || pts.length < 5) return pts;
    const K_NN = 3;
    const isol = pts.map((p, i) => {
      const ds = [];
      for (let j = 0; j < pts.length; j++) {
        if (j === i) continue;
        const dx = pts[j].x_m - p.x_m, dy = pts[j].y_m - p.y_m, dz = pts[j].z_m - p.z_m;
        ds.push(Math.sqrt(dx*dx + dy*dy + dz*dz));
      }
      ds.sort((a, b) => a - b);
      const k = Math.min(K_NN, ds.length);
      let s = 0; for (let m = 0; m < k; m++) s += ds[m];
      return s / k;
    });
    const sorted = isol.slice().sort((a, b) => a - b);
    const med = sorted[Math.floor(sorted.length / 2)];
    const absDev = isol.map(d => Math.abs(d - med)).sort((a, b) => a - b);
    const mad = Math.max(absDev[Math.floor(absDev.length / 2)], 1e-3);
    const cutoff = med + kappa * 1.4826 * mad;
    const survivors = pts.filter((_, i) => isol[i] <= cutoff);
    return survivors.length >= 4 && survivors.length < pts.length ? survivors : pts;
  }

  // Per-axis ballistic LSQ with gravity pinned. Mirrors viewer's
  // `ballisticFit` but reads `{x_m, y_m, z_m, t_rel_s}` and returns
  // `evaluate(t) -> {x_m, y_m, z_m}`.
  function ballisticFitDash(pts) {
    if (!pts || pts.length < 4) return null;
    const G = 9.81;
    const t0 = pts[0].t_rel_s;
    function fitAxis(getVal, accelTerm) {
      let sumT = 0, sumTT = 0, sumP = 0, sumTP = 0;
      const n = pts.length;
      for (const p of pts) {
        const tau = p.t_rel_s - t0;
        const v = getVal(p) - accelTerm * tau * tau;
        sumT += tau; sumTT += tau*tau; sumP += v; sumTP += tau*v;
      }
      const det = n * sumTT - sumT * sumT;
      if (Math.abs(det) < 1e-12) return { p0: getVal(pts[0]), v0: 0 };
      return {
        p0: (sumP * sumTT - sumT * sumTP) / det,
        v0: (n * sumTP - sumT * sumP) / det,
      };
    }
    const fx = fitAxis(p => p.x_m, 0);
    const fy = fitAxis(p => p.y_m, 0);
    const fz = fitAxis(p => p.z_m, -0.5 * G);
    function evaluate(t) {
      const tau = t - t0;
      return {
        x_m: fx.p0 + fx.v0 * tau,
        y_m: fy.p0 + fy.v0 * tau,
        z_m: fz.p0 + fz.v0 * tau - 0.5 * G * tau * tau,
      };
    }
    return { evaluate, t0, t1: pts[pts.length - 1].t_rel_s };
  }

  function inspectTracesFor(sid, result, color) {
    const passResidual = p => !Number.isFinite(p.residual_m)
      || !Number.isFinite(dashResidualCapM)
      || p.residual_m <= dashResidualCapM;
    const raw = pointsForEntry(result)
      .filter(passResidual)
      .slice()
      .sort((a, b) => a.t_rel_s - b.t_rel_s);
    if (!raw.length) return [];
    const clean = spatialIsolationFilterDash(raw);
    const fit = ballisticFitDash(clean);
    if (!fit) {
      // Too few points to fit — show whatever we have as bare markers,
      // no connecting line (a line through 1-3 points would be misleading).
      return [{
        type: 'scatter3d', mode: 'markers',
        x: raw.map(p => p.x_m), y: raw.map(p => p.y_m), z: raw.map(p => p.z_m),
        marker: { color, size: 3, opacity: 0.7 },
        name: `${sid} · ${dashFitSource} pts (${raw.length})`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<extra></extra>`,
        customdata: raw.map(p => p.t_rel_s),
        showlegend: true,
      }];
    }
    const N = 80;
    const cx = [], cy = [], cz = [];
    for (let i = 0; i <= N; i++) {
      const t = fit.t0 + (fit.t1 - fit.t0) * (i / N);
      const q = fit.evaluate(t);
      cx.push(q.x_m); cy.push(q.y_m); cz.push(q.z_m);
    }
    const dropped = raw.length - clean.length;
    return [
      {
        type: 'scatter3d', mode: 'lines',
        x: cx, y: cy, z: cz,
        line: { color, width: 4 },
        name: `${sid} · ${dashFitSource} fit (${clean.length}${dropped ? `/${raw.length}` : ''} pts)`,
        hovertemplate: `${sid} · ballistic fit<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
        showlegend: true,
      },
      {
        type: 'scatter3d', mode: 'markers',
        x: clean.map(p => p.x_m), y: clean.map(p => p.y_m), z: clean.map(p => p.z_m),
        marker: { color, size: 2, opacity: 0.55 },
        name: `${sid} · samples`,
        hoverinfo: 'skip',
        showlegend: false,
      },
    ];
  }

  function replayTracesFor(sid, result, color) {
    const raw = pointsForEntry(result);
    const bounds = trajectoryBounds(raw);
    if (!bounds) return inspectTracesFor(sid, result, color);
    const tActive = bounds.t0 + playheadFrac * (bounds.t1 - bounds.t0);
    const ball = sampleTrajectory(raw, tActive);
    if (!ball) return [];
    const trailWindowS = 0.12;
    const trailPts = raw.filter(p => p.t_rel_s >= (tActive - trailWindowS) && p.t_rel_s <= tActive);
    if (!trailPts.length || trailPts[trailPts.length - 1].t_rel_s < tActive) {
      trailPts.push(ball);
    }
    return [
      {
        type: 'scatter3d', mode: 'lines',
        x: raw.map(p => p.x_m),
        y: raw.map(p => p.y_m),
        z: raw.map(p => p.z_m),
        line: { color, width: 4 },
        name: `${sid} · path`,
        hovertemplate: `${sid}<extra></extra>`,
        showlegend: true,
        opacity: 0.45,
      },
      {
        type: 'scatter3d', mode: 'lines',
        x: trailPts.map(p => p.x_m),
        y: trailPts.map(p => p.y_m),
        z: trailPts.map(p => p.z_m),
        line: { color, width: 6 },
        name: `${sid} · trail`,
        hoverinfo: 'skip',
        showlegend: false,
        opacity: 0.8,
      },
      {
        type: 'scatter3d', mode: 'markers',
        x: [ball.x_m], y: [ball.y_m], z: [ball.z_m],
        marker: {
          color: '#D9A441', size: 9, symbol: 'circle',
          line: { color: '#4A3E24', width: 1.5 },
        },
        name: `${sid} · ball`,
        hovertemplate: `${sid}<br>t=%{customdata:.3f}s<br>(x,y,z)=(%{x:.2f}, %{y:.2f}, %{z:.2f})<extra></extra>`,
        customdata: [tActive - bounds.t0],
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
    const rayByCam = liveRayStore.get(sid);
    // Mode pick: ≥2 cams producing rays → triangulation is happening, the
    // 3D points are the canonical viz so suppress rays to keep the canvas
    // clean. <2 cams (only one phone online, peer offline / not yet
    // streaming) → no triangulation possible, the rays ARE the only thing
    // to show. Counting cams with non-empty ray arrays (not just `online`)
    // matches what's actually being rendered.
    const camsStreaming = rayByCam
      ? [...rayByCam.entries()].filter(([_, rays]) => rays && rays.length).length
      : 0;
    const showRays = camsStreaming < 2;
    const showPoints = camsStreaming >= 2;
    if (rayByCam && showRays) {
      const colors = { A: 'rgba(74,107,140,0.34)', B: 'rgba(211,84,0,0.34)' };
      for (const [cam, rays] of rayByCam.entries()) {
        if (!rays.length) continue;
        const xs = [], ys = [], zs = [];
        for (const r of rays) {
          xs.push(r.origin[0], r.endpoint[0], null);
          ys.push(r.origin[1], r.endpoint[1], null);
          zs.push(r.origin[2], r.endpoint[2], null);
        }
        traces.push({
          type: 'scatter3d',
          mode: 'lines',
          x: xs,
          y: ys,
          z: zs,
          line: { color: colors[cam] || 'rgba(42,37,32,0.28)', width: 2 },
          name: `${sid} · live rays ${cam}`,
          hoverinfo: 'skip',
          showlegend: true,
        });
      }
    }
    const pts = livePointStore.get(sid) || [];
    if (!pts.length || !showPoints) return traces;
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

  function pushLiveRay(sid, cam, ray) {
    let byCam = liveRayStore.get(sid);
    if (!byCam) {
      byCam = new Map();
      liveRayStore.set(sid, byCam);
    }
    const arr = byCam.get(cam) || [];
    arr.push(ray);
    byCam.set(cam, arr);
  }

  function scheduleLiveRayRepaint() {
    if (liveRayPaintPending) return;
    liveRayPaintPending = true;
    requestAnimationFrame(() => {
      liveRayPaintPending = false;
      repaintCanvas();
    });
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
    // was confusing when replays had different durations and made the
    // canvas too busy when several sessions overlapped in space.
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

  // --- Fit source pills (svr / live, no fallback) ---------------------------
  const dashSrcPills = Array.from(document.querySelectorAll('.ff-src-pill'));
  function paintDashSourcePills() {
    for (const btn of dashSrcPills) {
      btn.setAttribute('aria-pressed', btn.dataset.src === dashFitSource ? 'true' : 'false');
    }
  }
  for (const btn of dashSrcPills) {
    btn.addEventListener('click', () => {
      const next = btn.dataset.src;
      if (next !== 'live' && next !== 'server_post') return;
      if (next === dashFitSource) return;
      dashFitSource = next;
      try { localStorage.setItem(DASH_SRC_KEY, dashFitSource); } catch {}
      paintDashSourcePills();
      repaintCanvas();
    });
  }
  paintDashSourcePills();

  // --- Fit filter sliders ---------------------------------------------------
  const dashResSlider = document.getElementById('dash-residual-slider');
  const dashResReadout = document.getElementById('dash-residual-readout');
  const dashOutSlider = document.getElementById('dash-outlier-slider');
  const dashOutReadout = document.getElementById('dash-outlier-readout');
  function paintDashResidualReadout() {
    if (!dashResReadout) return;
    dashResReadout.textContent = Number.isFinite(dashResidualCapM)
      ? `≤ ${Math.round(dashResidualCapM * 100)} cm` : 'off';
  }
  function paintDashOutlierReadout() {
    if (!dashOutReadout) return;
    dashOutReadout.textContent = Number.isFinite(dashOutlierKappa)
      ? `κ ≤ ${dashOutlierKappa.toFixed(1)}` : 'off';
  }
  if (dashResSlider) {
    dashResSlider.value = Number.isFinite(dashResidualCapM)
      ? String(Math.round(dashResidualCapM * 100)) : '200';
    paintDashResidualReadout();
    dashResSlider.addEventListener('input', () => {
      const cm = parseFloat(dashResSlider.value);
      if (!Number.isFinite(cm) || cm >= 200) {
        dashResidualCapM = Infinity;
        try { localStorage.setItem(DASH_RES_KEY, 'off'); } catch {}
      } else {
        dashResidualCapM = cm / 100;
        try { localStorage.setItem(DASH_RES_KEY, String(cm)); } catch {}
      }
      paintDashResidualReadout();
      repaintCanvas();
    });
  }
  if (dashOutSlider) {
    dashOutSlider.value = Number.isFinite(dashOutlierKappa)
      ? String(Math.round(dashOutlierKappa * 10)) : '60';
    paintDashOutlierReadout();
    dashOutSlider.addEventListener('input', () => {
      const raw = parseFloat(dashOutSlider.value);
      const k = raw / 10.0;
      if (!Number.isFinite(k) || k >= 6.0) {
        dashOutlierKappa = Infinity;
        try { localStorage.setItem(DASH_OUT_KEY, 'off'); } catch {}
      } else {
        dashOutlierKappa = k;
        try { localStorage.setItem(DASH_OUT_KEY, String(k)); } catch {}
      }
      paintDashOutlierReadout();
      repaintCanvas();
    });
  }

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
      const dur = activeReplayDuration();
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
    if (activeReplayDuration() <= 0) return;  // nothing to play
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

  // Mirrors server-side _render_battery_chip: hidden when device is offline
  // or hasn't reported battery yet.
  function batteryChip(device, online) {
    if (!online || !device) return '';
    const level = device.battery_level;
    if (typeof level !== 'number' || level < 0 || level > 1) return '';
    const pct = Math.max(0, Math.min(100, Math.round(level * 100)));
    const state = device.battery_state || 'unknown';
    let cls, icon;
    if (state === 'charging' || state === 'full') { cls = 'charging'; icon = '⚡'; }
    else if (pct <= 15) { cls = 'low';  icon = '▁'; }
    else if (pct <= 35) { cls = 'mid';  icon = '▃'; }
    else                 { cls = 'ok';   icon = '▅'; }
    return `<span class="chip battery ${cls}" title="battery · ${esc(state)}">${icon} ${pct}%</span>`;
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
      // Failed / cancelled: surface the server-side `detail` inline so
      // the operator sees *why* without having to pull server logs.
      const base = autoLast.summary || autoLast.status || 'failed';
      const det = autoLast.detail ? ` — ${autoLast.detail}` : '';
      return `${base}${det}`;
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
      const autoLogBtn = (autoLast && autoLast.status === 'failed')
        ? `<button type="button" class="btn small secondary" data-auto-cal-log="${esc(cam)}" title="Copy full auto-cal log to clipboard for debugging">Copy log</button>`
        : '';
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
              <span class="item ${autoDot}" title="${esc(autoLabel)}"><span class="dot ${autoDot}"></span>auto-cal · ${esc(autoLabel)}</span>
            </div>
            <div class="chip-col">${batteryChip(deviceRecord, online)}${statusChip(cam, online, isCal)}</div>
          </div>
          <div class="device-actions">${previewBtn}${autoCalBtn}${autoLogBtn}</div>
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

  const MODE_LABELS = { camera_only: 'Camera-only' };
  const PATH_LABELS = {
    live: ['Live stream', 'iOS → WS'],
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

  // Dashboard Session Monitor card was removed — the operator's only
  // during-stream concern is the live 3D canvas. fps/frame telemetry
  // still gets tracked on `currentLiveSession` (frame_samples +
  // frame_fps) via pushFrameSample so post-session consumers (e.g. the
  // viewer page, telemetry panel) have the data. This stub keeps the
  // legacy call sites from erroring.
  function renderActiveSession(_liveSession) {
    return;
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

  function armReadiness(state) {
    if (state && state.arm_readiness) return state.arm_readiness;
    const devices = (state && state.devices) || [];
    const calibrations = new Set((state && state.calibrations) || []);
    const online = devices.map(d => String(d.camera_id)).filter(Boolean);
    const synced = new Set(devices.filter(d => d && d.time_synced).map(d => String(d.camera_id)));
    const usable = online.filter(cam => calibrations.has(cam)).sort();
    const uncalibrated = online.filter(cam => !calibrations.has(cam)).sort();
    const blockers = [];
    const warnings = [];
    if (!online.length) {
      blockers.push('no camera online');
    } else if (uncalibrated.length) {
      uncalibrated.forEach(cam => blockers.push(`${cam} not calibrated`));
    } else if (usable.length >= 2) {
      usable.forEach(cam => { if (!synced.has(cam)) blockers.push(`${cam} not time-synced`); });
    } else {
      warnings.push(`single-camera session (${usable[0]}); no triangulation`);
    }
    return {
      ready: blockers.length === 0,
      blockers,
      warnings,
      online_cameras: online.sort(),
      calibrated_online_cameras: usable,
      synced_calibrated_online_cameras: usable.filter(cam => synced.has(cam)),
      requires_time_sync: usable.length >= 2,
      mode: usable.length >= 2 ? 'stereo' : (usable.length ? 'single_camera' : 'blocked'),
    };
  }

  function renderSession(state) {
    if (!sessionBox) { /* nav-only render still executes below */ }
    const s = state.session;
    const armed = !!(s && s.armed);
    const readiness = armReadiness(state);
    const canArm = !!(readiness && readiness.ready);
    const blockers = (readiness && readiness.blockers) || [];
    const warnings = (readiness && readiness.warnings) || [];
    currentDefaultPaths = state.default_paths || currentDefaultPaths || ['live'];
    currentLiveSession = state.live_session || currentLiveSession;
    const chip = armed ? `<span class="chip armed">armed</span>` : `<span class="chip idle">idle</span>`;
    const sid = s && s.id ? `<span class="session-id">${esc(s.id)}</span>` : '';
    const clearBtn = (!armed && s && s.id)
      ? `<form class="inline" method="POST" action="/sessions/clear">
           <button class="btn" type="submit">Clear</button>
         </form>`
      : '';
    const gateRow = (!armed && blockers.length)
      ? `<div class="arm-gate"><span class="gate-label">Need:</span> ${esc(blockers.join(', '))}</div>`
      : ((!armed && warnings.length)
        ? `<div class="arm-gate"><span class="gate-label">Mode:</span> ${esc(warnings.join(', '))}</div>`
        : '');
    const sessHtml = `
      <div class="session-head">${chip}${sid}</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sessions/arm">
          <button class="btn" type="submit" ${armed || !canArm ? 'disabled' : ''}>Arm session</button>
        </form>
        <form class="inline" method="POST" action="/sessions/stop">
          <button class="btn danger" type="submit" ${armed ? '' : 'disabled'}>Stop</button>
        </form>
        ${clearBtn}
      </div>
      ${gateRow}
      <div class="card-subtitle">Time Sync</div>
      <div class="session-actions">
        <form class="inline" method="POST" action="/sync/trigger">
          <button class="btn secondary" type="submit" ${armed ? 'disabled' : ''}>Quick chirp</button>
        </form>
        ${renderSyncLed(state, 'A')}
        ${renderSyncLed(state, 'B')}
      </div>`;
    if (sessionBox) sessionBox.innerHTML = sessHtml;
    renderActiveSession(currentLiveSession);

    // Mirror live state into the shared app-header status strip.
    // Three chips only — devices / cal / sync — matching
    // render_shared.py::_render_nav_status. The editorial badge +
    // headline were redundant with the per-device rows downstream.
    if (navStatus) {
      const online = (state.devices || []).length;
      const usable = (readiness.calibrated_online_cameras || []).length;
      const syncedUsable = (readiness.synced_calibrated_online_cameras || []).length;
      const check = (label, value, ok) =>
        `<span class="status-check ${ok ? 'ok' : 'warn'}"><span class="k">${label}</span><span class="v">${value}</span></span>`;
      navStatus.innerHTML = `
        <div class="status-checks">
          ${check('Devices', `${online}`, online >= 1)}
          ${check('Cal', `${usable}`, usable >= 1)}
          ${check('Sync', readiness.requires_time_sync ? `${syncedUsable}/${usable}` : 'single', !readiness.requires_time_sync || syncedUsable >= usable)}
        </div>`;
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
      const triangulated = Number(e.n_triangulated || 0);
      // Two pipelines, two independent chips. State (on/err/-) from
      // path_status; count from n_ball_frames_by_path.
      const pathStatus = e.path_status || {};
      const pathCounts = e.n_ball_frames_by_path || {};
      const pathTitles = {
        live: 'Live — iOS real-time detection (WS streamed)',
        server_post: 'SVR — server-side detection on decoded MOV',
      };
      const pathChips = [['live', 'L'], ['server_post', 'S']]
        .map(([path, label]) => {
          const status = pathStatus[path] || '-';
          const counts = pathCounts[path] || {};
          const total = Object.values(counts).reduce((a, v) => a + Number(v || 0), 0);
          const cls = status === 'done' ? ' on' : status === 'error' ? ' err' : '';
          const countHtml = total > 0 ? `<span class="pc">${total}</span>` : '';
          const detail = Object.keys(counts).sort().map(c => `${c}:${counts[c]}`).join(', ');
          const title = detail ? `${pathTitles[path]} · ${detail}` : pathTitles[path];
          return `<span class="path-chip${cls}" title="${esc(title)}">${label}${countHtml}</span>`;
        })
        .join('');
      const confirmMsg = `刪除 session ${e.session_id}？此動作無法復原。`;
      const trashMsg = `移動 session ${e.session_id} 到垃圾桶？`;
      // Trajectory overlay toggle: only sessions with triangulated points qualify.
      const hasTraj = triangulated > 0;
      const color = hasTraj ? trajColorFor(e.session_id) : '';
      const checked = selectedTrajIds.has(e.session_id) ? 'checked' : '';
      const toggle = hasTraj
        ? `<label class="traj-toggle" title="Overlay trajectory on canvas">
             <input type="checkbox" data-traj-sid="${sid}" ${checked}>
             <span class="swatch" style="background:${color}"></span>
           </label>`
        : `<span class="traj-toggle-placeholder" aria-hidden="true"></span>`;
      const metaBits = [];
      if (triangulated > 0) metaBits.push(`<span class="k">pts</span><span class="v">${triangulated}</span>`);
      if (e.duration_s != null) metaBits.push(`<span class="k">dur</span><span class="v">${Number(e.duration_s).toFixed(2)}s</span>`);
      if (e.peak_z_m != null) metaBits.push(`<span class="k">z</span><span class="v">${Number(e.peak_z_m).toFixed(2)}m</span>`);
      const metaHtml = metaBits.length ? `<div class="event-meta">${metaBits.join('')}</div>` : '';
      const processingState = e.processing_state ? `<span class="chip ${esc(e.processing_state)}">${esc(e.processing_state)}</span>` : '';
      const serverStatus = (e.path_status || {}).server_post || '-';
      const showRunServer = currentEventsBucket !== 'trash'
        && serverStatus !== 'done'
        && e.processing_state !== 'queued'
        && e.processing_state !== 'processing';
      const processingAction = e.processing_state === 'queued' || e.processing_state === 'processing'
        ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/cancel_processing">
             <button class="event-action warn" type="submit">Cancel</button>
           </form>`
        : showRunServer
          ? `<form class="event-action-form" method="POST" action="/sessions/${sid}/run_server_post">
               <button class="event-action ok" type="submit">Run srv</button>
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
      // Only surface real signal: path chips already encode per-
      // pipeline completion, so `partial`/`paired`/`paired_no_points`
      // are noise. `error` is the only result-status chip worth
      // showing; processing states (queued/processing/...) stay.
      const statusChipHtml = (e.status === 'error')
        ? `<span class="chip ${esc(e.status || '')}">${esc(stat)}</span>`
        : '';
      return `
        <div class="event-item">
          ${toggle}
          <a class="event-row" href="/viewer/${sid}">
            <div class="event-head">
              <span class="sid">${sid}</span>
              ${pathChips}
            </div>
            ${metaHtml}
          </a>
          <div class="event-status">
            ${processingState}
            ${statusChipHtml}
          </div>
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
        // Round battery to 5% buckets so tiny-wobble heartbeats don't
        // repaint the whole devices card every second.
        batt: (typeof d.battery_level === 'number') ? Math.round(d.battery_level * 20) : null,
        bstate: d.battery_state || null,
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
      id: e.session_id, status: e.status, n: e.n_triangulated,
      p: e.processing_state,
      srv: (e.path_status || {}).server_post || '-',
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

  // Event-row actions intercept: fetch + single events-tick refresh so
  // the button state never bounces back to the previous value between
  // the POST and the next tickEvents round-trip.
  document.addEventListener('submit', async (e) => {
    const form = e.target;
    if (form.action && /\/sessions\/[^/]+\/(trash|restore|delete|cancel_processing|resume_processing|run_server_post)$/.test(form.action)) {
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

  // Copy a full auto-cal failure log to the clipboard. Surfaces the
  // active + last-run dump plus a /status snapshot so the operator can
  // paste the whole context into an AI / bug report without digging
  // through server logs.
  function autoCalLogCopyFallback(text) {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.style.cssText = 'background:var(--surface,#fff);padding:16px;border:1px solid var(--border,#ccc);border-radius:6px;max-width:80vw;max-height:80vh;display:flex;flex-direction:column;gap:8px;';
    const hdr = document.createElement('div');
    hdr.style.cssText = 'font-family:var(--mono,monospace);font-size:11px;color:var(--sub,#555);letter-spacing:0.08em;text-transform:uppercase;';
    hdr.textContent = 'Auto-copy blocked — press ⌘C / Ctrl+C then Esc';
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.style.cssText = 'flex:1;min-width:60vw;min-height:60vh;font-family:var(--mono,monospace);font-size:11px;padding:8px;';
    panel.appendChild(hdr);
    panel.appendChild(ta);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    ta.focus();
    ta.select();
    const close = () => { document.body.removeChild(overlay); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  }

  function copyPlainTextSync(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (_) { return false; }
  }

  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-auto-cal-log]');
    if (!btn) return;
    const cam = btn.dataset.autoCalLog;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Copying…';
    const active = (currentAutoCalibration && currentAutoCalibration.active || {})[cam] || null;
    const last   = (currentAutoCalibration && currentAutoCalibration.last   || {})[cam] || null;
    let serverStatus = null;
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (r.ok) serverStatus = await r.json();
    } catch (_) {}
    const payload = {
      collected_at: new Date().toISOString(),
      camera_id: cam,
      page_url: window.location.href,
      user_agent: navigator.userAgent,
      auto_cal: { active, last },
      server_status: serverStatus,
    };
    const evSource = (last && Array.isArray(last.events)) ? last.events
                     : (active && Array.isArray(active.events)) ? active.events
                     : [];
    const evLines = evSource.map(ev => {
      const t = (typeof ev.t === 'number') ? ev.t.toFixed(3).padStart(7) : '   ?   ';
      const lv = (ev.level || 'info').padEnd(5);
      const data = ev.data ? ' ' + JSON.stringify(ev.data) : '';
      return `[${t}s ${lv}] ${ev.msg}${data}`;
    });
    const header = [
      `# auto-cal log · camera=${cam} · collected ${new Date().toISOString()}`,
      last ? `# run_id=${last.id} status=${last.status} summary=${last.summary || ''} detail=${last.detail || ''}` : '# no last run',
      `# ${evSource.length} event(s):`,
      ...evLines,
      '',
      '# --- full JSON payload ---',
    ].join('\n');
    const text = header + '\n' + JSON.stringify(payload, null, 2);
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        ok = copyPlainTextSync(text);
      }
    } catch (_) {
      ok = copyPlainTextSync(text);
    }
    if (ok) {
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
    } else {
      autoCalLogCopyFallback(text);
      btn.textContent = 'Manual copy';
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2600);
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

  // ------ Intrinsics (ChArUco) card --------------------------------------
  // Refreshes the card body from /calibration/intrinsics. Records are keyed
  // by identifierForVendor UUID so the dropdown populates from the
  // currently-online role→device map the same endpoint returns.
  function _shortDeviceId(did) {
    if (!did) return '';
    return did.length > 10 ? did.slice(0, 8) + '…' : did;
  }
  function _fmtTs(ts) {
    if (ts == null) return '—';
    try {
      const d = new Date(Number(ts) * 1000);
      const pad = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
           + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch (_) { return '—'; }
  }
  function renderIntrinsicsCard(items, onlineRoles) {
    const body = document.getElementById('intrinsics-body');
    if (!body) return;
    items = items || [];
    onlineRoles = onlineRoles || {};
    const esc = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    // Role strip
    const roleKeys = Object.keys(onlineRoles).sort();
    const knownIds = new Set(items.map(i => i.device_id));
    let roleStripHtml;
    if (!roleKeys.length) {
      roleStripHtml = '<div class="intrinsics-roles-empty">'
                    + 'No phones online — heartbeats populate this when a device connects.'
                    + '</div>';
    } else {
      const chips = roleKeys.map(role => {
        const info = onlineRoles[role] || {};
        const did = info.device_id || '';
        const model = info.device_model || '';
        if (!did) {
          return `<span class="chip idle" title="${esc(role)}: no device_id yet">${esc(role)} · legacy client</span>`;
        }
        const label = `${esc(role)} → ${esc(_shortDeviceId(did))}${model ? ` (${esc(model)})` : ''}`;
        const cls = knownIds.has(did) ? 'ok' : 'warn';
        return `<span class="chip ${cls}" title="${esc(did)}">${label}</span>`;
      }).join('');
      roleStripHtml = `<div class="intrinsics-roles">${chips}</div>`;
    }

    // Records list
    let listHtml;
    if (!items.length) {
      listHtml = '<div class="intrinsics-empty">'
               + 'No ChArUco records yet. Run <code>calibrate_intrinsics.py</code> '
               + 'on the phone\'s shots, then upload the resulting JSON below.'
               + '</div>';
    } else {
      const rows = items.map(rec => {
        const did = rec.device_id || '';
        const model = rec.device_model || 'unknown';
        const fx = typeof rec.fx === 'number' ? rec.fx.toFixed(0) : '—';
        const fy = typeof rec.fy === 'number' ? rec.fy.toFixed(0) : '—';
        const rms = typeof rec.rms_reprojection_px === 'number'
                    ? rec.rms_reprojection_px.toFixed(2) + ' px' : '—';
        const n = typeof rec.n_images === 'number' ? String(rec.n_images) : '?';
        const hasDist = Array.isArray(rec.distortion) && rec.distortion.length === 5;
        const distChip = hasDist
          ? '<span class="chip ok small">dist ✓</span>'
          : '<span class="chip warn small">no dist</span>';
        const sw = rec.source_width_px, sh = rec.source_height_px;
        const dimSpan = (typeof sw === 'number' && typeof sh === 'number')
          ? `<span class="dim">${sw}×${sh}</span>` : '';
        return `<div class="intrinsics-row">
          <div class="intrinsics-row-top">
            <span class="dev-id" title="${esc(did)}">${esc(_shortDeviceId(did))}</span>
            <span class="dev-model">${esc(model)}</span>
            ${dimSpan}
            ${distChip}
            <button type="button" class="btn small danger" data-intrinsics-delete="${esc(did)}" title="Delete ChArUco record for ${esc(did)}">×</button>
          </div>
          <div class="intrinsics-row-sub">
            fx=${fx} · fy=${fy} · RMS ${rms} · ${n} shots · ${esc(_fmtTs(rec.calibrated_at))}
          </div>
        </div>`;
      }).join('');
      listHtml = `<div class="intrinsics-list">${rows}</div>`;
    }

    // Upload dropdown (role → device_id from online map)
    const options = roleKeys
      .filter(role => (onlineRoles[role] || {}).device_id)
      .map(role => {
        const info = onlineRoles[role];
        const label = info.device_model
          ? `${esc(role)} (${esc(info.device_model)})`
          : esc(role);
        return `<option value="${esc(info.device_id)}" data-role="${esc(role)}">${label}</option>`;
      }).join('');
    const selectHtml = options.length
      ? `<select id="intrinsics-target">${options}</select>`
      : `<select id="intrinsics-target" disabled><option>No phones online</option></select>`;

    body.innerHTML = roleStripHtml + listHtml
      + `<div class="intrinsics-upload">
          <div class="intrinsics-upload-row">
            ${selectHtml}
            <input type="file" id="intrinsics-file" accept=".json,application/json">
            <button type="button" class="btn small" id="intrinsics-upload-btn">Upload</button>
          </div>
          <div class="intrinsics-upload-hint">
            Accepts <code>calibrate_intrinsics.py</code> output JSON
            (<code>fx / fy / cx / cy / distortion_coeffs / image_width / image_height</code>).
          </div>
          <div id="intrinsics-upload-status" class="intrinsics-upload-status"></div>
        </div>`;
  }

  async function tickIntrinsics() {
    try {
      const r = await fetch('/calibration/intrinsics', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderIntrinsicsCard(body.items || [], body.online_roles || {});
    } catch (_) { /* silent */ }
  }

  // Accept either the direct DeviceIntrinsics body OR the looser
  // calibrate_intrinsics.py output shape. The CLI emits fx/fy/cx/cy at
  // the top level (plus image_width / image_height / distortion_coeffs /
  // rms_reprojection_error_px / num_images_used), which we pivot into
  // the {source_width_px, source_height_px, intrinsics: {...}} shape the
  // endpoint expects. Keeps the operator from hand-editing JSON.
  function _adaptIntrinsicsJson(parsed) {
    if (!parsed || typeof parsed !== 'object') {
      throw new Error('file is not a JSON object');
    }
    // Already DeviceIntrinsics-shaped?
    if (parsed.intrinsics && parsed.source_width_px && parsed.source_height_px) {
      return parsed;
    }
    // CLI output adaption.
    const fx = Number(parsed.fx);
    const fy = Number(parsed.fy);
    const cx = Number(parsed.cx);
    const cy = Number(parsed.cy);
    const w = Number(parsed.image_width || parsed.source_width_px);
    const h = Number(parsed.image_height || parsed.source_height_px);
    if (!Number.isFinite(fx) || !Number.isFinite(fy)
        || !Number.isFinite(cx) || !Number.isFinite(cy)
        || !Number.isFinite(w) || !Number.isFinite(h)) {
      throw new Error('missing fx/fy/cx/cy/image_width/image_height');
    }
    const dist = Array.isArray(parsed.distortion_coeffs)
      ? parsed.distortion_coeffs
      : (Array.isArray(parsed.distortion) ? parsed.distortion : null);
    return {
      source_width_px: Math.round(w),
      source_height_px: Math.round(h),
      intrinsics: {
        fx, fz: fy, cx, cy,
        distortion: (dist && dist.length === 5) ? dist.map(Number) : null,
      },
      rms_reprojection_px: typeof parsed.rms_reprojection_error_px === 'number'
        ? parsed.rms_reprojection_error_px
        : (typeof parsed.rms_reprojection_px === 'number' ? parsed.rms_reprojection_px : null),
      n_images: typeof parsed.num_images_used === 'number'
        ? parsed.num_images_used
        : (typeof parsed.n_images === 'number' ? parsed.n_images : null),
      calibrated_at: typeof parsed.calibrated_at === 'number'
        ? parsed.calibrated_at
        : (Date.now() / 1000),
      source_label: parsed.source_label || null,
    };
  }

  function _setIntrinsicsStatus(cls, text) {
    const el = document.getElementById('intrinsics-upload-status');
    if (!el) return;
    el.className = 'intrinsics-upload-status' + (cls ? ' ' + cls : '');
    el.textContent = text || '';
  }

  document.addEventListener('click', async (ev) => {
    const uploadBtn = ev.target.closest && ev.target.closest('#intrinsics-upload-btn');
    if (uploadBtn) {
      ev.preventDefault();
      const sel = document.getElementById('intrinsics-target');
      const fileInput = document.getElementById('intrinsics-file');
      const deviceId = sel && sel.value;
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!deviceId) { _setIntrinsicsStatus('err', 'Select a target device first.'); return; }
      if (!file)     { _setIntrinsicsStatus('err', 'Pick a JSON file first.'); return; }
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        const body = _adaptIntrinsicsJson(parsed);
        const label = (sel.options[sel.selectedIndex] || {}).dataset;
        if (label && label.role) body.source_label = body.source_label || `charuco-role-${label.role}`;
        _setIntrinsicsStatus('', 'Uploading…');
        const r = await fetch(`/calibration/intrinsics/${encodeURIComponent(deviceId)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const errBody = await r.text();
          _setIntrinsicsStatus('err', `Upload failed (${r.status}): ${errBody.slice(0, 200)}`);
          return;
        }
        _setIntrinsicsStatus('ok', 'Uploaded.');
        if (fileInput) fileInput.value = '';
        tickIntrinsics();
      } catch (e) {
        _setIntrinsicsStatus('err', `Upload error: ${e.message || e}`);
      }
      return;
    }
    const deleteBtn = ev.target.closest && ev.target.closest('[data-intrinsics-delete]');
    if (deleteBtn) {
      ev.preventDefault();
      const deviceId = deleteBtn.getAttribute('data-intrinsics-delete');
      if (!deviceId) return;
      if (!window.confirm(`Delete ChArUco record for ${deviceId}?`)) return;
      try {
        const r = await fetch(`/calibration/intrinsics/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
        if (!r.ok) {
          _setIntrinsicsStatus('err', `Delete failed (${r.status})`);
          return;
        }
        _setIntrinsicsStatus('ok', 'Deleted.');
        tickIntrinsics();
      } catch (e) {
        _setIntrinsicsStatus('err', `Delete error: ${e.message || e}`);
      }
    }
  });

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
        liveRayStore.set(data.sid, new Map());
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
    es.addEventListener('ray', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        const sid = data.sid;
        const cam = data.cam || '?';
        if (!currentLiveSession || currentLiveSession.session_id !== sid) return;
        if (!Array.isArray(data.origin) || !Array.isArray(data.endpoint)) return;
        pushLiveRay(sid, cam, {
          origin: data.origin.map(Number),
          endpoint: data.endpoint.map(Number),
          t_rel_s: Number(data.t_rel_s || 0),
          frame_index: Number(data.frame_index || 0),
        });
        scheduleLiveRayRepaint();
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
  initHSVControls();
  tickStatus();
  tickCalibration();
  tickEvents();
  tickExtendedMarkers();
  tickIntrinsics();
  setInterval(tickStatus, 1000);
  setInterval(tickCalibration, 5000);
  setInterval(tickEvents, 5000);
  setInterval(tickExtendedMarkers, 5000);
  setInterval(tickIntrinsics, 5000);
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
