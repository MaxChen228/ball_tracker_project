"use strict";

const API_BASE = window.location.origin;

const state = {
  items: [],
  current: null,
  fps: null,
  totalFrames: 0,
  inFrame: null,
  outFrame: null,
  seedFrame: null,
  seedPoint: null,
  pendingSeedClick: false,
  seedMaskReady: false,
  propagateStatus: "idle",
  doneFrames: new Set(),
  seedMaskUrl: null,
  propMaskUrlByFrame: new Map(),
  seedComputing: false,
  seedComputeStartMs: null,
  propPhase: null,
  propExpected: 0,
  propDoneCount: 0,
  propPhaseElapsed: 0,
  propDevice: null,
  propModel: null,
  propStartMs: null,
  lastDisplayedMediaTime: null,
  lastDisplayedFrame: -1,
  rvfcHandle: null,
  sse: null,
  ptsTable: null,
  pendingTargetFrame: null,
  isSeeking: false,
  lastSeekTarget: null,
  scrubMode: false,
  queueRunning: false,
  queueSse: null,
  // Pre-tinted canvases keyed by source frame index. We build one per mask
  // (sync, ~5ms each) the first time we see it, then `showMaskFor` blits it
  // in O(1). Without this cache, every arrow-key step re-decoded the PNG and
  // re-ran the per-pixel tint loop, leaving the overlay blank for 30-60ms —
  // the visible "flicker" the user reported.
  tintedCache: new Map(),
  prefetchAbort: 0,
  // ImageBitmap LRU keyed by source frame index. Scrub-mode paint draws
  // bitmaps to #frame-canvas synchronously alongside the mask blit, so the
  // base image and the overlay can never desynchronise. Without this we
  // had a race: <img>.src=... only starts a fetch+decode, mask blits from
  // memory immediately — two consecutive arrow keys faster than image
  // decode left the displayed frame stale while the mask jumped ahead.
  frameBitmapCache: new Map(),
  framePrefetchAbort: 0,
  scrubPaintToken: 0,
  frameBitmapAspect: null,
  warmUpProgress: null,
};

const FRAME_BITMAP_CACHE_MAX = 800;
const FRAME_PREFETCH_CONCURRENCY = 4;
const WARM_UP_CONCURRENCY = 16;
// Display-only target width for decoded ImageBitmaps. The full-resolution
// JPG (1920×1080 from a 240fps iPhone clip) costs ~5-8ms per drawImage on
// M-series, which jams scrubber drag at 60Hz. Decoding to 960px wide cuts
// drawImage cost ~4×. Server-side frame extraction and SAM2 propagation
// continue to run on the full-res source MOV — this only affects the
// browser preview.
const SCRUB_PREVIEW_MAX_W = 640;

function clearSeedMask() {
  if (state.seedMaskUrl) URL.revokeObjectURL(state.seedMaskUrl);
  state.seedMaskUrl = null;
}

function clearPropMasks() {
  for (const url of state.propMaskUrlByFrame.values()) URL.revokeObjectURL(url);
  state.propMaskUrlByFrame.clear();
}

const el = {
  video: document.getElementById("video"),
  videoWrap: document.querySelector("#video-wrap"),
  frameCanvas: document.getElementById("frame-canvas"),
  overlay: document.getElementById("overlay"),
  scrubber: document.getElementById("scrubber"),
  fills: document.getElementById("timeline-fills"),
  mIn: document.getElementById("marker-in"),
  mOut: document.getElementById("marker-out"),
  mSeed: document.getElementById("marker-seed"),
  status: document.getElementById("status-line"),
  statusbar: document.getElementById("statusbar"),
  itemSlug: document.getElementById("item-slug"),
  itemList: document.getElementById("item-list"),
  btnRescan: document.getElementById("btn-rescan"),
  btnQueue: document.getElementById("btn-queue"),
  seedModel: document.getElementById("seed-model"),
  propModel: document.getElementById("prop-model"),
  btnIn: document.getElementById("btn-in"),
  btnOut: document.getElementById("btn-out"),
  btnSeed: document.getElementById("btn-seed"),
  btnPropagate: document.getElementById("btn-propagate"),
  btnCancel: document.getElementById("btn-cancel"),
};

function showError(msg) {
  el.statusbar.classList.add("error");
  el.status.textContent = "ERROR: " + msg;
  console.error(msg);
  alert(msg);
}

function clearError() {
  el.statusbar.classList.remove("error");
}

function currentFrame() {
  // Frame index = position in the dense PTS list (the only source of truth,
  // mirrors viewer's `unionTimes`). Snap to nearest PTS so rVFC's mediaTime
  // (exact PTS of compositor-painted frame) lands on the matching index.
  const tbl = state.ptsTable;
  if (!tbl || tbl.length === 0) return 0;
  const t = state.lastDisplayedMediaTime != null
    ? state.lastDisplayedMediaTime
    : el.video.currentTime;
  if (t <= tbl[0]) return 0;
  if (t >= tbl[tbl.length - 1]) return tbl.length - 1;
  let lo = 0, hi = tbl.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (tbl[mid] <= t) lo = mid; else hi = mid - 1;
  }
  if (lo + 1 < tbl.length && Math.abs(tbl[lo + 1] - t) < Math.abs(tbl[lo] - t)) return lo + 1;
  return lo;
}

function frameToTime(f) {
  // Dense pts list — pts[f] always defined. Seek to mid-display-window so the
  // browser unambiguously decodes f (not f-1 or f+1).
  const tbl = state.ptsTable;
  if (!tbl || tbl.length === 0) return 0;
  const pts = tbl[f];
  if (f + 1 < tbl.length) return (pts + tbl[f + 1]) / 2;
  const prevGap = f > 0 ? (pts - tbl[f - 1]) : (1 / 240);
  return pts + prevGap / 2;
}

function fmt(n) {
  return String(n == null ? "-" : n);
}

function updateStatus() {
  const f = currentFrame();
  // Scrubber thumb is derived state — keep it locked to currentFrame so play /
  // arrow / pause / propagate-tick paths can never leave a stale value behind.
  if (state.totalFrames > 0 && document.activeElement !== el.scrubber) {
    const want = String(f);
    if (el.scrubber.value !== want) el.scrubber.value = want;
  }
  // Derive `t` from the snapped frame index so f and t can never disagree.
  // Pre-fix `t` came from `el.video.currentTime` during play; that runs
  // ahead of rVFC's `mediaTime` (the actually-painted PTS) by up to one
  // frame, which made `f=NNNN t=X.YYYs` look self-inconsistent.
  const tbl = state.ptsTable;
  let t;
  if (tbl && f >= 0 && f < tbl.length) {
    t = tbl[f];
  } else if (state.lastDisplayedMediaTime != null) {
    t = state.lastDisplayedMediaTime;
  } else {
    t = el.video.currentTime || 0;
  }
  const pt = state.seedPoint ? `(${state.seedPoint[0]},${state.seedPoint[1]})` : "-";
  const fStr = String(f).padStart(4, "0");
  let statusTag = state.propagateStatus;
  let computing = false;
  if (state.seedComputing) {
    const elapsed = ((performance.now() - state.seedComputeStartMs) / 1000).toFixed(1);
    statusTag = `seeding... ${elapsed}s (SAM2 image predictor)`;
    computing = true;
  } else if (state.propagateStatus === "running") {
    const phase = state.propPhase || "starting";
    if (phase === "extracting") {
      const elapsed = state.propStartMs ? ((performance.now() - state.propStartMs) / 1000).toFixed(1) : "0.0";
      statusTag = `propagate: extracting frames [${state.propExpected}] ${elapsed}s`;
    } else if (phase === "extracted") {
      statusTag = `propagate: extracted ${state.propExpected} frames in ${state.propPhaseElapsed}s, loading model...`;
    } else if (phase === "model_loading") {
      statusTag = `propagate: loading SAM2 video predictor...`;
    } else if (phase === "model_ready") {
      statusTag = `propagate: model ready (${state.propModel || "?"} on ${state.propDevice || "?"} in ${state.propPhaseElapsed}s)`;
    } else if (phase === "propagating") {
      const total = state.propExpected || 1;
      const done = state.propDoneCount;
      const elapsedS = state.propStartMs ? (performance.now() - state.propStartMs) / 1000 : 0;
      const fps = elapsedS > 0 ? (done / elapsedS).toFixed(2) : "?";
      const etaS = (fps !== "?" && fps > 0) ? Math.max(0, (total - done) / parseFloat(fps)).toFixed(0) : "?";
      const pct = ((done / total) * 100).toFixed(1);
      statusTag = `propagate: ${done}/${total} (${pct}%) @ ${fps} fps, ETA ${etaS}s`;
    } else {
      statusTag = `propagate: ${phase}`;
    }
    computing = true;
  } else if (state.propagateStatus === "done" && state.propDoneCount > 0) {
    const tail = state.propPhaseElapsed > 0
      ? `in ${state.propPhaseElapsed}s`
      : "(cached on disk)";
    statusTag = `propagate done: ${state.propDoneCount}/${state.propExpected} frames ${tail}`;
  }
  if (computing) el.statusbar.classList.add("computing");
  else el.statusbar.classList.remove("computing");
  let line =
    `f=${fStr} t=${t.toFixed(3)}s | in=${fmt(state.inFrame)} out=${fmt(state.outFrame)} seed=${fmt(state.seedFrame)} pt=${pt} | status=${statusTag}${state.pendingSeedClick ? " (click to set seed point)" : ""}`;
  if (state.warmUpProgress) {
    const { done, total } = state.warmUpProgress;
    line += ` | warm ${done}/${total}`;
  }
  el.status.textContent = line;
}

