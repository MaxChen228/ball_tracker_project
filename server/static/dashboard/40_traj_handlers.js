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
  if (eventsBox) eventsBox.addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-traj-sid]');
    if (!cb) return;
    const sid = cb.dataset.trajSid;
    // Single-select preview: clicking one row always replaces the
    // selection (clicking again on the same row deselects). Multi-select
    // was retired with the dashboard 3D refactor — the scene shows one
    // pitch's fit + speed at a time; viewer.html owns scrub-overlay UX.
    if (cb.checked) {
      selectedTrajIds.clear();
      selectedTrajIds.add(sid);
      eventsBox.querySelectorAll('input[data-traj-sid]').forEach(other => {
        if (other !== cb) other.checked = false;
      });
    } else {
      selectedTrajIds.delete(sid);
    }
    persistTrajSelection();
    repaintCanvas();
    updateLatestPitchBadge();
  });
  // Show-points toggle — surfaces raw triangulated points coloured by
  // segment under the fit curves. Default off; reading is instantaneous
  // once toggled on (data is already cached on `trajCache`).
  const _showPointsToggle = document.getElementById('dash-show-points-toggle');
  if (_showPointsToggle) {
    _showPointsToggle.checked = showPointsEnabled();
    _showPointsToggle.addEventListener('change', () => {
      setShowPoints(_showPointsToggle.checked);
      repaintCanvas();
    });
  }
