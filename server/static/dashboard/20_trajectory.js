// === selected pitch state + trajectory cache ===

  // The dashboard 3D scene shows ONE pitch at a time. `selectedTrajIds`
  // keeps Set semantics for backward compatibility with the existing
  // events list checkbox, but its size is invariant ≤ 1: clicking a row
  // replaces the selection. Multi-overlay was retired in the dashboard
  // refactor — viewer.html owns time-aligned single-pitch playback;
  // dashboard 3D answers "what's the latest pitch's fit / speed?".
  const TRAJ_STORAGE_KEY = 'ball_tracker_dashboard_selected_trajectories';
  const selectedTrajIds = (() => {
    try {
      const raw = localStorage.getItem(TRAJ_STORAGE_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      // Trim to single-element invariant on load.
      return new Set(arr.slice(0, 1));
    } catch { return new Set(); }
  })();

  // Active pitch palette: a single accent. Per-session colour rotation
  // was useful with overlay; with single-select it just adds noise.
  const _PITCH_FIT_COLOR = '#C0392B';
  const _PITCH_GHOST_COLOR = 'rgba(192, 57, 43, 0.20)';
  const _PITCH_POINTS_COLOR = 'rgba(74, 62, 36, 0.55)';

  const trajCache = new Map();       // sid -> {points, segments}
  let basePlot = null;               // last /calibration/state .plot payload

  function persistTrajSelection() {
    try { localStorage.setItem(TRAJ_STORAGE_KEY, JSON.stringify([...selectedTrajIds])); }
    catch { /* storage full / private mode — selection stays in-memory */ }
  }

  function trajColorFor(_sid) {
    return _PITCH_FIT_COLOR;
  }

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      const entry = {
        points: (data.points || []).slice().sort((a, b) => a.t_rel_s - b.t_rel_s),
        segments: Array.isArray(data.segments) ? data.segments : [],
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  // Patch in a fresh segments array for `sid` — used by the `fit` SSE
  // handler so a recompute / cycle_end push refreshes the cache without
  // refetching /results.
  function patchTrajSegments(sid, segments) {
    const entry = trajCache.get(sid);
    if (entry) entry.segments = Array.isArray(segments) ? segments : [];
  }

  // Show-points toggle (default OFF). Persisted in localStorage so the
  // operator's preference survives reloads. Surfaces the raw triangulated
  // points (the same dots the /fit page used to colour by segment) under
  // the fit curves; useful when a fit looks suspicious and you want to
  // see what the segmenter saw.
  const _POINTS_KEY = 'ball_tracker_dashboard_show_points';
  let _showPoints = (() => {
    try { return localStorage.getItem(_POINTS_KEY) === '1'; }
    catch { return false; }
  })();
  function showPointsEnabled() { return _showPoints; }
  function setShowPoints(v) {
    _showPoints = !!v;
    try { localStorage.setItem(_POINTS_KEY, _showPoints ? '1' : '0'); } catch {}
  }
