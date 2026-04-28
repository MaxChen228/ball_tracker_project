/* Editor — video + unified timeline + click-to-seed.
 *
 * The timeline below the video is the SOLE temporal control surface:
 *   - Background heatmap of detection density (per /gt/timeline).
 *   - Translucent shade between rangeStart and rangeEnd.
 *   - Two draggable handles at start / end.
 *   - Red tick at click_t (also draggable to nudge seed time without
 *     re-clicking the video — useful when operator picked a good xy
 *     but wants to shift the seed by one frame).
 *   - Black cursor at video.currentTime; dragging it scrubs the video.
 * Clicking on bare timeline area seeks the video.
 *
 * Spatial click on the <video> element sets:
 *   - click.x / click.y in IMAGE-pixel space (CSS-px × videoWidth /
 *     clientWidth — the wrap div lays out as the same dims as the
 *     <video>, so clientWidth on the video is the rendered CSS width).
 *   - click.t = currentTime
 *   - rangeStart auto-snaps to currentTime (operator typically clicks
 *     at the moment they want propagation to start; range_start follows).
 *
 * The on-video click marker is only rendered when |currentTime - click_t|
 * is within ~1 frame; scrub away and it disappears so the operator
 * isn't confused by a marker hovering over a non-ball location.
 *
 * Submit body shape (matches `routes/gt.py::QueueAddBody`):
 *   { session_id, camera_id, time_range:[start,end], click_x, click_y,
 *     click_t_video_rel }
 */
