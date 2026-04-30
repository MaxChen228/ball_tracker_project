// === intrinsics card + adapter + handlers ===
//
// The card has three orthogonal sections (mirroring `render_dashboard_intrinsics.py`):
//   - #intrinsics-pairing: current Cam A / B → device mapping, always
//     shows both rows. Offline roles render `Cam A · offline`; online
//     roles annotate with `cal ✓` / `cal ?` based on record presence.
//   - .intrinsics-list: device_id-keyed records, persistent across
//     sessions; each row tags `[USED AS A/B]` when an online role
//     currently points at this device.
//   - upload section: target picker = union of online roles + known
//     records; manual `device_id` field for fully new offline devices.
//
// Dynamic patching only touches the role-state-dependent pieces
// (#intrinsics-dynamic + the <select>'s <option> children + the manual
// device_id input's `placeholder`/disabled state). The file input and
// upload button are SSR-static — replacing them would wipe the
// operator's picked file (DOM file lists can't be reattached).

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
  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  const _ROLES = ['A', 'B'];

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

  function _renderRecordRow(rec, deviceToRoles) {
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
    const usedRoles = (deviceToRoles.get(did) || []).slice().sort();
    const usedChips = usedRoles.map(r =>
      `<span class="chip ok small used-as">used as ${_esc(r)}</span>`
    ).join('');
    return `<div class="intrinsics-row">
      <div class="intrinsics-row-top">
        <span class="dev-id" title="${_esc(did)}">${_esc(_shortDeviceId(did))}</span>
        <span class="dev-model">${_esc(model)}</span>
        ${dimSpan}
        ${distChip}
        ${usedChips}
        <button type="button" class="btn small danger" data-intrinsics-delete="${_esc(did)}" title="Delete ChArUco record for ${_esc(did)}">×</button>
      </div>
      <div class="intrinsics-row-sub">
        fx=${fx} · fy=${fy} · RMS ${rms} · ${n} shots · ${_esc(_fmtTs(rec.calibrated_at))}
      </div>
    </div>`;
  }

  function renderIntrinsicsCard(items, onlineRoles) {
    items = items || [];
    onlineRoles = onlineRoles || {};
    const recordIds = new Set(items.map(i => i.device_id));
    const deviceToRoles = new Map();
    for (const role of Object.keys(onlineRoles)) {
      const info = onlineRoles[role] || {};
      const did = info.device_id || '';
      if (!did) continue;
      if (!deviceToRoles.has(did)) deviceToRoles.set(did, []);
      deviceToRoles.get(did).push(role);
    }

    // --- Pairing section: always emit both A and B rows ---
    const pairingRows = _ROLES.map(role =>
      _renderPairingRow(role, onlineRoles[role], recordIds)
    ).join('');
    const pairingHtml =
      `<div class="intrinsics-section">`
      + `<div class="intrinsics-section-title">Pairing</div>`
      + `<div class="intrinsics-pairing">${pairingRows}</div>`
      + `</div>`;

    // --- Records section ---
    let recordsHtml;
    if (!items.length) {
      recordsHtml =
        `<div class="intrinsics-section">`
        + `<div class="intrinsics-section-title">Records</div>`
        + `<div class="intrinsics-empty">`
        + `No ChArUco records yet. Run <code>calibrate_intrinsics.py</code> `
        + `on the phone's shots, then upload the resulting JSON below.`
        + `</div>`
        + `</div>`;
    } else {
      const rows = items.map(rec => _renderRecordRow(rec, deviceToRoles)).join('');
      recordsHtml =
        `<div class="intrinsics-section">`
        + `<div class="intrinsics-section-title">Records</div>`
        + `<div class="intrinsics-list">${rows}</div>`
        + `</div>`;
    }

    const dyn = document.getElementById('intrinsics-dynamic');
    if (dyn) dyn.innerHTML = pairingHtml + recordsHtml;

    // --- Upload <select>: patch options in place. Identity preserved so
    // the operator's selection survives across polls. Options grouped
    // into Online (online_roles with device_id) and Known offline
    // (records whose device_id isn't online). A device that's both
    // online AND has a record only appears in Online — never duplicate.
    const sel = document.getElementById('intrinsics-target');
    if (sel) {
      const seen = new Set();
      const onlineOpts = [];
      const sortedRoles = Object.keys(onlineRoles).sort();
      for (const role of sortedRoles) {
        const info = onlineRoles[role] || {};
        const did = info.device_id || '';
        if (!did || seen.has(did)) continue;
        seen.add(did);
        const model = info.device_model || '';
        const label = `Cam ${_esc(role)} → ${_esc(_shortDeviceId(did))}${model ? ` (${_esc(model)})` : ''}`;
        onlineOpts.push(
          `<option value="${_esc(did)}" data-role="${_esc(role)}">${label}</option>`
        );
      }
      const offlineOpts = [];
      for (const rec of items) {
        const did = rec.device_id || '';
        if (!did || seen.has(did)) continue;
        seen.add(did);
        const model = rec.device_model || '';
        const label = `${_esc(_shortDeviceId(did))}${model ? ` (${_esc(model)})` : ''}`;
        offlineOpts.push(`<option value="${_esc(did)}">${label}</option>`);
      }
      const groups = [];
      if (onlineOpts.length) groups.push(`<optgroup label="Online">${onlineOpts.join('')}</optgroup>`);
      if (offlineOpts.length) groups.push(`<optgroup label="Known (offline)">${offlineOpts.join('')}</optgroup>`);
      const prev = sel.value;
      if (groups.length) {
        sel.innerHTML = groups.join('');
        sel.disabled = false;
        if (prev) {
          const stillThere = Array.from(sel.options).some(o => o.value === prev);
          if (stillThere) sel.value = prev;
        }
      } else {
        sel.innerHTML = '<option value="">(no devices yet — use manual id below)</option>';
        sel.disabled = true;
      }
    }
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
    if (parsed.intrinsics && parsed.source_width_px && parsed.source_height_px) {
      return parsed;
    }
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
        fx, fy, cx, cy,
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

  // device_id resolution: manual field wins when non-empty (operator
  // explicitly typed an id for an offline phone). Falls back to the
  // dropdown selection. Keeps the manual field as the escape hatch
  // without requiring radio-button-style UI mode-switching.
  // Server-side `_DEVICE_ID_RE` is `^[A-Za-z0-9_\-]{1,64}$`; mirror
  // that here so we fail fast with a useful message instead of
  // letting the upload roundtrip to a 400.
  const _MANUAL_DEVICE_ID_RE = /^[A-Za-z0-9_\-]{1,64}$/;
  function _resolveTargetDeviceId() {
    const manual = document.getElementById('intrinsics-target-manual');
    const manualVal = manual && manual.value ? manual.value.trim() : '';
    if (manualVal) {
      if (!_MANUAL_DEVICE_ID_RE.test(manualVal)) {
        throw new Error('manual device_id must match [A-Za-z0-9_-]{1,64}');
      }
      return { id: manualVal, source: 'manual', role: null };
    }
    const sel = document.getElementById('intrinsics-target');
    const id = sel && sel.value ? sel.value : '';
    if (!id) return { id: '', source: 'select', role: null };
    const opt = sel.options[sel.selectedIndex];
    const role = (opt && opt.dataset && opt.dataset.role) || null;
    return { id, source: 'select', role };
  }

  document.addEventListener('click', async (ev) => {
    const uploadBtn = ev.target.closest && ev.target.closest('#intrinsics-upload-btn');
    if (uploadBtn) {
      ev.preventDefault();
      const fileInput = document.getElementById('intrinsics-file');
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!file) { _setIntrinsicsStatus('err', 'Pick a JSON file first.'); return; }
      let target;
      try {
        target = _resolveTargetDeviceId();
      } catch (e) {
        _setIntrinsicsStatus('err', e.message || String(e));
        return;
      }
      if (!target.id) {
        _setIntrinsicsStatus('err', 'Pick a target device or paste a device_id.');
        return;
      }
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        const body = _adaptIntrinsicsJson(parsed);
        if (target.role) {
          body.source_label = body.source_label || `charuco-role-${target.role}`;
        }
        _setIntrinsicsStatus('', 'Uploading…');
        const r = await fetch(`/calibration/intrinsics/${encodeURIComponent(target.id)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const errBody = await r.text();
          _setIntrinsicsStatus('err', `Upload failed (${r.status}): ${errBody.slice(0, 200)}`);
          return;
        }
        _setIntrinsicsStatus('ok', `Uploaded for ${_shortDeviceId(target.id)}.`);
        if (fileInput) fileInput.value = '';
        const manual = document.getElementById('intrinsics-target-manual');
        if (manual) manual.value = '';
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
