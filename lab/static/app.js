"use strict";

const API_BASE = window.location.origin;

const state = {
  items: [],            // each item carries .segments[] + .active_segment_id
  current: null,        // current slug
  fps: null,
  totalFrames: 0,
  // Active segment is the one targeted by [/]/S/Enter. Reads/writes go through
  // activeSegment(); never read state.activeSegmentId directly without lookup.
  activeSegmentId: null,
  pendingSeedClick: false,
  doneFrames: new Set(),                         // active-segment frames with masks
  seedMaskUrls: new Map(),                       // seg_id → blob URL for the seed-frame mask
  propMaskUrlsBySeg: new Map(),                  // seg_id → Map<frame, url>
  seedComputingSeg: null,                        // seg_id currently mid-seed (or null)
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
  showSeedMarker: true,
  queueRunning: false,
  queueSse: null,
  queueSnapshot: { running: false, current: null, done: 0, ready: 0, total: 0 },
  tintedCache: new Map(),
  prefetchAbort: 0,
  frameSource: null,
  scrubPaintToken: 0,
  frameSourceLoading: false,
};

// ---- segment helpers -------------------------------------------------------

function currentItem() {
  return state.items.find(i => i.slug === state.current) || null;
}

function activeSegment() {
  const it = currentItem();
  if (!it || !state.activeSegmentId) return null;
  return (it.segments || []).find(s => s.id === state.activeSegmentId) || null;
}

function segmentsOf(slug) {
  const it = state.items.find(i => i.slug === slug);
  return it ? (it.segments || []) : [];
}

function maskUrlsForActive() {
  return state.propMaskUrlsBySeg.get(state.activeSegmentId) || new Map();
}

function setActiveSegmentLocal(item, segId) {
  state.activeSegmentId = segId;
  if (item) item.active_segment_id = segId;
}

function aggregateStatus(it) {
  const segs = it.segments || [];
  if (segs.length === 0) return "idle";
  const statuses = segs.map(s => s.propagate_status);
  if (statuses.includes("running")) return "running";
  if (statuses.includes("failed")) return "failed";
  const allDone = statuses.every(s => s === "done");
  if (allDone) return "done";
  const anyReady = segs.some(s => s.propagate_status === "idle" && s.seed_frame != null);
  if (anyReady) return "ready";
  return "idle";
}