function updateMarker(markerEl, frame) {
  if (frame == null || state.totalFrames <= 0) {
    markerEl.hidden = true;
    return;
  }
  const pct = (frame / Math.max(1, state.totalFrames - 1)) * 100;
  markerEl.style.left = `calc(${pct}% )`;
  markerEl.hidden = false;
}

function updateMarkers() {
  updateMarker(el.mIn, state.inFrame);
  updateMarker(el.mOut, state.outFrame);
  updateMarker(el.mSeed, state.seedFrame);
}

function updatePropagateBtn() {
  const ready =
    state.inFrame != null &&
    state.outFrame != null &&
    state.seedFrame != null &&
    state.seedPoint != null &&
    state.seedMaskReady &&
    state.propagateStatus !== "running";
  el.btnPropagate.disabled = !ready;
}

function addDoneFill(frame) {
  if (state.totalFrames <= 0) return;
  state.doneFrames.add(frame);
  const div = document.createElement("div");
  div.className = "fill-done";
  const denom = Math.max(1, state.totalFrames - 1);
  const cellPct = 100 / denom;
  const w = Math.max(cellPct, 0.2);
  div.style.left = `${(frame / denom) * 100 - w / 2}%`;
  div.style.width = `${w}%`;
  el.fills.appendChild(div);
}

function clearDoneFills() {
  state.doneFrames.clear();
  el.fills.innerHTML = "";
  clearPropMasks();
  state.tintedCache.clear();
  state.prefetchAbort++;
}

// Diagnostic to pinpoint which layer is broken when user reports
// "different frame indices show identical pixels". Compares:
//   - bytes received from server (sha256 of blob)
//   - decoded ImageBitmap pixels (via offscreen draw + getImageData)
//   - what's actually on the visible #frame-canvas right now
window.debugFrames = async function (a, b) {
  const slug = state.current;
  if (!slug) { console.log("no current slug"); return; }
  const c = el.frameCanvas;
  const ctx = c.getContext("2d");
  const out = { slug, a, b };

  async function sha(buf) {
    const h = await crypto.subtle.digest("SHA-256", buf);
    return [...new Uint8Array(h)].slice(0, 8).map(x => x.toString(16).padStart(2,"0")).join("");
  }
  async function pull(f) {
    const url = `${API_BASE}/frame/${encodeURIComponent(slug)}/${String(f).padStart(5,"0")}.jpg`;
    const r = await fetch(url, { cache: "no-store" });
    const buf = await r.arrayBuffer();
    const blob = new Blob([buf], { type: "image/jpeg" });
    const bm = await createImageBitmap(blob);
    const off = new OffscreenCanvas(bm.width, bm.height);
    const octx = off.getContext("2d");
    octx.drawImage(bm, 0, 0);
    const data = octx.getImageData(0, 0, Math.min(200, bm.width), Math.min(200, bm.height)).data;
    return { bytesHash: await sha(buf), bytesLen: buf.byteLength, w: bm.width, h: bm.height, sample: data, bm };
  }

  const [ra, rb] = await Promise.all([pull(a), pull(b)]);
  out.fetched_a = { hash: ra.bytesHash, len: ra.bytesLen, dim: `${ra.w}x${ra.h}` };
  out.fetched_b = { hash: rb.bytesHash, len: rb.bytesLen, dim: `${rb.w}x${rb.h}` };

  let sampleSame = 0;
  for (let i = 0; i < ra.sample.length; i++) if (ra.sample[i] === rb.sample[i]) sampleSame++;
  out.decoded_pixel_same_pct = (sampleSame / ra.sample.length * 100).toFixed(2) + "%";

  // Force-paint a then b to the real visible canvas with delay so user can SEE.
  ctx.clearRect(0, 0, c.width, c.height);
  if (c.width !== ra.w) c.width = ra.w;
  if (c.height !== ra.h) c.height = ra.h;
  ctx.drawImage(ra.bm, 0, 0);
  await new Promise(r => setTimeout(r, 800));
  const visA = ctx.getImageData(0, 0, 200, 200).data;

  ctx.clearRect(0, 0, c.width, c.height);
  ctx.drawImage(rb.bm, 0, 0);
  await new Promise(r => setTimeout(r, 800));
  const visB = ctx.getImageData(0, 0, 200, 200).data;

  let visSame = 0;
  for (let i = 0; i < visA.length; i++) if (visA[i] === visB[i]) visSame++;
  out.canvas_pixel_same_pct = (visSame / visA.length * 100).toFixed(2) + "%";

  // Cache state
  out.cache_a = state.frameBitmapCache.has(a);
  out.cache_b = state.frameBitmapCache.has(b);
  out.cache_a_eq_b = state.frameBitmapCache.get(a) === state.frameBitmapCache.get(b);

  // Visible elements
  const vw = el.videoWrap.getBoundingClientRect();
  const cv = c.getBoundingClientRect();
  const vd = el.video.getBoundingClientRect();
  out.scrub_class = el.videoWrap.classList.contains("scrub");
  out.canvas_rect = `${cv.width.toFixed(0)}x${cv.height.toFixed(0)} @ ${cv.left.toFixed(0)},${cv.top.toFixed(0)}`;
  out.video_rect = `${vd.width.toFixed(0)}x${vd.height.toFixed(0)} @ ${vd.left.toFixed(0)},${vd.top.toFixed(0)}`;
  out.video_visibility = getComputedStyle(el.video).visibility;
  out.video_currentTime = el.video.currentTime;

  console.log(out);
  return out;
};

