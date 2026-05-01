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
  queueSnapshot: { running: false, current: null, done: 0, ready: 0, total: 0 },
  // Pre-tinted canvases keyed by source frame index, built lazily as masks
  // arrive over SSE or via scrub. Costs ~5ms per first paint, then O(1) blits.
  tintedCache: new Map(),
  prefetchAbort: 0,
  // WebCodecs frame source: one MP4 download → mp4box demux → VideoDecoder
  // → ImageBitmap cache, keyed by frame index. Recreated per-slug.
  frameSource: null,
  scrubPaintToken: 0,
  frameSourceLoading: false,
};

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

function clearSeedMask() {
  if (state.seedMaskUrl) URL.revokeObjectURL(state.seedMaskUrl);
  state.seedMaskUrl = null;
}

function clearPropMasks() {
  for (const url of state.propMaskUrlByFrame.values()) URL.revokeObjectURL(url);
  state.propMaskUrlByFrame.clear();
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
  const pt = state.seedPoint ? `(${state.seedPoint[0]},${state.seedPoint[1]})` : "-";
  const fStr = String(f).padStart(4, "0");
  let statusTag = state.propagateStatus;
  let computing = false;
  if (state.frameSourceLoading) {
    statusTag = "loading clip (WebCodecs decode init)…";
    computing = true;
  } else if (state.seedComputing) {
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
    const tail = state.propPhaseElapsed > 0 ? `in ${state.propPhaseElapsed}s` : "(cached on disk)";
    statusTag = `propagate done: ${state.propDoneCount}/${state.propExpected} frames ${tail}`;
  }
  if (computing) el.statusbar.classList.add("computing");
  else el.statusbar.classList.remove("computing");
  el.status.textContent =
    `f=${fStr} t=${t.toFixed(3)}s | in=${fmt(state.inFrame)} out=${fmt(state.outFrame)} seed=${fmt(state.seedFrame)} pt=${pt} | status=${statusTag}${state.pendingSeedClick ? " (click to set seed point)" : ""}`;
}

