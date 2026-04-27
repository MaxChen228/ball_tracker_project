// === events bucket + traj toggle handlers ===

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
  // Strike-zone toggle: server-rendered traces stay in basePlot; the
  // checkbox flips a localStorage flag and forces a repaint, which
  // filters the strike-zone traces in or out at composition time.
  const _szToggle = document.getElementById('dash-strike-zone-toggle');
  if (_szToggle) {
    _szToggle.checked = strikeZoneVisible();
    _szToggle.addEventListener('change', () => {
      setStrikeZoneVisible(_szToggle.checked);
      repaintCanvas();
    });
  }
  // Speed overlay: shared with viewer via window.BallTrackerOverlays.
  const _speedToggle = document.getElementById('dash-speed-toggle');
  if (_speedToggle) {
    _speedToggle.checked = _OVL.speedVisible();
    _speedToggle.addEventListener('change', () => {
      _OVL.setSpeedVisible(_speedToggle.checked);
      repaintCanvas();
    });
  }
  // Fit overlay: shared with viewer via window.BallTrackerOverlays.
  // Source pills pick which trajectory bucket the fit reads from —
  // svr = selected event's /results points (server_post triangulation),
  // live = current armed session's WS-streamed live points.
  const _fitToggle = document.getElementById('dash-fit-toggle');
  if (_fitToggle) {
    _fitToggle.checked = _OVL.fitVisible();
    _fitToggle.addEventListener('change', () => {
      _OVL.setFitVisible(_fitToggle.checked);
      repaintCanvas();
    });
  }
  function paintDashFitSourcePills() {
    const cur = _OVL.fitSource();
    document.querySelectorAll('.ff-src-pill').forEach(btn => {
      btn.setAttribute('aria-pressed', btn.dataset.src === cur ? 'true' : 'false');
    });
  }
  paintDashFitSourcePills();
  document.querySelectorAll('.ff-src-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      _OVL.setFitSource(btn.dataset.src);
      paintDashFitSourcePills();
      if (_OVL.fitVisible()) repaintCanvas();
    });
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