window.debugMask = async function (frame) {
  const out = { frame };
  out.urlMapHas = state.propMaskUrlByFrame.has(frame);
  out.url = state.propMaskUrlByFrame.get(frame) || null;
  out.cacheHas = state.tintedCache.has(frame);
  if (out.cacheHas) {
    const c = state.tintedCache.get(frame);
    out.cacheSize = `${c.width}x${c.height}`;
    const ctx = c.getContext("2d");
    const img = ctx.getImageData(0, 0, c.width, c.height);
    let nz = 0;
    for (let i = 3; i < img.data.length; i += 4) if (img.data[i] > 0) nz++;
    out.cacheNonzeroAlpha = nz;
  }
  if (out.url) {
    const r = await fetch(out.url);
    out.fetchStatus = r.status;
    const blob = await r.blob();
    out.fetchBytes = blob.size;
    const im = new Image();
    im.src = URL.createObjectURL(blob);
    await new Promise((res, rej) => { im.onload = res; im.onerror = rej; });
    out.imgNatural = `${im.naturalWidth}x${im.naturalHeight}`;
    const tmp = document.createElement("canvas");
    tmp.width = im.naturalWidth; tmp.height = im.naturalHeight;
    const tctx = tmp.getContext("2d");
    tctx.drawImage(im, 0, 0);
    const id = tctx.getImageData(0, 0, tmp.width, tmp.height);
    let nz = 0;
    for (let i = 0; i < id.data.length; i += 4) if (id.data[i] > 0) nz++;
    out.rawNonzeroPixels = nz;
  }
  out.overlaySize = `${el.overlay.width}x${el.overlay.height}`;
  out.currentFrame = currentFrame();
  console.log(JSON.stringify(out, null, 2));
  return out;
};

window.debugTimeline = function () {
  const wrap = document.getElementById("timeline-wrap");
  const wrapRect = wrap.getBoundingClientRect();
  const W = wrapRect.width;
  const N = state.totalFrames;
  const denom = Math.max(1, N - 1);
  const expectPct = (f) => (f / denom) * 100;
  const elemPct = (rect) => ((rect.left - wrapRect.left) / W) * 100;
  const out = {
    totalFrames: N,
    timelineWidthPx: W.toFixed(2),
    inFrame: state.inFrame, outFrame: state.outFrame, seedFrame: state.seedFrame,
    expectedPct: {
      in: state.inFrame != null ? expectPct(state.inFrame).toFixed(3) : null,
      out: state.outFrame != null ? expectPct(state.outFrame).toFixed(3) : null,
      seed: state.seedFrame != null ? expectPct(state.seedFrame).toFixed(3) : null,
    },
    actualMarkerPct: {
      in: !el.mIn.hidden ? elemPct(el.mIn.getBoundingClientRect()).toFixed(3) : "hidden",
      out: !el.mOut.hidden ? elemPct(el.mOut.getBoundingClientRect()).toFixed(3) : "hidden",
      seed: !el.mSeed.hidden ? elemPct(el.mSeed.getBoundingClientRect()).toFixed(3) : "hidden",
    },
  };
  const fillDivs = el.fills.children;
  if (fillDivs.length > 0) {
    const first = fillDivs[0].getBoundingClientRect();
    const last = fillDivs[fillDivs.length - 1].getBoundingClientRect();
    const sortedFrames = [...state.doneFrames].sort((a, b) => a - b);
    out.fillCount = fillDivs.length;
    out.fillFrameRange = [sortedFrames[0], sortedFrames[sortedFrames.length - 1]];
    out.fillFirstActualPct = elemPct(first).toFixed(3);
    out.fillFirstExpectedPct = expectPct(sortedFrames[0]).toFixed(3);
    out.fillLastActualLeftPct = elemPct(last).toFixed(3);
    out.fillLastActualRightPct = (((last.right - wrapRect.left) / W) * 100).toFixed(3);
    out.fillLastExpectedPct = expectPct(sortedFrames[sortedFrames.length - 1]).toFixed(3);
  }
  const thumbVal = parseInt(el.scrubber.value, 10);
  out.scrubberValue = thumbVal;
  out.scrubberExpectedPct = expectPct(thumbVal).toFixed(3);
  console.table(out);
  return out;
};

function videoDisplayRect() {
  // Returns the actual displayed video frame rect inside the <video> element,
  // accounting for letterbox/pillarbox. Coords are in CSS pixels relative to
  // the viewport (same frame as getBoundingClientRect()).
  const v = el.video;
  if (!v.videoWidth || !v.videoHeight) return null;
  const rect = v.getBoundingClientRect();
  const elemRatio = rect.width / rect.height;
  const vidRatio = v.videoWidth / v.videoHeight;
  let dispW, dispH, padX, padY;
  if (elemRatio > vidRatio) {
    dispH = rect.height; dispW = dispH * vidRatio;
    padX = (rect.width - dispW) / 2; padY = 0;
  } else {
    dispW = rect.width; dispH = dispW / vidRatio;
    padX = 0; padY = (rect.height - dispH) / 2;
  }
  return { left: rect.left + padX, top: rect.top + padY, width: dispW, height: dispH };
}

function resizeOverlay() {
  const v = el.video;
  const disp = videoDisplayRect();
  if (!disp) return;
  // Only assign canvas .width / .height when they actually change — assigning
  // even the same value is destructive (it resets the canvas and wipes the
  // previously-blitted mask). Calling this every rVFC tick was the source
  // of the play-time mask flicker.
  if (el.overlay.width !== v.videoWidth) el.overlay.width = v.videoWidth;
  if (el.overlay.height !== v.videoHeight) el.overlay.height = v.videoHeight;
  const wrapRect = el.video.parentElement.getBoundingClientRect();
  el.overlay.style.width = disp.width + "px";
  el.overlay.style.height = disp.height + "px";
  el.overlay.style.left = (disp.left - wrapRect.left) + "px";
  el.overlay.style.top = (disp.top - wrapRect.top) + "px";
}

function clearOverlay() {
  const ctx = el.overlay.getContext("2d");
  ctx.clearRect(0, 0, el.overlay.width, el.overlay.height);
}

function _evictOldestFrameBitmap() {
  const it = state.frameBitmapCache.entries().next();
  if (it.done) return;
  const [k, bm] = it.value;
  state.frameBitmapCache.delete(k);
  if (bm && typeof bm.close === "function") bm.close();
}

function enforceFrameCacheLRU() {
  while (state.frameBitmapCache.size > FRAME_BITMAP_CACHE_MAX) _evictOldestFrameBitmap();
}

function touchFrameBitmap(frame) {
  const v = state.frameBitmapCache.get(frame);
  if (v === undefined) return undefined;
  state.frameBitmapCache.delete(frame);
  state.frameBitmapCache.set(frame, v);
  return v;
}

function clearFrameBitmapCache() {
  state.framePrefetchAbort++;
  for (const bm of state.frameBitmapCache.values()) {
    if (bm && typeof bm.close === "function") bm.close();
  }
  state.frameBitmapCache.clear();
  state.frameBitmapAspect = null;
}

async function fetchFrameBitmap(slug, frame) {
  const url = `${API_BASE}/frame/${encodeURIComponent(slug)}/${String(frame).padStart(5, "0")}.jpg`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`frame ${frame} HTTP ${r.status}`);
  const blob = await r.blob();
  // Decode at preview resolution (resize done off-thread as part of decode).
  // resizeWidth alone preserves aspect ratio per spec; Chrome/Safari honour
  // this. Only the browser preview uses this — server-side propagation reads
  // the original full-res frames directly off disk.
  return await createImageBitmap(blob, {
    resizeWidth: SCRUB_PREVIEW_MAX_W,
    resizeQuality: "medium",
  });
}

async function ensureFrameBitmap(slug, frame) {
  const cached = touchFrameBitmap(frame);
  if (cached) return cached;
  const bm = await fetchFrameBitmap(slug, frame);
  // If the user switched items mid-fetch, drop the bitmap rather than
  // polluting the new item's cache with a stale frame. The caller's
  // scrubPaintToken check will short-circuit before it tries to draw.
  if (slug !== state.current) {
    if (bm && typeof bm.close === "function") bm.close();
    return bm;
  }
  const existing = state.frameBitmapCache.get(frame);
  if (existing) {
    if (bm && typeof bm.close === "function") bm.close();
    return touchFrameBitmap(frame);
  }
  state.frameBitmapCache.set(frame, bm);
  enforceFrameCacheLRU();
  return bm;
}

