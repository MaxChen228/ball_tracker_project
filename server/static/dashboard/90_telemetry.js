// === telemetry panel ===

  // ------ Telemetry panel -------------------------------------------------
  // Collapsible debug overlay bottom-left of canvas. Operator rarely looks
  // at it — it's a diagnostic when "feels slow" needs an evidence trail.
  // All metrics are derived client-side from existing SSE + /status signals;
  // no new server endpoints required.
  function percentile(arr, p) {
    if (!arr.length) return null;
    const sorted = [...arr].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor(sorted.length * p)));
    return sorted[idx];
  }

  function drawTelemetrySpark(canvas, values, maxVal) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width = canvas.clientWidth;
    const H = canvas.height = canvas.clientHeight;
    ctx.clearRect(0, 0, W, H);
    if (!values || values.length < 2) return;
    const maxY = maxVal !== undefined ? maxVal : Math.max(1, ...values);
    ctx.strokeStyle = '#4A6B8C';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = (i / (values.length - 1)) * W;
      const y = H - (Math.max(0, v) / maxY) * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function sessionPathMatrix() {
    // Derived from the events list (most recent 10). Each cell shows
    // whether a given path completed for that session.
    const cells = [];
    const list = (currentEvents || []).slice(0, 10);
    for (const ev of list) {
      const paths = new Set(ev.paths_completed || []);
      cells.push({
        sid: ev.session_id,
        live: paths.has('live'),
        srv: paths.has('server_post'),
      });
    }
    return cells;
  }

  function renderTelemetry() {
    const box = document.getElementById('telemetry-body');
    if (!box) return;
    // Per-cam fps sparkline — reuse frame_samples on currentLiveSession
    const camRow = (cam) => {
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      const avg = fps.length ? fps.reduce((a,b)=>a+b,0) / fps.length : 0;
      const min = fps.length ? Math.min(...fps) : 0;
      return `
        <div class="tel-row">
          <span class="k">${cam} fps</span>
          <canvas class="tel-spark" data-tel-spark="${cam}"></canvas>
          <span class="v">avg ${avg.toFixed(0)} · min ${min.toFixed(0)}</span>
        </div>`;
    };
    // Pair rate: trailing-window count of pair timestamps over 1s
    const nowMs = Date.now();
    const pairsLast1s = pairTimestamps.filter(t => nowMs - t <= 1000).length;
    const pairsAvg = pairTimestamps.length / Math.max(1, TELEMETRY_WINDOW_MS / 1000);
    // Latency stats aggregated across cams
    const allLat = [];
    for (const cam of ['A','B']) {
      for (const s of (latencySamples[cam] || [])) allLat.push(s.latency);
    }
    const p50 = percentile(allLat, 0.50);
    const p95 = percentile(allLat, 0.95);
    const maxLat = allLat.length ? Math.max(...allLat) : null;
    const latTxt = p50 === null
      ? '—'
      : `p50 ${p50.toFixed(0)}ms · p95 ${p95.toFixed(0)}ms · max ${maxLat.toFixed(0)}ms`;
    // Path completion matrix
    const matrix = sessionPathMatrix();
    const matrixHtml = matrix.length
      ? matrix.map(c => `<span class="tel-cell" title="${esc(c.sid)}">${c.live?'L':'·'}${c.ios?'i':'·'}${c.srv?'s':'·'}</span>`).join('')
      : '<span class="tel-none">no sessions yet</span>';
    // Errors
    const errHtml = errorLog.length
      ? errorLog.map(e => {
          const ts = new Date(e.t_ms).toLocaleTimeString();
          return `<div class="tel-err"><span class="t">${ts}</span> <span class="msg">${esc(e.message)}</span></div>`;
        }).join('')
      : '<span class="tel-none">none</span>';
    box.innerHTML = `
      ${camRow('A')}
      ${camRow('B')}
      <div class="tel-row">
        <span class="k">Pairs</span>
        <span class="v">${pairsLast1s}/s · avg ${pairsAvg.toFixed(1)}/s</span>
      </div>
      <div class="tel-row">
        <span class="k">WS latency</span>
        <span class="v">${latTxt}</span>
      </div>
      <div class="tel-block">
        <span class="k">Last 10 sessions (L/i/s)</span>
        <div class="tel-matrix">${matrixHtml}</div>
      </div>
      <div class="tel-block">
        <span class="k">Errors</span>
        <div class="tel-errors">${errHtml}</div>
      </div>`;
    // Draw sparklines after DOM replacement
    ['A','B'].forEach(cam => {
      const canvas = box.querySelector(`[data-tel-spark="${cam}"]`);
      const samples = ((currentLiveSession && currentLiveSession.frame_samples) || {})[cam] || [];
      const fps = [];
      for (let i = 1; i < samples.length; i++) {
        const dtS = Math.max(0.001, (samples[i].t - samples[i - 1].t) / 1000);
        fps.push((samples[i].count - samples[i - 1].count) / dtS);
      }
      drawTelemetrySpark(canvas, fps, 240);
    });
  }
