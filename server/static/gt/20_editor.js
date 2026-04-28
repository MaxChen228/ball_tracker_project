/* Editor panel — single-cam scrubber + range handles + add to queue.
 *
 * Auto-fill default: selecting a row + cam pre-populates the range
 * from frames_live (px ≠ None bracket), clamped to [0, video_duration].
 * Operator only touches the timeline / number inputs when auto range
 * is wrong.
 *
 * Mark in/out keys: `[`, `]`, `\` set start/end at currentTime / clear.
 * Listener checks event.target so typing in prompt input doesn't leak.
 */
(function () {
  const elTitle = document.getElementById('gt-editor-title');
  const elCamToggle = document.getElementById('gt-cam-toggle');
  const elVideoWrap = document.getElementById('gt-video-wrap');
  const elVideo = document.getElementById('gt-video');
  const elVideoMeta = document.getElementById('gt-video-meta');
  const elTimeline = document.getElementById('gt-timeline');
  const elTimelineSvg = document.getElementById('gt-timeline-svg');
  const elTimelineHint = document.getElementById('gt-timeline-hint');
  const elRangeRow = document.getElementById('gt-range-row');
  const elRangeStart = document.getElementById('gt-range-start');
  const elRangeEnd = document.getElementById('gt-range-end');
  const elPrompt = document.getElementById('gt-prompt');
  const elAddRow = document.getElementById('gt-add-row');
  const elAddBtn = document.getElementById('gt-add-btn');
  const elAddError = document.getElementById('gt-add-error');
  const elOverwriteWarn = document.getElementById('gt-overwrite-warn');
  const elDetailActions = document.getElementById('gt-detail-actions');
  const elValidateBtn = document.getElementById('gt-validate-btn');
  const elReportLink = document.getElementById('gt-report-link');
  const elSkipBtn = document.getElementById('gt-skip-btn');
  const elUnskipBtn = document.getElementById('gt-unskip-btn');
  const elEmptyHint = document.getElementById('gt-empty-hint');

  function selectedSessionState() {
    const sid = window.GT.selected.sid;
    if (!sid) return null;
    return window.GT.sessions.find((s) => s.session_id === sid) || null;
  }

  function camDuration(s, cam) {
    return s && s.video_duration_s ? s.video_duration_s[cam] : null;
  }

  function camMovUrl(sid, cam) {
    return `/videos/session_${sid}_${cam}.mov`;
  }

  function autoFillRange(s, cam) {
    const tFirst = s.t_first_video_rel ? s.t_first_video_rel[cam] : null;
    const tLast = s.t_last_video_rel ? s.t_last_video_rel[cam] : null;
    if (tFirst == null || tLast == null) {
      window.GT.editor.rangeStart = null;
      window.GT.editor.rangeEnd = null;
      return;
    }
    const dur = camDuration(s, cam) || (tLast + 0.1);
    const start = Math.max(0, tFirst - 0.1);
    const end = Math.min(dur, tLast + 0.1);
    window.GT.editor.rangeStart = start;
    window.GT.editor.rangeEnd = end;
  }

  function updateInputsFromState() {
    elRangeStart.value = window.GT.editor.rangeStart != null
      ? window.GT.editor.rangeStart.toFixed(2) : '';
    elRangeEnd.value = window.GT.editor.rangeEnd != null
      ? window.GT.editor.rangeEnd.toFixed(2) : '';
    elPrompt.value = window.GT.editor.prompt;
    const valid = window.GT.editor.rangeStart != null
      && window.GT.editor.rangeEnd != null
      && window.GT.editor.rangeStart < window.GT.editor.rangeEnd
      && window.GT.editor.prompt.trim().length > 0;
    elAddBtn.disabled = !valid;
  }

  function fetchHeatmapAndDraw(sid, cam) {
    const key = `${sid}/${cam}`;
    if (window.GT.heatmap[key]) {
      drawHeatmap(window.GT.heatmap[key]);
      return;
    }
    fetch(`/gt/timeline/${sid}/${cam}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data) => {
        window.GT.heatmap[key] = data;
        drawHeatmap(data);
      })
      .catch(() => {
        elTimelineHint.textContent = 'no detections — manual range';
        elTimelineSvg.innerHTML = '';
      });
  }

  function drawHeatmap(data) {
    const buckets = data.buckets || [];
    const maxCount = Math.max(1, ...buckets);
    const dur = data.duration_s || 1.0;
    const w = elTimeline.clientWidth || 600;
    const h = 60;
    const colorH = 40; // soft amber
    let svg = `<rect x="0" y="0" width="${w}" height="${h}" fill="var(--surface)"/>`;
    buckets.forEach((c, i) => {
      const t0 = i * data.bucket_size_s;
      const x = (t0 / dur) * w;
      const bw = (data.bucket_size_s / dur) * w;
      const intensity = c / maxCount;
      svg += `<rect x="${x.toFixed(2)}" y="0" width="${bw.toFixed(2)}" height="${h}"
        fill="hsl(${colorH}, 60%, ${(80 - intensity * 50).toFixed(0)}%)"
        opacity="${(0.2 + intensity * 0.7).toFixed(2)}"/>`;
    });
    // range overlay
    const rs = window.GT.editor.rangeStart;
    const re = window.GT.editor.rangeEnd;
    if (rs != null && re != null) {
      const xs = (rs / dur) * w;
      const xe = (re / dur) * w;
      svg += `<rect x="${xs}" y="0" width="${xe - xs}" height="${h}"
        fill="var(--ink)" opacity="0.10"/>`;
      svg += `<line x1="${xs}" x2="${xs}" y1="0" y2="${h}" stroke="var(--ink)" stroke-width="2"/>`;
      svg += `<line x1="${xe}" x2="${xe}" y1="0" y2="${h}" stroke="var(--ink)" stroke-width="2"/>`;
    }
    elTimelineSvg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    elTimelineSvg.innerHTML = svg;
    elTimelineHint.textContent = data.source === 'empty'
      ? 'no detections — manual range'
      : '';
  }

  function renderEditor() {
    const sid = window.GT.selected.sid;
    if (!sid) {
      elTitle.textContent = '← pick a session';
      elCamToggle.hidden = true;
      elVideoWrap.hidden = true;
      elTimeline.hidden = true;
      elRangeRow.hidden = true;
      elAddRow.hidden = true;
      elDetailActions.hidden = true;
      elEmptyHint.hidden = false;
      return;
    }
    const s = selectedSessionState();
    if (!s) return;
    const cam = window.GT.selected.cam;
    const dets = (s.n_live_dets && s.n_live_dets[cam]) || 0;
    const dur = camDuration(s, cam);
    const durStr = dur != null ? `${dur.toFixed(2)}s` : '—';
    elTitle.textContent = `${sid} · ${dets} dets · ${durStr}`;

    elCamToggle.hidden = false;
    elVideoWrap.hidden = false;
    elTimeline.hidden = false;
    elRangeRow.hidden = false;
    elAddRow.hidden = false;
    elDetailActions.hidden = false;
    elEmptyHint.hidden = true;

    // cam toggle radio state
    document.querySelectorAll('input[name="gt-cam"]').forEach((el) => {
      el.checked = el.value === cam;
      const camMissing = !(s.has_mov && s.has_mov[el.value]);
      el.disabled = camMissing && !(s.cams_present && s.cams_present[el.value]);
    });

    // video src — only touch when sid/cam actually changed (mini-plan
    // v4 reviewer N6: tick handlers must NOT trip src reload).
    const newSrc = camMovUrl(sid, cam);
    if (s.has_mov && s.has_mov[cam]) {
      if (elVideo.src !== window.location.origin + newSrc
          && !elVideo.src.endsWith(newSrc)) {
        elVideo.src = newSrc;
      }
    } else {
      elVideo.removeAttribute('src');
      elVideo.load();
    }
    elVideoMeta.textContent = `${dets} live detections${dur != null ? ' · ' + dur.toFixed(2) + 's' : ''}`;

    // skip / unskip
    elSkipBtn.hidden = !!s.is_skipped;
    elUnskipBtn.hidden = !s.is_skipped;
    // validate / report
    const aGt = !!(s.has_gt && s.has_gt.A);
    const bGt = !!(s.has_gt && s.has_gt.B);
    elValidateBtn.disabled = !(aGt || bGt);
    elReportLink.href = `/report/${sid}`;
    elReportLink.hidden = !(aGt || bGt);

    // overwrite warning
    const willOverwrite = !!(s.has_gt && s.has_gt[cam]);
    elOverwriteWarn.hidden = !willOverwrite;

    // range / prompt sync
    if (!window.GT.editor.dirty) {
      autoFillRange(s, cam);
      updateInputsFromState();
    }
    fetchHeatmapAndDraw(sid, cam);
  }

  // Cam toggle change
  elCamToggle.addEventListener('change', (e) => {
    if (e.target && e.target.name === 'gt-cam') {
      if (window.GT.editor.dirty) {
        const ok = window.confirm('未加入佇列的修改會丟失，確定切換 cam?');
        if (!ok) {
          e.target.checked = false;
          document.querySelector(`input[name="gt-cam"][value="${window.GT.selected.cam}"]`).checked = true;
          return;
        }
      }
      window.GT.selected.cam = e.target.value;
      window.GT.editor.dirty = false;
      renderEditor();
    }
  });

  // Range input changes
  function handleRangeInput() {
    const s = parseFloat(elRangeStart.value);
    const e = parseFloat(elRangeEnd.value);
    window.GT.editor.rangeStart = isNaN(s) ? null : s;
    window.GT.editor.rangeEnd = isNaN(e) ? null : e;
    window.GT.editor.dirty = true;
    drawHeatmapForCurrent();
    updateInputsFromState();
  }
  elRangeStart.addEventListener('input', handleRangeInput);
  elRangeEnd.addEventListener('input', handleRangeInput);
  elPrompt.addEventListener('input', () => {
    window.GT.editor.prompt = elPrompt.value;
    window.GT.editor.dirty = true;
    updateInputsFromState();
  });

  function drawHeatmapForCurrent() {
    if (!window.GT.selected.sid) return;
    const key = `${window.GT.selected.sid}/${window.GT.selected.cam}`;
    if (window.GT.heatmap[key]) drawHeatmap(window.GT.heatmap[key]);
  }

  // Mark in/out keys (target-guarded)
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (!window.GT.selected.sid) return;
    if (e.key === '[') {
      window.GT.editor.rangeStart = elVideo.currentTime;
      window.GT.editor.dirty = true;
      autoSwap();
      updateInputsFromState();
      drawHeatmapForCurrent();
    } else if (e.key === ']') {
      window.GT.editor.rangeEnd = elVideo.currentTime;
      window.GT.editor.dirty = true;
      autoSwap();
      updateInputsFromState();
      drawHeatmapForCurrent();
    } else if (e.key === '\\') {
      window.GT.editor.rangeStart = null;
      window.GT.editor.rangeEnd = null;
      window.GT.editor.dirty = true;
      updateInputsFromState();
      drawHeatmapForCurrent();
    } else if (e.key === ',') {
      const fps = 240; // approximation; precision not critical
      elVideo.currentTime = Math.max(0, elVideo.currentTime - 1 / fps);
    } else if (e.key === '.') {
      elVideo.currentTime = elVideo.currentTime + 1 / fps;
    }
  });

  function autoSwap() {
    const a = window.GT.editor.rangeStart;
    const b = window.GT.editor.rangeEnd;
    if (a != null && b != null && a > b) {
      window.GT.editor.rangeStart = b;
      window.GT.editor.rangeEnd = a;
    }
  }

  // Add to queue
  elAddBtn.addEventListener('click', async () => {
    elAddError.hidden = true;
    const sid = window.GT.selected.sid;
    const cam = window.GT.selected.cam;
    const start = window.GT.editor.rangeStart;
    const end = window.GT.editor.rangeEnd;
    const prompt = window.GT.editor.prompt.trim();
    if (sid == null || start == null || end == null || !prompt) return;

    if (elOverwriteWarn.hidden === false) {
      const ok = window.confirm('該 (session, cam) 已有 GT — 加入佇列會在 worker 跑完時覆蓋舊檔。確定?');
      if (!ok) return;
    }

    try {
      const r = await fetch('/gt/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sid,
          camera_id: cam,
          time_range: [start, end],
          prompt: prompt,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        elAddError.textContent = body.detail || `error ${r.status}`;
        elAddError.hidden = false;
        return;
      }
      window.GT.lastPrompt = prompt;
      window.GT.editor.dirty = false;
      // refresh queue immediately so the operator sees their item
      if (window.GT.tickQueue) window.GT.tickQueue();
    } catch (err) {
      elAddError.textContent = String(err);
      elAddError.hidden = false;
    }
  });

  // Validate
  elValidateBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    elValidateBtn.disabled = true;
    try {
      await fetch(`/sessions/${window.GT.selected.sid}/run_validation`, { method: 'POST' });
    } finally {
      // re-enabled by next tick render
    }
  });

  // Skip / unskip
  elSkipBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    if (!window.confirm(`Skip ${window.GT.selected.sid}? 之後只有 [show skipped] 看得到`)) return;
    await fetch(`/gt/sessions/${window.GT.selected.sid}/skip`, { method: 'POST' });
    if (window.GT.tickSessions) window.GT.tickSessions();
  });
  elUnskipBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    await fetch(`/gt/sessions/${window.GT.selected.sid}/unskip`, { method: 'POST' });
    if (window.GT.tickSessions) window.GT.tickSessions();
  });

  window.GT.render.editor = renderEditor;
})();