function drawFrameBitmap(bm) {
  const c = el.frameCanvas;
  const ctx = c.getContext("2d");
  if (c.width !== bm.width) c.width = bm.width;
  if (c.height !== bm.height) c.height = bm.height;
  // Backing-store aspect drives the canvas's intrinsic ratio so
  // `object-fit: contain` produces the same letterbox box as the overlay's
  // `videoDisplayRect()`-derived rect.
  state.frameBitmapAspect = bm.width / bm.height;
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.drawImage(bm, 0, 0);
}

function ensureTintedCanvas(frame, url) {
  return new Promise((resolve) => {
    if (state.tintedCache.has(frame)) { resolve(); return; }
    const img = new Image();
    img.onload = () => {
      try {
        if (el.overlay.width && el.overlay.height) {
          state.tintedCache.set(frame, buildTintedCanvas(img));
        }
      } catch (_) { /* swallow — caller will fall back to clearOverlay */ }
      resolve();
    };
    img.onerror = () => resolve();
    img.src = url;
  });
}

// Atomic visual update: image + mask drawn in the same tick so the user
// can never observe one moving without the other. Caller is responsible
// for ensuring the tinted-mask cache is warm (via scheduleScrubPaint).
function paintAtomic(frame, bm) {
  drawFrameBitmap(bm);
  const url = maskUrlForFrame(frame);
  if (url) {
    if (!blitCachedMask(frame)) clearOverlay();
  } else {
    clearOverlay();
    if (frame === state.seedFrame && state.seedPoint) {
      drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
    }
  }
}

// Schedules an atomic paint for `frame`. On full cache hit (bitmap + mask
// tinted) paints synchronously this tick. On any miss keeps the previous
// canvas pixels (no flicker, no mid-load desync) and paints both layers
// together once everything is ready — but only if the user hasn't moved
// on to a different frame in the meantime.
// Find the closest frame index that already has a cached bitmap. Used as a
// "good enough" preview while the exact target is still being fetched, so
// fast scrub-drags don't visibly stall on cache misses.
function nearestCachedFrame(frame) {
  let best = null, bestDist = Infinity;
  for (const f of state.frameBitmapCache.keys()) {
    const d = Math.abs(f - frame);
    if (d < bestDist) { bestDist = d; best = f; }
  }
  return best;
}

async function scheduleScrubPaint(frame) {
  const token = ++state.scrubPaintToken;
  let bm = touchFrameBitmap(frame);
  if (!bm) {
    // Cache miss: paint nearest cached neighbor immediately so the scrubber
    // tracks the user's drag without visible stutter. The exact target lands
    // when the fetch completes (token-checked).
    const near = nearestCachedFrame(frame);
    if (near != null) {
      const nearBm = state.frameBitmapCache.get(near);
      if (nearBm) paintAtomic(near, nearBm);
    }
    try {
      bm = await ensureFrameBitmap(state.current, frame);
    } catch (e) {
      console.warn("frame bitmap fetch failed", frame, e);
      return;
    }
    if (token !== state.scrubPaintToken) return;
  }
  const maskUrl = maskUrlForFrame(frame);
  if (maskUrl && !state.tintedCache.has(frame)) {
    await ensureTintedCanvas(frame, maskUrl);
    if (token !== state.scrubPaintToken) return;
  }
  if (state.current == null) return;
  paintAtomic(frame, bm);
}

async function prefetchFrames(slug) {
  const myToken = ++state.framePrefetchAbort;
  const total = state.totalFrames;
  if (!total) return;
  // Priority order: [in, out] first, then expand outward toward 0 and total-1
  // so scrub-drag outside the trim range still hits cache after a brief warm-up.
  const lo = state.inFrame != null ? state.inFrame : 0;
  const hi = state.outFrame != null ? state.outFrame : total - 1;
  const seen = new Set();
  const frames = [];
  const push = (f) => {
    if (f < 0 || f >= total) return;
    if (seen.has(f)) return;
    seen.add(f);
    if (!state.frameBitmapCache.has(f)) frames.push(f);
  };
  for (let f = lo; f <= hi; f++) push(f);
  // Expand outward in alternating steps from the range edges.
  const maxOut = Math.max(lo, total - 1 - hi);
  for (let d = 1; d <= maxOut; d++) {
    push(lo - d);
    push(hi + d);
  }
  let cursor = 0;
  const worker = async () => {
    while (cursor < frames.length) {
      if (state.current !== slug || myToken !== state.framePrefetchAbort) return;
      const f = frames[cursor++];
      try {
        const bm = await fetchFrameBitmap(slug, f);
        if (state.current !== slug || myToken !== state.framePrefetchAbort) {
          if (bm && typeof bm.close === "function") bm.close();
          return;
        }
        if (state.frameBitmapCache.has(f)) {
          if (bm && typeof bm.close === "function") bm.close();
        } else {
          state.frameBitmapCache.set(f, bm);
          enforceFrameCacheLRU();
        }
      } catch (_) {
        // Per-frame fetch failures are non-fatal; the on-demand path will
        // surface a real error if the user ever lands on this frame.
      }
    }
  };
  await Promise.all(Array.from({ length: FRAME_PREFETCH_CONCURRENCY }, worker));
}

// Aggressive warm-up: decode every frame in [in, out] at high concurrency so
// the user can scrub the full clip with 100% cache hits. After warm-up
// finishes, falls through to prefetchFrames() for outward expansion.
async function warmUpClip(slug) {
  const myToken = ++state.framePrefetchAbort;
  const lo = state.inFrame;
  const hi = state.outFrame;
  if (lo == null || hi == null) return;

  const frames = [];
  for (let f = lo; f <= hi; f++) {
    if (!state.frameBitmapCache.has(f)) frames.push(f);
  }
  if (!frames.length) {
    prefetchFrames(slug);
    return;
  }

  state.warmUpProgress = { done: 0, total: frames.length };
  updateStatus();

  const aborted = () =>
    state.current !== slug || myToken !== state.framePrefetchAbort;

  let cursor = 0;
  const worker = async () => {
    while (cursor < frames.length) {
      if (aborted()) return;
      const f = frames[cursor++];
      try {
        const bm = await fetchFrameBitmap(slug, f);
        if (aborted()) {
          if (bm && typeof bm.close === "function") bm.close();
          return;
        }
        if (state.frameBitmapCache.has(f)) {
          if (bm && typeof bm.close === "function") bm.close();
        } else {
          state.frameBitmapCache.set(f, bm);
          enforceFrameCacheLRU();
        }
        state.warmUpProgress.done++;
        updateStatus();
      } catch (_) {
        // non-fatal; on-demand fetch will surface real errors
      }
    }
  };
  await Promise.all(Array.from({ length: WARM_UP_CONCURRENCY }, worker));
  // Only this invocation may clear progress — if we were superseded, the
  // newer warmUpClip owns the field.
  if (myToken === state.framePrefetchAbort) {
    state.warmUpProgress = null;
    updateStatus();
    if (state.current === slug) prefetchFrames(slug);
  }
}

function drawClickMarker(x, y) {
  const c = el.overlay;
  const ctx = c.getContext("2d");
  ctx.save();
  ctx.strokeStyle = "rgba(239, 68, 68, 1.0)";
  ctx.fillStyle = "rgba(239, 68, 68, 0.6)";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.arc(x, y, 14, 0, 2 * Math.PI);
  ctx.fill();
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x - 22, y); ctx.lineTo(x + 22, y);
  ctx.moveTo(x, y - 22); ctx.lineTo(x, y + 22);
  ctx.stroke();
  ctx.restore();
}

function buildTintedCanvas(img) {
  const c = document.createElement("canvas");
  c.width = el.overlay.width;
  c.height = el.overlay.height;
  const ctx = c.getContext("2d");
  ctx.drawImage(img, 0, 0, c.width, c.height);
  const data = ctx.getImageData(0, 0, c.width, c.height);
  const px = data.data;
  for (let i = 0; i < px.length; i += 4) {
    const a = px[i] || px[i + 1] || px[i + 2];
    if (a > 8) {
      px[i] = 34;
      px[i + 1] = 197;
      px[i + 2] = 94;
      px[i + 3] = 128;
    } else {
      px[i + 3] = 0;
    }
  }
  ctx.putImageData(data, 0, 0);
  return c;
}

