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
};

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
  overlay: document.getElementById("overlay"),
  scrubber: document.getElementById("scrubber"),
  fills: document.getElementById("timeline-fills"),
  mIn: document.getElementById("marker-in"),
  mOut: document.getElementById("marker-out"),
  mSeed: document.getElementById("marker-seed"),
  status: document.getElementById("status-line"),
  statusbar: document.getElementById("statusbar"),
  itemSlug: document.getElementById("item-slug"),
  itemPicker: document.getElementById("item-picker"),
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
  const t = el.video.currentTime || 0;
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
  el.status.textContent =
    `f=${fStr} t=${t.toFixed(3)}s | in=${fmt(state.inFrame)} out=${fmt(state.outFrame)} seed=${fmt(state.seedFrame)} pt=${pt} | status=${statusTag}${state.pendingSeedClick ? " (click to set seed point)" : ""}`;
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
  const w = 100 / state.totalFrames;
  div.style.left = `${(frame / state.totalFrames) * 100}%`;
  div.style.width = `${Math.max(w, 0.2)}%`;
  el.fills.appendChild(div);
}

function clearDoneFills() {
  state.doneFrames.clear();
  el.fills.innerHTML = "";
  clearPropMasks();
}

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
  el.overlay.width = v.videoWidth;
  el.overlay.height = v.videoHeight;
  // Position overlay over the displayed frame (not the element box) so the
  // canvas pixels line up exactly with what the user sees.
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

function drawMaskTinted(img) {
  const c = el.overlay;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  const tmp = document.createElement("canvas");
  tmp.width = c.width;
  tmp.height = c.height;
  const tctx = tmp.getContext("2d");
  tctx.drawImage(img, 0, 0, c.width, c.height);
  const data = tctx.getImageData(0, 0, c.width, c.height);
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
  tctx.putImageData(data, 0, 0);
  ctx.drawImage(tmp, 0, 0);
}

function maskUrlForFrame(frame) {
  if (state.propMaskUrlByFrame.has(frame)) return state.propMaskUrlByFrame.get(frame);
  if (frame === state.seedFrame && state.seedMaskUrl) return state.seedMaskUrl;
  return null;
}

async function loadMaskForFrame(frame) {
  const url = maskUrlForFrame(frame);
  if (!url) {
    clearOverlay();
    return;
  }
  const img = new Image();
  img.onload = () => {
    if (currentFrame() === frame) {
      drawMaskTinted(img);
      if (frame === state.seedFrame && state.seedPoint) {
        drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
      }
    }
  };
  img.onerror = () => clearOverlay();
  img.src = url;
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
  el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
  el.scrubber.value = "0";
  el.video.src = `${API_BASE}/clip/${item.slug}.mp4`;
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
    el.seedModel.addEventListener("change", () => setActiveModel("seed", el.seedModel.value));
    el.propModel.addEventListener("change", () => setActiveModel("prop", el.propModel.value));
  } catch (e) {
    showError(`models init failed: ${e}`);
  }
  el.itemPicker.innerHTML = "";
  for (const it of state.items) {
    const opt = document.createElement("option");
    opt.value = it.slug;
    opt.textContent = it.slug;
    el.itemPicker.appendChild(opt);
  }
  el.itemPicker.addEventListener("change", () => {
    window.location.hash = `slug=${el.itemPicker.value}`;
  });
  window.addEventListener("hashchange", onHashChange);
  const slug = pickInitialSlug(state.items);
  if (!slug) {
    showError("no items returned by /api/items");
    return;
  }
  el.itemPicker.value = slug;
  selectSlug(slug);
}

function onHashChange() {
  const m = (window.location.hash || "").match(/slug=([^&]+)/);
  if (!m) return;
  const slug = decodeURIComponent(m[1]);
  el.itemPicker.value = slug;
  selectSlug(slug);
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
    if (frame === currentFrame()) loadMaskForFrame(frame);
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
    updatePropagateBtn();
    updateStatus();
  });
  es.addEventListener("error", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
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
  if (!state.isSeeking) flushStep();
}

