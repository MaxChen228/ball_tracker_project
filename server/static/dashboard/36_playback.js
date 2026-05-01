// === dashboard 3D-only playback ===
//
// The dashboard transport scrubs only the 3D ball marker for the
// selected session/path. Full video-synchronised playback remains the
// viewer's job.

  const dashPlayback = {
    key: null,
    range: null,
    t: 0,
    playing: false,
    rate: 1.0,
    raf: null,
    lastMs: 0,
  };

  const dp = {
    root: document.getElementById('dash-playback-bar'),
    play: document.getElementById('dash-playback-play'),
    scrub: document.getElementById('dash-playback-scrub'),
    time: document.getElementById('dash-playback-time'),
    all: document.getElementById('dash-playback-all'),
    rates: Array.from(document.querySelectorAll('[data-dash-playback-rate]')),
  };

  function _playbackRange(view) {
    if (!view) return null;
    let minT = Infinity;
    let maxT = -Infinity;
    for (const seg of view.segments || []) {
      if (Number.isFinite(seg.t_start)) minT = Math.min(minT, seg.t_start);
      if (Number.isFinite(seg.t_end)) maxT = Math.max(maxT, seg.t_end);
    }
    for (const p of view.points || []) {
      if (Number.isFinite(p.t_rel_s)) {
        minT = Math.min(minT, p.t_rel_s);
        maxT = Math.max(maxT, p.t_rel_s);
      }
    }
    if (!Number.isFinite(minT) || !Number.isFinite(maxT) || maxT < minT) return null;
    if (maxT === minT) maxT = minT + 1 / 240;
    return { min: minT, max: maxT };
  }

  function _fmtPlaybackTime(t) {
    const r = dashPlayback.range;
    if (!r) return '—';
    return `${(t - r.min).toFixed(3)} / ${(r.max - r.min).toFixed(3)}s`;
  }

  function _setPlaybackControlsEnabled(on) {
    if (!dp.play || !dp.scrub || !dp.all) return;
    dp.play.disabled = !on;
    dp.scrub.disabled = !on;
    dp.all.disabled = !on;
  }

  function _paintPlaybackControls() {
    if (!dp.root) return;
    const hasRange = !!dashPlayback.range;
    _setPlaybackControlsEnabled(hasRange);
    if (dp.play) dp.play.textContent = dashPlayback.playing ? 'Pause' : 'Play';
    if (dp.time) dp.time.textContent = hasRange ? _fmtPlaybackTime(dashPlayback.t) : '—';
    if (dp.scrub && hasRange) {
      dp.scrub.min = String(dashPlayback.range.min);
      dp.scrub.max = String(dashPlayback.range.max);
      dp.scrub.value = String(dashPlayback.t);
    }
    for (const b of dp.rates) {
      const r = Number(b.dataset.dashPlaybackRate);
      b.classList.toggle('active', Math.abs(r - dashPlayback.rate) < 1e-6);
    }
  }

  function _stopDashboardPlayback() {
    dashPlayback.playing = false;
    if (dashPlayback.raf !== null) cancelAnimationFrame(dashPlayback.raf);
    dashPlayback.raf = null;
    _paintPlaybackControls();
  }

  function _setDashboardPlaybackTime(t) {
    const r = dashPlayback.range;
    if (!r) return;
    dashPlayback.t = Math.max(r.min, Math.min(r.max, t));
    if (dp.scrub) dp.scrub.value = String(dashPlayback.t);
    if (dp.time) dp.time.textContent = _fmtPlaybackTime(dashPlayback.t);
    if (window.BallTrackerDashboardScene) {
      window.BallTrackerDashboardScene.setPlaybackTime(dashPlayback.t);
    }
  }

  function _startDashboardPlayback() {
    if (!dashPlayback.range || dashPlayback.playing) return;
    dashPlayback.playing = true;
    dashPlayback.lastMs = performance.now();
    const tick = (now) => {
      if (!dashPlayback.playing || !dashPlayback.range) return;
      const dt = (now - dashPlayback.lastMs) / 1000;
      dashPlayback.lastMs = now;
      const next = dashPlayback.t + dt * dashPlayback.rate;
      if (next >= dashPlayback.range.max) {
        _setDashboardPlaybackTime(dashPlayback.range.max);
        _stopDashboardPlayback();
        return;
      }
      _setDashboardPlaybackTime(next);
      dashPlayback.raf = requestAnimationFrame(tick);
    };
    dashPlayback.raf = requestAnimationFrame(tick);
    _paintPlaybackControls();
  }

  function _setDashboardPlaybackAllMode() {
    _stopDashboardPlayback();
    if (window.BallTrackerDashboardScene) {
      window.BallTrackerDashboardScene.setPlaybackMode('all');
    }
  }

  function syncDashboardPlayback(sid, view) {
    const path = view && view.path ? view.path : '';
    const key = sid && view ? `${sid}:${path}` : null;
    const range = _playbackRange(view);
    if (!key || !range) {
      _stopDashboardPlayback();
      dashPlayback.key = null;
      dashPlayback.range = null;
      dashPlayback.t = 0;
      if (window.BallTrackerDashboardScene) {
        window.BallTrackerDashboardScene.setPlaybackMode('all');
      }
      _paintPlaybackControls();
      return;
    }
    if (dashPlayback.key !== key) {
      _stopDashboardPlayback();
      dashPlayback.key = key;
      dashPlayback.range = range;
      dashPlayback.t = range.min;
      if (window.BallTrackerDashboardScene) {
        window.BallTrackerDashboardScene.setPlaybackMode('all');
      }
    } else {
      dashPlayback.range = range;
      dashPlayback.t = Math.max(range.min, Math.min(range.max, dashPlayback.t));
    }
    _paintPlaybackControls();
  }

  if (dp.play) {
    dp.play.addEventListener('click', () => {
      if (!dashPlayback.range) return;
      if (dashPlayback.playing) _stopDashboardPlayback();
      else {
        if (dashPlayback.t >= dashPlayback.range.max) {
          _setDashboardPlaybackTime(dashPlayback.range.min);
        }
        _startDashboardPlayback();
      }
    });
  }
  if (dp.scrub) {
    dp.scrub.addEventListener('input', () => {
      _stopDashboardPlayback();
      _setDashboardPlaybackTime(Number(dp.scrub.value));
    });
  }
  if (dp.all) {
    dp.all.addEventListener('click', () => _setDashboardPlaybackAllMode());
  }
  for (const b of dp.rates) {
    b.addEventListener('click', () => {
      const r = Number(b.dataset.dashPlaybackRate);
      if (!Number.isFinite(r) || r <= 0) return;
      dashPlayback.rate = r;
      _paintPlaybackControls();
    });
  }