function blitCachedMask(frame) {
  const cached = state.tintedCache.get(frame);
  if (!cached) return false;
  const ctx = el.overlay.getContext("2d");
  ctx.clearRect(0, 0, el.overlay.width, el.overlay.height);
  ctx.drawImage(cached, 0, 0);
  if (frame === state.seedFrame && state.seedPoint) {
    drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
  }
  return true;
}

function maskUrlForFrame(frame) {
  if (state.propMaskUrlByFrame.has(frame)) return state.propMaskUrlByFrame.get(frame);
  if (frame === state.seedFrame && state.seedMaskUrl) return state.seedMaskUrl;
  return null;
}

function loadMaskForFrame(frame) {
  const url = maskUrlForFrame(frame);
  if (!url) {
    clearOverlay();
    return;
  }
  if (blitCachedMask(frame)) return;
  // Cache miss — keep whatever is on the overlay (avoid the blank-flash) and
  // build the tint asynchronously. Once cached, redraw if the user is still
  // on this frame.
  const img = new Image();
  img.onload = () => {
    if (!el.overlay.width || !el.overlay.height) return;
    state.tintedCache.set(frame, buildTintedCanvas(img));
    if (currentFrame() === frame) blitCachedMask(frame);
  };
  img.onerror = () => clearOverlay();
  img.src = url;
}

async function prefetchMasks(slug) {
  const myToken = ++state.prefetchAbort;
  if (!el.overlay.width || !el.overlay.height) {
    el.video.addEventListener("loadedmetadata", () => prefetchMasks(slug), { once: true });
    return;
  }
  const entries = Array.from(state.propMaskUrlByFrame.entries())
    .sort((a, b) => a[0] - b[0])
    .filter(([frame]) => !state.tintedCache.has(frame));
  // Parallel load with bounded concurrency. Sequential await meant a 262-mask
  // prefetch took ~3 seconds; users dragging the scrubber within that window
  // hit cold cache and saw no mask. 8-way parallel finishes in ~400ms over
  // localhost.
  const concurrency = 8;
  let cursor = 0;
  const worker = async () => {
    while (cursor < entries.length) {
      if (state.current !== slug || myToken !== state.prefetchAbort) return;
      const [frame, url] = entries[cursor++];
      await new Promise((resolve) => {
        const img = new Image();
        img.onload = () => {
          try { state.tintedCache.set(frame, buildTintedCanvas(img)); } catch (_) {}
          resolve();
        };
        img.onerror = () => resolve();
        img.src = url;
      });
    }
  };
  await Promise.all(Array.from({ length: concurrency }, worker));
}

async function fetchItems() {
  const r = await fetch(`${API_BASE}/api/items`);
  if (!r.ok) throw new Error(`/api/items HTTP ${r.status}`);
  const j = await r.json();
  if (!j || !Array.isArray(j.items)) throw new Error("/api/items: malformed response");
  return j.items;
}

function pickInitialSlug(items) {
  const hash = window.location.hash || "";
  const m = hash.match(/slug=([^&]+)/);
  if (m) return decodeURIComponent(m[1]);
  if (items.length === 0) return null;
  return items[0].slug;
}

function syncFromItem(item) {
  state.current = item.slug;
  if (item.fps == null) {
    showError(`item ${item.slug}: fps missing in /api/items response`);
    return false;
  }
  state.fps = item.fps;
  // totalFrames + scrubber.max get set authoritatively by fetchPts below;
  // seed with item.total_frames so the UI isn't blank during the few hundred
  // ms it takes to load the dense PTS list.
  state.totalFrames = item.total_frames || 0;
  state.inFrame = item.in_frame == null ? null : item.in_frame;
  state.outFrame = item.out_frame == null ? null : item.out_frame;
  state.seedFrame = item.seed_frame == null ? null : item.seed_frame;
  state.seedPoint = item.seed_point == null ? null : item.seed_point;
  state.propagateStatus = item.propagate_status || "idle";
  state.seedMaskReady = state.seedFrame != null && state.seedPoint != null;
  el.itemSlug.textContent = item.slug;
  updateSidebarActive();
  el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
  el.scrubber.value = "0";
  el.video.src = `${API_BASE}/clip/${item.slug}.mp4`;
  state.lastDisplayedFrame = 0;
  enterScrubMode();
  // Drop the previous item's bitmap cache (closes ImageBitmaps to free GPU
  // memory) and request frame 0 via the atomic paint pipeline.
  clearFrameBitmapCache();
  state.scrubPaintToken++;
  scheduleScrubPaint(0);
  // Grab focus from the URL bar so the very first space-press toggles play
  // without the user having to click into the page first.
  el.videoWrap.focus();
  clearSeedMask();
  clearDoneFills();
  clearOverlay();
  state.ptsTable = null;
  state.pendingTargetFrame = null;
  state.isSeeking = false;
  state.lastSeekTarget = null;
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  startSse();
  fetchPts(item.slug);
  rehydrateMasks(item.slug);
  return true;
}

async function selectSlug(slug) {
  const item = state.items.find((it) => it.slug === slug);
  if (!item) {
    showError(`slug not found: ${slug}`);
    return;
  }
  syncFromItem(item);
}

async function fetchModels() {
  const r = await fetch(`${API_BASE}/api/models`);
  if (!r.ok) throw new Error(`/api/models HTTP ${r.status}`);
  return r.json();
}

function populateModelPicker(sel, available, active) {
  sel.innerHTML = "";
  for (const id of available) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id.replace("facebook/sam2-hiera-", "");
    if (id === active) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function setActiveModel(kind, modelId) {
  const r = await fetch(`${API_BASE}/api/models`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, model_id: modelId }),
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    showError(`set model failed: HTTP ${r.status} ${text}`);
    return;
  }
  console.log(`active model[${kind}] = ${modelId} (will load on next ${kind === "seed" ? "/seed" : "/propagate"})`);
}

async function bootstrap() {
  try {
    state.items = await fetchItems();
  } catch (e) {
    showError(String(e));
    return;
  }
  try {
    const models = await fetchModels();
    populateModelPicker(el.seedModel, models.available, models.active.seed);
    populateModelPicker(el.propModel, models.available, models.active.prop);
    el.seedModel.addEventListener("change", () => { setActiveModel("seed", el.seedModel.value); el.seedModel.blur(); });
    el.propModel.addEventListener("change", () => { setActiveModel("prop", el.propModel.value); el.propModel.blur(); });
  } catch (e) {
    showError(`models init failed: ${e}`);
  }
  renderSidebar();
  el.btnRescan.addEventListener("click", rescanItems);
  el.btnQueue.addEventListener("click", toggleQueue);
  startQueueSse();
  window.addEventListener("hashchange", onHashChange);
  const slug = pickInitialSlug(state.items);
  if (!slug) {
    return;  // empty workspace; user can press Rescan
  }
  selectSlug(slug);
}

function onHashChange() {
  const m = (window.location.hash || "").match(/slug=([^&]+)/);
  if (!m) return;
  const slug = decodeURIComponent(m[1]);
  selectSlug(slug);
}

// "ready" = seed marked but not yet propagated → eligible for queue run.
function effectiveStatus(it) {
  const ps = it.status || it.propagate_status || "idle";
  if (ps === "idle" && it.seed_frame != null) return "ready";
  return ps;
}

