  function markManualSeekWindow(ms = 180) {
    suppressVideoFeedbackUntilMs = Math.max(suppressVideoFeedbackUntilMs, performance.now() + ms);
  }
  function shouldIgnoreVideoFeedback() { return isScrubbing || performance.now() < suppressVideoFeedbackUntilMs; }
  function beginTimelineInteraction() { pauseAllPlayback(); markManualSeekWindow(); updatePlayBtnLabel(); }
  function resetVideoPlaybackRates() { for (const v of vids) if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate; }
  function syncFollowerVideosToMaster(masterT) {
    if (!masterVideo || !isFinite(masterT)) return;
    for (const v of vids) {
      if (v === masterVideo) continue;
      const off = offsetByCam[v.dataset.cam] ?? 0;
      const want = Math.max(0, masterT - off);
      if (!isFinite(v.currentTime)) continue;
      const drift = v.currentTime - want;
      if (Math.abs(drift) >= HARD_SYNC_THRESHOLD_S) {
        try { v.currentTime = want; } catch {}
        if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate;
        continue;
      }
      if (v.paused || Math.abs(drift) <= SOFT_SYNC_THRESHOLD_S) {
        if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate;
        continue;
      }
      const correction = Math.max(-MAX_RATE_NUDGE, Math.min(MAX_RATE_NUDGE, -drift * 6.0));
      const targetRate = Math.max(0.1, currentRate * (1 + correction));
      if (Math.abs(v.playbackRate - targetRate) > 0.001) v.playbackRate = targetRate;
    }
  }
  let seekTargetT = tMin;
  function syncVideosToT(t) {
    if (!isFinite(t)) return;
    seekTargetT = t;
    markManualSeekWindow();
    if (seekRafPending) return;
    seekRafPending = true;
    requestAnimationFrame(() => {
      seekRafPending = false;
      const tt = seekTargetT;
      for (const v of vids) {
        const off = offsetByCam[v.dataset.cam] ?? 0;
        const want = Math.max(0, tt - off);
        if (Math.abs((v.currentTime || 0) - want) < 1e-4) continue;
        try { v.currentTime = want; } catch {}
      }
      resetVideoPlaybackRates();
    });
  }
  function readMasterTFromVideo() {
    if (masterVideo && !isNaN(masterVideo.currentTime)) return masterVideo.currentTime + (offsetByCam[masterVideo.dataset.cam] ?? 0);
    for (const v of vids) if (!isNaN(v.currentTime)) return v.currentTime + (offsetByCam[v.dataset.cam] ?? 0);
    return currentT;
  }
  function frameIndexForT(t) {
    let lo = 0, hi = TOTAL_FRAMES - 1;
    if (t <= unionTimes[lo]) return lo;
    if (t >= unionTimes[hi]) return hi;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (unionTimes[mid] <= t) lo = mid; else hi = mid;
    }
    // Floor: pick largest unionTimes[lo] ≤ t. Matches the HTML5 video
    // element seek behaviour (displays PTS ≤ currentTime) so RVFC's
    // reverse mapping (mediaTime → currentFrame) lands on the same frame
    // the video element is displaying, not its temporal neighbour.
    return lo;
  }
  // Translate one camAtFrameByPath entry into (mark glyph, status key).
  // Status key drives the CSS class on each surface: timeline label uses
  // `.fl-det-${...}` (PHYSICS_LAB ink palette), per-cam HUD uses
  // `.hud-mark-${...}` (dark-bg overlay palette). Verdict logic centralised
  // here so the two surfaces can never drift in interpretation.
  //   ✓ kept       — chain_filter validated detection
  //   ✓ unscored   — detected but no chain_filter ran (live path normal)
  //   F flicker    — chain_filter rejected_flicker (chain too short)
  //   J jump       — chain_filter rejected_jump   (ray broke max_jump_px)
  //   · no         — non-detection
  function frameVerdict(entry) {
    if (!entry || !entry.detected) return ["·", "no"];
    switch (entry.filter_status) {
      case "rejected_flicker": return ["F", "flicker"];
      case "rejected_jump":    return ["J", "jump"];
      case "kept":             return ["✓", "kept"];
      default:                 return ["✓", "unscored"];
    }
  }
  const FL_DET_CLS = {
    no: "fl-det fl-det-no",
    flicker: "fl-det fl-det-warn",
    jump: "fl-det fl-det-bad",
    kept: "fl-det",
    unscored: "fl-det fl-det-unscored",
  };
  function renderFrameLabel() {
    const v = String(currentFrame);
    if (document.activeElement !== frameInput && frameInput.value !== v) frameInput.value = v;
    const tRel = currentT - tMin;
    framePrimary.textContent = `t=${tRel.toFixed(3)}s`;
    // Structured per-path rows: `<PATH>  A:idx ✓  B:idx ✓`. Two cams pad
    // into fixed slots so the card width doesn't jitter as idx changes
    // width (e.g. going from 99 → 100).
    const rows = [];
    for (const path of PATHS) {
      const cams = camsWithFramesByPath[path];
      if (!cams.length) continue;
      const cells = ["A", "B"].map((cam) => {
        if (!cams.includes(cam)) return `<span class="fl-cell fl-cell-blank">${cam}:—</span>`;
        const entry = camAtFrameByPath[path][cam][currentFrame];
        if (!entry) return `<span class="fl-cell fl-cell-blank">${cam}:—</span>`;
        const [mark, status] = frameVerdict(entry);
        // frame_index = physical source frame counter (iOS capture-queue
        // index for live, PyAV decode order for server_post). Distinct
        // from `idx` which is array position post timestamp-sort —
        // exposes the throttle/drop gaps that array idx hides.
        const fidx = entry.frame_index != null ? `<span class="fl-fidx">/${entry.frame_index}</span>` : "";
        return `<span class="fl-cell">${cam}:${entry.idx}${fidx}<span class="${FL_DET_CLS[status]}">${mark}</span></span>`;
      }).join("");
      rows.push(`<div class="fl-row"><span class="fl-pathlabel">${PATH_LABEL[path]}</span>${cells}</div>`);
    }
    frameSub.innerHTML = rows.join("");
  }
  // Per-cam HUD: subset of the timeline label scoped to one camera, layered
  // over the video as a DOM overlay. Same verdict / frame_index data, but
  // independent surface so OVL=0 (pure video) keeps the HUD legible.
  function renderHuds() {
    for (const cam of ["A", "B"]) {
      const hud = document.querySelector(`[data-cam-hud="${cam}"]`);
      if (!hud) continue;
      const lines = [];
      for (const path of PATHS) {
        if (!camsWithFramesByPath[path].includes(cam)) continue;
        const entry = camAtFrameByPath[path][cam][currentFrame];
        if (!entry) continue;
        const [mark, status] = frameVerdict(entry);
        const fidxBit = entry.frame_index != null
          ? `<span class="hud-fidx">/${entry.frame_index}</span>` : "";
        lines.push(
          `<div class="hud-row">`
          + `<span class="hud-path">${PATH_LABEL[path]}</span>`
          + `<span class="hud-idx">${entry.idx}${fidxBit}</span>`
          + `<span class="hud-mark hud-mark-${status}">${mark}</span>`
          + `</div>`
        );
      }
      hud.innerHTML = lines.join("");
    }
  }
  function setFrame(f, { seekVideos = true } = {}) {
    currentFrame = Math.max(0, Math.min(TOTAL_FRAMES - 1, f | 0));
    currentT = unionTimes[currentFrame];
    scrubber.value = String(currentFrame);
    renderFrameLabel();
    renderHuds();
    renderDetectionStrip();
    if (seekVideos) syncVideosToT(currentT);
    // Schedule both independent paint paths. Each owns its own RAF
    // dedup so a heavy Plotly.react can't block the canvas2D virtual cam
    // paints — they fall behind only when the JS event loop itself stalls
    // (in which case so does the video, and the operator wouldn't perceive
    // a sync glitch). In "all" mode the scene doesn't depend on currentT,
    // so skip the expensive scene redraw — virtual cameras still tick.
    scheduleVirtualDraw();
    if (mode === "playback") scheduleSceneDraw();
  }
  let virtualRAF = null;
  let virtualLastPerfMs = 0;
  let virtualTime = 0;
  function virtualPlaying() { return virtualRAF !== null; }
  function startVirtualClock() {
    if (virtualRAF !== null) return;
    virtualLastPerfMs = performance.now();
    virtualTime = currentT;
    const tick = (now) => {
      virtualRAF = requestAnimationFrame(tick);
      const dt = (now - virtualLastPerfMs) / 1000 * currentRate;
      virtualLastPerfMs = now;
      virtualTime += dt;
      if (virtualTime >= unionTimes[TOTAL_FRAMES - 1]) {
        setFrame(TOTAL_FRAMES - 1);
        stopVirtualClock();
        updatePlayBtnLabel();
        return;
      }
      setFrame(frameIndexForT(virtualTime));
    };
    virtualRAF = requestAnimationFrame(tick);
  }
  function stopVirtualClock() { if (virtualRAF !== null) { cancelAnimationFrame(virtualRAF); virtualRAF = null; } }
  function pauseAllPlayback() { vids.forEach(v => v.pause()); resetVideoPlaybackRates(); stopVirtualClock(); }
  function stepFrames(delta) { beginTimelineInteraction(); setFrame(currentFrame + delta); }
  function jumpDetection(dir) {
    // Step to the next frame where *any* currently-visible pipeline reports
    // a detection. Respecting the pills means the hotkey follows what the
    // operator is actually looking at: hide LIVE and D/F will skip through
    // svr+post only.
    let i = currentFrame + dir;
    while (i >= 0 && i < TOTAL_FRAMES) {
      for (const path of PATHS) {
        for (const cam of camsWithFramesByPath[path]) {
          if (!isLayerVisible(`cam${cam}`, path)) continue;
          const e = camAtFrameByPath[path][cam][i];
          if (e && e.detected) { beginTimelineInteraction(); setFrame(i); return; }
        }
      }
      i += dir;
    }
  }
  function onVideoTimeUpdate() {
    if (rvfcEnabled || seekRafPending || shouldIgnoreVideoFeedback()) return;
    requestAnimationFrame(() => {
      if (shouldIgnoreVideoFeedback()) return;
      setFrame(frameIndexForT(readMasterTFromVideo()), { seekVideos: false });
    });
  }
  playBtn.addEventListener("click", () => {
    if (vids.length > 0) {
      const anyPaused = vids.some(v => v.paused);
      if (anyPaused) {
        syncFollowerVideosToMaster(readMasterTFromVideo());
        resetVideoPlaybackRates();
        vids.forEach(v => { try { v.play(); } catch {} });
      } else vids.forEach(v => v.pause());
      return;
    }
    if (virtualPlaying()) stopVirtualClock(); else startVirtualClock();
    updatePlayBtnLabel();
  });
  function updatePlayBtnLabel() { playBtn.textContent = vids.length > 0 ? (vids.every(v => v.paused) ? "Play" : "Pause") : (virtualPlaying() ? "Pause" : "Play"); }
  const hasRVFC = typeof HTMLVideoElement !== "undefined" && "requestVideoFrameCallback" in HTMLVideoElement.prototype;
  function driveWithRVFC() {
    if (!masterVideo) return;
    rvfcEnabled = true;
    const master = masterVideo;
    const off = offsetByCam[master.dataset.cam] ?? 0;
    const onFrame = (_now, metadata) => {
      if (shouldIgnoreVideoFeedback()) { master.requestVideoFrameCallback(onFrame); return; }
      const mediaT = (metadata && typeof metadata.mediaTime === "number") ? metadata.mediaTime : master.currentTime;
      const t = mediaT + off;
      syncFollowerVideosToMaster(t);
      setFrame(frameIndexForT(t), { seekVideos: false });
      master.requestVideoFrameCallback(onFrame);
    };
    master.requestVideoFrameCallback(onFrame);
  }
  vids.forEach(v => { v.addEventListener("play", updatePlayBtnLabel); v.addEventListener("pause", updatePlayBtnLabel); v.addEventListener("timeupdate", onVideoTimeUpdate); v.addEventListener("seeked", onVideoTimeUpdate); });
  if (hasRVFC) driveWithRVFC();
  scrubber.addEventListener("pointerdown", () => { isScrubbing = true; beginTimelineInteraction(); });
  const endScrub = () => { if (!isScrubbing) return; isScrubbing = false; markManualSeekWindow(120); };
  scrubber.addEventListener("pointerup", endScrub);
  scrubber.addEventListener("pointercancel", endScrub);
  scrubber.addEventListener("blur", endScrub);
  window.addEventListener("pointerup", endScrub);
  scrubber.addEventListener("input", () => { beginTimelineInteraction(); setFrame(Number(scrubber.value)); });
  scrubber.addEventListener("keydown", (ev) => {
    switch (ev.key) {
      case "ArrowLeft": ev.preventDefault(); stepFrames(-1); break;
      case "ArrowRight": ev.preventDefault(); stepFrames(+1); break;
      case "Home": ev.preventDefault(); beginTimelineInteraction(); setFrame(0); break;
      case "End": ev.preventDefault(); beginTimelineInteraction(); setFrame(TOTAL_FRAMES - 1); break;
      case "PageUp": ev.preventDefault(); stepFrames(-10); break;
      case "PageDown": ev.preventDefault(); stepFrames(+10); break;
    }
  });
  frameInput.addEventListener("change", () => {
    const f = Number(frameInput.value);
    if (!isFinite(f)) { frameInput.value = String(currentFrame); return; }
    beginTimelineInteraction();
    setFrame(f);
  });
  frameInput.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); frameInput.blur(); } });
  stepFirstBtn.addEventListener("click", () => stepFrames(-TOTAL_FRAMES));
  stepLastBtn.addEventListener("click", () => stepFrames(+TOTAL_FRAMES));
  stepBackBtn.addEventListener("click", () => stepFrames(-1));
  stepFwdBtn.addEventListener("click", () => stepFrames(+1));
  let currentRate = 1.0;
  speedGroup.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-rate]");
    if (!btn) return;
    const r = parseFloat(btn.dataset.rate);
    if (!isFinite(r) || r <= 0) return;
    currentRate = r;
    resetVideoPlaybackRates();
    for (const b of speedGroup.querySelectorAll("button")) b.classList.toggle("active", b === btn);
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if (hintOverlay.classList.contains("open")) { ev.preventDefault(); setHintOpen(false); }
      return;
    }
    const tag = (ev.target && ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    switch (ev.key) {
      case " ": ev.preventDefault(); playBtn.click(); break;
      case ",": ev.preventDefault(); stepFrames(ev.shiftKey ? -10 : -1); break;
      case ".": ev.preventDefault(); stepFrames(ev.shiftKey ? +10 : +1); break;
      case "ArrowLeft": ev.preventDefault(); stepFrames(-Math.round(0.5 * MASTER_FPS)); break;
      case "ArrowRight": ev.preventDefault(); stepFrames(+Math.round(0.5 * MASTER_FPS)); break;
      case "Home": ev.preventDefault(); stepFrames(-TOTAL_FRAMES); break;
      case "End": ev.preventDefault(); stepFrames(+TOTAL_FRAMES); break;
      case "d": case "D": ev.preventDefault(); jumpDetection(-1); break;
      case "f": case "F": ev.preventDefault(); jumpDetection(+1); break;
      case "?": ev.preventDefault(); setHintOpen(!hintOverlay.classList.contains("open")); break;
      case "1": case "2": case "3": case "4": case "5": {
        const idx = Number(ev.key) - 1;
        const buttons = speedGroup.querySelectorAll("button[data-rate]");
        if (buttons[idx]) { ev.preventDefault(); buttons[idx].click(); }
        break;
      }
    }
  });
