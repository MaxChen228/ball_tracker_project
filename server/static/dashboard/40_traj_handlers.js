// === events bucket + traj row-click handlers ===

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
  // Row click = "load this fit into dashboard 3D". Single-select toggle:
  // clicking the same row deselects. Multi-overlay was retired with the
  // dashboard 3D refactor — the scene shows one pitch at a time; viewer
  // owns scrub-overlay UX. Clicks on the explicit "→ viewer" link or any
  // <button> / action <form> in row 3 must NOT trigger row selection.
  if (eventsBox) eventsBox.addEventListener('click', (e) => {
    if (e.target.closest('.ev-viewer-link')) return;
    if (e.target.closest('.ev-action-form, button')) return;
    const row = e.target.closest('.event-item[data-sid]');
    if (!row) return;
    const sid = row.dataset.sid;
    if (selectedTrajIds.has(sid)) {
      selectedTrajIds.delete(sid);
    } else {
      selectedTrajIds.clear();
      selectedTrajIds.add(sid);
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
