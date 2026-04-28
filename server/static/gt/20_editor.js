/* Editor — single-cam scrubber + click-to-seed + range handles.
 *
 * SAM 2 era (2026-04-29): operator scrubs the <video> to the first
 * frame where the ball is clearly visible, clicks the ball, sets a
 * range end (start auto-fills to the click moment), and submits. The
 * worker spawns label_with_sam2.py, which seeds SAM 2 at the click
 * frame and propagates forward.
 *
 * Click coords: captured in CSS-px relative to the <video>'s bounding
 * rect, then scaled to image-pixel space via
 *     x_image = evt.offsetX * video.videoWidth / video.clientWidth
 * before storing in window.GT.editor.click. Server expects image-px.
 *
 * Mark in/out keys are gone — operators confirmed range_start = click
 * timestamp covers the common case. Range end = currentTime via a
 * dedicated button or numeric input. `event.target.tagName` guard is
 * preserved on the keyboard handlers to avoid leaking ',' / '.' frame
 * step into focused number inputs.
 */
(function () {
  const elTitle = document.getElementById('gt-editor-title');
  const elCamToggle = document.getElementById('gt-cam-toggle');
  const elClickHint = document.getElementById('gt-click-hint');
  const elVideoWrap = document.getElementById('gt-video-wrap');
  const elVideo = document.getElementById('gt-video');
  const elVideoMeta = document.getElementById('gt-video-meta');
  const elVideoPlay = document.getElementById('gt-video-play');
  const elVideoStepBack = document.getElementById('gt-video-step-back');
  const elVideoStepFwd = document.getElementById('gt-video-step-fwd');
  const elVideoTime = document.getElementById('gt-video-time');
  const elClickMarker = document.getElementById('gt-click-marker');
  const elTimeline = document.getElementById('gt-timeline');
  const elTimelineSvg = document.getElementById('gt-timeline-svg');
  const elTimelineHint = document.getElementById('gt-timeline-hint');
  const elRangeRow = document.getElementById('gt-range-row');
  const elRangeStart = document.getElementById('gt-range-start');
  const elRangeEnd = document.getElementById('gt-range-end');
  const elClickReadout = document.getElementById('gt-click-readout');
  const elAddRow = document.getElementById('gt-add-row');
  const elAddBtn = document.getElementById('gt-add-btn');
  const elAddError = document.getElementById('gt-add-error');
  const elOverwriteWarn = document.getElementById('gt-overwrite-warn');
  const elDetailActions = document.getElementById('gt-detail-actions');
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

  function autoFillRangeFromDetections(s, cam) {
    /* Auto-fill range FROM live-detection bracket only when the
       operator hasn't clicked yet. After a click we let the click
       drive range_start (see captureClick). */
    const tFirst = s.t_first_video_rel ? s.t_first_video_rel[cam] : null;
    const tLast = s.t_last_video_rel ? s.t_last_video_rel[cam] : null;
    if (tFirst == null || tLast == null) {
      window.GT.editor.rangeStart = null;
      window.GT.editor.rangeEnd = null;
      return;
    }
    const dur = camDuration(s, cam) || (tLast + 0.1);
    window.GT.editor.rangeStart = Math.max(0, tFirst - 0.1);
    window.GT.editor.rangeEnd = Math.min(dur, tLast + 0.1);
  }

  function updateInputsFromState() {
    elRangeStart.value = window.GT.editor.rangeStart != null
      ? window.GT.editor.rangeStart.toFixed(2) : '';
    elRangeEnd.value = window.GT.editor.rangeEnd != null
      ? window.GT.editor.rangeEnd.toFixed(2) : '';
    const c = window.GT.editor.click;
    if (c.x != null && c.y != null && c.t != null) {
      elClickReadout.textContent = `click: (${c.x}, ${c.y}) @ ${c.t.toFixed(2)}s`;
    } else {
      elClickReadout.textContent = 'click: — (click ball on video to seed)';
    }
    const valid = window.GT.editor.rangeStart != null
      && window.GT.editor.rangeEnd != null
      && window.GT.editor.rangeStart < window.GT.editor.rangeEnd
      && c.x != null && c.y != null && c.t != null
      && c.t >= window.GT.editor.rangeStart
      && c.t <= window.GT.editor.rangeEnd;
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
    const colorH = 40;
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
    const ct = window.GT.editor.click.t;
    if (ct != null) {
      const xc = (ct / dur) * w;
      svg += `<line x1="${xc}" x2="${xc}" y1="0" y2="${h}" stroke="var(--failed)" stroke-width="2"/>`;
    }
    elTimelineSvg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    elTimelineSvg.innerHTML = svg;
    elTimelineHint.textContent = data.source === 'empty'
      ? 'no detections — manual range'
      : '';
  }

  function repositionClickMarker() {
    const c = window.GT.editor.click;
    if (c.x == null || c.y == null || !elVideo.videoWidth) {
      elClickMarker.hidden = true;
      return;
    }
    // image-px → CSS-px on the rendered <video>. The wrap div has same
    // dims as the video (display:block), so positioning relative to
    // wrap aligns with the video pixels.
    const cssX = c.x * elVideo.clientWidth / elVideo.videoWidth;
    const cssY = c.y * elVideo.clientHeight / elVideo.videoHeight;
    elClickMarker.style.left = cssX + 'px';
    elClickMarker.style.top = cssY + 'px';
    elClickMarker.hidden = false;
  }

  function renderEditor() {
    const sid = window.GT.selected.sid;
    if (!sid) {
      elTitle.textContent = '← pick a session';
      elCamToggle.hidden = true;
      elClickHint.hidden = true;
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
    elClickHint.hidden = false;
    elVideoWrap.hidden = false;
    elTimeline.hidden = false;
    elRangeRow.hidden = false;
    elAddRow.hidden = false;
    elDetailActions.hidden = false;
    elEmptyHint.hidden = true;

    document.querySelectorAll('input[name="gt-cam"]').forEach((el) => {
      el.checked = el.value === cam;
      const camMissing = !(s.has_mov && s.has_mov[el.value]);
      el.disabled = camMissing && !(s.cams_present && s.cams_present[el.value]);
    });

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

    elSkipBtn.hidden = !!s.is_skipped;
    elUnskipBtn.hidden = !s.is_skipped;
    const willOverwrite = !!(s.has_gt && s.has_gt[cam]);
    elOverwriteWarn.hidden = !willOverwrite;

    // First-load: if no click and no dirty edit, auto-fill the
    // range from live-detection bracket as a starting point. After
    // operator clicks, range_start moves to the click time.
    if (!window.GT.editor.dirty && window.GT.editor.click.x == null) {
      autoFillRangeFromDetections(s, cam);
    }
    updateInputsFromState();
    repositionClickMarker();
    fetchHeatmapAndDraw(sid, cam);
  }

  // ----- video click → seed point -----
  elVideoWrap.addEventListener('click', (evt) => {
    if (!elVideo.videoWidth) return;  // not loaded yet
    // Only react to clicks on the video / overlay layer; not on the
    // controls row beneath the video.
    if (!evt.target.matches('#gt-video, #gt-video-overlay, .gt-click-marker')) {
      return;
    }
    const rect = elVideo.getBoundingClientRect();
    const cssX = evt.clientX - rect.left;
    const cssY = evt.clientY - rect.top;
    if (cssX < 0 || cssY < 0 || cssX > rect.width || cssY > rect.height) return;
    const imgX = Math.round(cssX * elVideo.videoWidth / rect.width);
    const imgY = Math.round(cssY * elVideo.videoHeight / rect.height);
    const t = elVideo.currentTime;
    window.GT.editor.click = { x: imgX, y: imgY, t };
    window.GT.editor.dirty = true;
    // Seed time = click time. Operator only needs to set range END
    // (or accept the auto-fill from detections).
    window.GT.editor.rangeStart = t;
    if (window.GT.editor.rangeEnd != null && window.GT.editor.rangeEnd <= t) {
      window.GT.editor.rangeEnd = null;
    }
    repositionClickMarker();
    updateInputsFromState();
    drawHeatmapForCurrent();
  });

  function drawHeatmapForCurrent() {
    if (!window.GT.selected.sid) return;
    const key = `${window.GT.selected.sid}/${window.GT.selected.cam}`;
    if (window.GT.heatmap[key]) drawHeatmap(window.GT.heatmap[key]);
  }

  // ----- video controls -----
  elVideoPlay.addEventListener('click', () => {
    if (elVideo.paused) {
      elVideo.play();
      elVideoPlay.textContent = 'Pause';
    } else {
      elVideo.pause();
      elVideoPlay.textContent = 'Play';
    }
  });
  elVideo.addEventListener('pause', () => { elVideoPlay.textContent = 'Play'; });
  elVideo.addEventListener('play', () => { elVideoPlay.textContent = 'Pause'; });
  elVideo.addEventListener('timeupdate', () => {
    elVideoTime.textContent =
      `${elVideo.currentTime.toFixed(2)} / ${(elVideo.duration || 0).toFixed(2)} s`;
  });
  elVideo.addEventListener('loadedmetadata', repositionClickMarker);
  window.addEventListener('resize', repositionClickMarker);

  function stepFrame(direction) {
    // Approximate frame step at 240 fps. Not exact (browser only seeks
    // to keyframes precisely) but good enough to find a visible-ball
    // frame within ±1 frame visually.
    const fps = 240;
    elVideo.pause();
    elVideo.currentTime = Math.max(0, elVideo.currentTime + direction * (1 / fps));
  }
  elVideoStepBack.addEventListener('click', () => stepFrame(-1));
  elVideoStepFwd.addEventListener('click', () => stepFrame(1));

  // ----- keyboard: ',' / '.' frame step (target-guarded) -----
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (!window.GT.selected.sid || !elVideo.videoWidth) return;
    if (e.key === ',') stepFrame(-1);
    else if (e.key === '.') stepFrame(1);
  });

  // ----- cam toggle -----
  elCamToggle.addEventListener('change', (e) => {
    if (e.target && e.target.name === 'gt-cam') {
      if (window.GT.editor.dirty) {
        const ok = window.confirm('未加入佇列的修改會丟失，確定切換 cam?');
        if (!ok) {
          document.querySelector(`input[name="gt-cam"][value="${window.GT.selected.cam}"]`).checked = true;
          return;
        }
      }
      window.GT.selected.cam = e.target.value;
      window.GT.editor.dirty = false;
      window.GT.editor.click = { x: null, y: null, t: null };
      renderEditor();
    }
  });

  // ----- range input -----
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

  // ----- add to queue -----
  elAddBtn.addEventListener('click', async () => {
    elAddError.hidden = true;
    const sid = window.GT.selected.sid;
    const cam = window.GT.selected.cam;
    const start = window.GT.editor.rangeStart;
    const end = window.GT.editor.rangeEnd;
    const c = window.GT.editor.click;
    if (sid == null || start == null || end == null
        || c.x == null || c.y == null || c.t == null) return;

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
          click_x: c.x,
          click_y: c.y,
          click_t_video_rel: c.t,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        elAddError.textContent =
          typeof body.detail === 'string' ? body.detail : `error ${r.status}`;
        elAddError.hidden = false;
        return;
      }
      window.GT.editor.dirty = false;
      window.GT.editor.click = { x: null, y: null, t: null };
      repositionClickMarker();
      updateInputsFromState();
      if (window.GT.tickQueue) window.GT.tickQueue();
    } catch (err) {
      elAddError.textContent = String(err);
      elAddError.hidden = false;
    }
  });

  // ----- skip / unskip -----
  elSkipBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    if (!window.confirm(`Skip ${window.GT.selected.sid}? 之後只有顯示在帶 (⊘) 的 row.`)) return;
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
