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