const el = {
  video: document.getElementById("video"),
  videoWrap: document.querySelector("#video-wrap"),
  frameCanvas: document.getElementById("frame-canvas"),
  overlay: document.getElementById("overlay"),
  scrubber: document.getElementById("scrubber"),
  fills: document.getElementById("timeline-fills"),
  markerLayer: document.getElementById("marker-layer"),
  segmentsStrip: document.getElementById("segments-strip"),
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
  btnNewSeg: document.getElementById("btn-new-seg"),
  btnPropagate: document.getElementById("btn-propagate"),
  btnCancel: document.getElementById("btn-cancel"),
  btnToggleSeedMarker: document.getElementById("btn-toggle-seed-marker"),
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

function clearSeedMask(segId) {
  // segId omitted → clear all (slug switch).
  if (segId == null) {
    for (const url of state.seedMaskUrls.values()) URL.revokeObjectURL(url);
    state.seedMaskUrls.clear();
    return;
  }
  const url = state.seedMaskUrls.get(segId);
  if (url) URL.revokeObjectURL(url);
  state.seedMaskUrls.delete(segId);
}

function clearPropMasks(segId) {
  if (segId == null) {
    for (const m of state.propMaskUrlsBySeg.values()) {
      for (const url of m.values()) {
        if (url.startsWith("blob:")) URL.revokeObjectURL(url);
      }
    }
    state.propMaskUrlsBySeg.clear();
    return;
  }
  const m = state.propMaskUrlsBySeg.get(segId);
  if (!m) return;
  for (const url of m.values()) {
    if (url.startsWith("blob:")) URL.revokeObjectURL(url);
  }
  state.propMaskUrlsBySeg.delete(segId);
}

function currentFrame() {
  // Scrub mode: painted canvas is truth — return lastDisplayedFrame so the
  // status line never claims a frame the user can't see yet.
  if (state.scrubMode) {
    return state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0;
  }
  // Play mode: snap rVFC mediaTime to the dense PTS list.
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
  const tbl = state.ptsTable;
  if (!tbl || tbl.length === 0) return 0;
  const pts = tbl[f];
  if (f + 1 < tbl.length) return (pts + tbl[f + 1]) / 2;
  const prevGap = f > 0 ? (pts - tbl[f - 1]) : (1 / 240);
  return pts + prevGap / 2;
}

function fmt(n) { return String(n == null ? "-" : n); }

function updateStatus() {
  const f = currentFrame();
  if (state.totalFrames > 0 && !state.scrubMode && document.activeElement !== el.scrubber) {
    const want = String(f);
    if (el.scrubber.value !== want) el.scrubber.value = want;
  }
  const tbl = state.ptsTable;
  let t;
  if (tbl && f >= 0 && f < tbl.length) t = tbl[f];
  else if (state.lastDisplayedMediaTime != null) t = state.lastDisplayedMediaTime;
  else t = el.video.currentTime || 0;
  const seg = activeSegment();
  const inF = seg ? seg.in_frame : null;
  const outF = seg ? seg.out_frame : null;
  const seedF = seg ? seg.seed_frame : null;
  const seedP = seg ? seg.seed_point : null;
  const pStat = seg ? seg.propagate_status : "idle";
  const segLabel = seg ? seg.id.slice(4) : "—";
  const pt = seedP ? `(${seedP[0]},${seedP[1]})` : "-";
  const fStr = String(f).padStart(4, "0");
  let statusTag = pStat;
  let computing = false;
  if (state.frameSourceLoading) {
    statusTag = "loading clip (WebCodecs decode init)…";
    computing = true;
  } else if (state.seedComputingSeg && state.seedComputingSeg === state.activeSegmentId) {
    const elapsed = ((performance.now() - state.seedComputeStartMs) / 1000).toFixed(1);
    statusTag = `seeding... ${elapsed}s (SAM2 image predictor)`;
    computing = true;
  } else if (pStat === "running") {
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
  } else if (pStat === "done" && state.propDoneCount > 0) {
    const tail = state.propPhaseElapsed > 0 ? `in ${state.propPhaseElapsed}s` : "(cached on disk)";
    statusTag = `propagate done: ${state.propDoneCount}/${state.propExpected} frames ${tail}`;
  }
  if (computing) el.statusbar.classList.add("computing");
  else el.statusbar.classList.remove("computing");
  el.status.textContent =
    `f=${fStr} t=${t.toFixed(3)}s | seg=${segLabel} in=${fmt(inF)} out=${fmt(outF)} seed=${fmt(seedF)} pt=${pt} | status=${statusTag}${state.pendingSeedClick ? " (click to set seed point)" : ""}`;
}

function updateMarkers() {
  // Render per-segment in/out/seed markers under #marker-layer. Active segment
  // gets a brighter accent + click-to-activate; non-active segments dim.
  const layer = el.markerLayer;
  layer.innerHTML = "";
  const it = currentItem();
  if (!it || state.totalFrames <= 0) return;
  const denom = Math.max(1, state.totalFrames - 1);
  for (const seg of (it.segments || [])) {
    const isActive = seg.id === state.activeSegmentId;
    const cls = isActive ? "active" : "dim";
    if (seg.in_frame != null && seg.out_frame != null) {
      const span = document.createElement("div");
      span.className = `seg-span ${cls}`;
      span.style.left = `${(seg.in_frame / denom) * 100}%`;
      span.style.width = `${((seg.out_frame - seg.in_frame) / denom) * 100}%`;
      span.title = `${seg.id} [${seg.in_frame}–${seg.out_frame}]`;
      span.addEventListener("click", () => activateSegment(seg.id));
      layer.appendChild(span);
    }
    const mk = (frame, kind) => {
      if (frame == null) return;
      const m = document.createElement("div");
      m.className = `seg-marker seg-marker-${kind} ${cls}`;
      m.style.left = `${(frame / denom) * 100}%`;
      if (kind === "seed") m.textContent = "★";
      layer.appendChild(m);
    };
    mk(seg.in_frame, "in");
    mk(seg.out_frame, "out");
    mk(seg.seed_frame, "seed");
  }
}

function updatePropagateBtn() {
  const seg = activeSegment();
  const ready =
    seg != null &&
    seg.in_frame != null &&
    seg.out_frame != null &&
    seg.seed_frame != null &&
    seg.seed_point != null &&
    state.seedMaskUrls.has(seg.id) &&
    seg.propagate_status !== "running";
  el.btnPropagate.disabled = !ready;
}

function renderSegmentsStrip() {
  const strip = el.segmentsStrip;
  strip.innerHTML = "";
  const it = currentItem();
  if (!it) return;
  for (const seg of (it.segments || [])) {
    const chip = document.createElement("div");
    const isActive = seg.id === state.activeSegmentId;
    chip.className = `seg-chip seg-chip-${seg.propagate_status}` + (isActive ? " active" : "");
    chip.title = `${seg.id} (${seg.propagate_status})`;
    const dot = document.createElement("span");
    dot.className = "seg-chip-dot";
    chip.appendChild(dot);
    chip.appendChild(document.createTextNode(seg.id.replace("seg_", "")));
    chip.addEventListener("click", () => activateSegment(seg.id));
    if (!isActive || (it.segments || []).length > 1) {
      const x = document.createElement("button");
      x.className = "seg-chip-x";
      x.type = "button";
      x.textContent = "×";
      x.title = "Delete segment";
      x.addEventListener("click", (e) => { e.stopPropagation(); deleteSegment(seg.id); });
      chip.appendChild(x);
    }
    strip.appendChild(chip);
  }
  const addBtn = document.createElement("button");
  addBtn.className = "seg-chip-add";
  addBtn.type = "button";
  addBtn.textContent = "+ New segment (N)";
  addBtn.addEventListener("click", () => createSegment());
  strip.appendChild(addBtn);
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

function videoDisplayRect() {
  // Returns the actual displayed video frame rect inside the wrap, accounting
  // for letterbox/pillarbox. In scrub mode the canvas is what's painted; in
  // play mode the <video> element shows. Both are object-fit: contain inside
  // the same wrap, with identical intrinsic aspect, so a single rect logic
  // works for either.
  const wrapRect = el.videoWrap.getBoundingClientRect();
  const fs = state.frameSource;
  let w, h;
  if (fs && fs.width && fs.height) { w = fs.width; h = fs.height; }
  else if (el.video.videoWidth && el.video.videoHeight) {
    w = el.video.videoWidth; h = el.video.videoHeight;
  } else return null;
  // Mirror the wrap's effective box. We intentionally use the canvas/video
  // bounding rect because the wrap may have padding/decoration in future.
  const refRect = state.scrubMode
    ? el.frameCanvas.getBoundingClientRect()
    : el.video.getBoundingClientRect();
  if (refRect.width === 0 || refRect.height === 0) return null;
  const elemRatio = refRect.width / refRect.height;
  const vidRatio = w / h;
  let dispW, dispH, padX, padY;
  if (elemRatio > vidRatio) {
    dispH = refRect.height; dispW = dispH * vidRatio;
    padX = (refRect.width - dispW) / 2; padY = 0;
  } else {
    dispW = refRect.width; dispH = dispW / vidRatio;
    padX = 0; padY = (refRect.height - dispH) / 2;
  }
  return { left: refRect.left + padX, top: refRect.top + padY, width: dispW, height: dispH };
}

function resizeOverlay() {
  const fs = state.frameSource;
  const w = (fs && fs.width) || el.video.videoWidth;
  const h = (fs && fs.height) || el.video.videoHeight;
  if (!w || !h) return;
  const disp = videoDisplayRect();
  if (!disp) return;
  if (el.overlay.width !== w) el.overlay.width = w;
  if (el.overlay.height !== h) el.overlay.height = h;
  const wrapRect = el.videoWrap.getBoundingClientRect();
  el.overlay.style.width = disp.width + "px";
  el.overlay.style.height = disp.height + "px";
  el.overlay.style.left = (disp.left - wrapRect.left) + "px";
  el.overlay.style.top = (disp.top - wrapRect.top) + "px";
}

function clearOverlay() {
  const ctx = el.overlay.getContext("2d");
  ctx.clearRect(0, 0, el.overlay.width, el.overlay.height);
}

function drawFrameBitmap(bm) {
  const c = el.frameCanvas;
  const ctx = c.getContext("2d");
  if (c.width !== bm.width) c.width = bm.width;
  if (c.height !== bm.height) c.height = bm.height;
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
      } catch (_) {}
      resolve();
    };
    img.onerror = () => resolve();
    img.src = url;
  });
}

