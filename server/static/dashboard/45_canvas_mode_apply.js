// === canvas mode + playback handlers ===

  // --- Canvas mode toggle: INSPECT vs REPLAY -------------------------------
  function applyCanvasMode(nextMode) {
    if (nextMode !== 'inspect' && nextMode !== 'replay') return;
    canvasMode = nextMode;
    try { localStorage.setItem(CANVAS_MODE_KEY, canvasMode); } catch {}
    document.querySelectorAll('.canvas-mode-toggle button').forEach(b => {
      b.classList.toggle('active', b.dataset.canvasMode === canvasMode);
    });
    // Playback bar only makes sense in replay mode; pause + reset the
    // scrubber when leaving so we don't keep the animation loop running
    // invisibly (wasted frames + broken readout on return).
    if (canvasMode === 'replay') {
      if (playbackBar) playbackBar.classList.add('show');
      updateTimeReadout();
    } else {
      if (playbackBar) playbackBar.classList.remove('show');
      setPlaying(false);
    }
    repaintCanvas();
  }
  document.querySelectorAll('.canvas-mode-toggle button').forEach(btn => {
    btn.addEventListener('click', () => applyCanvasMode(btn.dataset.canvasMode));
  });
  // Initial mode sync (localStorage value may already be 'replay').
  applyCanvasMode(canvasMode);

  // --- Playback controls ---------------------------------------------------
  // Track whether the user is currently mid-drag on the canvas. Plotly
  // 3D orbit/pan rely on a continuous pointer-down gesture with no
  // DOM-level repaint interruptions between mousedown and mouseup —
  // every Plotly.react during that window wipes the drag state before
  // the next mousemove can extend it. During replay playback we issue
  // Plotly.react every frame for the ball's new position, which stomps
  // on any orbit attempt and manifests as "only wheel zoom works".
  // Suppress visual repaints (not the playhead logic) while dragging;
  // the ball will catch up on mouseup.
  let isUserInteracting = false;
  if (sceneRoot) {
    sceneRoot.addEventListener('pointerdown', () => { isUserInteracting = true; });
    // mouseup/pointerup can fire OUTSIDE the canvas if the user releases
    // after dragging away — bind to window, not sceneRoot, so we never
    // miss the release and leave the flag stuck true.
    window.addEventListener('pointerup', () => { isUserInteracting = false; });
    window.addEventListener('pointercancel', () => { isUserInteracting = false; });
  }

  function setPlaying(flag) {
    isPlaying = !!flag;
    if (playpauseBtn) playpauseBtn.textContent = isPlaying ? '❚❚' : '▶';
    if (isPlaying) {
      lastFrameTs = null;
      requestAnimationFrame(animationTick);
    }
  }
  function animationTick(ts) {
    if (!isPlaying) return;
    if (lastFrameTs !== null) {
      const dur = activeReplayDuration();
      if (dur > 0) {
        const dt = (ts - lastFrameTs) / 1000.0;
        playheadFrac += (dt * playbackSpeed) / dur;
        if (playheadFrac >= 1.0) {
          // Loop back to start so the operator can keep playing without
          // clicking ▶ after every pitch. If single-shot is ever desired,
          // gate on a `loop` flag from a future UI element.
          playheadFrac = 0.0;
        }
        updateTimeReadout();
        // Skip the heavy repaint while the user is mid-drag — playhead
        // still advances silently so playback resumes at the correct
        // time on pointerup.
        if (!isUserInteracting) repaintCanvas();
      }
    }
    lastFrameTs = ts;
    if (isPlaying) requestAnimationFrame(animationTick);
  }
  if (playpauseBtn) playpauseBtn.addEventListener('click', () => {
    if (activeReplayDuration() <= 0) return;  // nothing to play
    setPlaying(!isPlaying);
  });
  if (scrubSlider) scrubSlider.addEventListener('input', () => {
    playheadFrac = Math.max(0, Math.min(1, parseInt(scrubSlider.value, 10) / 1000.0));
    setPlaying(false);  // user scrub pauses playback
    updateTimeReadout();
    repaintCanvas();
  });
  document.querySelectorAll('.playback-bar .speed button').forEach(btn => {
    btn.addEventListener('click', () => {
      playbackSpeed = parseFloat(btn.dataset.speed);
      document.querySelectorAll('.playback-bar .speed button').forEach(b =>
        b.classList.toggle('active', b === btn)
      );
    });
  });
  // Spacebar: play/pause when replay visible and user isn't typing in a form.
  window.addEventListener('keydown', (e) => {
    if (canvasMode !== 'replay') return;
    if (e.target.matches('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); playpauseBtn.click(); }
  });
