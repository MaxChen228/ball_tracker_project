// === device pool card — multi-camera rig assignment (Phase 0 PR2) ===
//
// Reads /devices/pool every 5 s and re-renders #device-pool-dynamic.
// Assign / unassign buttons hit /devices/assign + /devices/unassign.
//
// In PR2 the assignments are advisory: the WS handshake still accepts
// whatever cam_id the phone connects with. PR3 will gate WS on these
// records so an unassigned phone sits in pending mode until promoted.

  function _short(u) {
    if (!u) return '';
    return u.length > 10 ? u.slice(0, 8) + '…' : u;
  }
  function _escPool(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _assignmentRow(rec) {
    const cam = _escPool(rec.camera_id);
    const uuid = rec.device_uuid || '';
    const model = rec.device_model || '';
    const online = !!rec.online;
    const chip = online
      ? '<span class="chip ok small">online</span>'
      : '<span class="chip warn small">offline</span>';
    const modelHtml = model ? `<span class="pool-model">(${_escPool(model)})</span>` : '';
    return `<div class="device-pool-row assigned">`
         + `<span class="pool-cam">Cam ${cam}</span>`
         + `<span class="pool-arrow">→</span>`
         + `<span class="pool-uuid" title="${_escPool(uuid)}">${_escPool(_short(uuid))}</span>`
         + `${modelHtml}${chip}`
         + `<button type="button" class="pool-action" `
         + `data-device-pool-action="unassign" data-camera-id="${cam}">Unassign</button>`
         + `</div>`;
  }

  function _observedRow(rec) {
    const cam = _escPool(rec.camera_id);
    const uuid = rec.device_uuid || '';
    const model = rec.device_model || '';
    const modelHtml = model ? `<span class="pool-model">(${_escPool(model)})</span>` : '';
    return `<div class="device-pool-row observed">`
         + `<span class="pool-cam-current">currently Cam ${cam}</span>`
         + `<span class="pool-uuid" title="${_escPool(uuid)}">${_escPool(_short(uuid))}</span>`
         + `${modelHtml}`
         + `<button type="button" class="pool-action" `
         + `data-device-pool-action="assign" `
         + `data-device-uuid="${_escPool(uuid)}" `
         + `data-suggested-camera-id="${cam}" `
         + `data-device-model="${_escPool(model)}">Assign…</button>`
         + `</div>`;
  }

  function renderDevicePool(body) {
    body = body || {};
    const assigned = body.assignments || [];
    const observed = body.observed_unassigned || [];
    const dyn = document.getElementById('device-pool-dynamic');
    if (!dyn) return;
    const parts = [];
    if (!assigned.length && !observed.length) {
      parts.push(
        '<div class="device-pool-empty muted">'
        + 'No devices yet. Connect a phone or assign a known UUID below.'
        + '</div>'
      );
    }
    if (assigned.length) {
      const rows = assigned.map(_assignmentRow).join('');
      parts.push(
        '<div class="device-pool-section">'
        + '<div class="device-pool-section-title">Assigned</div>'
        + `<div class="device-pool-rows">${rows}</div>`
        + '</div>'
      );
    }
    if (observed.length) {
      const rows = observed.map(_observedRow).join('');
      parts.push(
        '<div class="device-pool-section">'
        + '<div class="device-pool-section-title">Observed (unassigned)</div>'
        + `<div class="device-pool-rows">${rows}</div>`
        + '</div>'
      );
    }
    dyn.innerHTML = parts.join('');
  }

  async function tickDevicePool() {
    try {
      const r = await fetch('/devices/pool', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderDevicePool(body);
    } catch (_) { /* silent — next tick retries */ }
  }

  // Delegated click handler for Assign / Unassign buttons.
  // Bound on document so newly-rendered rows pick it up without a
  // per-render rebind.
  document.addEventListener('click', async (ev) => {
    const btn = ev.target && ev.target.closest('[data-device-pool-action]');
    if (!btn) return;
    const action = btn.getAttribute('data-device-pool-action');
    btn.disabled = true;
    try {
      if (action === 'unassign') {
        const cam = btn.getAttribute('data-camera-id') || '';
        if (!cam) return;
        const r = await fetch('/devices/unassign', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ camera_id: cam }),
        });
        if (!r.ok) {
          const detail = await r.text();
          alert(`Unassign failed (${r.status}): ${detail}`);
        }
      } else if (action === 'assign') {
        const uuid = btn.getAttribute('data-device-uuid') || '';
        const suggested = btn.getAttribute('data-suggested-camera-id') || '';
        const model = btn.getAttribute('data-device-model') || '';
        if (!uuid) return;
        const cam = window.prompt(
          `Assign cam_id for device ${_short(uuid)}:`,
          suggested,
        );
        if (!cam) return;
        const r = await fetch('/devices/assign', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            device_uuid: uuid,
            camera_id: cam,
            device_model: model || null,
          }),
        });
        if (!r.ok) {
          const detail = await r.text();
          alert(`Assign failed (${r.status}): ${detail}`);
        }
      }
    } catch (e) {
      console.error('device pool action failed', e);
    } finally {
      btn.disabled = false;
      // Refresh immediately so the row state matches what the server now
      // has on disk — don't wait for the 5 s tick.
      tickDevicePool();
    }
  });