function jumpToFrame(f) {
  if (!state.ptsTable) return;
  if (!el.video.paused) el.video.pause();
  const target = Math.max(0, Math.min(state.totalFrames - 1, f));
  state.pendingTargetFrame = target;
  if (!state.isSeeking) flushStep();
}

function flushStep() {
  if (state.pendingTargetFrame == null) return;
  const target = state.pendingTargetFrame;
  state.pendingTargetFrame = null;
  state.isSeeking = true;
  state.lastSeekTarget = target;
  // Optimistic UI snap: paint mask + scrubber + status BEFORE the browser
  // finishes seeking. rVFC overwrites with truth when the real frame paints.
  const tbl = state.ptsTable;
  state.lastDisplayedMediaTime = tbl[target];
  state.lastDisplayedFrame = target;
  el.scrubber.value = String(target);
  if (maskUrlForFrame(target) != null) loadMaskForFrame(target);
  else clearOverlay();
  if (target === state.seedFrame && state.seedPoint && maskUrlForFrame(target) == null) {
    drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
  }
  updateStatus();
  el.video.currentTime = frameToTime(target);
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
  } catch (e) {
    console.warn("rehydrate masks failed", e);
  }
}

function onVideoClick(e) {
  if (!state.pendingSeedClick) {
    if (el.video.paused) el.video.play(); else el.video.pause();
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
  // Allow keys to pass through into the scrubber (we suppress its native arrow
  // handling below); for any other input element, leave the user alone.
  if (isInput && e.target !== el.scrubber) return;

  // Big jumps: Shift+arrow = ±10, Alt/Option+arrow = ±100, plain = ±1.
  // Industry-standard ,/. as alternates so right-handed mouse + left-hand
  // keyboard works without claw-grip on the cursor cluster.
  if (e.key === " ") {
    e.preventDefault();
    if (el.video.paused) el.video.play(); else el.video.pause();
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
  // Fires once per actually-presented video frame. metadata.mediaTime is the
  // exact PTS of the frame the compositor just painted. Drive every per-frame
  // visual update from here so mask / scrubber / overlay stay locked to the
  // displayed video frame, not to the throttled timeupdate event.
  state.lastDisplayedMediaTime = metadata.mediaTime;
  if (state.ptsTable) {
    const f = currentFrame();
    // Diagnostic — log when rVFC's f disagrees with the in-flight seek target.
    // Helps confirm whether browser snap-quantized to a neighbor frame.
    if (state.lastSeekTarget != null && f !== state.lastSeekTarget) {
      console.log(`[rVFC] mediaTime=${metadata.mediaTime.toFixed(6)} computed_f=${f} target=${state.lastSeekTarget} (diff=${f - state.lastSeekTarget})`);
    }
    if (state.lastSeekTarget != null && Math.abs(f - state.lastSeekTarget) <= 1) {
      state.lastSeekTarget = null;
    }
    if (f !== state.lastDisplayedFrame) {
      state.lastDisplayedFrame = f;
      el.scrubber.value = String(f);
      if (maskUrlForFrame(f) != null) loadMaskForFrame(f);
      else clearOverlay();
      // Also redraw the click marker if we are on the seed frame, so it stays
      // visible even after the mask reload clearRect'd it.
      if (f === state.seedFrame && state.seedPoint && maskUrlForFrame(f) == null) {
        drawClickMarker(state.seedPoint[0], state.seedPoint[1]);
      }
    }
  }
  resizeOverlay();
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
  el.video.addEventListener("click", onVideoClick);
  // Use ResizeObserver for layout-sensitive overlay; window resize alone misses
  // CSS-driven size changes (e.g. picker dropdown reflow).
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => resizeOverlay()).observe(el.video);
  } else {
    window.addEventListener("resize", resizeOverlay);
  }
  // Also resize on seeked since seeking can change layout briefly on some
  // browsers when the metadata updates. Also: clear the seek gate so any
  // arrow-key presses queued during the in-flight seek can drain.
  el.video.addEventListener("seeked", () => {
    resizeOverlay();
    state.isSeeking = false;
    if (state.pendingTargetFrame != null) flushStep();
  });

  el.scrubber.addEventListener("input", () => {
    const f = parseInt(el.scrubber.value, 10);
    if (!state.ptsTable) return;
    jumpToFrame(f);
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
