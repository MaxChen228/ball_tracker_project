  setFrame(0, { seekVideos: true });
  scheduleSceneDraw();
  updatePlayBtnLabel();
  requestAnimationFrame(resizeDetectionCanvas);

  // Bottom-dock sizing: the transport timeline is `position:fixed` at the
  // viewport bottom, so .viewer needs `padding-bottom` matching the dock
  // height to keep .work content from scrolling under it. ResizeObserver
  // recomputes whenever the dock reflows (e.g. layer toggles wrap to a
  // second row, viewport width shrinks). Initial value is set
  // synchronously before the first paint so the .viewer doesn't briefly
  // size to the fallback 200px before the observer fires.
  const _dock = document.querySelector(".timeline");
  const _viewerEl = document.querySelector(".viewer");
  if (_dock && _viewerEl) {
    const _applyDockHeight = () => {
      _viewerEl.style.setProperty("--timeline-h", `${_dock.offsetHeight}px`);
    };
    _applyDockHeight();
    if (window.ResizeObserver) {
      new ResizeObserver(_applyDockHeight).observe(_dock);
    }
  }
})();
