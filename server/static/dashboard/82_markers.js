// === extended markers handlers + render ===

  // Register extended markers from the picked camera.
  document.addEventListener('click', async (e) => {
    if (e.target && e.target.id === 'marker-register-btn') {
      const sel = document.getElementById('marker-register-cam');
      const cam = sel && sel.value;
      if (!cam) return;
      e.target.disabled = true;
      try {
        const r = await fetch('/calibration/markers/register/' + encodeURIComponent(cam),
                              { method: 'POST' });
        if (!r.ok) {
          let msg = 'Register failed';
          try { const body = await r.json(); if (body.detail) msg = body.detail; } catch (_) {}
          alert(msg);
        }
        tickExtendedMarkers();
      } finally {
        e.target.disabled = false;
      }
      return;
    }
    if (e.target && e.target.id === 'marker-clear-btn') {
      if (!confirm('Clear all extended markers?')) return;
      try {
        await fetch('/calibration/markers/clear', { method: 'POST',
          headers: { 'Content-Type': 'application/json' } });
      } catch (_) {}
      tickExtendedMarkers();
      return;
    }
    const remBtn = e.target.closest('[data-marker-remove]');
    if (remBtn) {
      const mid = remBtn.dataset.markerRemove;
      try {
        await fetch('/calibration/markers/' + encodeURIComponent(mid),
                    { method: 'DELETE' });
      } catch (_) {}
      tickExtendedMarkers();
    }
  });

  function renderExtendedMarkers(markers) {
    const listEl = document.getElementById('marker-list');
    if (!listEl) return;
    if (!markers || markers.length === 0) {
      listEl.innerHTML = '<div class="marker-list-empty">No extended markers registered.</div>';
      return;
    }
    const rows = markers.map(row => {
      const id = Number(row.id);
      const wx = Number(row.wx);
      const wy = Number(row.wy);
      const fmt = v => (v >= 0 ? '+' : '') + v.toFixed(3);
      return '<div class="marker-row">' +
             '<span class="mid">#' + id + '</span>' +
             '<span class="mxy">(' + fmt(wx) + ', ' + fmt(wy) + ') m</span>' +
             '<button type="button" data-marker-remove="' + id +
             '" title="Remove marker ' + id + '">&times;</button>' +
             '</div>';
    }).join('');
    listEl.innerHTML = '<div class="marker-list">' + rows + '</div>';
  }

  async function tickExtendedMarkers() {
    try {
      const r = await fetch('/calibration/markers', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderExtendedMarkers(body.markers || []);
    } catch (e) { /* silent */ }
  }
