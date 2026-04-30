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

  // sid -> { points, segments, cost_threshold, gap_threshold_m }
  // Thresholds drive the dashboard's client-side mask over `points`
  // (pairing emits the full set; cost/gap are pure overlay filters,
  // mirroring the viewer's slider behaviour).
  const trajCache = new Map();

  function persistTrajSelection() {
    try { localStorage.setItem(TRAJ_STORAGE_KEY, JSON.stringify([...selectedTrajIds])); }
    catch { /* storage full / private mode — selection stays in-memory */ }
  }

  function trajColorFor(_sid) {
    return _PITCH_FIT_COLOR;
  }

  function resultHasRenderableFit(result) {
    if (!result) return false;
    const segs = result.segments_by_path || {};
    const pts = result.points_by_path || {};
    for (const path of _FIT_PATHS) {
      if (Array.isArray(segs[path]) && segs[path].length) return true;
      if (Array.isArray(pts[path]) && pts[path].length) return true;
    }
    return false;
  }

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      // Server pre-sorts `points` by t_rel_s in stamp_segments_on_result
      // so `SegmentRecord.original_indices` indexes into a time-sorted
      // list — do NOT re-sort here, that would invalidate the contract.
      const entry = {
        points_by_path: data.triangulated_by_path || {},
        segments_by_path: data.segments_by_path || {},
        paths_completed: new Set(Array.isArray(data.paths_completed) ? data.paths_completed : []),
        // None on legacy SessionResult predating recompute → null here.
        // Filter logic treats null as "no mask" (all points pass).
        cost_threshold: data.cost_threshold == null ? null : Number(data.cost_threshold),
        gap_threshold_m: data.gap_threshold_m == null ? null : Number(data.gap_threshold_m),
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  // SSE `fit` payload only carries the authority-path segments (server
  // emits one bucket: server_post if it just finished, else live). Drop
  // the cache entry so the next repaint refetches `/results/{sid}`,
  // which carries the full by-path surface — keeps live + server_post
  // segments coherent without us needing to guess which bucket the
  // payload belongs to.
  function patchTrajResult(sid, _payload) {
    trajCache.delete(sid);
  }

  // ---- fit path mode (live | server_post) ----
  const _FIT_PATHS = ['live', 'server_post'];
  const _FIT_PATH_KEY = 'ball_tracker_dashboard_fit_path';
  let _fitPathMode = (() => {
    try {
      const v = localStorage.getItem(_FIT_PATH_KEY);
      return _FIT_PATHS.includes(v) ? v : 'live';
    } catch { return 'live'; }
  })();
  function fitPathMode() { return _fitPathMode; }
  function setFitPathMode(v) {
    if (!_FIT_PATHS.includes(v)) return;
    _fitPathMode = v;
    try { localStorage.setItem(_FIT_PATH_KEY, v); } catch {}
  }

  // Resolve which path an entry should display under. Honours the
  // operator's selected mode when that path completed, otherwise demotes
  // to whichever path is actually available. Pill-disabled UI prevents
  // the user from selecting an unavailable path; this fallback only
  // fires across session switches where the previous selection no
  // longer applies. Returns null when no path has any data.
  function pickPathForEntry(entry) {
    if (!entry) return null;
    const completed = entry.paths_completed || new Set();
    if (completed.has(_fitPathMode)) return _fitPathMode;
    for (const p of _FIT_PATHS) if (completed.has(p)) return p;
    // paths_completed missing on legacy results — fall back to whichever
    // bucket actually has data.
    const segs = entry.segments_by_path || {};
    const pts = entry.points_by_path || {};
    for (const p of _FIT_PATHS) {
      if ((Array.isArray(segs[p]) && segs[p].length)
          || (Array.isArray(pts[p]) && pts[p].length)) return p;
    }
    return null;
  }

  // Build the `applyFit` payload for the currently active path. Shape
  // matches the legacy single-path entry the dashboard layer expects:
  // `{ points, segments, cost_threshold, gap_threshold_m }`.
  function resolvedFitView(entry) {
    if (!entry) return null;
    const path = pickPathForEntry(entry);
    if (!path) return null;
    const segs = (entry.segments_by_path && entry.segments_by_path[path]) || [];
    const pts = (entry.points_by_path && entry.points_by_path[path]) || [];
    return {
      path,
      points: pts,
      segments: segs,
      cost_threshold: entry.cost_threshold,
      gap_threshold_m: entry.gap_threshold_m,
    };
  }

  // Show-points toggle (default OFF). Persisted in localStorage so the
  // operator's preference survives reloads. Surfaces the raw triangulated
  // points coloured by segment under the fit curves; useful when a fit
  // looks suspicious and you want to
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