function renderSidebar() {
  el.itemList.innerHTML = "";
  for (const it of state.items) {
    const card = document.createElement("div");
    card.className = "item-card" + (it.slug === state.current ? " active" : "");
    card.dataset.slug = it.slug;
    const status = effectiveStatus(it);
    const fps = it.fps ? `${Math.round(it.fps)}fps` : "";
    const frames = it.total_frames ? `${it.total_frames}f` : "";
    const meta = [fps, frames].filter(Boolean).join(" · ");
    const nameDiv = document.createElement("div");
    nameDiv.className = "item-card-name";
    const dot = document.createElement("span");
    dot.className = `item-status item-status-${status}`;
    nameDiv.appendChild(dot);
    nameDiv.appendChild(document.createTextNode(it.slug));
    const metaDiv = document.createElement("div");
    metaDiv.className = "item-card-meta";
    metaDiv.textContent = meta;
    const delBtn = document.createElement("button");
    delBtn.className = "item-card-delete";
    delBtn.type = "button";
    delBtn.title = "Delete";
    delBtn.textContent = "✕";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteItem(it.slug);
    });
    card.appendChild(nameDiv);
    card.appendChild(metaDiv);
    card.appendChild(delBtn);
    card.addEventListener("click", () => {
      if (state.current === it.slug) return;
      window.location.hash = `slug=${it.slug}`;
    });
    el.itemList.appendChild(card);
  }
  updateQueueButton();
}

function updateQueueButton() {
  const ready = state.items.filter(it => effectiveStatus(it) === "ready").length;
  if (state.queueRunning) {
    el.btnQueue.textContent = "■ Stop Queue";
    el.btnQueue.classList.add("running");
    el.btnQueue.disabled = false;
  } else {
    el.btnQueue.textContent = `▶ Run Queue (${ready})`;
    el.btnQueue.classList.remove("running");
    el.btnQueue.disabled = ready === 0;
  }
}

function updateSidebarActive() {
  for (const card of el.itemList.querySelectorAll(".item-card")) {
    card.classList.toggle("active", card.dataset.slug === state.current);
  }
}

function updateSidebarStatus(slug, status) {
  const it = state.items.find(i => i.slug === slug);
  if (it) {
    it.status = status;
    it.propagate_status = status;
  }
  const card = el.itemList.querySelector(`.item-card[data-slug="${slug}"]`);
  if (card) {
    const dot = card.querySelector(".item-status");
    if (dot) dot.className = `item-status item-status-${it ? effectiveStatus(it) : status}`;
  }
  updateQueueButton();
}

async function toggleQueue() {
  if (state.queueRunning) {
    if (!confirm("Stop queue? The currently-running session will be cancelled.")) return;
    await fetch(`${API_BASE}/api/queue/cancel`, { method: "POST" });
    return;
  }
  const ready = state.items.filter(it => effectiveStatus(it) === "ready").length;
  if (!ready) return;
  if (!confirm(`Run queue on ${ready} ready session(s)?\n\nSeed model will be unloaded to free memory; reload it via the dropdown when finished.`)) return;
  const r = await fetch(`${API_BASE}/api/queue/run`, { method: "POST" });
  if (!r.ok) {
    showError(`queue start failed: HTTP ${r.status}`);
  }
}

function startQueueSse() {
  if (state.queueSse) { state.queueSse.close(); state.queueSse = null; }
  const es = new EventSource(`${API_BASE}/api/items/__queue__/events`);
  state.queueSse = es;
  es.addEventListener("queue", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    state.queueRunning = !!payload.running;
    updateQueueButton();
  });
  es.onerror = () => { /* browser auto-reconnects */ };
}

async function rescanItems() {
  el.btnRescan.disabled = true;
  try {
    const r = await fetch(`${API_BASE}/api/items/rescan`, { method: "POST" });
    if (!r.ok) { showError(`rescan failed: HTTP ${r.status}`); return; }
    const data = await r.json();
    state.items = data.items || [];
    renderSidebar();
    if (!state.current && state.items.length) {
      selectSlug(state.items[0].slug);
    }
  } finally {
    el.btnRescan.disabled = false;
  }
}

async function deleteItem(slug) {
  if (!confirm(`Delete "${slug}"?\n\nThis removes the workspace files (masks, frames, PTS cache).\nThe source video file is kept intact.`)) return;
  const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/delete`, { method: "POST" });
  if (!r.ok) { showError(`delete failed: HTTP ${r.status}`); return; }
  const wasCurrent = state.current === slug;
  state.items = state.items.filter(i => i.slug !== slug);
  if (wasCurrent) {
    state.current = null;
    if (state.sse) { state.sse.close(); state.sse = null; }
  }
  renderSidebar();
  if (wasCurrent) {
    if (state.items.length) {
      window.location.hash = `slug=${state.items[0].slug}`;
    } else {
      el.itemSlug.textContent = "no item";
    }
  }
}

function startSse() {
  if (state.sse) {
    state.sse.close();
    state.sse = null;
  }
  if (!state.current) return;
  const url = `${API_BASE}/api/items/${encodeURIComponent(state.current)}/events`;
  const es = new EventSource(url);
  state.sse = es;
  es.addEventListener("mask", (ev) => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    const frame = payload.frame;
    const maskUrl = payload.mask_url;
    if (typeof frame !== "number" || typeof maskUrl !== "string") return;
    state.propMaskUrlByFrame.set(frame, maskUrl);
    state.propDoneCount = state.propMaskUrlByFrame.size;
    addDoneFill(frame);
    // Warm the tinted-canvas cache as soon as the SSE event lands so the user
    // never hits an async-decode flicker even on the very first scrub-through.
    if (el.overlay.width && el.overlay.height) {
      const img = new Image();
      img.onload = () => {
        try { state.tintedCache.set(frame, buildTintedCanvas(img)); } catch (_) {}
        if (currentFrame() === frame) blitCachedMask(frame);
      };
      img.src = maskUrl;
    } else if (frame === currentFrame()) {
      loadMaskForFrame(frame);
    }
    updateStatus();
  });
  es.addEventListener("phase", (ev) => {
    let payload;
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    state.propPhase = payload.phase || null;
    if (typeof payload.expected_frames === "number") state.propExpected = payload.expected_frames;
    if (typeof payload.elapsed_s === "number") state.propPhaseElapsed = payload.elapsed_s;
    if (payload.device) state.propDevice = payload.device;
    if (payload.model) state.propModel = payload.model;
    console.log("propagate phase", payload);
    updateStatus();
  });
  es.addEventListener("done", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
    state.propagateStatus = "done";
    state.propPhase = "done";
    if (typeof payload.elapsed_s === "number") state.propPhaseElapsed = payload.elapsed_s;
    if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
    if (state.current) updateSidebarStatus(state.current, "done");
    updatePropagateBtn();
    updateStatus();
    if (state.current) prefetchMasks(state.current);
  });
  es.addEventListener("error", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
    if (state.current) updateSidebarStatus(state.current, "failed");
    if (payload.msg) showError(`propagate: ${payload.msg}`);
  });
  es.onerror = () => {
    // SSE 中斷由瀏覽器自動重連；這裡不假裝成功
  };
}

async function postJson(path, body) {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body == null ? "{}" : JSON.stringify(body),
  });
  return r;
}

async function markIn() {
  if (!state.current) return;
  const f = currentFrame();
  state.inFrame = f;
  if (state.outFrame != null) await sendTrim();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  console.log("mark in", f);
}

async function markOut() {
  if (!state.current) return;
  const f = currentFrame();
  state.outFrame = f;
  if (state.inFrame != null) await sendTrim();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  console.log("mark out", f);
}

async function sendTrim() {
  if (state.inFrame == null || state.outFrame == null) return;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(state.current)}/trim`, {
      in_frame: state.inFrame,
      out_frame: state.outFrame,
    });
    if (!r.ok) showError(`trim failed: HTTP ${r.status}`);
    else if (state.current) warmUpClip(state.current);
  } catch (e) {
    showError(`trim failed: ${e}`);
  }
}

function markSeed() {
  if (!state.current) return;
  state.seedFrame = currentFrame();
  state.seedMaskReady = false;
  state.seedPoint = null;
  state.pendingSeedClick = true;
  clearSeedMask();
  clearOverlay();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  console.log("mark seed", state.seedFrame, "awaiting click");
}

