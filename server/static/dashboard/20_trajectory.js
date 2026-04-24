// === trajectory overlay state ===

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
      const entry = {
        points: (data.points || []).slice().sort((a, b) => a.t_rel_s - b.t_rel_s),
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

