// === Detection-config card (phase 3 of unified-config redesign) ===
//
// One form, one Apply button, atomic POST to /detection/config. Sliders
// edit local form state only — they no longer hit the server on every
// drag. Preset buttons load a preset's HSV + shape gate values into
// the form, but DO NOT apply server-side until the operator clicks
// Apply. Reset-to-preset is a server-side snap that also reloads the
// page so the SSR identity header refreshes.

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

  // Manual slider/number edit clears any pending preset-identity claim
  // — once the operator nudges a value, the form no longer matches the
  // preset they clicked, so Apply must send preset=null (custom).
  // Without this, a click-Tennis → drag-h_min → Apply request would
  // claim preset=tennis with non-tennis values, get rejected by the
  // server's strict identity validation (400), and the operator has
  // to click the same Apply again. Drop the claim eagerly for a clean
  // single-click recovery.
  function _clearPendingPreset(form) {
    delete form.dataset.pendingPreset;
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

    // HSV slider <-> number two-way bind. Manual edit clears any
    // pending preset-identity claim (see _clearPendingPreset).
    form.querySelectorAll('[data-hsv-range]').forEach((input) => {
      input.addEventListener('input', () => {
        _syncHSVField(form, input.dataset.hsvRange, input.value);
        _clearPendingPreset(form);
      });
    });
    form.querySelectorAll('[data-hsv-number]').forEach((input) => {
      input.addEventListener('input', () => {
        _syncHSVField(form, input.dataset.hsvNumber, input.value);
        _clearPendingPreset(form);
      });
    });

    // Shape-gate slider (0..100) <-> number (0..1).
    form.querySelectorAll('[data-shape-range]').forEach((slider) => {
      slider.addEventListener('input', () => {
        _syncShape(form,slider.dataset.shapeRange, Number(slider.value) / 100);
        _clearPendingPreset(form);
      });
    });
    form.querySelectorAll('[data-shape-number]').forEach((num) => {
      num.addEventListener('input', () => {
        _syncShape(form,num.dataset.shapeNumber, Number(num.value));
        _clearPendingPreset(form);
      });
    });

    // Preset button: load HSV + shape-gate values into the form. Does
    // NOT apply server-side — operator confirms with Apply.
    document.querySelectorAll('[data-hsv-preset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        _syncHSVField(form, 'h_min', btn.dataset.hMin);
        _syncHSVField(form, 'h_max', btn.dataset.hMax);
        _syncHSVField(form, 's_min', btn.dataset.sMin);
        _syncHSVField(form, 's_max', btn.dataset.sMax);
        _syncHSVField(form, 'v_min', btn.dataset.vMin);
        _syncHSVField(form, 'v_max', btn.dataset.vMax);
        _syncShape(form,'aspect_min', btn.dataset.aspectMin);
        _syncShape(form,'fill_min', btn.dataset.fillMin);
        // Stash the chosen preset name so Apply can claim identity.
        form.dataset.pendingPreset = btn.dataset.hsvPreset;
      });
    });

    // Apply button: POST /detection/config with the full triple. The
    // identity claim (`preset`) is only sent if the operator just
    // clicked a preset button and hasn't dragged anything since —
    // otherwise we send `preset: null` and the server records custom.
    form.addEventListener('submit', async (evt) => {
      evt.preventDefault();
      const status = form.querySelector('[data-detection-apply-status]');
      const presetClaim = form.dataset.pendingPreset || null;
      const body = {
        hsv: _readHSV(form),
        shape_gate: _readShape(form),
        preset: presetClaim,
      };
      if (status) status.textContent = '…';
      try {
        const r = await fetch('/detection/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          const t = await r.text();
          if (status) status.textContent = `error: ${t.slice(0, 200)}`;
          return;
        }
        // Clear pending preset claim so subsequent Apply (after manual
        // edits) defaults back to `preset: null`.
        delete form.dataset.pendingPreset;
        // Reload so the SSR identity header re-renders against the
        // new state (active preset / modified flag / etc.).
        window.location.reload();
      } catch (e) {
        if (status) status.textContent = `network error: ${e}`;
      }
    });

    // Reset-to-preset button (only present when current state has
    // modified_fields, see render_dashboard_session._render_hsv_body).
    // Errors surface in the same status node as Apply so a failed
    // reset doesn't silently no-op.
    const status = form.querySelector('[data-detection-apply-status]');
    document.querySelectorAll('[data-detection-reset-preset]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const presetName = btn.dataset.detectionResetPreset;
        if (status) status.textContent = '…';
        try {
          const r = await fetch('/detection/config/reset_to_preset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset: presetName }),
          });
          if (!r.ok) {
            const t = await r.text();
            if (status) status.textContent = `reset error: ${t.slice(0, 200)}`;
            return;
          }
          window.location.reload();
        } catch (e) {
          if (status) status.textContent = `network error: ${e}`;
        }
      });
    });
  }
