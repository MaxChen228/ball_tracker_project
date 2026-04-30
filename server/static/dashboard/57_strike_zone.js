// === strike-zone settings card ===

  let strikeZoneDom = null;
  let currentStrikeZone = null;
  let strikeZoneDirty = false;
  let lastStrikeZoneSig = null;

  function _strikeZoneSig(zone) {
    return zone ? JSON.stringify(zone) : '';
  }

  function buildStrikeZoneDom() {
    const form = document.querySelector('[data-strike-zone-form]');
    if (!form) return null;
    const dom = {
      form,
      range: form.querySelector('[data-strike-zone-range]'),
      number: form.querySelector('[data-strike-zone-number]'),
      status: form.querySelector('[data-strike-zone-status]'),
      apply: form.querySelector('[data-strike-zone-apply]'),
      bottom: form.querySelector('[data-strike-zone-bottom]'),
      top: form.querySelector('[data-strike-zone-top]'),
      height: form.querySelector('[data-strike-zone-height]'),
      width: form.querySelector('[data-strike-zone-width]'),
      depth: form.querySelector('[data-strike-zone-depth]'),
    };
    if (!dom.range || !dom.number || !dom.status || !dom.apply) return null;
    const sync = (value, source) => {
      const text = String(value);
      if (source !== dom.range && dom.range.value !== text) dom.range.value = text;
      if (source !== dom.number && dom.number.value !== text) dom.number.value = text;
      strikeZoneDirty = !currentStrikeZone || Number(value) !== Number(currentStrikeZone.batter_height_cm);
      dom.status.textContent = strikeZoneDirty ? 'Modified' : '';
    };
    dom.range.addEventListener('input', () => sync(dom.range.value, dom.range));
    dom.number.addEventListener('input', () => sync(dom.number.value, dom.number));
    dom.form.addEventListener('submit', onStrikeZoneSubmit);
    strikeZoneDom = dom;
    return dom;
  }

  function strikeZoneDomRef() {
    return strikeZoneDom || buildStrikeZoneDom();
  }

  function setStrikeZoneReadouts(zone) {
    const dom = strikeZoneDomRef();
    if (!dom || !zone) return;
    dom.bottom.textContent = `${Number(zone.z_bottom_m).toFixed(3)} m`;
    dom.top.textContent = `${Number(zone.z_top_m).toFixed(3)} m`;
    dom.height.textContent = `${Number(zone.z_height_m).toFixed(3)} m`;
    dom.width.textContent = `${(Number(zone.x_half_m) * 2).toFixed(3)} m`;
    dom.depth.textContent = `${(Number(zone.y_back_m) - Number(zone.y_front_m)).toFixed(3)} m`;
  }

  function maybeApplyStrikeZoneToScene(zone) {
    const sig = _strikeZoneSig(zone);
    if (!zone || sig === lastStrikeZoneSig) return;
    lastStrikeZoneSig = sig;
    if (window.BallTrackerScene && typeof window.BallTrackerScene.setStrikeZone === 'function') {
      window.BallTrackerScene.setStrikeZone(zone);
    }
  }

  function renderStrikeZone(state, opts = {}) {
    const dom = strikeZoneDomRef();
    const zone = state && state.strike_zone ? state.strike_zone : null;
    if (!dom || !zone) return;
    currentStrikeZone = zone;
    setStrikeZoneReadouts(zone);
    if (!strikeZoneDirty || opts.forceInputs) {
      const text = String(zone.batter_height_cm);
      if (dom.range.value !== text) dom.range.value = text;
      if (dom.number.value !== text) dom.number.value = text;
      strikeZoneDirty = false;
      if (!opts.preserveStatus) dom.status.textContent = '';
    }
    maybeApplyStrikeZoneToScene(zone);
  }

  async function onStrikeZoneSubmit(evt) {
    evt.preventDefault();
    const dom = strikeZoneDomRef();
    if (!dom) return;
    const height = Number.parseInt(dom.number.value, 10);
    dom.apply.disabled = true;
    dom.status.textContent = 'Applying…';
    try {
      const r = await fetch('/settings/strike_zone', {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ height_cm: height }),
      });
      let payload = {};
      try { payload = await r.json(); } catch (_) {}
      if (!r.ok) {
        const detail = payload && payload.detail ? payload.detail : `HTTP ${r.status}`;
        dom.status.textContent = String(detail);
        return;
      }
      strikeZoneDirty = false;
      renderStrikeZone({ strike_zone: payload.strike_zone }, { forceInputs: true });
      dom.status.textContent = 'Applied';
    } catch (e) {
      dom.status.textContent = e && e.message ? e.message : 'Network error';
    } finally {
      dom.apply.disabled = false;
    }
  }
