// === page mode + dom refs + globals ===
  // Camera roles the dashboard renders slots for. Injected by the SSR
  // (`render_dashboard_html` writes `window.__EXPECTED_CAMS__` from
  // `state.expected_camera_ids()`). Frozen here so subsequent code can
  // treat it as a constant for the lifetime of the page; a rig
  // reconfiguration requires a page reload to refresh.
  // Cold-failure: missing the global is a server-side template bug,
  // not a runtime fallback condition — let the page break loud.
  if (!Array.isArray(window.__EXPECTED_CAMS__)) {
    throw new Error('dashboard bootstrap: window.__EXPECTED_CAMS__ missing/invalid');
  }
  const EXPECTED = Object.freeze(window.__EXPECTED_CAMS__.slice());
  const pageMode = document.body?.dataset.page || '';
  const setupCompareMode = pageMode === 'setup';

  const sceneRoot = document.getElementById('scene-root');
  const devicesBox = document.getElementById('devices-body');
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

