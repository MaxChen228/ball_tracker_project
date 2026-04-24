// === audio cues + degraded banner ===

  // ------ Audio cues (opt-in via localStorage toggle) --------------------
  let audioCtx = null;
  function audioEnabled() {
    try { return localStorage.getItem('ball_tracker_audio_cues') === '1'; } catch { return false; }
  }
  function playCue(kind) {
    if (!audioEnabled()) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const freq = kind === 'armed' ? 220 : kind === 'ended' ? 440 : 150;
      const durS = kind === 'degraded' ? 0.2 : 0.08;
      osc.frequency.value = freq;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0.12, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + durS);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + durS);
    } catch (_) {}
  }

  // ------ Degraded banner: WS lost > grace window on an armed cam ---------
  let lastDegradedState = false;
  function updateDegradedBanner() {
    const banner = document.getElementById('degraded-banner');
    if (!banner) return;
    const now = Date.now();
    const armed = currentLiveSession && currentLiveSession.armed;
    const stale = [];
    for (const [cam, st] of wsStatus) {
      if (!st.connected && now - st.since_ms > WS_GRACE_MS) stale.push(cam);
    }
    const degraded = armed && stale.length > 0;
    if (degraded) {
      banner.style.display = 'flex';
      banner.querySelector('[data-degraded-body]').textContent =
        `Cam ${stale.join(', ')} WebSocket lost — falling back to post-pass. Next session will be 2-8s latency.`;
    } else {
      banner.style.display = 'none';
    }
    if (degraded && !lastDegradedState) playCue('degraded');
    lastDegradedState = degraded;
  }
