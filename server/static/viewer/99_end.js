  // Expose the data the Three.js viewer-layers module needs. It runs
  // in a separate `<script type="module">` block (deferred until after
  // this classic IIFE finishes parsing); its boot-poll waits for both
  // `BallTrackerScene` and `VIEWER_DATA` before calling
  // `setupViewerLayers`. No race because classic IIFE runs first.
  window.VIEWER_DATA = {
    SCENE: SCENE,
    SEGMENTS: SEGMENTS,
    SEGMENTS_BY_PATH: SEGMENTS_BY_PATH,
    TRAJ_BY_PATH: TRAJ_BY_PATH,
    HAS_TRIANGULATED: HAS_TRIANGULATED,
    FALLBACK_COLOR: FALLBACK,
    tMin: tMin,
    layerVisibility: layerVisibility,
  };
  window._viewerPatchSegmentsState = function(nextSegments, nextSegmentsByPath) {
    SEGMENTS = Array.isArray(nextSegments) ? nextSegments : [];
    SEGMENTS_BY_PATH = nextSegmentsByPath || {};
    updateSpeedBadge();
  };

  setFrame(0, { seekVideos: true });
  scheduleSceneDraw();
  updatePlayBtnLabel();
  requestAnimationFrame(resizeDetectionCanvas);

  // Bottom-dock sizing: the transport timeline is `position:fixed` at the
  // viewport bottom, so .viewer needs `padding-bottom` matching the dock
  // height to keep .work content from scrolling under it. ResizeObserver
  // recomputes whenever the dock reflows (e.g. layer toggles wrap to a
  // second row, viewport width shrinks). Initial value is set
  // synchronously before the first paint so the CSS fallback (80px) only
  // briefly applies for the first frame.
  const dock = document.querySelector(".timeline");
  const viewerEl = document.querySelector(".viewer");
  const applyDockHeight = () => {
    viewerEl.style.setProperty("--timeline-h", `${dock.offsetHeight}px`);
  };
  applyDockHeight();
  // ResizeObserver is universally available in target browsers (iPad
  // Safari ≥ 13.1, all desktops); no capability fallback — without it
  // dock height would freeze at first paint and silently misalign when
  // the layer-toggles row wraps.
  new ResizeObserver(applyDockHeight).observe(dock);
})();
