// === canvas mode + playback state ===
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
    const bounds = r ? trajectoryBounds(r.points || []) : null;
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