// Atomic visual update: image + mask drawn in the same tick.
function paintAtomic(frame, bm) {
  drawFrameBitmap(bm);
  const url = maskUrlForFrame(frame);
  if (url) {
    if (!blitCachedMask(frame)) clearOverlay();
  } else {
    clearOverlay();
    const seg = activeSegment();
    if (seg && frame === seg.seed_frame && seg.seed_point && state.showSeedMarker) {
      drawClickMarker(seg.seed_point[0], seg.seed_point[1]);
    }
  }
}

// Paint pipeline: token-guarded, never paints stale frames. Sync hot path
// (cache + cache) does NOT await — it paints synchronously, no Promise tick.
async function scheduleScrubPaint(frame) {
  const token = ++state.scrubPaintToken;
  const fs = state.frameSource;
  if (!fs) return;
  let bm = fs.peek(frame);
  if (!bm) {
    try {
      bm = await fs.getFrame(frame);
    } catch (e) {
      const msg = e && e.message;
      // Silent on benign races: closed = slug switched away;
      // not-loaded = user scrubbed before mp4 demux finished.
      if (msg !== "FrameSource closed" && msg !== "FrameSource not loaded") {
        console.warn("getFrame failed", frame, e);
      }
      return;
    }
    if (token !== state.scrubPaintToken) return;
  }
  const maskUrl = maskUrlForFrame(frame);
  if (maskUrl && !state.tintedCache.has(frame)) {
    await ensureTintedCanvas(frame, maskUrl);
    if (token !== state.scrubPaintToken) return;
  }
  paintAtomic(frame, bm);
  state.lastDisplayedFrame = frame;
  if (state.ptsTable && frame >= 0 && frame < state.ptsTable.length) {
    state.lastDisplayedMediaTime = state.ptsTable[frame];
  }
  updateStatus();
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
      px[i] = 34; px[i + 1] = 197; px[i + 2] = 94; px[i + 3] = 128;
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
  const seg = activeSegment();
  if (seg && frame === seg.seed_frame && seg.seed_point && state.showSeedMarker) {
    drawClickMarker(seg.seed_point[0], seg.seed_point[1]);
  }
  return true;
}

function maskUrlForFrame(frame) {
  const urls = maskUrlsForActive();
  if (urls.has(frame)) return urls.get(frame);
  const seg = activeSegment();
  if (seg && frame === seg.seed_frame) {
    const sm = state.seedMaskUrls.get(seg.id);
    if (sm) return sm;
  }
  return null;
}

function loadMaskForFrame(frame) {
  const url = maskUrlForFrame(frame);
  if (!url) { clearOverlay(); return; }
  if (blitCachedMask(frame)) return;
  const img = new Image();
  img.onload = () => {
    if (!el.overlay.width || !el.overlay.height) return;
    state.tintedCache.set(frame, buildTintedCanvas(img));
    if (currentFrame() === frame) blitCachedMask(frame);
  };
  img.onerror = () => clearOverlay();
  img.src = url;
}

// Single source of truth for "redraw the overlay for the current frame given
// current active segment + state". Call this from any state-change site that
// doesn't naturally trigger a frame change (chip activation, rehydrate after
// reload, SSE mask delivery on the visible frame, optimistic seek snap).
// loadMaskForFrame's blitCachedMask already handles the seed-frame crosshair
// fallback when a mask exists; the else branch here covers no-mask + crosshair.
function repaintOverlayForCurrentFrame() {
  const f = currentFrame();
  const url = maskUrlForFrame(f);
  const seg = activeSegment();
  console.log("[repaint]", {
    f, url, segId: seg?.id, seedFrame: seg?.seed_frame, seedPoint: seg?.seed_point,
    activeSegId: state.activeSegmentId,
    mapForActive: state.propMaskUrlsBySeg.get(state.activeSegmentId),
    overlayWH: [el.overlay.width, el.overlay.height],
    scrubMode: state.scrubMode,
    lastDisplayedFrame: state.lastDisplayedFrame,
  });
  if (url != null) {
    loadMaskForFrame(f);
    return;
  }
  clearOverlay();
  if (seg && f === seg.seed_frame && seg.seed_point && state.showSeedMarker) {
    drawClickMarker(seg.seed_point[0], seg.seed_point[1]);
  }
}

