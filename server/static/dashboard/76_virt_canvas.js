// === virt canvas + plate overlay (placeholders) ===

  // Per-camera mini 3D pose canvas — renders beside each preview panel.
  // Reuses `basePlot` (from /calibration/state) by keeping traces with
  // meta.camera_id == this cam PLUS shared world traces (no meta/camera_id).
  // Tiny Plotly react on each calibration tick; layout cached per host.
  // Per-camera 2D reprojection (K·[R|t]·P). Ported from the viewer's
  // drawVirtCanvas: project the home-plate pentagon through THIS camera's
  // own calibration so the dashed outline lands where the camera sees the
  // plate. If the reprojected outline doesn't sit on top of the plate in
  // the real preview above, calibration is off.
  {PLATE_WORLD_JS}
  // Populated by tickCalibration from /calibration/state `scene.cameras`.
  const virtCamMeta = new Map();
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
  {DRAW_PLATE_OVERLAY_JS}
  function drawVirtCanvas(canvas, cam) {
    return !!drawVirtualBase(canvas, cam);
  }
  function redrawAllVirtCanvases() {
    for (const canvas of document.querySelectorAll('[data-virt-canvas]')) {
      const cam = canvas.dataset.virtCanvas;
      const meta = virtCamMeta.get(cam);
      const cell = canvas.closest('.virt-cell');
      const ok = drawVirtCanvas(canvas, meta);
      if (cell) cell.classList.toggle('ready', ok);
    }
  }
  function redrawAllPreviewPlateOverlays() {
    for (const svg of document.querySelectorAll('[data-preview-overlay]')) {
      const cam = svg.dataset.previewOverlay;
      const meta = virtCamMeta.get(cam);
      redrawPlateOverlay(svg, meta);
    }
  }
  window.addEventListener('resize', () => {
    redrawAllVirtCanvases();
    redrawAllPreviewPlateOverlays();
  });
