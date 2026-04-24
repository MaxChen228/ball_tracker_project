// === page mode + dom refs + globals ===
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