async function prefetchMasks(slug) {
  const myToken = ++state.prefetchAbort;
  if (!el.overlay.width || !el.overlay.height) {
    el.video.addEventListener("loadedmetadata", () => prefetchMasks(slug), { once: true });
    return;
  }
  const entries = Array.from(maskUrlsForActive().entries())
    .sort((a, b) => a[0] - b[0])
    .filter(([frame]) => !state.tintedCache.has(frame));
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

async function loadFrameSource(slug) {
  // Tear down previous source (releases GPU mem from cached ImageBitmaps).
  if (state.frameSource) {
    state.frameSource.close();
    state.frameSource = null;
  }
  const fs = new FrameSource();
  state.frameSource = fs;
  state.frameSourceLoading = true;
  updateStatus();
  try {
    await fs.load(`${API_BASE}/clip/${slug}.mp4`);
  } catch (e) {
    if (state.frameSource === fs) state.frameSource = null;
    state.frameSourceLoading = false;
    showError(`frame source load failed: ${e.message || e}`);
    return;
  }
  if (state.current !== slug || state.frameSource !== fs) {
    fs.close();
    return;
  }
  state.frameSourceLoading = false;
  // Canvas backing-store at PREVIEW resolution (what we actually draw). The
  // overlay below stays at native resolution for mask alignment with the
  // server-side full-res masks.
  el.frameCanvas.width = fs.previewWidth;
  el.frameCanvas.height = fs.previewHeight;
  resizeOverlay();
  updateStatus();
  // Prefetch the active segment's trim range so subsequent scrub is all cache hits.
  const seg = activeSegment();
  if (seg && seg.in_frame != null && seg.out_frame != null) {
    fs.prefetchRange(seg.in_frame, seg.out_frame);
  }
  // Reload UX: syncFromItem reset lastDisplayedFrame=0, but the active seg's
  // mask lives at seed_frame ∈ [in, out]. Default to landing on seed_frame so
  // the user sees the existing mask immediately without hunting on the timeline.
  // Only auto-jump if we haven't moved off frame 0 (don't yank a user who
  // already scrubbed somewhere during the load).
  console.log("[loadFrameSource done]", { activeSegId: state.activeSegmentId, segSeedFrame: seg?.seed_frame, lastDisplayedFrame: state.lastDisplayedFrame, ptsLoaded: !!state.ptsTable });
  if (seg && seg.seed_frame != null && state.lastDisplayedFrame === 0 && state.ptsTable) {
    console.log("[loadFrameSource] auto-jump →", seg.seed_frame);
    jumpToFrame(seg.seed_frame);
    return;
  }
  // Repaint the current frame now that the source is ready.
  scheduleScrubPaint(state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0);
}

function syncFromItem(item) {
  state.current = item.slug;
  if (item.fps == null) {
    showError(`item ${item.slug}: fps missing in /api/items response`);
    return false;
  }
  state.fps = item.fps;
  state.totalFrames = item.total_frames || 0;
  state.activeSegmentId = item.active_segment_id || null;
  el.itemSlug.textContent = item.slug;
  updateSidebarActive();
  el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
  el.scrubber.value = "0";
  el.video.src = `${API_BASE}/clip/${item.slug}.mp4`;
  state.lastDisplayedFrame = 0;
  enterScrubMode();
  state.scrubPaintToken++;
  clearSeedMask();        // all segments
  clearPropMasks();       // all segments
  clearDoneFills();
  clearOverlay();
  state.ptsTable = null;
  state.pendingTargetFrame = null;
  state.isSeeking = false;
  state.lastSeekTarget = null;
  state.pendingSeedClick = false;
  state.seedComputingSeg = null;
  state.seedComputeStartMs = null;
  state.propPhase = null;
  state.propDoneCount = 0;
  state.propExpected = 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = null;
  if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
  renderSegmentsStrip();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  startSse();
  fetchPts(item.slug);
  rehydrateMasks(item.slug);
  loadFrameSource(item.slug);
  el.videoWrap.focus();
  return true;
}

async function selectSlug(slug) {
  // Refetch /api/items first so propagate_status / seed_frame for sessions
  // that completed in the background (SSE was closed when we switched away)
  // reflect what's actually on disk. Cheap (~50ms LAN). Without this,
  // switching back to a session that finished propagating elsewhere shows
  // stale "running" state until the next reload.
  try {
    const items = await fetchItems();
    state.items = items;
    renderSidebar();
  } catch (e) { console.warn("items refresh on selectSlug failed", e); }
  // Rapid hash navigation can race: if the user already moved to a different
  // slug while we were awaiting fetchItems, abandon this sync — the newer
  // selectSlug call will handle the latest target.
  const m = (window.location.hash || "").match(/slug=([^&]+)/);
  const wantedSlug = m ? decodeURIComponent(m[1]) : slug;
  if (wantedSlug !== slug) return;
  const item = state.items.find((it) => it.slug === slug);
  if (!item) { showError(`slug not found: ${slug}`); return; }
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
  try { state.items = await fetchItems(); }
  catch (e) { showError(String(e)); return; }
  try {
    const models = await fetchModels();
    populateModelPicker(el.seedModel, models.available, models.active.seed);
    populateModelPicker(el.propModel, models.available, models.active.prop);
    el.seedModel.addEventListener("change", () => { setActiveModel("seed", el.seedModel.value); el.seedModel.blur(); });
    el.propModel.addEventListener("change", () => { setActiveModel("prop", el.propModel.value); el.propModel.blur(); });
  } catch (e) { showError(`models init failed: ${e}`); }
  renderSidebar();
  el.btnRescan.addEventListener("click", rescanItems);
  el.btnQueue.addEventListener("click", toggleQueue);
  startQueueSse();
  fetchQueueStatus();
  window.addEventListener("hashchange", onHashChange);
  const slug = pickInitialSlug(state.items);
  if (!slug) return;
  selectSlug(slug);
}

function onHashChange() {
  const m = (window.location.hash || "").match(/slug=([^&]+)/);
  if (!m) return;
  selectSlug(decodeURIComponent(m[1]));
}

function effectiveStatus(it) {
  return aggregateStatus(it);
}

function segmentCounts(it) {
  const segs = it.segments || [];
  const done = segs.filter(s => s.propagate_status === "done").length;
  return { done, total: segs.length };
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
    const counts = segmentCounts(it);
    const segLabel = counts.total > 0 ? `${counts.done}/${counts.total} seg` : "";
    const meta = [fps, frames, segLabel].filter(Boolean).join(" · ");
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
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteItem(it.slug); });
    card.appendChild(nameDiv);
    card.appendChild(metaDiv);
    if (state.queueSnapshot.current && state.queueSnapshot.current.slug === it.slug) {
      const bar = document.createElement("div");
      bar.className = "item-card-progress";
      const fill = document.createElement("div");
      fill.className = "item-card-progress-fill";
      const snap = state.queueSnapshot;
      const pct = snap.frame_total > 0
        ? Math.min(100, (snap.frame_done / snap.frame_total) * 100) : 0;
      fill.style.width = `${pct.toFixed(1)}%`;
      bar.appendChild(fill);
      const label = document.createElement("div");
      label.className = "item-card-progress-label";
      label.textContent = snap.frame_total > 0 ? `${snap.frame_done}/${snap.frame_total}` : "starting…";
      card.appendChild(bar);
      card.appendChild(label);
    }
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
    const snap = state.queueSnapshot;
    const sessions = snap.total > 0 ? ` ${snap.done}/${snap.total}` : "";
    const elapsed = snap.elapsed_s > 0 ? ` · ${Math.round(snap.elapsed_s)}s` : "";
    el.btnQueue.textContent = `■ Stop Queue${sessions}${elapsed}`;
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

function updateSidebarStatus(slug, segId, status) {
  // Stamps the segment-level propagate_status, then refreshes the sidebar dot
  // (uses aggregateStatus across all segments) and the segments strip if
  // this slug is currently active.
  const it = state.items.find(i => i.slug === slug);
  if (it) {
    const seg = (it.segments || []).find(s => s.id === segId);
    if (seg) seg.propagate_status = status;
  }
  const card = el.itemList.querySelector(`.item-card[data-slug="${slug}"]`);
  if (card) {
    const dot = card.querySelector(".item-status");
    if (dot) dot.className = `item-status item-status-${it ? effectiveStatus(it) : status}`;
  }
  if (slug === state.current) renderSegmentsStrip();
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
  if (!r.ok) showError(`queue start failed: HTTP ${r.status}`);
}