async function sendSeed(frameIndex, x, y) {
  state.seedComputing = true;
  state.seedComputeStartMs = performance.now();
  updateStatus();
  const tickHandle = setInterval(updateStatus, 200);
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(state.current)}/seed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame_index: frameIndex, x, y }),
    });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      showError(`seed failed: HTTP ${r.status} ${text}`);
      return;
    }
    const blob = await r.blob();
    clearSeedMask();
    state.seedMaskUrl = URL.createObjectURL(blob);
    state.seedMaskReady = true;
    if (currentFrame() === frameIndex) loadMaskForFrame(frameIndex);
    updatePropagateBtn();
    // Reflect seed_frame back into the in-memory items list so the sidebar
    // card flips to "ready" (blue dot) and the queue counter increments.
    const it = state.items.find(i => i.slug === state.current);
    if (it) {
      it.seed_frame = frameIndex;
      it.seed_point = [x, y];
      const card = el.itemList.querySelector(`.item-card[data-slug="${state.current}"]`);
      if (card) {
        const dot = card.querySelector(".item-status");
        if (dot) dot.className = `item-status item-status-${effectiveStatus(it)}`;
      }
      updateQueueButton();
    }
  } catch (e) {
    showError(`seed failed: ${e}`);
  } finally {
    clearInterval(tickHandle);
    state.seedComputing = false;
    state.seedComputeStartMs = null;
    updateStatus();
  }
}

async function propagate() {
  if (el.btnPropagate.disabled) return;
  state.propagateStatus = "running";
  if (state.current) updateSidebarStatus(state.current, "running");
  state.propPhase = "starting";
  state.propDoneCount = 0;
  state.propExpected = (state.outFrame - state.inFrame + 1) || 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = performance.now();
  clearDoneFills();
  updatePropagateBtn();
  updateStatus();
  if (state.propTickHandle) clearInterval(state.propTickHandle);
  state.propTickHandle = setInterval(updateStatus, 200);
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(state.current)}/propagate`);
    if (!r.ok) {
      state.propagateStatus = "failed";
      const text = await r.text().catch(() => "");
      showError(`propagate failed: HTTP ${r.status} ${text}`);
      clearInterval(state.propTickHandle);
      state.propTickHandle = null;
      updatePropagateBtn();
      updateStatus();
    }
  } catch (e) {
    state.propagateStatus = "failed";
    clearInterval(state.propTickHandle);
    state.propTickHandle = null;
    showError(`propagate failed: ${e}`);
    updatePropagateBtn();
    updateStatus();
  }
}

async function cancelOrEscape() {
  if (state.pendingSeedClick) {
    state.pendingSeedClick = false;
    updateStatus();
    return;
  }
  if (state.propagateStatus === "running" && state.current) {
    try {
      const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(state.current)}/propagate/cancel`, { method: "POST" });
      if (r.status !== 404 && !r.ok) {
        console.warn(`cancel HTTP ${r.status}`);
      }
    } catch (e) {
      console.warn("cancel failed", e);
    }
    state.propagateStatus = "idle";
    if (state.current) updateSidebarStatus(state.current, "idle");
    updatePropagateBtn();
    updateStatus();
  }
}

function stepFrames(delta) {
  if (!state.ptsTable) return;
  if (!el.video.paused) el.video.pause();
  // Priority for "where am I now": queued target (not yet flushed) > in-flight
  // seek target (rVFC may not have updated mediaTime) > displayed frame.
  // Without lastSeekTarget, two fast right-arrow presses race and land on the
  // same target.
  const start = state.pendingTargetFrame != null
    ? state.pendingTargetFrame
    : (state.lastSeekTarget != null ? state.lastSeekTarget : currentFrame());
  const target = Math.max(0, Math.min(state.totalFrames - 1, start + delta));
  state.pendingTargetFrame = target;
  flushStep();
}

function jumpToFrame(f) {
  if (!state.ptsTable) return;
  if (!el.video.paused) el.video.pause();
  const target = Math.max(0, Math.min(state.totalFrames - 1, f));
  state.pendingTargetFrame = target;
  flushStep();
}

function enterScrubMode() {
  if (state.scrubMode) return;
  state.scrubMode = true;
  el.videoWrap.classList.add("scrub");
  if (!el.video.paused) el.video.pause();
}

function exitScrubMode() {
  if (!state.scrubMode) return;
  state.scrubMode = false;
  el.videoWrap.classList.remove("scrub");
}

function flushStep() {
  if (state.pendingTargetFrame == null) return;
  const target = state.pendingTargetFrame;
  state.pendingTargetFrame = null;
  enterScrubMode();
  const tbl = state.ptsTable;
  state.lastDisplayedMediaTime = tbl[target];
  state.lastDisplayedFrame = target;
  el.scrubber.value = String(target);
  // Atomic frame+mask paint. Cache hit paints synchronously this tick;
  // cache miss leaves the previous frame visible (no flicker, no desync)
  // and paints both layers together once the bitmap arrives.
  scheduleScrubPaint(target);
  // Background-sync the underlying video element to the same PTS so togglePlay
  // can call play() without burning the user-gesture token on a seek. Chrome
  // sometimes refuses paused sub-frame seeks but always honours the LAST seek
  // before play(), so cumulative scrubbing leaves video positioned correctly.
  if (Math.abs(el.video.currentTime - tbl[target]) > 0.005) {
    el.video.currentTime = tbl[target];
  }
  updateStatus();
}

async function fetchPts(slug) {
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/pts`);
    if (!r.ok) {
      showError(`pts fetch HTTP ${r.status}`);
      return;
    }
    const j = await r.json();
    if (j && Array.isArray(j.pts) && typeof j.total_frames === "number") {
      state.ptsTable = j.pts;
      // Authoritative frame count comes from the dense PTS list, not the
      // /api/items snapshot. /api/items can be stale if scan_sources ran with
      // a previous (legacy) total_frames; the PTS list is rebuilt from source.
      state.totalFrames = j.total_frames;
      el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
      updateMarkers();
      console.log(`pts loaded: ${j.pts.length} frames`);
      // Kick off background prefetch over the trimmed range so subsequent
      // scrubbing hits the bitmap cache instead of fetch+decode.
      if (state.inFrame != null && state.outFrame != null) warmUpClip(slug);
    }
  } catch (e) {
    showError(`pts fetch failed: ${e}`);
  }
}

async function rehydrateMasks(slug) {
  // SSE only fans out live propagate events; on reload the previous run's
  // masks are durable on disk but the timeline shows empty. Re-list them
  // here so the URL cache + green fills get rebuilt.
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/masks`);
    if (!r.ok) return;
    const j = await r.json();
    if (slug !== state.current) return;  // user switched item mid-fetch
    if (!Array.isArray(j.frames)) return;
    for (const f of j.frames) {
      const url = `${API_BASE}/mask/${encodeURIComponent(slug)}/${String(f).padStart(5, "0")}.png`;
      state.propMaskUrlByFrame.set(f, url);
      addDoneFill(f);
    }
    state.propDoneCount = state.propMaskUrlByFrame.size;
    if (state.propagateStatus === "done") {
      state.propExpected = (state.outFrame != null && state.inFrame != null)
        ? (state.outFrame - state.inFrame + 1) : state.propDoneCount;
    }
    const f = currentFrame();
    if (state.propMaskUrlByFrame.has(f)) loadMaskForFrame(f);
    updateStatus();
    prefetchMasks(slug);
  } catch (e) {
    console.warn("rehydrate masks failed", e);
  }
}

