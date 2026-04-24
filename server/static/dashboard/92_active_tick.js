// === tickActiveSession ===

  // 10 Hz tick for the time-sensitive active-session fields (elapsed
  // counter + last-point-age). Cheaper than re-rendering the whole card
  // on every SSE event, and ensures the "stale" flag trips within 100 ms
  // of the 200 ms threshold being crossed.
  function tickActiveSession() {
    if (!currentLiveSession || !currentLiveSession.armed) return;
    const elapsedEl = activeBox && activeBox.querySelector('[data-elapsed]');
    if (elapsedEl && currentLiveSession.armed_at_ms) {
      elapsedEl.textContent = fmtElapsed(Date.now() - currentLiveSession.armed_at_ms);
    }
    // Re-evaluate stale flag without a full re-render
    const pairsEl = activeBox && activeBox.querySelector('.live-pairs');
    if (pairsEl && currentLiveSession.last_point_at_ms) {
      const age = Date.now() - currentLiveSession.last_point_at_ms;
      pairsEl.classList.toggle('stale', age > 200);
    }
  }