function applyQueueSnapshot(snap) {
  state.queueSnapshot = {
    running: snap.running, current: snap.current, done: snap.done, ready: snap.ready, total: snap.total,
    frame_done: snap.frame_done ?? 0, frame_total: snap.frame_total ?? 0, elapsed_s: snap.elapsed_s ?? 0,
  };
  state.queueRunning = state.queueSnapshot.running;
  updateQueueButton();
  renderSidebar();
}

async function fetchQueueStatus() {
  try {
    const r = await fetch(`${API_BASE}/api/queue/status`);
    if (!r.ok) return;
    applyQueueSnapshot(await r.json());
  } catch (_) {}
}

function startQueueSse() {
  if (state.queueSse) { state.queueSse.close(); state.queueSse = null; }
  const es = new EventSource(`${API_BASE}/api/items/__queue__/events`);
  state.queueSse = es;
  es.addEventListener("queue", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    applyQueueSnapshot(payload);
  });
  es.onerror = () => {};
}

async function rescanItems() {
  el.btnRescan.disabled = true;
  try {
    const r = await fetch(`${API_BASE}/api/items/rescan`, { method: "POST" });
    if (!r.ok) { showError(`rescan failed: HTTP ${r.status}`); return; }
    const data = await r.json();
    state.items = data.items || [];
    renderSidebar();
    if (!state.current && state.items.length) selectSlug(state.items[0].slug);
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
    if (state.frameSource) { state.frameSource.close(); state.frameSource = null; }
  }
  renderSidebar();
  if (wasCurrent) {
    if (state.items.length) window.location.hash = `slug=${state.items[0].slug}`;
    else el.itemSlug.textContent = "no item";
  }
}

function startSse() {
  if (state.sse) { state.sse.close(); state.sse = null; }
  if (!state.current) return;
  // Capture the slug this SSE is bound to. Per WHATWG spec, EventSource.close()
  // prevents new connections but doesn't synchronously kill an already-queued
  // dispatch task — so a "done" event for slug A can theoretically still fire
  // a microtask after we switched to B and closed A's stream. Every handler
  // below gates on capturedSlug so it can never bleed into the new view.
  const capturedSlug = state.current;
  const url = `${API_BASE}/api/items/${encodeURIComponent(capturedSlug)}/events`;
  const es = new EventSource(url);
  state.sse = es;
  es.addEventListener("mask", (ev) => {
    if (state.current !== capturedSlug) return;
    let payload;
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    const frame = payload.frame;
    const maskUrl = payload.mask_url;
    const segId = payload.seg_id;
    if (typeof frame !== "number" || typeof maskUrl !== "string" || typeof segId !== "string") return;
    let m = state.propMaskUrlsBySeg.get(segId);
    if (!m) { m = new Map(); state.propMaskUrlsBySeg.set(segId, m); }
    m.set(frame, maskUrl);
    if (segId === state.activeSegmentId) {
      state.propDoneCount = m.size;
      addDoneFill(frame);
      if (el.overlay.width && el.overlay.height) {
        const img = new Image();
        img.onload = () => {
          try { state.tintedCache.set(frame, buildTintedCanvas(img)); } catch (_) {}
          if (state.current === capturedSlug && currentFrame() === frame) blitCachedMask(frame);
        };
        img.src = maskUrl;
      } else if (frame === currentFrame()) {
        repaintOverlayForCurrentFrame();
      }
      updateStatus();
    }
  });
  es.addEventListener("phase", (ev) => {
    if (state.current !== capturedSlug) return;
    let payload;
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    if (payload.seg_id !== state.activeSegmentId) return;  // not the segment we're viewing
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
    const segId = payload.seg_id;
    if (typeof segId !== "string") return;
    updateSidebarStatus(capturedSlug, segId, "done");
    if (state.current === capturedSlug && segId === state.activeSegmentId) {
      state.propPhase = "done";
      if (typeof payload.elapsed_s === "number") state.propPhaseElapsed = payload.elapsed_s;
      if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
      updatePropagateBtn();
      updateStatus();
      prefetchMasks(capturedSlug);
    }
  });
  es.addEventListener("error", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
    const segId = payload.seg_id;
    if (typeof segId === "string") {
      updateSidebarStatus(capturedSlug, segId, "failed");
    }
    if (state.current === capturedSlug && payload.msg && segId === state.activeSegmentId) {
      showError(`propagate: ${payload.msg}`);
    }
  });
  es.onerror = () => {};
}

async function postJson(path, body) {
  return await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body == null ? "{}" : JSON.stringify(body),
  });
}

async function ensureActiveEditableSegment() {
  // Returns the active segment if writable; else creates a fresh one and
  // returns it. "Auto-new" guard: we never overwrite a done/running segment's
  // in/out — that would silently invalidate masks that took 30s+ to compute.
  let seg = activeSegment();
  if (seg && seg.propagate_status !== "done" && seg.propagate_status !== "running") {
    return seg;
  }
  if (currentItem() == null) return null;
  const newSeg = await createSegment();
  return newSeg;
}

async function markIn() {
  if (!state.current) return;
  const seg = await ensureActiveEditableSegment();
  if (!seg) return;
  seg.in_frame = currentFrame();
  updateMarkers();
  renderSegmentsStrip();
  updatePropagateBtn();
  updateStatus();
  if (seg.out_frame != null) sendTrim(seg);
}

async function markOut() {
  if (!state.current) return;
  const seg = await ensureActiveEditableSegment();
  if (!seg) return;
  seg.out_frame = currentFrame();
  updateMarkers();
  renderSegmentsStrip();
  updatePropagateBtn();
  updateStatus();
  if (seg.in_frame != null) sendTrim(seg);
}

