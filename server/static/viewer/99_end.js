  setFrame(0, { seekVideos: true });
  scheduleSceneDraw();
  updatePlayBtnLabel();
  requestAnimationFrame(resizeDetectionCanvas);
})();
