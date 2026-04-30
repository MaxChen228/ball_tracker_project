// === Detection-config card — preset-only model ===
//
// Mental model: preset files are the only source of truth for detection
// config. Slider drags are local UI state; they never touch the server
// until the operator presses Apply, and Apply *always* means "save as
// new preset". There is no anonymous "live custom config" — saving a
// new preset atomically writes the file and switches the live active
// preset to it.
//
// Path summary (post-redesign):
//   - Slider drag           → local <input> updates only, form marked dirty
//   - Apply                 → POST /presets {name, label, hsv, shape_gate}
//                             (server saves + switches active; 409 on
//                              duplicate name → operator picks again)
//   - Click preset button   → POST /presets/active {name}  (pure switch)
//   - Manage modal Use      → POST /presets/active {name}
//   - Manage modal Duplicate→ POST /presets {…} on the source's values
//   - Manage modal Delete   → DELETE /presets/{name} (409 if active)

  function _syncHSVField(form, key, value) {
    const range = form.querySelector(`[data-hsv-range="${key}"]`);
    const num = form.querySelector(`[data-hsv-number="${key}"]`);
    if (range) range.value = String(value);
    if (num) num.value = String(value);
  }

  function _syncShape(form, key, value01) {
    const range = form.querySelector(`[data-shape-range="${key}"]`);
    const num = form.querySelector(`[data-shape-number="${key}"]`);
    const v = Math.max(0, Math.min(1, Number(value01) || 0));
    if (range) range.value = String(Math.round(v * 100));
    if (num) num.value = v.toFixed(2);
  }

  // Slider drag marks the form dirty (for visual cues; styling is the
  // caller's concern). Apply reads from the form regardless — there is
  // no "claim preset identity" path anymore, since Apply always saves
  // a fresh file.
  function _markDirty(form) {
    form.dataset.dirty = '1';
  }

  function _readHSV(form) {
    const get = (k) => Number(form.querySelector(`[data-hsv-number="${k}"]`).value);
    return {
      h_min: get('h_min'), h_max: get('h_max'),
      s_min: get('s_min'), s_max: get('s_max'),
      v_min: get('v_min'), v_max: get('v_max'),
    };
  }

  function _readShape(form) {
    const get = (k) => Number(form.querySelector(`[data-shape-number="${k}"]`).value);
    return { aspect_min: get('aspect_min'), fill_min: get('fill_min') };
  }

  function initDetectionConfigControls() {
    const form = document.getElementById('detection-config-form');
    if (!form) return;

    // HSV slider <-> number two-way bind. Drag marks the form dirty so
    // Apply / Save-as-new can be distinguished from a fresh page load.
    form.querySelectorAll('[data-hsv-range]').forEach((input) => {
      input.addEventListener('input', () => {
        _syncHSVField(form, input.dataset.hsvRange, input.value);
        _markDirty(form);
      });
    });
    form.querySelectorAll('[data-hsv-number]').forEach((input) => {
      input.addEventListener('input', () => {
        _syncHSVField(form, input.dataset.hsvNumber, input.value);
        _markDirty(form);
      });
    });

    // Shape-gate slider (0..100) <-> number (0..1).
    form.querySelectorAll('[data-shape-range]').forEach((slider) => {
      slider.addEventListener('input', () => {
        _syncShape(form, slider.dataset.shapeRange, Number(slider.value) / 100);
        _markDirty(form);
      });
    });
    form.querySelectorAll('[data-shape-number]').forEach((num) => {
      num.addEventListener('input', () => {
        _syncShape(form, num.dataset.shapeNumber, Number(num.value));
        _markDirty(form);
      });
    });

    const status = form.querySelector('[data-detection-apply-status]');

    // Preset row buttons — clicking one switches active immediately.
    // Server snaps the live config to that preset's values + broadcasts
    // WS settings; the page reload re-renders sliders against the new
    // active state.
    document.querySelectorAll('[data-hsv-preset]').forEach((btn) => {
      btn.addEventListener('click', () => _switchActive(btn.dataset.hsvPreset, status));
    });

    // Apply = Save as new preset. Slider values land on disk under a
    // fresh name; the server-side POST /presets handler switches active
    // on success so the form re-renders against the just-saved values.
    form.addEventListener('submit', (evt) => {
      evt.preventDefault();
      _saveAsNew(form, status);
    });

    // Defensive reset button (rendered only when the renderer detects
    // values diverge from the active preset — reachable today only via
    // direct curl POST /detection/config since the dashboard UI never
    // produces modified state). Treat as "snap back to canonical
    // values for this preset name".
    document.querySelectorAll('[data-detection-reset-preset]').forEach((btn) => {
      btn.addEventListener('click', () => _switchActive(btn.dataset.detectionResetPreset, status));
    });

    _initPresetLibraryControls(form, status);
  }

  // ===== Endpoint helpers =============================================

  async function _switchActive(name, status) {
    if (status) status.textContent = '…';
    try {
      const r = await fetch('/presets/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) {
        const t = await r.text();
        if (status) status.textContent = `switch error: ${t.slice(0, 200)}`;
        return;
      }
      window.location.reload();
    } catch (e) {
      if (status) status.textContent = `network error: ${e}`;
    }
  }

  function _slugFromPrompt(suggestion) {
    // POST /presets validates the slug server-side; this is just a
    // client-side hint to nudge operators toward a valid value before
    // a round-trip. The server is the source of truth for both slug
    // shape and uniqueness.
    const raw = window.prompt(
      'Preset slug (filename, [a-z0-9_]{1,32}):',
      suggestion,
    );
    if (raw === null) return null;
    return raw.trim();
  }

  async function _saveAsNew(form, status) {
    const slug = _slugFromPrompt('');
    if (!slug) return;
    const label = window.prompt('Operator-facing label:', slug);
    if (label === null) return;
    const body = {
      name: slug,
      label: label,
      hsv: _readHSV(form),
      shape_gate: _readShape(form),
    };
    if (status) status.textContent = '…';
    try {
      const r = await fetch('/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.status === 409) {
        // Duplicate name — surface the message verbatim and let the
        // operator pick a different slug. No automatic retry; doing so
        // would silently overwrite the operator's prior name choice.
        const t = await r.text();
        if (status) status.textContent = `duplicate name: ${t.slice(0, 200)}`;
        return;
      }
      if (!r.ok) {
        const t = await r.text();
        if (status) status.textContent = `save error: ${t.slice(0, 200)}`;
        return;
      }
      window.location.reload();
    } catch (e) {
      if (status) status.textContent = `network error: ${e}`;
    }
  }

  function _setModalStatus(modal, msg) {
    const el = modal.querySelector('[data-preset-modal-status]');
    if (el) el.textContent = msg;
  }

  async function _useFromLibrary(name, modal) {
    _setModalStatus(modal, '…');
    try {
      const r = await fetch('/presets/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) {
        const t = await r.text();
        _setModalStatus(modal, `use error: ${t.slice(0, 200)}`);
        return;
      }
      window.location.reload();
    } catch (e) {
      _setModalStatus(modal, `network error: ${e}`);
    }
  }

  async function _duplicate(name, modal) {
    const slug = _slugFromPrompt(`${name}_copy`);
    if (!slug) return;
    _setModalStatus(modal, '…');
    try {
      // Read source preset, then POST a new file under the new slug.
      // No server-side duplicate endpoint — keeping the API surface
      // small; the round-trip cost is one extra GET per duplicate.
      const src = await fetch(`/presets/${encodeURIComponent(name)}`);
      if (!src.ok) {
        _setModalStatus(modal, `read error: ${src.status}`);
        return;
      }
      const srcBody = await src.json();
      const label = window.prompt('Label for the duplicate:', `${srcBody.label} (copy)`);
      if (label === null) return;
      const r = await fetch('/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: slug,
          label: label,
          hsv: srcBody.hsv,
          shape_gate: srcBody.shape_gate,
        }),
      });
      if (!r.ok) {
        const t = await r.text();
        _setModalStatus(modal, `duplicate error: ${t.slice(0, 200)}`);
        return;
      }
      window.location.reload();
    } catch (e) {
      _setModalStatus(modal, `network error: ${e}`);
    }
  }

  async function _deletePreset(name, modal) {
    if (!window.confirm(`Delete preset "${name}"? Built-in seeds re-create on restart.`)) {
      return;
    }
    _setModalStatus(modal, '…');
    try {
      const r = await fetch(`/presets/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      });
      if (r.status === 409) {
        // Active preset — surface the message and prompt the operator
        // to switch active first. Deleting the active would leave the
        // detection config dangling; the route enforces the invariant.
        const t = await r.text();
        _setModalStatus(modal, `cannot delete active: ${t.slice(0, 200)}`);
        return;
      }
      if (!r.ok) {
        const t = await r.text();
        _setModalStatus(modal, `delete error: ${t.slice(0, 200)}`);
        return;
      }
      window.location.reload();
    } catch (e) {
      _setModalStatus(modal, `network error: ${e}`);
    }
  }

  function _initPresetLibraryControls(form, status) {
    const saveBtn = document.querySelector('[data-preset-save-as]');
    if (saveBtn) {
      saveBtn.addEventListener('click', () => _saveAsNew(form, status));
    }
    const modal = document.getElementById('preset-manage-modal');
    const manageBtn = document.querySelector('[data-preset-manage]');
    if (manageBtn && modal && typeof modal.showModal === 'function') {
      manageBtn.addEventListener('click', () => modal.showModal());
    }
    if (modal) {
      const closeBtn = modal.querySelector('[data-preset-modal-close]');
      if (closeBtn) closeBtn.addEventListener('click', () => modal.close());
      modal.querySelectorAll('[data-preset-use]').forEach((b) => {
        b.addEventListener('click', () => _useFromLibrary(b.dataset.presetUse, modal));
      });
      modal.querySelectorAll('[data-preset-duplicate]').forEach((b) => {
        b.addEventListener('click', () => _duplicate(b.dataset.presetDuplicate, modal));
      });
      modal.querySelectorAll('[data-preset-delete]').forEach((b) => {
        b.addEventListener('click', () => _deletePreset(b.dataset.presetDelete, modal));
      });
    }
  }