async function sendTrim(seg) {
  if (!seg || seg.in_frame == null || seg.out_frame == null) return;
  const capturedSlug = state.current;
  const segId = seg.id;
  const inF = seg.in_frame, outF = seg.out_frame;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/trim`, {
      seg_id: segId, in_frame: inF, out_frame: outF,
    });
    if (state.current !== capturedSlug) return;
    if (!r.ok) showError(`trim failed: HTTP ${r.status}`);
    else if (state.frameSource && state.activeSegmentId === segId) {
      state.frameSource.prefetchRange(inF, outF);
    }
  } catch (e) {
    if (state.current === capturedSlug) showError(`trim failed: ${e}`);
  }
}

async function markSeed() {
  if (!state.current) return;
  const seg = await ensureActiveEditableSegment();
  if (!seg) return;
  const f = currentFrame();
  if (seg.in_frame == null || seg.out_frame == null) {
    showError("mark in/out first before seeding");
    return;
  }
  if (f < seg.in_frame || f > seg.out_frame) {
    showError(`current frame ${f} outside segment range [${seg.in_frame}, ${seg.out_frame}] — scrub into range first`);
    return;
  }
  seg.seed_frame = f;
  seg.seed_point = null;
  state.pendingSeedClick = true;
  clearSeedMask(seg.id);
  clearOverlay();
  updateMarkers();
  renderSegmentsStrip();
  updatePropagateBtn();
  updateStatus();
  console.log("mark seed", seg.id, seg.seed_frame, "awaiting click");
}

async function createSegment() {
  if (!state.current) return null;
  const capturedSlug = state.current;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/segments/new`);
    if (!r.ok) { showError(`new segment failed: HTTP ${r.status}`); return null; }
    const j = await r.json();
    const segId = j.seg_id;
    if (state.current !== capturedSlug) return null;
    const it = currentItem();
    if (!it) return null;
    it.segments = it.segments || [];
    const newSeg = {
      id: segId, in_frame: null, out_frame: null,
      seed_frame: null, seed_point: null, propagate_status: "idle",
    };
    it.segments.push(newSeg);
    setActiveSegmentLocal(it, segId);
    clearDoneFills();   // active changed → wipe overlay state
    clearOverlay();
    state.propDoneCount = 0;
    state.propExpected = 0;
    state.propPhase = null;
    renderSegmentsStrip();
    updateMarkers();
    updatePropagateBtn();
    renderSidebar();
    updateStatus();
    return newSeg;
  } catch (e) { showError(`new segment failed: ${e}`); return null; }
}

async function deleteSegment(segId) {
  if (!confirm(`Delete segment ${segId}?\n\nMasks for this segment will be removed.`)) return;
  const capturedSlug = state.current;
  const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/segments/${segId}/delete`);
  if (!r.ok) { showError(`delete segment failed: HTTP ${r.status}`); return; }
  if (state.current !== capturedSlug) return;
  const it = currentItem();
  if (!it) return;
  it.segments = (it.segments || []).filter(s => s.id !== segId);
  if (state.activeSegmentId === segId) {
    setActiveSegmentLocal(it, it.segments.length ? it.segments[it.segments.length - 1].id : null);
  }
  clearSeedMask(segId);
  clearPropMasks(segId);
  clearDoneFills();
  // Re-add fills for whatever the new active segment has.
  const m = maskUrlsForActive();
  for (const f of m.keys()) addDoneFill(f);
  renderSegmentsStrip();
  renderSidebar();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  scheduleScrubPaint(state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0);
}

async function activateSegment(segId) {
  if (!state.current || state.activeSegmentId === segId) return;
  const it = currentItem();
  if (!it) return;
  setActiveSegmentLocal(it, segId);
  // Persist server-side (best-effort; UI doesn't depend on it).
  postJson(`/api/items/${encodeURIComponent(it.slug)}/segments/${segId}/active`)
    .catch(() => {});
  clearDoneFills();
  const m = maskUrlsForActive();
  for (const f of m.keys()) addDoneFill(f);
  state.propDoneCount = m.size;
  const seg = activeSegment();
  state.propExpected = (seg && seg.in_frame != null && seg.out_frame != null)
    ? (seg.out_frame - seg.in_frame + 1) : 0;
  state.propPhase = null;
  state.propStartMs = null;
  state.propPhaseElapsed = 0;
  if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
  renderSegmentsStrip();
  updateMarkers();
  updatePropagateBtn();
  renderSidebar();
  if (state.frameSource && seg && seg.in_frame != null && seg.out_frame != null) {
    state.frameSource.prefetchRange(seg.in_frame, seg.out_frame);
  }
  scheduleScrubPaint(state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0);
  // scheduleScrubPaint relies on FrameSource being loaded; if user just reloaded
  // and clicks a chip before the mp4 demux finished, the paint silently no-ops.
  // repaintOverlayForCurrentFrame only touches the overlay canvas — no FrameSource
  // needed — so the mask + crosshair show up immediately even on cold cache.
  repaintOverlayForCurrentFrame();
  updateStatus();
}

async function sendSeed(frameIndex, x, y) {
  const capturedSlug = state.current;
  const capturedSegId = state.activeSegmentId;
  if (!capturedSegId) return;
  state.seedComputingSeg = capturedSegId;
  state.seedComputeStartMs = performance.now();
  updateStatus();
  const tickHandle = setInterval(updateStatus, 200);
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(capturedSlug)}/seed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seg_id: capturedSegId, frame_index: frameIndex, x, y }),
    });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      if (state.current === capturedSlug) showError(`seed failed: HTTP ${r.status} ${text}`);
      else console.warn(`seed for ${capturedSlug}/${capturedSegId} failed: HTTP ${r.status}`);
      return;
    }
    const blob = await r.blob();
    // Mirror server-truth into items[].segments
    const it = state.items.find(i => i.slug === capturedSlug);
    const seg = it && (it.segments || []).find(s => s.id === capturedSegId);
    if (seg) {
      seg.seed_frame = frameIndex;
      seg.seed_point = [x, y];
      // Backend wipes prior PNGs on reseed and resets propagate_status to idle.
      // Mirror that here so chip color + queue counts stay in sync.
      seg.propagate_status = "idle";
    }
    if (it) {
      const card = el.itemList.querySelector(`.item-card[data-slug="${capturedSlug}"]`);
      if (card) {
        const dot = card.querySelector(".item-status");
        if (dot) dot.className = `item-status item-status-${effectiveStatus(it)}`;
      }
      updateQueueButton();
    }
    if (state.current === capturedSlug) {
      clearSeedMask(capturedSegId);
      // Backend wipes ALL prior PNGs in masks/<seg>/ on reseed (orphan seeds
      // + stale propagate masks). Mirror that purge in client caches.
      // Order matters: do destructive cleanup BEFORE creating the new blob URL
      // — clearPropMasks revokes all blob URLs in propMaskUrlsBySeg, and we
      // don't want it eating the URL we just made.
      if (state.activeSegmentId === capturedSegId) {
        // Active seg: full reset — wipe done fills, all-seg prop URLs, tinted
        // cache. Counters back to 0; chip color flipped via seg.propagate_status
        // mutation above.
        clearDoneFills();
        state.propDoneCount = 0;
        state.propExpected = 0;
        state.propPhase = null;
      } else {
        // Background seg: only wipe its own propMask map + tintedCache entries.
        const oldMap = state.propMaskUrlsBySeg.get(capturedSegId);
        if (oldMap) {
          for (const f of oldMap.keys()) state.tintedCache.delete(f);
        }
        clearPropMasks(capturedSegId);
      }
      const blobUrl = URL.createObjectURL(blob);
      state.seedMaskUrls.set(capturedSegId, blobUrl);
      const segMap = new Map();
      state.propMaskUrlsBySeg.set(capturedSegId, segMap);
      segMap.set(frameIndex, blobUrl);
      if (state.activeSegmentId === capturedSegId && currentFrame() === frameIndex) {
        loadMaskForFrame(frameIndex);
      }
      renderSegmentsStrip();
      updatePropagateBtn();
      updateStatus();
    }
  } catch (e) {
    if (state.current === capturedSlug) showError(`seed failed: ${e}`);
    else console.warn(`seed for ${capturedSlug}/${capturedSegId} threw:`, e);
  } finally {
    clearInterval(tickHandle);
    if (state.seedComputingSeg === capturedSegId) {
      state.seedComputingSeg = null;
      state.seedComputeStartMs = null;
      updateStatus();
    }
  }
}

async function propagate() {
  if (el.btnPropagate.disabled) return;
  const capturedSlug = state.current;
  const seg = activeSegment();
  if (!seg) return;
  const capturedSegId = seg.id;
  seg.propagate_status = "running";
  updateSidebarStatus(capturedSlug, capturedSegId, "running");
  state.propPhase = "starting";
  state.propDoneCount = 0;
  state.propExpected = (seg.out_frame - seg.in_frame + 1) || 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = performance.now();
  // Wipe THIS segment's prior masks so a re-propagate doesn't show stale data.
  clearPropMasks(capturedSegId);
  clearDoneFills();
  updatePropagateBtn();
  updateStatus();
  if (state.propTickHandle) clearInterval(state.propTickHandle);
  state.propTickHandle = setInterval(updateStatus, 200);
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/propagate`,
      { seg_id: capturedSegId });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      updateSidebarStatus(capturedSlug, capturedSegId, "failed");
      if (state.current === capturedSlug && state.activeSegmentId === capturedSegId) {
        showError(`propagate failed: HTTP ${r.status} ${text}`);
        clearInterval(state.propTickHandle);
        state.propTickHandle = null;
        updatePropagateBtn();
        updateStatus();
      }
    }
  } catch (e) {
    updateSidebarStatus(capturedSlug, capturedSegId, "failed");
    if (state.current === capturedSlug && state.activeSegmentId === capturedSegId) {
      clearInterval(state.propTickHandle);
      state.propTickHandle = null;
      showError(`propagate failed: ${e}`);
      updatePropagateBtn();
      updateStatus();
    }
  }
}

