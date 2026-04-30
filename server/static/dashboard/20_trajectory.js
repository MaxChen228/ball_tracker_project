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

  async function ensureTrajLoaded(sid) {
    if (trajCache.has(sid)) return trajCache.get(sid);
    try {
      const r = await fetch(`/results/${encodeURIComponent(sid)}`, { cache: 'no-store' });
      if (!r.ok) return null;
      const data = await r.json();
      const entry = {
        // Server pre-sorts `points` by t_rel_s in stamp_segments_on_result
        // so `SegmentRecord.original_indices` indexes into a time-sorted
        // list — do NOT re-sort here, that would invalidate the contract.
        points: data.points || [],
        segments: Array.isArray(data.segments) ? data.segments : [],
        // None on legacy SessionResult predating recompute → null here.
        // Filter logic treats null as "no mask" (all points pass).
        cost_threshold: data.cost_threshold == null ? null : Number(data.cost_threshold),
        gap_threshold_m: data.gap_threshold_m == null ? null : Number(data.gap_threshold_m),
      };
      trajCache.set(sid, entry);
      return entry;
    } catch { return null; }
  }

  // Patch the cached `sid` entry from a `fit` SSE event payload.
  // Recompute (Apply) and cycle_end both broadcast `{segments,
  // cost_threshold, gap_threshold_m}`; thresholds need to land in the
  // cache so the next repaint applies the new client-side mask over
  // `points`. `points` itself is invariant under recompute (pairing
  // emits the full set regardless of tuning post Phase 1-5).
  function patchTrajResult(sid, payload) {
    const entry = trajCache.get(sid);
    if (!entry) return;
    if (Array.isArray(payload.segments)) entry.segments = payload.segments;
    if ('cost_threshold' in payload) {
      entry.cost_threshold = payload.cost_threshold == null
        ? null : Number(payload.cost_threshold);
    }
    if ('gap_threshold_m' in payload) {
      entry.gap_threshold_m = payload.gap_threshold_m == null
        ? null : Number(payload.gap_threshold_m);
    }
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
