// === intrinsics card + adapter + handlers ===

  // ------ Intrinsics (ChArUco) card --------------------------------------
  // Refreshes the card body from /calibration/intrinsics. Records are keyed
  // by identifierForVendor UUID so the dropdown populates from the
  // currently-online role→device map the same endpoint returns.
  function _shortDeviceId(did) {
    if (!did) return '';
    return did.length > 10 ? did.slice(0, 8) + '…' : did;
  }
  function _fmtTs(ts) {
    if (ts == null) return '—';
    try {
      const d = new Date(Number(ts) * 1000);
      const pad = n => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
           + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    } catch (_) { return '—'; }
  }
  function renderIntrinsicsCard(items, onlineRoles) {
    const body = document.getElementById('intrinsics-body');
    if (!body) return;
    items = items || [];
    onlineRoles = onlineRoles || {};
    const esc = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    // Role strip
    const roleKeys = Object.keys(onlineRoles).sort();
    const knownIds = new Set(items.map(i => i.device_id));
    let roleStripHtml;
    if (!roleKeys.length) {
      roleStripHtml = '<div class="intrinsics-roles-empty">'
                    + 'No phones online — heartbeats populate this when a device connects.'
                    + '</div>';
    } else {
      const chips = roleKeys.map(role => {
        const info = onlineRoles[role] || {};
        const did = info.device_id || '';
        const model = info.device_model || '';
        if (!did) {
          return `<span class="chip idle" title="${esc(role)}: no device_id yet">${esc(role)} · legacy client</span>`;
        }
        const label = `${esc(role)} → ${esc(_shortDeviceId(did))}${model ? ` (${esc(model)})` : ''}`;
        const cls = knownIds.has(did) ? 'ok' : 'warn';
        return `<span class="chip ${cls}" title="${esc(did)}">${label}</span>`;
      }).join('');
      roleStripHtml = `<div class="intrinsics-roles">${chips}</div>`;
    }

    // Records list
    let listHtml;
    if (!items.length) {
      listHtml = '<div class="intrinsics-empty">'
               + 'No ChArUco records yet. Run <code>calibrate_intrinsics.py</code> '
               + 'on the phone\'s shots, then upload the resulting JSON below.'
               + '</div>';
    } else {
      const rows = items.map(rec => {
        const did = rec.device_id || '';
        const model = rec.device_model || 'unknown';
        const fx = typeof rec.fx === 'number' ? rec.fx.toFixed(0) : '—';
        const fy = typeof rec.fy === 'number' ? rec.fy.toFixed(0) : '—';
        const rms = typeof rec.rms_reprojection_px === 'number'
                    ? rec.rms_reprojection_px.toFixed(2) + ' px' : '—';
        const n = typeof rec.n_images === 'number' ? String(rec.n_images) : '?';
        const hasDist = Array.isArray(rec.distortion) && rec.distortion.length === 5;
        const distChip = hasDist
          ? '<span class="chip ok small">dist ✓</span>'
          : '<span class="chip warn small">no dist</span>';
        const sw = rec.source_width_px, sh = rec.source_height_px;
        const dimSpan = (typeof sw === 'number' && typeof sh === 'number')
          ? `<span class="dim">${sw}×${sh}</span>` : '';
        return `<div class="intrinsics-row">
          <div class="intrinsics-row-top">
            <span class="dev-id" title="${esc(did)}">${esc(_shortDeviceId(did))}</span>
            <span class="dev-model">${esc(model)}</span>
            ${dimSpan}
            ${distChip}
            <button type="button" class="btn small danger" data-intrinsics-delete="${esc(did)}" title="Delete ChArUco record for ${esc(did)}">×</button>
          </div>
          <div class="intrinsics-row-sub">
            fx=${fx} · fy=${fy} · RMS ${rms} · ${n} shots · ${esc(_fmtTs(rec.calibrated_at))}
          </div>
        </div>`;
      }).join('');
      listHtml = `<div class="intrinsics-list">${rows}</div>`;
    }

    // Upload dropdown (role → device_id from online map)
    const options = roleKeys
      .filter(role => (onlineRoles[role] || {}).device_id)
      .map(role => {
        const info = onlineRoles[role];
        const label = info.device_model
          ? `${esc(role)} (${esc(info.device_model)})`
          : esc(role);
        return `<option value="${esc(info.device_id)}" data-role="${esc(role)}">${label}</option>`;
      }).join('');
    const selectHtml = options.length
      ? `<select id="intrinsics-target">${options}</select>`
      : `<select id="intrinsics-target" disabled><option>No phones online</option></select>`;

    body.innerHTML = roleStripHtml + listHtml
      + `<div class="intrinsics-upload">
          <div class="intrinsics-upload-row">
            ${selectHtml}
            <input type="file" id="intrinsics-file" accept=".json,application/json">
            <button type="button" class="btn small" id="intrinsics-upload-btn">Upload</button>
          </div>
          <div class="intrinsics-upload-hint">
            Accepts <code>calibrate_intrinsics.py</code> output JSON
            (<code>fx / fy / cx / cy / distortion_coeffs / image_width / image_height</code>).
          </div>
          <div id="intrinsics-upload-status" class="intrinsics-upload-status"></div>
        </div>`;
  }

  async function tickIntrinsics() {
    try {
      const r = await fetch('/calibration/intrinsics', { cache: 'no-store' });
      if (!r.ok) return;
      const body = await r.json();
      renderIntrinsicsCard(body.items || [], body.online_roles || {});
    } catch (_) { /* silent */ }
  }

  // Accept either the direct DeviceIntrinsics body OR the looser
  // calibrate_intrinsics.py output shape. The CLI emits fx/fy/cx/cy at
  // the top level (plus image_width / image_height / distortion_coeffs /
  // rms_reprojection_error_px / num_images_used), which we pivot into
  // the {source_width_px, source_height_px, intrinsics: {...}} shape the
  // endpoint expects. Keeps the operator from hand-editing JSON.
  function _adaptIntrinsicsJson(parsed) {
    if (!parsed || typeof parsed !== 'object') {
      throw new Error('file is not a JSON object');
    }
    // Already DeviceIntrinsics-shaped?
    if (parsed.intrinsics && parsed.source_width_px && parsed.source_height_px) {
      return parsed;
    }
    // CLI output adaption.
    const fx = Number(parsed.fx);
    const fy = Number(parsed.fy);
    const cx = Number(parsed.cx);
    const cy = Number(parsed.cy);
    const w = Number(parsed.image_width || parsed.source_width_px);
    const h = Number(parsed.image_height || parsed.source_height_px);
    if (!Number.isFinite(fx) || !Number.isFinite(fy)
        || !Number.isFinite(cx) || !Number.isFinite(cy)
        || !Number.isFinite(w) || !Number.isFinite(h)) {
      throw new Error('missing fx/fy/cx/cy/image_width/image_height');
    }
    const dist = Array.isArray(parsed.distortion_coeffs)
      ? parsed.distortion_coeffs
      : (Array.isArray(parsed.distortion) ? parsed.distortion : null);
    return {
      source_width_px: Math.round(w),
      source_height_px: Math.round(h),
      intrinsics: {
        fx, fz: fy, cx, cy,
        distortion: (dist && dist.length === 5) ? dist.map(Number) : null,
      },
      rms_reprojection_px: typeof parsed.rms_reprojection_error_px === 'number'
        ? parsed.rms_reprojection_error_px
        : (typeof parsed.rms_reprojection_px === 'number' ? parsed.rms_reprojection_px : null),
      n_images: typeof parsed.num_images_used === 'number'
        ? parsed.num_images_used
        : (typeof parsed.n_images === 'number' ? parsed.n_images : null),
      calibrated_at: typeof parsed.calibrated_at === 'number'
        ? parsed.calibrated_at
        : (Date.now() / 1000),
      source_label: parsed.source_label || null,
    };
  }

  function _setIntrinsicsStatus(cls, text) {
    const el = document.getElementById('intrinsics-upload-status');
    if (!el) return;
    el.className = 'intrinsics-upload-status' + (cls ? ' ' + cls : '');
    el.textContent = text || '';
  }

  document.addEventListener('click', async (ev) => {
    const uploadBtn = ev.target.closest && ev.target.closest('#intrinsics-upload-btn');
    if (uploadBtn) {
      ev.preventDefault();
      const sel = document.getElementById('intrinsics-target');
      const fileInput = document.getElementById('intrinsics-file');
      const deviceId = sel && sel.value;
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!deviceId) { _setIntrinsicsStatus('err', 'Select a target device first.'); return; }
      if (!file)     { _setIntrinsicsStatus('err', 'Pick a JSON file first.'); return; }
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        const body = _adaptIntrinsicsJson(parsed);
        const label = (sel.options[sel.selectedIndex] || {}).dataset;
        if (label && label.role) body.source_label = body.source_label || `charuco-role-${label.role}`;
        _setIntrinsicsStatus('', 'Uploading…');
        const r = await fetch(`/calibration/intrinsics/${encodeURIComponent(deviceId)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const errBody = await r.text();
          _setIntrinsicsStatus('err', `Upload failed (${r.status}): ${errBody.slice(0, 200)}`);
          return;
        }
        _setIntrinsicsStatus('ok', 'Uploaded.');
        if (fileInput) fileInput.value = '';
        tickIntrinsics();
      } catch (e) {
        _setIntrinsicsStatus('err', `Upload error: ${e.message || e}`);
      }
      return;
    }
    const deleteBtn = ev.target.closest && ev.target.closest('[data-intrinsics-delete]');
    if (deleteBtn) {
      ev.preventDefault();
      const deviceId = deleteBtn.getAttribute('data-intrinsics-delete');
      if (!deviceId) return;
      if (!window.confirm(`Delete ChArUco record for ${deviceId}?`)) return;
      try {
        const r = await fetch(`/calibration/intrinsics/${encodeURIComponent(deviceId)}`, { method: 'DELETE' });
        if (!r.ok) {
          _setIntrinsicsStatus('err', `Delete failed (${r.status})`);
          return;
        }
        _setIntrinsicsStatus('ok', 'Deleted.');
        tickIntrinsics();
      } catch (e) {
        _setIntrinsicsStatus('err', `Delete error: ${e.message || e}`);
      }
    }
  });