async function cancelOrEscape() {
  if (state.pendingSeedClick) {
    state.pendingSeedClick = false;
    updateStatus();
    return;
  }
  const seg = activeSegment();
  if (!seg || seg.propagate_status !== "running" || !state.current) return;
  const capturedSlug = state.current;
  const capturedSegId = seg.id;
  try {
    const r = await postJson(
      `/api/items/${encodeURIComponent(capturedSlug)}/propagate/cancel`,
      { seg_id: capturedSegId },
    );
    if (r.status !== 404 && !r.ok) console.warn(`cancel HTTP ${r.status}`);
  } catch (e) { console.warn("cancel failed", e); }
  updateSidebarStatus(capturedSlug, capturedSegId, "idle");
  if (state.current === capturedSlug && state.activeSegmentId === capturedSegId) {
    updatePropagateBtn();
    updateStatus();
  }
}

function stepFrames(delta) {
  if (!state.ptsTable) return;
  if (!el.video.paused) el.video.pause();
  const start = state.pendingTargetFrame != null
    ? state.pendingTargetFrame
    : parseInt(el.scrubber.value, 10);
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
  el.scrubber.value = String(target);
  // Snap lastDisplayedFrame so currentFrame()/repaintOverlayForCurrentFrame()
  // see the new frame even before scheduleScrubPaint's async paintAtomic lands.
  state.lastDisplayedFrame = target;
  scheduleScrubPaint(target);
  // Belt-and-suspenders: scheduleScrubPaint's internal mask handling has races
  // around overlay resize + ensureTintedCanvas timing. loadMaskForFrame (called
  // from repaintOverlayForCurrentFrame) has an independent image.onload → blit
  // lifecycle that survives those races.
  repaintOverlayForCurrentFrame();
}

async function fetchPts(slug) {
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/pts`);
    if (slug !== state.current) return;
    if (!r.ok) { showError(`pts fetch HTTP ${r.status}`); return; }
    const j = await r.json();
    if (slug !== state.current) return;
    if (j && Array.isArray(j.pts) && typeof j.total_frames === "number") {
      state.ptsTable = j.pts;
      state.totalFrames = j.total_frames;
      el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
      updateMarkers();
      console.log(`pts loaded: ${j.pts.length} frames`);
      // Reload UX (mirror of loadFrameSource): if pts arrives second, do the
      // seed-frame auto-jump now. Guarded the same way so we never yank a
      // user who already moved.
      const seg = activeSegment();
      console.log("[fetchPts done]", { activeSegId: state.activeSegmentId, segSeedFrame: seg?.seed_frame, lastDisplayedFrame: state.lastDisplayedFrame, fsReady: !!state.frameSource && !state.frameSourceLoading });
      if (seg && seg.seed_frame != null && state.lastDisplayedFrame === 0
          && state.frameSource && !state.frameSourceLoading) {
        console.log("[fetchPts] auto-jump →", seg.seed_frame);
        jumpToFrame(seg.seed_frame);
      }
    }
  } catch (e) { if (slug === state.current) showError(`pts fetch failed: ${e}`); }
}

async function rehydrateMasks(slug) {
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/masks`);
    if (!r.ok) return;
    const j = await r.json();
    if (slug !== state.current) return;
    if (!j.segments || typeof j.segments !== "object") return;
    for (const [segId, frames] of Object.entries(j.segments)) {
      if (!Array.isArray(frames)) continue;
      let m = state.propMaskUrlsBySeg.get(segId);
      if (!m) { m = new Map(); state.propMaskUrlsBySeg.set(segId, m); }
      for (const f of frames) {
        const url = `${API_BASE}/mask/${encodeURIComponent(slug)}/${segId}/${String(f).padStart(5, "0")}.png`;
        m.set(f, url);
      }
    }
    // Render fills for the active segment only.
    const active = maskUrlsForActive();
    for (const f of active.keys()) addDoneFill(f);
    state.propDoneCount = active.size;
    const seg = activeSegment();
    if (seg && seg.propagate_status === "done") {
      state.propExpected = (seg.in_frame != null && seg.out_frame != null)
        ? (seg.out_frame - seg.in_frame + 1) : state.propDoneCount;
    }
    repaintOverlayForCurrentFrame();
    updateStatus();
    prefetchMasks(slug);
  } catch (e) { console.warn("rehydrate masks failed", e); }
}