function togglePlay() {
  if (state.scrubMode) {
    const tbl = state.ptsTable;
    const f = state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0;
    const targetT = tbl ? tbl[f] : null;
    if (targetT == null) return;
    // flushStep keeps el.video.currentTime in sync as the user scrubs, so by
    // now the video is already positioned. Just call play() — no seek means
    // no microtask before play, so the user-gesture token survives.
    if (Math.abs(el.video.currentTime - targetT) > 0.005) {
      el.video.currentTime = targetT;
    }
    state.lastDisplayedMediaTime = targetT;
    exitScrubMode();
    el.video.play().catch((e) => {
      console.warn("play failed", e);
      enterScrubMode();
    });
  } else {
    el.video.pause();
    const f = currentFrame();
    state.lastDisplayedFrame = f;
    if (state.ptsTable) state.lastDisplayedMediaTime = state.ptsTable[f];
    el.scrubber.value = String(f);
    enterScrubMode();
    scheduleScrubPaint(f);
    updateStatus();
  }
}

function onVideoClick(e) {
  if (!state.pendingSeedClick) {
    togglePlay();
    return;
  }
  if (state.seedFrame == null) return;
  const v = el.video;
  if (!v.videoWidth || !v.videoHeight) {
    showError("video metadata not ready");
    return;
  }
  const disp = videoDisplayRect();
  if (!disp) {
    showError("video display rect unresolved");
    return;
  }
  // Reject clicks landing in the letterbox margin (outside the actual frame).
  if (e.clientX < disp.left || e.clientX > disp.left + disp.width ||
      e.clientY < disp.top || e.clientY > disp.top + disp.height) {
    console.warn("click outside video frame area, ignored");
    return;
  }
  const x = Math.round((e.clientX - disp.left) * (v.videoWidth / disp.width));
  const y = Math.round((e.clientY - disp.top) * (v.videoHeight / disp.height));
  console.log("seed click", { client: [e.clientX, e.clientY], disp, native: [x, y] });
  state.seedPoint = [x, y];
  state.pendingSeedClick = false;
  updateStatus();
  sendSeed(state.seedFrame, x, y);
}

function onKeydown(e) {
  const tag = (e.target && e.target.tagName) || "";
  const isInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  // Space (play/pause) and arrows always work — otherwise the focused-on-a-
  // dropdown case eats the key and the user can't toggle playback. Other
  // shortcuts respect text inputs.
  const isNavKey = e.key === " " || e.key === "ArrowLeft" || e.key === "ArrowRight"
    || e.key === "Home" || e.key === "End";
  if (isInput && e.target !== el.scrubber && !isNavKey) return;

  // Big jumps: Shift+arrow = ±10, Alt/Option+arrow = ±100, plain = ±1.
  // Industry-standard ,/. as alternates so right-handed mouse + left-hand
  // keyboard works without claw-grip on the cursor cluster.
  if (e.key === " ") {
    e.preventDefault();
    togglePlay();
  } else if (e.key === "ArrowLeft" || e.key === ",") {
    e.preventDefault();
    const d = e.altKey ? -100 : (e.shiftKey ? -10 : -1);
    stepFrames(d);
  } else if (e.key === "ArrowRight" || e.key === ".") {
    e.preventDefault();
    const d = e.altKey ? 100 : (e.shiftKey ? 10 : 1);
    stepFrames(d);
  } else if (e.key === "Home") {
    e.preventDefault();
    jumpToFrame(state.inFrame != null ? state.inFrame : 0);
  } else if (e.key === "End") {
    e.preventDefault();
    jumpToFrame(state.outFrame != null ? state.outFrame : state.totalFrames - 1);
  } else if (e.key === "[") {
    markIn();
  } else if (e.key === "]") {
    markOut();
  } else if (e.key === "s" || e.key === "S") {
    markSeed();
  } else if (e.key === "Enter") {
    propagate();
  } else if (e.key === "Escape") {
    cancelOrEscape();
  }
}

function onDisplayedFrame(_now, metadata) {
  // Fires once per actually-presented video frame during playback. While in
  // scrub mode the video element is hidden and we drive the overlay off
  // img-swaps, so rVFC must not override our state.
  if (state.scrubMode) {
    state.rvfcHandle = el.video.requestVideoFrameCallback(onDisplayedFrame);
    return;
  }
  state.lastDisplayedMediaTime = metadata.mediaTime;
  if (state.ptsTable) {
    const f = currentFrame();
    if (state.lastSeekTarget != null && Math.abs(f - state.lastSeekTarget) <= 1) {
      state.lastSeekTarget = null;
    }
    if (f !== state.lastDisplayedFrame) {
      state.lastDisplayedFrame = f;
      el.scrubber.value = String(f);
      if (maskUrlForFrame(f) != null) loadMaskForFrame(f);
      else clearOverlay();
      if (f === state.seedFrame && state.seedPoint && maskUrlForFrame(f) == null) {
        drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
      }
    }
  }
  updateStatus();
  state.rvfcHandle = el.video.requestVideoFrameCallback(onDisplayedFrame);
}

function bindUi() {
  el.video.addEventListener("loadedmetadata", () => {
    resizeOverlay();
    updateStatus();
    if (typeof el.video.requestVideoFrameCallback === "function") {
      if (state.rvfcHandle != null) el.video.cancelVideoFrameCallback(state.rvfcHandle);
      state.rvfcHandle = el.video.requestVideoFrameCallback(onDisplayedFrame);
    } else {
      // Fallback: pre-Safari 17 / very old browsers. Will lag but at least works.
      console.warn("requestVideoFrameCallback unsupported; falling back to timeupdate");
    }
  });
  // timeupdate is a fallback only — fires at coarse 250ms cadence.
  el.video.addEventListener("timeupdate", () => {
    if (typeof el.video.requestVideoFrameCallback === "function") return;
    if (state.fps != null) el.scrubber.value = String(currentFrame());
    const f = currentFrame();
    if (maskUrlForFrame(f) != null) loadMaskForFrame(f);
    else clearOverlay();
    updateStatus();
  });
  el.videoWrap.addEventListener("click", onVideoClick);
  el.video.addEventListener("play", () => exitScrubMode());
  el.video.addEventListener("pause", () => {
    if (!state.scrubMode && state.current && state.ptsTable) {
      const f = currentFrame();
      enterScrubMode();
      state.lastDisplayedFrame = f;
      state.lastDisplayedMediaTime = state.ptsTable[f];
      el.scrubber.value = String(f);
      scheduleScrubPaint(f);
      updateStatus();
    }
  });
  // Use ResizeObserver for layout-sensitive overlay; window resize alone misses
  // CSS-driven size changes (e.g. picker dropdown reflow).
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => resizeOverlay()).observe(el.video);
  } else {
    window.addEventListener("resize", resizeOverlay);
  }
  el.video.addEventListener("seeked", () => resizeOverlay());

  // rAF-coalesce scrubber input. The native input event fires per pixel of
  // mouse movement (200+/sec on a fast drag), each call queues a paintAtomic
  // (1920×1080 drawImage). Without coalescing the main thread saturates and
  // click events queue behind paints — Mark Out feels delayed and the drag
  // visually stutters. Coalescing caps paint rate at display refresh (~60Hz)
  // and leaves room for input handling.
  let scrubRafPending = false;
  let scrubRafTarget = null;
  el.scrubber.addEventListener("input", () => {
    if (!state.ptsTable) return;
    scrubRafTarget = parseInt(el.scrubber.value, 10);
    if (scrubRafPending) return;
    scrubRafPending = true;
    requestAnimationFrame(() => {
      scrubRafPending = false;
      if (state.ptsTable && scrubRafTarget != null) jumpToFrame(scrubRafTarget);
    });
  });
  // Suppress the range element's native arrow-key step. Our onKeydown handler
  // (with PTS-aware seek + queue) runs instead.
  el.scrubber.addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
         "PageUp", "PageDown", "Home", "End"].includes(e.key)) {
      e.preventDefault();
    }
  });

  el.btnIn.addEventListener("click", markIn);
  el.btnOut.addEventListener("click", markOut);
  el.btnSeed.addEventListener("click", markSeed);
  el.btnPropagate.addEventListener("click", propagate);
  el.btnCancel.addEventListener("click", cancelOrEscape);

  document.addEventListener("keydown", onKeydown);
}

bindUi();
bootstrap();