function updateMarker(markerEl, frame) {
  if (frame == null || state.totalFrames <= 0) { markerEl.hidden = true; return; }
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
    if (frame === state.seedFrame && state.seedPoint) {
      drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
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

async function prefetchMasks(slug) {
  const myToken = ++state.prefetchAbort;
  if (!el.overlay.width || !el.overlay.height) {
    el.video.addEventListener("loadedmetadata", () => prefetchMasks(slug), { once: true });
    return;
  }
  const entries = Array.from(state.propMaskUrlByFrame.entries())
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
  // Prefetch the trim range so subsequent scrub is all cache hits.
  if (state.inFrame != null && state.outFrame != null) {
    fs.prefetchRange(state.inFrame, state.outFrame);
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
  state.scrubPaintToken++;
  clearSeedMask();
  clearDoneFills();
  clearOverlay();
  state.ptsTable = null;
  state.pendingTargetFrame = null;
  state.isSeeking = false;
  state.lastSeekTarget = null;
  // Clear cross-slug-leaking UI flags. Without these, "click to set seed
  // point", a "seeding…" status banner, or a stale propagate ticker can
  // bleed from the previous slug onto this one's view. The actual server
  // work for the prior slug keeps running and self-cleans via captured-slug
  // gates in sendSeed/propagate.
  state.pendingSeedClick = false;
  state.seedComputing = false;
  state.seedComputeStartMs = null;
  state.propPhase = null;
  state.propDoneCount = 0;
  state.propExpected = 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = null;
  if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
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
    delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteItem(it.slug); });
    card.appendChild(nameDiv);
    card.appendChild(metaDiv);
    if (state.queueSnapshot.current === it.slug) {
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

function updateSidebarStatus(slug, status) {
  const it = state.items.find(i => i.slug === slug);
  if (it) { it.status = status; it.propagate_status = status; }
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
    if (typeof frame !== "number" || typeof maskUrl !== "string") return;
    state.propMaskUrlByFrame.set(frame, maskUrl);
    state.propDoneCount = state.propMaskUrlByFrame.size;
    addDoneFill(frame);
    if (el.overlay.width && el.overlay.height) {
      const img = new Image();
      img.onload = () => {
        try { state.tintedCache.set(frame, buildTintedCanvas(img)); } catch (_) {}
        if (state.current === capturedSlug && currentFrame() === frame) blitCachedMask(frame);
      };
      img.src = maskUrl;
    } else if (frame === currentFrame()) {
      loadMaskForFrame(frame);
    }
    updateStatus();
  });
  es.addEventListener("phase", (ev) => {
    if (state.current !== capturedSlug) return;
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
    // Always reflect the captured slug's completion into the sidebar / items
    // — the propagation actually finished on disk for that slug.
    updateSidebarStatus(capturedSlug, "done");
    if (state.current === capturedSlug) {
      state.propagateStatus = "done";
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
    updateSidebarStatus(capturedSlug, "failed");
    if (state.current === capturedSlug && payload.msg) showError(`propagate: ${payload.msg}`);
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

async function markIn() {
  if (!state.current) return;
  const f = currentFrame();
  state.inFrame = f;
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  console.log("mark in", f);
  if (state.outFrame != null) sendTrim();
}

async function markOut() {
  if (!state.current) return;
  const f = currentFrame();
  state.outFrame = f;
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  console.log("mark out", f);
  if (state.inFrame != null) sendTrim();
}

async function sendTrim() {
  if (state.inFrame == null || state.outFrame == null) return;
  const capturedSlug = state.current;
  const inF = state.inFrame, outF = state.outFrame;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/trim`, {
      in_frame: inF, out_frame: outF,
    });
    if (state.current !== capturedSlug) return;  // user moved on; server still got the trim
    if (!r.ok) showError(`trim failed: HTTP ${r.status}`);
    else if (state.frameSource) state.frameSource.prefetchRange(state.inFrame, state.outFrame);
  } catch (e) {
    if (state.current === capturedSlug) showError(`trim failed: ${e}`);
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
  // Capture slug at call site so a slug switch mid-flight doesn't pollute
  // the new slug's state with this seed's result. Server-side the POST URL
  // already pins the work to capturedSlug; the issue is purely frontend
  // bookkeeping leaking across slugs.
  const capturedSlug = state.current;
  state.seedComputing = true;
  state.seedComputeStartMs = performance.now();
  updateStatus();
  const tickHandle = setInterval(updateStatus, 200);
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(capturedSlug)}/seed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frame_index: frameIndex, x, y }),
    });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      if (state.current === capturedSlug) showError(`seed failed: HTTP ${r.status} ${text}`);
      else console.warn(`seed for ${capturedSlug} failed (user already moved on): HTTP ${r.status}`);
      return;
    }
    const blob = await r.blob();
    // Always reflect server-side truth into items[] for the captured slug —
    // its seed file IS on disk now.
    const it = state.items.find(i => i.slug === capturedSlug);
    if (it) {
      it.seed_frame = frameIndex;
      it.seed_point = [x, y];
      const card = el.itemList.querySelector(`.item-card[data-slug="${capturedSlug}"]`);
      if (card) {
        const dot = card.querySelector(".item-status");
        if (dot) dot.className = `item-status item-status-${effectiveStatus(it)}`;
      }
      updateQueueButton();
    }
    // View-state writes ONLY if the user is still looking at this slug.
    // If they switched away, the blob just gets GC'd — we never made an
    // object URL for it on this branch.
    if (state.current === capturedSlug) {
      clearSeedMask();
      state.seedMaskUrl = URL.createObjectURL(blob);
      state.seedMaskReady = true;
      if (currentFrame() === frameIndex) loadMaskForFrame(frameIndex);
      updatePropagateBtn();
    }
  } catch (e) {
    if (state.current === capturedSlug) showError(`seed failed: ${e}`);
    else console.warn(`seed for ${capturedSlug} threw (user already moved on):`, e);
  } finally {
    clearInterval(tickHandle);
    // The "seeding…" status banner only makes sense for the slug we started
    // on; if user switched, B was never seeding so we shouldn't have shown
    // anything for it (handled by syncFromItem reset). Either way this clears
    // the global flag now that this in-flight call is done.
    if (state.current === capturedSlug) {
      state.seedComputing = false;
      state.seedComputeStartMs = null;
      updateStatus();
    }
  }
}

async function propagate() {
  if (el.btnPropagate.disabled) return;
  const capturedSlug = state.current;
  state.propagateStatus = "running";
  updateSidebarStatus(capturedSlug, "running");
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
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/propagate`);
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      updateSidebarStatus(capturedSlug, "failed");
      if (state.current === capturedSlug) {
        state.propagateStatus = "failed";
        showError(`propagate failed: HTTP ${r.status} ${text}`);
        clearInterval(state.propTickHandle);
        state.propTickHandle = null;
        updatePropagateBtn();
        updateStatus();
      } else {
        console.warn(`propagate POST for ${capturedSlug} failed (user moved on): HTTP ${r.status}`);
      }
    }
  } catch (e) {
    updateSidebarStatus(capturedSlug, "failed");
    if (state.current === capturedSlug) {
      state.propagateStatus = "failed";
      clearInterval(state.propTickHandle);
      state.propTickHandle = null;
      showError(`propagate failed: ${e}`);
      updatePropagateBtn();
      updateStatus();
    } else {
      console.warn(`propagate POST for ${capturedSlug} threw (user moved on):`, e);
    }
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
      if (r.status !== 404 && !r.ok) console.warn(`cancel HTTP ${r.status}`);
    } catch (e) { console.warn("cancel failed", e); }
    state.propagateStatus = "idle";
    if (state.current) updateSidebarStatus(state.current, "idle");
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
  scheduleScrubPaint(target);
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
    }
  } catch (e) { if (slug === state.current) showError(`pts fetch failed: ${e}`); }
}

async function rehydrateMasks(slug) {
  try {
    const r = await fetch(`${API_BASE}/api/items/${encodeURIComponent(slug)}/masks`);
    if (!r.ok) return;
    const j = await r.json();
    if (slug !== state.current) return;
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
  if (state.seedFrame == null) return;
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
  state.seedPoint = [x, y];
  state.pendingSeedClick = false;
  updateStatus();
  sendSeed(state.seedFrame, x, y);
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
    jumpToFrame(state.inFrame != null ? state.inFrame : 0);
  } else if (e.key === "End") {
    e.preventDefault();
    jumpToFrame(state.outFrame != null ? state.outFrame : state.totalFrames - 1);
  } else if (e.key === "[") markIn();
  else if (e.key === "]") markOut();
  else if (e.key === "s" || e.key === "S") markSeed();
  else if (e.key === "Enter") propagate();
  else if (e.key === "Escape") cancelOrEscape();
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
      console.warn("requestVideoFrameCallback unsupported; falling back to timeupdate");
    }
  });
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
  el.btnPropagate.addEventListener("click", propagate);
  el.btnCancel.addEventListener("click", cancelOrEscape);

  document.addEventListener("keydown", onKeydown);
}

bindUi();
bootstrap();