(function () {
  const elTitle = document.getElementById('gt-editor-title');
  const elCamToggle = document.getElementById('gt-cam-toggle');
  const elClickHint = document.getElementById('gt-click-hint');
  const elVideoWrap = document.getElementById('gt-video-wrap');
  const elVideo = document.getElementById('gt-video');
  const elVideoMeta = document.getElementById('gt-video-meta');
  const elVideoPlay = document.getElementById('gt-video-play');
  const elVideoTime = document.getElementById('gt-video-time');
  const elClickMarker = document.getElementById('gt-click-marker');
  const elTimeline = document.getElementById('gt-timeline');
  const elTimelineSvg = document.getElementById('gt-timeline-svg');
  const elTimelineHint = document.getElementById('gt-timeline-hint');
  const elReadout = document.getElementById('gt-readout');
  const elAddRow = document.getElementById('gt-add-row');
  const elAddBtn = document.getElementById('gt-add-btn');
  const elAddError = document.getElementById('gt-add-error');
  const elOverwriteWarn = document.getElementById('gt-overwrite-warn');
  const elDetailActions = document.getElementById('gt-detail-actions');
  const elSkipBtn = document.getElementById('gt-skip-btn');
  const elUnskipBtn = document.getElementById('gt-unskip-btn');
  const elEmptyHint = document.getElementById('gt-empty-hint');

  // tolerance in seconds for "currentTime matches click_t" — at 240 fps
  // a frame is ~4.2 ms; we widen to ~30 ms for usability when operator
  // scrubs near the seed (browser seek snaps to keyframes anyway).
  const CLICK_VISIBILITY_TOLERANCE_S = 0.03;

  // ----- video content rect (object-fit: contain aware) -------------
  //
  // <video> defaults to object-fit: contain. With our CSS (width: 100%,
  // max-height: 50vh) the layout box can have a different aspect ratio
  // than the intrinsic video — the browser then letterboxes (black bars
  // on the unused side). `getBoundingClientRect()` and `clientWidth/
  // clientHeight` give the LAYOUT BOX, not the actual content area, so
  // naive `cssX × videoWidth / clientWidth` is wrong by the offset of
  // the letterbox. Verified 2026-04-29 via overlay smoke: a click on
  // the ball gave imgX/imgY ~50 px off vs the same-frame live-HSV
  // detection — letterbox sides was eating the X coordinate.
  //
  // This helper returns the content rect in CSS-px relative to the
  // element's top-left. Both click capture and marker positioning go
  // through here so they stay coherent (a bad helper would still LOOK
  // right on screen but submit wrong image-px coords).
  function videoContentRect(video) {
    const cw = video.clientWidth;
    const ch = video.clientHeight;
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    if (!vw || !vh) return { x: 0, y: 0, w: cw, h: ch };
    const aspectLayout = cw / ch;
    const aspectVideo = vw / vh;
    if (aspectLayout > aspectVideo) {
      // Layout is wider than video → letterbox left/right.
      const w = ch * aspectVideo;
      return { x: (cw - w) / 2, y: 0, w, h: ch };
    } else {
      // Layout is taller than video → letterbox top/bottom.
      const h = cw / aspectVideo;
      return { x: 0, y: (ch - h) / 2, w: cw, h };
    }
  }

  // Drag state for timeline interactions. mode ∈ {null, "rangeStart",
  // "rangeEnd", "click", "cursor"}. We attach mousemove/mouseup at
  // document level once on first drag and tear them down on mouseup.
  let dragMode = null;

  function selectedSessionState() {
    const sid = window.GT.selected.sid;
    if (!sid) return null;
    return window.GT.sessions.find((s) => s.session_id === sid) || null;
  }

  function camDuration(s, cam) {
    return s && s.video_duration_s ? s.video_duration_s[cam] : null;
  }

  function timelineDuration() {
    /* Use the heatmap's reported duration if available (matches what
       /gt/timeline returns); fall back to video.duration. The two
       should agree but heatmap is what the SSR places handles
       against. */
    const s = selectedSessionState();
    const cam = window.GT.selected.cam;
    const key = `${window.GT.selected.sid}/${cam}`;
    const heat = window.GT.heatmap[key];
    if (heat && heat.duration_s) return heat.duration_s;
    if (elVideo.duration && isFinite(elVideo.duration)) return elVideo.duration;
    return camDuration(s, cam) || 1.0;
  }

  function camMovUrl(sid, cam) {
    return `/videos/session_${sid}_${cam}.mov`;
  }

  function autoFillRangeFromDetections(s, cam) {
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

  function updateReadout() {
    const c = window.GT.editor.click;
    const rs = window.GT.editor.rangeStart;
    const re = window.GT.editor.rangeEnd;
    const clickStr = (c.x != null && c.t != null)
      ? `(${c.x},${c.y}) @ ${c.t.toFixed(2)}s`
      : '—';
    const rangeStr = (rs != null && re != null)
      ? `${rs.toFixed(2)}s — ${re.toFixed(2)}s`
      : '—';
    elReadout.textContent = `click: ${clickStr} · range: ${rangeStr}`;
    const valid = rs != null && re != null && rs < re
      && c.x != null && c.y != null && c.t != null
      && c.t >= rs && c.t <= re;
    elAddBtn.disabled = !valid;
  }

  // ----- timeline rendering -----------------------------------------

  function fetchHeatmap(sid, cam) {
    const key = `${sid}/${cam}`;
    if (window.GT.heatmap[key]) {
      drawTimeline();
      return;
    }
    fetch(`/gt/timeline/${sid}/${cam}.json`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((data) => {
        window.GT.heatmap[key] = data;
        drawTimeline();
      })
      .catch(() => {
        elTimelineHint.textContent = 'no detections — manual range';
        elTimelineSvg.innerHTML = '';
      });
  }

  function drawTimeline() {
    const sid = window.GT.selected.sid;
    const cam = window.GT.selected.cam;
    if (!sid) return;
    const key = `${sid}/${cam}`;
    const data = window.GT.heatmap[key];
    const w = elTimeline.clientWidth || 600;
    const h = elTimeline.clientHeight || 96;
    const dur = timelineDuration();
    const x = (t) => Math.max(0, Math.min(w, (t / dur) * w));

    let svg = `<rect x="0" y="0" width="${w}" height="${h}" fill="var(--surface)"/>`;

    // 1. Heatmap (lower 60% of timeline, leaves top room for tick labels).
    if (data && data.buckets && data.buckets.length) {
      const buckets = data.buckets;
      const maxCount = Math.max(1, ...buckets);
      const heatY = h * 0.4;
      const heatH = h * 0.6;
      buckets.forEach((c, i) => {
        const t0 = i * data.bucket_size_s;
        const xs = x(t0);
        const bw = (data.bucket_size_s / dur) * w;
        const intensity = c / maxCount;
        svg += `<rect x="${xs.toFixed(2)}" y="${heatY}" width="${bw.toFixed(2)}" height="${heatH}"
          fill="hsl(40, 60%, ${(80 - intensity * 50).toFixed(0)}%)"
          opacity="${(0.2 + intensity * 0.7).toFixed(2)}"/>`;
      });
    }

    // 2. Range shade between rangeStart and rangeEnd.
    const rs = window.GT.editor.rangeStart;
    const re = window.GT.editor.rangeEnd;
    if (rs != null && re != null && rs < re) {
      const xs = x(rs);
      const xe = x(re);
      svg += `<rect x="${xs}" y="0" width="${xe - xs}" height="${h}"
        fill="var(--ink)" opacity="0.10" pointer-events="none"/>`;
      // Range handles — wide invisible hit area + visible thin bar.
      svg += renderHandle(xs, h, 'rangeStart');
      svg += renderHandle(xe, h, 'rangeEnd');
    }

    // 3. Click tick — red vertical bar with a small triangle pointing
    //    down at the top so it's distinguishable from range handles.
    const ct = window.GT.editor.click.t;
    if (ct != null) {
      const xc = x(ct);
      // hit area
      svg += `<rect x="${xc - 8}" y="0" width="16" height="${h}"
        fill="transparent" data-role="click" style="cursor:ew-resize"/>`;
      // visible tick
      svg += `<line x1="${xc}" x2="${xc}" y1="0" y2="${h}"
        stroke="var(--failed)" stroke-width="2" pointer-events="none"/>`;
      svg += `<polygon points="${xc - 6},0 ${xc + 6},0 ${xc},10"
        fill="var(--failed)" pointer-events="none"/>`;
    }

    // 4. Playback cursor — thicker than range handles so it pops.
    const t = elVideo.currentTime || 0;
    const xt = x(t);
    svg += `<rect x="${xt - 10}" y="0" width="20" height="${h}"
      fill="transparent" data-role="cursor" style="cursor:ew-resize"/>`;
    svg += `<line x1="${xt}" x2="${xt}" y1="0" y2="${h}"
      stroke="var(--ink)" stroke-width="3" pointer-events="none"/>`;

    elTimelineSvg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    elTimelineSvg.innerHTML = svg;
    elTimelineHint.textContent =
      (!data || !data.buckets || !data.buckets.length || data.source === 'empty')
        ? 'no detections — drag handles to set range manually'
        : '';
  }

  function renderHandle(xPx, hPx, role) {
    /* 16px hit area centered on xPx; thin visible bar. data-role lets
       the mousedown handler know what we're dragging without a closure
       per element. */
    return `<rect x="${xPx - 8}" y="0" width="16" height="${hPx}"
      fill="transparent" data-role="${role}" style="cursor:ew-resize"/>
      <line x1="${xPx}" x2="${xPx}" y1="0" y2="${hPx}"
        stroke="var(--ink)" stroke-width="2" pointer-events="none"/>`;
  }

  // ----- timeline mouse interactions --------------------------------

  function timeForEvent(evt) {
    const rect = elTimelineSvg.getBoundingClientRect();
    const px = evt.clientX - rect.left;
    const dur = timelineDuration();
    return Math.max(0, Math.min(dur, (px / rect.width) * dur));
  }

  elTimelineSvg.addEventListener('mousedown', (evt) => {
    const role = evt.target.dataset.role;
    if (role === 'rangeStart' || role === 'rangeEnd' || role === 'click' || role === 'cursor') {
      dragMode = role;
    } else {
      // Click on bare timeline → seek video.
      dragMode = 'cursor';
      const t = timeForEvent(evt);
      elVideo.currentTime = t;
    }
    elTimeline.classList.add('dragging');
    evt.preventDefault();
  });

  document.addEventListener('mousemove', (evt) => {
    if (!dragMode) return;
    const t = timeForEvent(evt);
    if (dragMode === 'rangeStart') {
      const re = window.GT.editor.rangeEnd;
      window.GT.editor.rangeStart = (re != null) ? Math.min(t, re - 0.01) : t;
      window.GT.editor.dirty = true;
    } else if (dragMode === 'rangeEnd') {
      const rs = window.GT.editor.rangeStart;
      window.GT.editor.rangeEnd = (rs != null) ? Math.max(t, rs + 0.01) : t;
      window.GT.editor.dirty = true;
    } else if (dragMode === 'click') {
      window.GT.editor.click.t = t;
      window.GT.editor.dirty = true;
      // Also seek so operator visually verifies the new click frame.
      elVideo.currentTime = t;
    } else if (dragMode === 'cursor') {
      elVideo.currentTime = t;
    }
    drawTimeline();
    updateReadout();
    repositionClickMarker();
  });

  document.addEventListener('mouseup', () => {
    if (!dragMode) return;
    dragMode = null;
    elTimeline.classList.remove('dragging');
  });

  // ----- on-video click marker ---------------------------------------

  function repositionClickMarker() {
    const c = window.GT.editor.click;
    if (c.x == null || c.y == null || c.t == null || !elVideo.videoWidth) {
      elClickMarker.hidden = true;
      return;
    }
    const dt = Math.abs((elVideo.currentTime || 0) - c.t);
    if (dt > CLICK_VISIBILITY_TOLERANCE_S) {
      elClickMarker.hidden = true;
      return;
    }
    // Map image-px → CSS-px through the content rect (NOT clientWidth)
    // so letterbox offsets are honoured. Marker is positioned absolutely
    // inside .gt-video-wrap which has the same dimensions as the
    // <video> element (display:block + no padding/border on the wrap).
    const content = videoContentRect(elVideo);
    const cssX = content.x + c.x * content.w / elVideo.videoWidth;
    const cssY = content.y + c.y * content.h / elVideo.videoHeight;
    elClickMarker.style.left = cssX + 'px';
    elClickMarker.style.top = cssY + 'px';
    elClickMarker.hidden = false;
  }

  // ----- video click → set seed -------------------------------------

  elVideoWrap.addEventListener('click', (evt) => {
    if (!elVideo.videoWidth) return;
    if (!evt.target.matches('#gt-video, #gt-video-overlay, .gt-click-marker')) {
      return;
    }
    const rect = elVideo.getBoundingClientRect();
    const cssX = evt.clientX - rect.left;
    const cssY = evt.clientY - rect.top;
    // Reject clicks on the letterbox region (outside actual video pixels).
    // Without this, clicking the black bar would map to a clamped image
    // edge — visually unhelpful. content rect coords are relative to
    // the element's top-left, same as cssX/cssY.
    const content = videoContentRect(elVideo);
    const localX = cssX - content.x;
    const localY = cssY - content.y;
    if (localX < 0 || localY < 0 || localX > content.w || localY > content.h) return;
    const imgX = Math.round(localX * elVideo.videoWidth / content.w);
    const imgY = Math.round(localY * elVideo.videoHeight / content.h);
    const t = elVideo.currentTime;
    window.GT.editor.click = { x: imgX, y: imgY, t };
    window.GT.editor.dirty = true;
    // Click sets range_start unless operator already pulled the start
    // handle to the left of currentTime — in that case respect their
    // choice. If range_start is to the right of currentTime, we shift
    // it to currentTime (the click is the seed and must be inside the
    // range; we never let click_t fall outside).
    if (window.GT.editor.rangeStart == null
        || window.GT.editor.rangeStart > t) {
      window.GT.editor.rangeStart = t;
    }
    if (window.GT.editor.rangeEnd != null
        && window.GT.editor.rangeEnd <= t) {
      window.GT.editor.rangeEnd = null;
    }
    drawTimeline();
    updateReadout();
    repositionClickMarker();
  });

  // ----- video controls ---------------------------------------------

  elVideoPlay.addEventListener('click', () => {
    if (elVideo.paused) elVideo.play(); else elVideo.pause();
  });
  elVideo.addEventListener('pause', () => { elVideoPlay.textContent = 'Play'; });
  elVideo.addEventListener('play', () => { elVideoPlay.textContent = 'Pause'; });
  elVideo.addEventListener('timeupdate', () => {
    elVideoTime.textContent =
      `${elVideo.currentTime.toFixed(2)} / ${(elVideo.duration || 0).toFixed(2)} s`;
    drawTimeline();
    repositionClickMarker();
  });
  elVideo.addEventListener('loadedmetadata', () => {
    drawTimeline();
    repositionClickMarker();
  });
  window.addEventListener('resize', () => {
    drawTimeline();
    repositionClickMarker();
  });

  // ----- keyboard ---------------------------------------------------

  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (!window.GT.selected.sid || !elVideo.videoWidth) return;
    const fps = 240;
    if (e.key === ',') {
      elVideo.pause();
      elVideo.currentTime = Math.max(0, elVideo.currentTime - 1 / fps);
    } else if (e.key === '.') {
      elVideo.pause();
      elVideo.currentTime = elVideo.currentTime + 1 / fps;
    } else if (e.key === ' ') {
      e.preventDefault();
      if (elVideo.paused) elVideo.play(); else elVideo.pause();
    }
  });

  // ----- cam toggle -------------------------------------------------

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

  // ----- add to queue -----------------------------------------------

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
          session_id: sid, camera_id: cam,
          time_range: [start, end],
          click_x: c.x, click_y: c.y, click_t_video_rel: c.t,
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
      drawTimeline();
      updateReadout();
      repositionClickMarker();
      if (window.GT.tickQueue) window.GT.tickQueue();
    } catch (err) {
      elAddError.textContent = String(err);
      elAddError.hidden = false;
    }
  });

  // ----- skip / unskip -----------------------------------------------

  elSkipBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    if (!window.confirm(`Skip ${window.GT.selected.sid}?`)) return;
    await fetch(`/gt/sessions/${window.GT.selected.sid}/skip`, { method: 'POST' });
    if (window.GT.tickSessions) window.GT.tickSessions();
  });
  elUnskipBtn.addEventListener('click', async () => {
    if (!window.GT.selected.sid) return;
    await fetch(`/gt/sessions/${window.GT.selected.sid}/unskip`, { method: 'POST' });
    if (window.GT.tickSessions) window.GT.tickSessions();
  });

  // ----- editor render ----------------------------------------------

  function renderEditor() {
    const sid = window.GT.selected.sid;
    if (!sid) {
      elTitle.textContent = '← pick a session';
      elCamToggle.hidden = true;
      elClickHint.hidden = true;
      elVideoWrap.hidden = true;
      elTimeline.hidden = true;
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

    if (!window.GT.editor.dirty && window.GT.editor.click.x == null) {
      autoFillRangeFromDetections(s, cam);
    }
    fetchHeatmap(sid, cam);
    drawTimeline();
    updateReadout();
    repositionClickMarker();
  }

  window.GT.render.editor = renderEditor;
})();