function togglePlay() {
  if (state.scrubMode) {
    const tbl = state.ptsTable;
    const f = state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0;
    const targetT = tbl ? tbl[f] : null;
    if (targetT == null) return;
    if (Math.abs(el.video.currentTime - targetT) > 0.005) el.video.currentTime = targetT;
    state.lastDisplayedMediaTime = targetT;
    exitScrubMode();
    el.video.play().catch((e) => { console.warn("play failed", e); enterScrubMode(); });
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
  if (!state.pendingSeedClick) { togglePlay(); return; }
  const seg = activeSegment();
  if (!seg || seg.seed_frame == null) return;
  const fs = state.frameSource;
  const nativeW = (fs && fs.width) || el.video.videoWidth;
  const nativeH = (fs && fs.height) || el.video.videoHeight;
  if (!nativeW || !nativeH) { showError("frame source not ready"); return; }
  const disp = videoDisplayRect();
  if (!disp) { showError("video display rect unresolved"); return; }
  if (e.clientX < disp.left || e.clientX > disp.left + disp.width ||
      e.clientY < disp.top || e.clientY > disp.top + disp.height) {
    console.warn("click outside video frame area, ignored");
    return;
  }
  const x = Math.round((e.clientX - disp.left) * (nativeW / disp.width));
  const y = Math.round((e.clientY - disp.top) * (nativeH / disp.height));
  console.log("seed click", { client: [e.clientX, e.clientY], disp, native: [x, y] });
  seg.seed_point = [x, y];
  state.pendingSeedClick = false;
  updateStatus();
  sendSeed(seg.seed_frame, x, y);
}

function onKeydown(e) {
  const tag = (e.target && e.target.tagName) || "";
  const isInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  const isNavKey = e.key === " " || e.key === "ArrowLeft" || e.key === "ArrowRight"
    || e.key === "Home" || e.key === "End";
  if (isInput && e.target !== el.scrubber && !isNavKey) return;

  if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft" || e.key === ",") {
    e.preventDefault();
    stepFrames(e.altKey ? -100 : (e.shiftKey ? -10 : -1));
  } else if (e.key === "ArrowRight" || e.key === ".") {
    e.preventDefault();
    stepFrames(e.altKey ? 100 : (e.shiftKey ? 10 : 1));
  } else if (e.key === "Home") {
    e.preventDefault();
    const seg = activeSegment();
    jumpToFrame(seg && seg.in_frame != null ? seg.in_frame : 0);
  } else if (e.key === "End") {
    e.preventDefault();
    const seg = activeSegment();
    jumpToFrame(seg && seg.out_frame != null ? seg.out_frame : state.totalFrames - 1);
  } else if (e.key === "[") markIn();
  else if (e.key === "]") markOut();
  else if (e.key === "s" || e.key === "S") markSeed();
  else if (e.key === "n" || e.key === "N") createSegment();
  else if (e.key === "Enter") propagate();
  else if (e.key === "Escape") cancelOrEscape();
  else if (e.key === "h" || e.key === "H") toggleSeedMarker();
}

function toggleSeedMarker() {
  state.showSeedMarker = !state.showSeedMarker;
  updateSeedMarkerBtn();
  const f = currentFrame();
  const seg = activeSegment();
  if (maskUrlForFrame(f) != null) loadMaskForFrame(f);
  else {
    clearOverlay();
    if (state.showSeedMarker && seg && f === seg.seed_frame && seg.seed_point) {
      drawClickMarker(seg.seed_point[0], seg.seed_point[1]);
    }
  }
}

function updateSeedMarkerBtn() {
  if (!el.btnToggleSeedMarker) return;
  el.btnToggleSeedMarker.textContent =
    state.showSeedMarker ? "Hide Seed × (H)" : "Show Seed × (H)";
}

function onDisplayedFrame(_now, metadata) {
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
      repaintOverlayForCurrentFrame();
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
      console.warn("requestVideoFrameCallback unsupported; falling back to timeupdate");
    }
  });
  el.video.addEventListener("timeupdate", () => {
    if (typeof el.video.requestVideoFrameCallback === "function") return;
    if (state.fps != null) el.scrubber.value = String(currentFrame());
    repaintOverlayForCurrentFrame();
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
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => resizeOverlay()).observe(el.video);
  } else {
    window.addEventListener("resize", resizeOverlay);
  }
  el.video.addEventListener("seeked", () => resizeOverlay());

  // rAF-coalesce scrubber input. Native input fires 200+/sec on a fast drag;
  // each call would queue a paint, saturating the main thread. Coalescing
  // caps paint rate at refresh and leaves room for input handling.
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
  el.scrubber.addEventListener("change", () => {
    if (!state.ptsTable) return;
    const f = parseInt(el.scrubber.value, 10);
    const t = state.ptsTable[f];
    if (t != null && Math.abs(el.video.currentTime - t) > 0.005) el.video.currentTime = t;
  });
  el.scrubber.addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
         "PageUp", "PageDown", "Home", "End"].includes(e.key)) {
      e.preventDefault();
    }
  });

  el.btnIn.addEventListener("click", markIn);
  el.btnOut.addEventListener("click", markOut);
  el.btnSeed.addEventListener("click", markSeed);
  el.btnNewSeg.addEventListener("click", () => createSegment());
  el.btnPropagate.addEventListener("click", propagate);
  el.btnCancel.addEventListener("click", cancelOrEscape);
  el.btnToggleSeedMarker.addEventListener("click", toggleSeedMarker);
  updateSeedMarkerBtn();

  document.addEventListener("keydown", onKeydown);
}

bindUi();
bootstrap();
