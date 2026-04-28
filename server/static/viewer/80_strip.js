  function resizeOneCanvas(canvas) {
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight || 28;
    const dpr = window.devicePixelRatio || 1;
    const pxW = Math.max(1, Math.floor(cssW * dpr));
    const pxH = Math.max(1, Math.floor(cssH * dpr));
    if (canvas.width !== pxW || canvas.height !== pxH) { canvas.width = pxW; canvas.height = pxH; }
  }
  // Every strip reserves one sub-track per cam, even when that cam has no
  // data on this pipeline — the empty row is load-bearing for single-camera
  // sessions (e.g. live-only A-only) so the operator can see "B is silent"
  // instead of misreading a full-width A track as both cams.
  const STRIP_CAMS = ["A", "B"];
  function drawStripInto(canvas, strips, path) {
    const W = canvas.width, H = canvas.height;
    if (!W || !H) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const rows = STRIP_CAMS.length;
    const rowH = Math.floor(H / rows);
    for (let ci = 0; ci < rows; ++ci) {
      const cam = STRIP_CAMS[ci];
      const strip = strips[cam];
      const y = ci * rowH;
      ctx.fillStyle = STRIP_EMPTY;
      ctx.fillRect(0, y, W, rowH);
      if (!strip) continue;
      // Strip detection coloring is independent of the 3D ray-layer
      // toggles — the strip exists to answer "which frames had a ball
      // detected on this pipeline?", a different question from "do I
      // want to see those rays in the 3D scene right now?". Always
      // paint detected frames in the cam-path colour.
      const detColor = colorForCamPath(cam, path);
      for (let x = 0; x < W; ++x) {
        const i = TOTAL_FRAMES <= 1 ? 0 : Math.min(TOTAL_FRAMES - 1, Math.round(x * (TOTAL_FRAMES - 1) / (W - 1)));
        const e = strip[i];
        if (e === null || e === undefined) continue;
        ctx.fillStyle = e.detected ? detColor : STRIP_MUTED;
        ctx.fillRect(x, y, 1, rowH);
      }
    }
    if (tMin <= 0 && tMax >= 0 && tMax > tMin) {
      const xChirp = Math.round((-tMin) * (W - 1) / (tMax - tMin));
      ctx.fillStyle = STRIP_CHIRP;
      ctx.fillRect(Math.max(0, xChirp - 1), 0, 2, H);
    }
    const xHead = TOTAL_FRAMES <= 1 ? 0 : Math.round(currentFrame * (W - 1) / (TOTAL_FRAMES - 1));
    ctx.fillStyle = STRIP_HEAD;
    ctx.fillRect(Math.max(0, xHead - 1), 0, 2, H);
  }
  function renderDetectionStrip() {
    for (const path of PATHS) {
      if (!HAS_PATH[path]) continue;
      drawStripInto(STRIP_ROWS[path].canvas, camAtFrameByPath[path], path);
    }
  }
  function resizeDetectionCanvas() {
    for (const path of PATHS) {
      if (!HAS_PATH[path]) continue;
      resizeOneCanvas(STRIP_ROWS[path].canvas);
    }
    renderDetectionStrip();
  }
  window.addEventListener("resize", resizeDetectionCanvas);
