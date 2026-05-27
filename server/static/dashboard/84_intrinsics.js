// === intrinsics card — pairing-only ===
//
// Renders one row per expected camera role (Cam A / Cam B). Each row
// shows whether the *currently-connected* device's `device_id` already
// has a ChArUco record on the server (`cal ✓` vs `cal ?`). There is no
// historical records list — `device_id` is `identifierForVendor` and
// rotates on iOS reinstall, so a catalog of past device_ids would be
// misleading. Records still live server-side; only the dashboard
// surface is trimmed.

  function _shortDeviceId(did) {
    if (!did) return '';
    return did.length > 10 ? did.slice(0, 8) + '…' : did;
  }
  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Rig cameras come from the SSR-injected EXPECTED list (see
  // 10_state_globals.js). Adding a third camera grows the pairing
  // table without code changes.
  const _ROLES = EXPECTED;

  function _renderPairingRow(role, info, recordIds) {
    if (!info) {
      return `<div class="intrinsics-pair offline">`
           + `<span class="pair-role">Cam ${_esc(role)}</span>`
           + `<span class="pair-arrow">·</span>`
           + `<span class="pair-state">offline</span>`
           + `</div>`;
    }
    const did = info.device_id || '';
    const model = info.device_model || '';
    if (!did) {
      return `<div class="intrinsics-pair legacy">`
           + `<span class="pair-role">Cam ${_esc(role)}</span>`
           + `<span class="pair-arrow">→</span>`
           + `<span class="pair-state">legacy client (no device_id)</span>`
           + `</div>`;
    }
    const calChip = recordIds.has(did)
      ? '<span class="chip ok small">cal ✓</span>'
      : '<span class="chip warn small">cal ?</span>';
    const modelHtml = model ? `<span class="pair-model">(${_esc(model)})</span>` : '';
    return `<div class="intrinsics-pair online">`
         + `<span class="pair-role">Cam ${_esc(role)}</span>`
         + `<span class="pair-arrow">→</span>`
         + `<span class="pair-id" title="${_esc(did)}">${_esc(_shortDeviceId(did))}</span>`
         + `${modelHtml}${calChip}`
         + `</div>`;
  }

  function renderIntrinsicsCard(items, onlineRoles) {
    items = items || [];
    onlineRoles = onlineRoles || {};
    const recordIds = new Set(items.map(i => i.device_id));
    const rows = _ROLES.map(role =>
      _renderPairingRow(role, onlineRoles[role], recordIds)
    ).join('');
    const dyn = document.getElementById('intrinsics-dynamic');
    if (dyn) dyn.innerHTML = `<div class="intrinsics-pairing">${rows}</div>`;
  }

  async function tickIntrinsics() {
    try {
      const r = await fetch('/calibration/intrinsics', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderIntrinsicsCard(body.items || [], body.online_roles || {});
    } catch (_) { /* silent */ }
  }
