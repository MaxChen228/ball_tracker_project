  function resizeOneCanvas(canvas) {
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight || 32;
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
  // A / B / SEG share strip height equally. Earlier 12/12/8 split made the
  // SEG band feel cramped when the operator dragged the timeline taller.
  // Equal thirds scale cleanly with `.timeline.is-resized` (canvas height
  // grows with the panel).
  const SEG_BAND_FRAC_A = 1 / 3;
  const SEG_BAND_FRAC_B = 1 / 3;
  // Mirror of points_layer.js SEG_PALETTE — duplicated here because the
  // classic IIFE bundle doesn't share an importmap with the ESM layer
  // modules. test_viewer.py asserts the two lists stay in lockstep.
  const SEG_PALETTE_HEX = [
    0xE45756, 0x4C78A8, 0x54A24B, 0xF58518,
    0xB279A2, 0x72B7B2, 0xFF9DA6, 0x9D755D,
  ];
  function _segCss(i) {
    const hex = SEG_PALETTE_HEX[i % SEG_PALETTE_HEX.length];
    return `#${hex.toString(16).padStart(6, "0")}`;
  }
  // Greedy interval-scheduling lane assignment for overlapping segments.
  // Within a single path, segments _can_ overlap on edge cases (e.g.
  // segmenter retunes mid-recompute). Stack overlapping segs into
  // separate lanes so we never overpaint and lose information; non-
  // overlapping segs all collapse into lane 0 (the common case).
  // Returns { laneOf: number[N] aligned with input order, laneCount }.
  function assignSegmentLanes(segs) {
    if (!segs || !segs.length) return { laneOf: [], laneCount: 0 };
    const items = segs.map((seg, idx) => ({ seg, idx }));
    items.sort((a, b) => a.seg.t_start - b.seg.t_start);
    const laneEnds = [];
    const laneOf = new Array(segs.length);
    for (const { seg, idx } of items) {
      let placed = -1;
      for (let i = 0; i < laneEnds.length; ++i) {
        // Strict <= so two segs sharing exactly t_end == t_start (back-to-
        // back) collapse into one lane.
        if (laneEnds[i] <= seg.t_start + 1e-6) {
          laneEnds[i] = seg.t_end;
          placed = i;
          break;
        }
      }
      if (placed === -1) {
        laneEnds.push(seg.t_end);
        placed = laneEnds.length - 1;
      }
      laneOf[idx] = placed;
    }
    return { laneOf, laneCount: laneEnds.length };
  }
  // Convert a SegmentRecord t-bound to a strip x pixel. Frame-index
  // linear so seg bands align with detection columns at the same frame
  // (time-linear would drift on non-uniform unionTimes, e.g. a session
  // with sparse pre-arm frames + dense in-flight frames).
  function _segXForT(t, W) {
    if (TOTAL_FRAMES <= 1) return 0;
    const i = frameIndexForT(t);
    return Math.round(i * (W - 1) / (TOTAL_FRAMES - 1));
  }
  // Layout cache of the SEG band positions so hover hit-tests can read
  // them without recomputing. Re-written every drawStripInto call;
  // hit-test reads via STRIP_ROWS[path].canvas._segLayout.
  function _layoutSegBand(canvas, sH, yS, segs) {
    const { laneOf, laneCount } = assignSegmentLanes(segs);
    const laneH = laneCount ? Math.max(1, Math.floor(sH / laneCount)) : sH;
    canvas._segLayout = {
      yS, sH, laneH, laneCount, laneOf,
      // segs reference is captured so the hit-test sees the same array
      // that was just painted; SEGMENTS_BY_PATH reassigns wholesale on
      // recompute, so a stale ref here would just resolve to "no hit".
      segs,
    };
    return canvas._segLayout;
  }
  function drawStripInto(canvas, strips, path) {
    const W = canvas.width, H = canvas.height;
    if (!W || !H) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    // 3 sub-bands stacked: A · B · SEG, equal thirds of the canvas
    // height. SEG is the residual so floor() rounding never leaks pixels.
    const aH = Math.floor(H * SEG_BAND_FRAC_A);
    const bH = Math.floor(H * SEG_BAND_FRAC_B);
    const sH = H - aH - bH;
    const yA = 0, yB = aH, yS = aH + bH;
    // ---- A / B detection ----
    for (const [yi, hh, cam] of [[yA, aH, "A"], [yB, bH, "B"]]) {
      ctx.fillStyle = STRIP_EMPTY;
      ctx.fillRect(0, yi, W, hh);
      const strip = strips[cam];
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
        ctx.fillRect(x, yi, 1, hh);
      }
    }
    // ---- SEG band ----
    ctx.fillStyle = STRIP_EMPTY;
    ctx.fillRect(0, yS, W, sH);
    const segs = (SEGMENTS_BY_PATH && Array.isArray(SEGMENTS_BY_PATH[path]))
      ? SEGMENTS_BY_PATH[path] : [];
    if (segs.length) {
      const layout = _layoutSegBand(canvas, sH, yS, segs);
      for (let i = 0; i < segs.length; ++i) {
        const seg = segs[i];
        if (!seg || typeof seg.t_start !== "number" || typeof seg.t_end !== "number") continue;
        const x0 = _segXForT(seg.t_start, W);
        const x1 = _segXForT(seg.t_end, W);
        const xLeft = Math.min(x0, x1);
        const w = Math.max(1, Math.abs(x1 - x0));
        const lane = layout.laneOf[i];
        const y = yS + lane * layout.laneH;
        // Last lane fills the remaining sliver so floor() truncation
        // doesn't leave a 1-px empty strip at the bottom.
        const h = (lane === layout.laneCount - 1)
          ? (sH - lane * layout.laneH)
          : layout.laneH;
        ctx.fillStyle = _segCss(i);
        ctx.globalAlpha = 0.85;
        ctx.fillRect(xLeft, y, w, h);
        // Playback active highlight — the seg whose [t_start, t_end]
        // contains currentT in playback mode reads at full alpha plus
        // a thin border. In "all" mode we don't mark anything because
        // currentT isn't conceptually pinned to a moment.
        if (mode === "playback"
            && currentT >= seg.t_start - 1e-3
            && currentT <= seg.t_end + 1e-3) {
          ctx.globalAlpha = 1.0;
          ctx.fillRect(xLeft, y, w, h);
          ctx.strokeStyle = "#1a1610";
          ctx.lineWidth = 1;
          ctx.strokeRect(xLeft + 0.5, y + 0.5, Math.max(0, w - 1), Math.max(0, h - 1));
        }
        ctx.globalAlpha = 1.0;
      }
    } else {
      canvas._segLayout = { yS, sH, laneH: sH, laneCount: 0, laneOf: [], segs: [] };
    }
    // ---- chirp marker + head cursor (z-order top, so they sit above
    // both detection columns and seg bands) ----
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

  // Hover tooltip + click-to-scrub for the SEG band. One DOM tooltip
  // shared across both LIVE / SVR canvases — there's no scenario where
  // the operator hovers both at once.
  let _segTooltip = null;
  function _ensureSegTooltip() {
    if (_segTooltip) return _segTooltip;
    const el = document.createElement("div");
    // Reuse the fit-hover-tooltip CSS (same look, same z-index) so the
    // 3D fit hover and the strip hover read as the same UI primitive.
    el.className = "fit-hover-tooltip seg-hover-tooltip";
    el.style.display = "none";
    document.body.appendChild(el);
    _segTooltip = el;
    return el;
  }
  function _segHitTest(canvas, ev) {
    const layout = canvas._segLayout;
    if (!layout || !layout.segs || !layout.segs.length) return null;
    const rect = canvas.getBoundingClientRect();
    // Map CSS px → backing-store px (DPR-aware) so y comparisons line
    // up with the y values we used at draw time.
    const dprY = canvas.height / Math.max(1, rect.height);
    const dprX = canvas.width / Math.max(1, rect.width);
    const cssX = ev.clientX - rect.left;
    const cssY = ev.clientY - rect.top;
    const px = cssX * dprX;
    const py = cssY * dprY;
    if (py < layout.yS || py >= layout.yS + layout.sH) return null;
    const lane = layout.laneCount
      ? Math.min(layout.laneCount - 1, Math.floor((py - layout.yS) / layout.laneH))
      : 0;
    const i = TOTAL_FRAMES <= 1
      ? 0
      : Math.min(TOTAL_FRAMES - 1,
                 Math.max(0, Math.round(px * (TOTAL_FRAMES - 1) / (canvas.width - 1))));
    const t = unionTimes[i];
    for (let si = 0; si < layout.segs.length; ++si) {
      if (layout.laneOf[si] !== lane) continue;
      const s = layout.segs[si];
      if (t >= s.t_start - 1e-3 && t <= s.t_end + 1e-3) {
        return { seg: s, segIndex: si, t };
      }
    }
    return null;
  }
  function _wireSegInteractions(canvas, path) {
    if (canvas.dataset.segWired === "1") return;
    canvas.dataset.segWired = "1";
    canvas.style.cursor = "default";
    const tooltip = _ensureSegTooltip();
    canvas.addEventListener("pointermove", (ev) => {
      const hit = _segHitTest(canvas, ev);
      if (!hit) {
        tooltip.style.display = "none";
        canvas.style.cursor = "default";
        return;
      }
      const seg = hit.seg;
      const durMs = Math.round((seg.t_end - seg.t_start) * 1000);
      const rmseCm = Number.isFinite(seg.rmse_m)
        ? `${(seg.rmse_m * 100).toFixed(1)}cm` : "—";
      const kph = Number.isFinite(seg.speed_kph)
        ? seg.speed_kph.toFixed(1) : "—";
      tooltip.textContent = `${kph} km/h · ${durMs}ms · rmse ${rmseCm}`;
      tooltip.style.left = `${ev.clientX + 12}px`;
      tooltip.style.top = `${ev.clientY - 24}px`;
      tooltip.style.display = "block";
      canvas.style.cursor = "pointer";
    });
    canvas.addEventListener("pointerleave", () => {
      tooltip.style.display = "none";
      canvas.style.cursor = "default";
    });
    canvas.addEventListener("click", (ev) => {
      const hit = _segHitTest(canvas, ev);
      if (!hit) return;
      // Jump scrubber + 3D head + cam-view to seg.t_start. setFrame
      // already coordinates videos, scrubber, and BallTrackerViewerScene.setT.
      setFrame(frameIndexForT(hit.seg.t_start));
    });
  }
  function wireSegStripInteractions() {
    for (const path of PATHS) {
      if (!HAS_PATH[path]) continue;
      _wireSegInteractions(STRIP_ROWS[path].canvas, path);
    }
  }
  // Wire interactions once per page-load; idempotent guard inside.
  // Called from 99_end.js boot tail.
  window._wireSegStripInteractions = wireSegStripInteractions;
  // Public hook so the timeline-resizer drag handler can re-fit canvas
  // backing-store px after the strip rows change CSS height.
  window._resizeDetectionCanvas = resizeDetectionCanvas;
