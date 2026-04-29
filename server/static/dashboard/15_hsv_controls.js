// === HSV controls init ===
  function initHSVControls() {
    const form = document.getElementById('hsv-form');
    if (!form) return;
    const syncField = (key, value) => {
      const range = form.querySelector(`[data-hsv-range="${key}"]`);
      const number = form.querySelector(`[data-hsv-number="${key}"]`);
      if (range) range.value = String(value);
      if (number) number.value = String(value);
    };
    form.querySelectorAll('[data-hsv-range]').forEach((input) => {
      input.addEventListener('input', () => syncField(input.dataset.hsvRange, input.value));
    });
    form.querySelectorAll('[data-hsv-number]').forEach((input) => {
      input.addEventListener('input', () => syncField(input.dataset.hsvNumber, input.value));
    });
    form.querySelectorAll('[data-hsv-preset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        syncField('h_min', btn.dataset.hMin);
        syncField('h_max', btn.dataset.hMax);
        syncField('s_min', btn.dataset.sMin);
        syncField('s_max', btn.dataset.sMax);
        syncField('v_min', btn.dataset.vMin);
        syncField('v_max', btn.dataset.vMax);
      });
    });
  }

  // === Shape gate controls init ===
  // Two-way bind slider (0-100 int) ↔ number (0.00-1.00). Submit is
  // a plain form POST — server returns 303 back to '/' just like HSV.
  function initShapeGateControls() {
    const form = document.getElementById('shape-gate-form');
    if (!form) return;
    form.querySelectorAll('[data-shape-range]').forEach((slider) => {
      slider.addEventListener('input', () => {
        const key = slider.dataset.shapeRange;
        const num = form.querySelector(`[data-shape-number="${key}"]`);
        if (num) num.value = (Number(slider.value) / 100).toFixed(2);
      });
    });
    form.querySelectorAll('[data-shape-number]').forEach((num) => {
      num.addEventListener('input', () => {
        const key = num.dataset.shapeNumber;
        const slider = form.querySelector(`[data-shape-range="${key}"]`);
        const val = Math.max(0, Math.min(1, Number(num.value) || 0));
        if (slider) slider.value = String(Math.round(val * 100));
      });
    });
  }

  // === Candidate selector controls init ===
  // Two-way bind for w_size / w_aspect / w_fill (slider 0-100 ↔ number
  // 0.00-1.00). r_px_expected is number-only. Submit posts to
  // /detection/candidate_selector — shape-prior cost (no temporal).
  function initCandidateSelectorControls() {
    const form = document.getElementById('candidate-selector-form');
    if (!form) return;
    form.querySelectorAll('[data-cs-range]').forEach((slider) => {
      slider.addEventListener('input', () => {
        const key = slider.dataset.csRange;
        const num = form.querySelector(`[data-cs-number="${key}"]`);
        if (num) num.value = (Number(slider.value) / 100).toFixed(2);
      });
    });
    form.querySelectorAll('[data-cs-number]').forEach((num) => {
      num.addEventListener('input', () => {
        const key = num.dataset.csNumber;
        const slider = form.querySelector(`[data-cs-range="${key}"]`);
        if (!slider) return;
        const val = Math.max(0, Math.min(1, Number(num.value) || 0));
        slider.value = String(Math.round(val * 100));
      });
    });
  }
