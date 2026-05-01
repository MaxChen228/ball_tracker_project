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
  if (state.fps == null) return 0;
  // Prefer mediaTime from requestVideoFrameCallback when available — it is the
  // exact PTS of the frame the compositor just painted, not the timeupdate-
  // throttled currentTime which can lag by up to 250ms.
  const t = state.lastDisplayedMediaTime != null
    ? state.lastDisplayedMediaTime
    : el.video.currentTime;
  return Math.round(t * state.fps);
}

function frameToTime(f) {
  if (state.fps == null) return 0;
  return f / state.fps;
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
    statusTag = `propagate done: ${state.propDoneCount}/${state.propExpected} frames in ${state.propPhaseElapsed}s`;
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
  state.seedMaskReady = false;
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  startSse();
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

async function bootstrap() {
  try {
    state.items = await fetchItems();
  } catch (e) {
    showError(String(e));
    return;
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
  if (state.fps == null) {
    showError("fps unset; cannot step frames");
    return;
  }
  const f = Math.max(0, Math.min(state.totalFrames - 1, currentFrame() + delta));
  el.video.currentTime = frameToTime(f);
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
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
    if (e.target !== el.scrubber) return;
  }
  if (e.key === " ") {
    e.preventDefault();
    if (el.video.paused) el.video.play(); else el.video.pause();
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    stepFrames(e.shiftKey ? -10 : -1);
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    stepFrames(e.shiftKey ? 10 : 1);
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
  if (state.fps != null) {
    const f = Math.round(metadata.mediaTime * state.fps);
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
  // browsers when the metadata updates.
  el.video.addEventListener("seeked", resizeOverlay);

  el.scrubber.addEventListener("input", () => {
    const f = parseInt(el.scrubber.value, 10);
    if (state.fps == null) return;
    el.video.currentTime = frameToTime(f);
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
