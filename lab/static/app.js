"use strict";

const API_BASE = window.location.origin;

const MASK_COLORS = {
  green:  [34, 197, 94],    // #22c55e
  blue:   [59, 130, 246],   // #3b82f6
  red:    [239, 68, 68],    // #ef4444
  yellow: [250, 204, 21],   // #facc15
};
const MASK_ALPHA = 128;
const MASK_OUTLINE_PX = 2;
const BITMAP_CACHE_LIMIT = 512;

const state = {
  items: [],            // each item carries .segments[]
  current: null,        // current slug
  fps: null,
  totalFrames: 0,
  // No active-segment concept. The seg targeted by [/]/S/Enter is derived from
  // the current frame: segContainingFrame(f) → openSegment() → createSegment().
  // pendingSeedClick stashes {segId, frame} so the next click knows where to
  // route the seed point (current-frame lookup at click time would race against
  // the user scrubbing while the seed mode is pending).
  pendingSeedClick: null,                        // null | {segId, frame}
  doneFrames: new Set(),                         // union of mask frames across all segs
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
  bitmapCache: new Map(),                        // frame → ImageBitmap (mask alpha; LRU bounded)
  prefetchAbort: 0,
  // Source-pixel dimensions of the current item, used for overlay backing-store
  // sizing + click→source coord conversion. Populated from /api/items.
  sourceWidth: 0,
  sourceHeight: 0,
  scrubPaintToken: 0,
  maskColor: (() => {
    const v = localStorage.getItem("labMaskColor");
    return (v && MASK_COLORS[v]) ? v : "green";
  })(),
  // review = QA layer on top of propagation: operator can flag bad-frame
  // ranges + tick "approved" once happy. Mirrored from manifest item.review.
  // pendingBadIn: source frame index where the next "Bad Out" will close,
  // null = no half-open range pending.
  pendingBadIn: null,
};

// ---- segment helpers -------------------------------------------------------

function currentItem() {
  return state.items.find(i => i.slug === state.current) || null;
}

function segmentsOf(slug) {
  const it = state.items.find(i => i.slug === slug);
  return it ? (it.segments || []) : [];
}

// First-class lookups over the always-on multi-seg display. There is no
// "active" seg — every read is keyed by the frame the user is looking at.
function segContainingFrame(f) {
  const it = currentItem();
  if (!it) return null;
  for (const seg of (it.segments || [])) {
    if (seg.in_frame != null && seg.out_frame != null
        && seg.in_frame <= f && f <= seg.out_frame) return seg;
  }
  return null;
}

// The unique seg with in xor out (mid-creation). [/] target before any seg
// contains f. Returns first match — there should never be two open at once
// since [/] always fills the existing open before creating new.
function openSegment() {
  const it = currentItem();
  if (!it) return null;
  for (const seg of (it.segments || [])) {
    const hasIn = seg.in_frame != null;
    const hasOut = seg.out_frame != null;
    if (hasIn !== hasOut) return seg;
  }
  return null;
}

// "The seg the user is contextually editing" — drives status bar, propagate
// button, cancel target. Prefers seg containing current frame (so scrubbing
// into a range surfaces that seg's status), else any open seg.
function contextSegment() {
  const f = currentFrameSafe();
  return segContainingFrame(f) || openSegment();
}

function currentFrameSafe() {
  // currentFrame() depends on ptsTable / scrubMode; safe wrapper for callers
  // (status display) that can be invoked before pts loads.
  if (!state.ptsTable && state.lastDisplayedFrame < 0) return 0;
  return currentFrame();
}

function segWithRunningProp() {
  const it = currentItem();
  if (!it) return null;
  return (it.segments || []).find(s => s.propagate_status === "running") || null;
}

// Mask lookup: scan all segs at this frame. Non-overlapping invariant means
// at most one seg owns the mask (prop fills [in,out]; seed is one frame).
function findMaskAtFrame(f) {
  for (const [segId, m] of state.propMaskUrlsBySeg) {
    if (m.has(f)) return { segId, url: m.get(f) };
  }
  // Fallback: seed mask blob URLs (only present until rehydrate replaces them
  // with /mask/ URLs in propMaskUrlsBySeg).
  const it = currentItem();
  if (it) {
    for (const seg of (it.segments || [])) {
      if (seg.seed_frame === f) {
        const sm = state.seedMaskUrls.get(seg.id);
        if (sm) return { segId: seg.id, url: sm };
      }
    }
  }
  return null;
}

// ---- review helpers --------------------------------------------------------

function reviewOf(it) {
  if (!it) return { approved: false, approved_at: null, bad_ranges: [] };
  const rv = it.review || {};
  return {
    approved: !!rv.approved,
    approved_at: rv.approved_at || null,
    bad_ranges: Array.isArray(rv.bad_ranges) ? rv.bad_ranges : [],
  };
}

function renderApproveCheckbox() {
  const it = currentItem();
  const rv = reviewOf(it);
  if (el.chkApprove) el.chkApprove.checked = rv.approved;
  if (el.approveLabel) el.approveLabel.classList.toggle("approved", rv.approved);
}

function renderBadRangesStrip() {
  const strip = el.badRangesStrip;
  if (!strip) return;
  strip.innerHTML = "";
  const it = currentItem();
  if (!it) return;
  const rv = reviewOf(it);
  if (rv.bad_ranges.length === 0 && state.pendingBadIn == null) return;
  const label = document.createElement("span");
  label.className = "bad-label";
  label.textContent = "BAD:";
  strip.appendChild(label);
  for (const br of rv.bad_ranges) {
    const chip = document.createElement("div");
    chip.className = "bad-chip";
    chip.title = `${br.id} [${br.in_frame}-${br.out_frame}] — click to jump`;
    chip.textContent = `${br.in_frame}–${br.out_frame}`;
    chip.addEventListener("click", () => jumpToFrame(br.in_frame));
    const x = document.createElement("button");
    x.className = "bad-chip-x";
    x.type = "button";
    x.textContent = "×";
    x.title = "Delete bad-range";
    x.addEventListener("click", (e) => { e.stopPropagation(); deleteBadRange(br.id); });
    chip.appendChild(x);
    strip.appendChild(chip);
  }
  if (state.pendingBadIn != null) {
    const pending = document.createElement("div");
    pending.className = "bad-chip";
    pending.style.borderStyle = "dashed";
    pending.textContent = `pending in=${state.pendingBadIn} (Bad Out to close)`;
    strip.appendChild(pending);
  }
}

async function markBadIn() {
  if (!state.current) return;
  state.pendingBadIn = currentFrame();
  renderBadRangesStrip();
  updateMarkers();
  updateStatus();
}

async function markBadOut() {
  if (!state.current) return;
  if (state.pendingBadIn == null) {
    showError("no pending Bad In — press Bad In first to start a range");
    return;
  }
  const f = currentFrame();
  let inF = state.pendingBadIn;
  let outF = f;
  if (outF < inF) { const tmp = inF; inF = outF; outF = tmp; }
  const capturedSlug = state.current;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/review/bad/add`,
      { in_frame: inF, out_frame: outF });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      showError(`bad-range add failed: HTTP ${r.status} ${text}`);
      return;
    }
    const j = await r.json();
    if (state.current !== capturedSlug) return;
    const it = currentItem();
    if (it) {
      const rv = it.review || (it.review = { approved: false, approved_at: null, bad_ranges: [] });
      rv.bad_ranges = rv.bad_ranges || [];
      rv.bad_ranges.push(j.entry);
      rv.bad_ranges.sort((a, b) => a.in_frame - b.in_frame);
      // Server clears approval on add — mirror locally.
      rv.approved = false;
      rv.approved_at = null;
    }
    state.pendingBadIn = null;
    renderBadRangesStrip();
    renderApproveCheckbox();
    renderSidebar();
    updateMarkers();
    updateStatus();
  } catch (e) { showError(`bad-range add failed: ${e}`); }
}

async function deleteBadRange(brId) {
  const capturedSlug = state.current;
  if (!capturedSlug) return;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/review/bad/delete`,
      { id: brId });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      showError(`bad-range delete failed: HTTP ${r.status} ${text}`);
      return;
    }
    if (state.current !== capturedSlug) return;
    const it = currentItem();
    if (it && it.review && Array.isArray(it.review.bad_ranges)) {
      it.review.bad_ranges = it.review.bad_ranges.filter(r => r.id !== brId);
    }
    renderBadRangesStrip();
    renderSidebar();
    updateMarkers();
  } catch (e) { showError(`bad-range delete failed: ${e}`); }
}

async function setApproved(approved) {
  const capturedSlug = state.current;
  if (!capturedSlug) return;
  try {
    const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/review/approve`,
      { approved: !!approved });
    if (!r.ok) {
      const text = await r.text().catch(() => "");
      showError(`approve toggle failed: HTTP ${r.status} ${text}`);
      // Revert checkbox on failure.
      renderApproveCheckbox();
      return;
    }
    const j = await r.json();
    if (state.current !== capturedSlug) return;
    const it = currentItem();
    if (it) it.review = j.review;
    renderApproveCheckbox();
    renderSidebar();
  } catch (e) {
    showError(`approve toggle failed: ${e}`);
    renderApproveCheckbox();
  }
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
  frameImg: document.getElementById("frame-img"),
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
  btnBadIn: document.getElementById("btn-bad-in"),
  btnBadOut: document.getElementById("btn-bad-out"),
  chkApprove: document.getElementById("chk-approve"),
  approveLabel: document.getElementById("approve-toggle"),
  badRangesStrip: document.getElementById("bad-ranges-strip"),
  playRate: document.getElementById("play-rate"),
};

el.playRate.addEventListener("change", () => {
  const r = parseFloat(el.playRate.value);
  if (Number.isFinite(r) && r > 0) el.video.playbackRate = r;
});

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
  const seg = contextSegment();
  const runningSeg = segWithRunningProp();
  const dispSeg = runningSeg || seg;
  const inF = seg ? seg.in_frame : null;
  const outF = seg ? seg.out_frame : null;
  const seedF = seg ? seg.seed_frame : null;
  const seedP = seg ? seg.seed_point : null;
  const pStat = dispSeg ? dispSeg.propagate_status : "idle";
  const segLabel = dispSeg ? dispSeg.id.slice(4) : "—";
  const pt = seedP ? `(${seedP[0]},${seedP[1]})` : "-";
  const fStr = String(f).padStart(4, "0");
  let statusTag = pStat;
  let computing = false;
  if (state.seedComputingSeg) {
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
      // Per-running-seg count, not the union — accurate %/ETA.
      const runningMap = state.propMaskUrlsBySeg.get(dispSeg ? dispSeg.id : null);
      const done = runningMap ? runningMap.size : 0;
      const elapsedS = state.propStartMs ? (performance.now() - state.propStartMs) / 1000 : 0;
      const fps = elapsedS > 0 ? (done / elapsedS).toFixed(2) : "?";
      const etaS = (fps !== "?" && fps > 0) ? Math.max(0, (total - done) / parseFloat(fps)).toFixed(0) : "?";
      const pct = ((done / total) * 100).toFixed(1);
      statusTag = `propagate: ${done}/${total} (${pct}%) @ ${fps} fps, ETA ${etaS}s`;
    } else {
      statusTag = `propagate: ${phase}`;
    }
    computing = true;
  } else if (pStat === "done") {
    const dispMap = state.propMaskUrlsBySeg.get(dispSeg ? dispSeg.id : null);
    const done = dispMap ? dispMap.size : 0;
    const total = (dispSeg && dispSeg.in_frame != null && dispSeg.out_frame != null)
      ? (dispSeg.out_frame - dispSeg.in_frame + 1) : done;
    const tail = state.propPhaseElapsed > 0 ? `in ${state.propPhaseElapsed}s` : "(cached on disk)";
    statusTag = `propagate done: ${done}/${total} frames ${tail}`;
  }
  if (computing) el.statusbar.classList.add("computing");
  else el.statusbar.classList.remove("computing");
  const pendingHint = state.pendingSeedClick ? " (click to set seed point)" : "";
  el.status.textContent =
    `f=${fStr} t=${t.toFixed(3)}s | seg=${segLabel} in=${fmt(inF)} out=${fmt(outF)} seed=${fmt(seedF)} pt=${pt} | status=${statusTag}${pendingHint}`;
}

function updateMarkers() {
  // All segs render with equal weight — no active/dim distinction.
  const layer = el.markerLayer;
  layer.innerHTML = "";
  const it = currentItem();
  if (!it || state.totalFrames <= 0) return;
  const denom = Math.max(1, state.totalFrames - 1);
  for (const seg of (it.segments || [])) {
    if (seg.in_frame != null && seg.out_frame != null) {
      const span = document.createElement("div");
      span.className = "seg-span";
      span.style.left = `${(seg.in_frame / denom) * 100}%`;
      span.style.width = `${((seg.out_frame - seg.in_frame) / denom) * 100}%`;
      span.title = `${seg.id} [${seg.in_frame}–${seg.out_frame}]`;
      layer.appendChild(span);
    }
    const mk = (frame, kind) => {
      if (frame == null) return;
      const m = document.createElement("div");
      m.className = `seg-marker seg-marker-${kind}`;
      m.style.left = `${(frame / denom) * 100}%`;
      if (kind === "seed") m.textContent = "★";
      layer.appendChild(m);
    };
    mk(seg.in_frame, "in");
    mk(seg.out_frame, "out");
    mk(seg.seed_frame, "seed");
  }
  // Bad-range overlay: red strips on top of seg-spans (drawn after so they
  // visually dominate). Frames range from in to out inclusive.
  const rv = reviewOf(it);
  for (const br of rv.bad_ranges) {
    if (br.in_frame == null || br.out_frame == null) continue;
    const span = document.createElement("div");
    span.className = "bad-span";
    span.style.left = `${(br.in_frame / denom) * 100}%`;
    const w = Math.max(((br.out_frame - br.in_frame) / denom) * 100, 0.3);
    span.style.width = `${w}%`;
    span.title = `${br.id} [${br.in_frame}-${br.out_frame}]`;
    layer.appendChild(span);
  }
  if (state.pendingBadIn != null) {
    const m = document.createElement("div");
    m.className = "bad-pending";
    m.style.left = `${(state.pendingBadIn / denom) * 100}%`;
    m.title = `Bad In pending @ ${state.pendingBadIn}`;
    layer.appendChild(m);
  }
}

function isSegReadyForPropagate(seg) {
  return seg != null
    && seg.in_frame != null && seg.out_frame != null
    && seg.seed_frame != null && seg.seed_point != null
    && (state.seedMaskUrls.has(seg.id) || (state.propMaskUrlsBySeg.get(seg.id) || new Map()).has(seg.seed_frame))
    && seg.propagate_status !== "running";
}

function pickPropagateTarget() {
  // Prefer seg containing current frame (user just seeded it), else any ready
  // seg. Returns null if none ready.
  const ctx = contextSegment();
  if (isSegReadyForPropagate(ctx)) return ctx;
  const it = currentItem();
  if (!it) return null;
  for (const seg of (it.segments || [])) {
    if (isSegReadyForPropagate(seg)) return seg;
  }
  return null;
}

function updatePropagateBtn() {
  el.btnPropagate.disabled = pickPropagateTarget() == null;
}

function renderSegmentsStrip() {
  // Chips are status indicators + delete handles. They're not selectors —
  // there is no active seg to select. Click on chip jumps to seed_frame for
  // convenience (so the user can scrub to the seg without manual hunting).
  const strip = el.segmentsStrip;
  strip.innerHTML = "";
  const it = currentItem();
  if (!it) return;
  for (const seg of (it.segments || [])) {
    const chip = document.createElement("div");
    chip.className = `seg-chip seg-chip-${seg.propagate_status}`;
    chip.title = `${seg.id} (${seg.propagate_status}) — click to jump to seed frame`;
    const dot = document.createElement("span");
    dot.className = "seg-chip-dot";
    chip.appendChild(dot);
    chip.appendChild(document.createTextNode(seg.id.replace("seg_", "")));
    chip.addEventListener("click", () => {
      if (seg.seed_frame != null) jumpToFrame(seg.seed_frame);
      else if (seg.in_frame != null) jumpToFrame(seg.in_frame);
    });
    // Rerun: clear propagation results, keep seed → user can re-Propagate
    // with the same seed click. Only shown when there's a seed to preserve.
    if (seg.seed_frame != null) {
      const rr = document.createElement("button");
      rr.className = "seg-chip-rerun";
      rr.type = "button";
      rr.textContent = "↻";
      rr.title = "Clear propagation results (keep seed)";
      rr.addEventListener("click", (e) => { e.stopPropagation(); clearSegmentResults(seg.id); });
      chip.appendChild(rr);
    }
    const x = document.createElement("button");
    x.className = "seg-chip-x";
    x.type = "button";
    x.textContent = "×";
    x.title = "Delete segment";
    x.addEventListener("click", (e) => { e.stopPropagation(); deleteSegment(seg.id); });
    chip.appendChild(x);
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

function clearAllBitmaps() {
  for (const bm of state.bitmapCache.values()) {
    try { bm.close(); } catch (_) {}
  }
  state.bitmapCache.clear();
}

function clearDoneFills() {
  state.doneFrames.clear();
  el.fills.innerHTML = "";
  clearPropMasks();
  clearAllBitmaps();
  state.prefetchAbort++;
}

function videoDisplayRect() {
  // Returns the actual displayed video frame rect inside the wrap, accounting
  // for letterbox/pillarbox. In scrub mode the proxy <img> is what's painted;
  // in play mode the <video> element shows. Both are object-fit: contain
  // inside the same wrap with the source's intrinsic aspect, so a single
  // rect calc works for either.
  let w = state.sourceWidth, h = state.sourceHeight;
  if ((!w || !h) && el.video.videoWidth && el.video.videoHeight) {
    w = el.video.videoWidth; h = el.video.videoHeight;
  }
  if (!w || !h) return null;
  const refRect = state.scrubMode
    ? el.frameImg.getBoundingClientRect()
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
  const w = state.sourceWidth || el.video.videoWidth;
  const h = state.sourceHeight || el.video.videoHeight;
  if (!w || !h) return;
  const disp = videoDisplayRect();
  if (!disp) return;
  if (el.overlay.width !== w || el.overlay.height !== h) {
    // Overlay size changes only on item switch (different video resolution).
    // Bitmaps from the previous item are stale; close + drop.
    clearAllBitmaps();
  }
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

function proxyUrl(slug, frame) {
  return `${API_BASE}/proxy/${encodeURIComponent(slug)}/${String(frame).padStart(5, "0")}.jpg`;
}

function setFrameImage(slug, frame) {
  // Browser image decoder + image cache handle the heavy lifting. Setting
  // .src is synchronous-ish: the browser kicks off async decode and paints
  // when ready. On warm cache (any frame seen this session) it's a single-
  // frame swap with zero JS cost.
  const url = proxyUrl(slug, frame);
  if (el.frameImg.src.endsWith(url) || el.frameImg.src === url) return;
  el.frameImg.src = url;
}

// Fetch mask URL → ImageBitmap (alpha-channel PNG decoded by browser, off
// main thread via createImageBitmap). Stores in bitmapCache, LRU evicts
// far-from-current entries when over limit. Idempotent across concurrent
// callers — second loader closes its bitmap and reuses the cached one.
async function loadMaskBitmap(frame, url) {
  if (state.bitmapCache.has(frame)) return state.bitmapCache.get(frame);
  let bm;
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const blob = await r.blob();
    bm = await createImageBitmap(blob);
  } catch (_) {
    return null;
  }
  if (state.bitmapCache.has(frame)) {
    try { bm.close(); } catch (_) {}
    return state.bitmapCache.get(frame);
  }
  state.bitmapCache.set(frame, bm);
  evictBitmapsBeyondLimit();
  return bm;
}

function evictBitmapsBeyondLimit() {
  if (state.bitmapCache.size <= BITMAP_CACHE_LIMIT) return;
  const cur = currentFrame();
  // Evict the frames furthest from the current view first.
  const sorted = [...state.bitmapCache.keys()]
    .sort((a, b) => Math.abs(b - cur) - Math.abs(a - cur));
  const drop = state.bitmapCache.size - BITMAP_CACHE_LIMIT;
  for (let i = 0; i < drop; i++) {
    const f = sorted[i];
    const bm = state.bitmapCache.get(f);
    if (bm) { try { bm.close(); } catch (_) {} }
    state.bitmapCache.delete(f);
  }
}

// Draw the red × crosshair for every seg whose seed_frame === frame. With
// non-overlapping segs there can still be more than one seg pinned at the
// same frame (e.g. user reseeded then created a new seg with overlapping
// in/out — invariant is best-effort, not strict). All visible.
function drawAllSeedCrosshairs(frame) {
  if (!state.showSeedMarker) return;
  const it = currentItem();
  if (!it) return;
  for (const seg of (it.segments || [])) {
    if (seg.seed_frame === frame && seg.seed_point) {
      drawClickMarker(seg.seed_point[0], seg.seed_point[1]);
    }
  }
}

// Paint pipeline (proxy strip era): swap <img>.src for the frame, then blit
// the mask overlay. Frame swap is browser-native (async decode + composite,
// off main thread); mask overlay is GPU canvas. Token-guarded so a fast
// scrub never blits a stale mask onto the wrong frame.
async function scheduleScrubPaint(frame) {
  const token = ++state.scrubPaintToken;
  const slug = state.current;
  if (!slug) return;
  setFrameImage(slug, frame);
  const maskUrl = maskUrlForFrame(frame);
  if (maskUrl && !state.bitmapCache.has(frame)) {
    await loadMaskBitmap(frame, maskUrl);
    if (token !== state.scrubPaintToken) return;
  }
  if (maskUrlForFrame(frame) != null && blitCachedMask(frame)) {
    // mask blitted; crosshair drawn inside blitCachedMask
  } else {
    clearOverlay();
    drawAllSeedCrosshairs(frame);
  }
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

function setMaskColor(name) {
  if (!MASK_COLORS[name] || state.maskColor === name) return;
  state.maskColor = name;
  localStorage.setItem("labMaskColor", name);
  // Bitmaps carry only alpha — color is applied at blit via fillStyle.
  // Switching color is a single repaint, no cache invalidation.
  repaintOverlayForCurrentFrame();
  updateColorSwatchActive();
}

function updateColorSwatchActive() {
  document.querySelectorAll("#mask-color-swatches .swatch").forEach(s => {
    s.classList.toggle("active", s.dataset.color === state.maskColor);
  });
}

function blitCachedMask(frame) {
  const bm = state.bitmapCache.get(frame);
  if (!bm) return false;
  const ctx = el.overlay.getContext("2d");
  const w = el.overlay.width, h = el.overlay.height;
  const [r, g, b] = MASK_COLORS[state.maskColor] || MASK_COLORS.green;
  ctx.clearRect(0, 0, w, h);

  // Outline-only render: erode the mask (AND of 4 cardinal-shifted copies) into
  // an offscreen canvas, then subtract it from the filled mask to leave a ring.
  // Ball is small + dark; full fill hides the object so the operator can't tell
  // the mask is mis-attached.
  const off = state.outlineCanvas || (state.outlineCanvas = document.createElement("canvas"));
  if (off.width !== w || off.height !== h) { off.width = w; off.height = h; }
  const octx = off.getContext("2d");
  octx.globalCompositeOperation = "source-over";
  octx.clearRect(0, 0, w, h);
  octx.drawImage(bm, 0, 0, w, h);
  octx.globalCompositeOperation = "destination-in";
  const k = MASK_OUTLINE_PX;
  octx.drawImage(bm, -k,  0, w, h);
  octx.drawImage(bm,  k,  0, w, h);
  octx.drawImage(bm,  0, -k, w, h);
  octx.drawImage(bm,  0,  k, w, h);
  octx.globalCompositeOperation = "source-over";

  ctx.fillStyle = `rgba(${r},${g},${b},1.0)`;
  ctx.fillRect(0, 0, w, h);
  ctx.globalCompositeOperation = "destination-in";
  ctx.drawImage(bm, 0, 0, w, h);
  ctx.globalCompositeOperation = "destination-out";
  ctx.drawImage(off, 0, 0, w, h);
  ctx.globalCompositeOperation = "source-over";
  drawAllSeedCrosshairs(frame);
  return true;
}

function maskUrlForFrame(frame) {
  const hit = findMaskAtFrame(frame);
  return hit ? hit.url : null;
}

function loadMaskForFrame(frame) {
  const url = maskUrlForFrame(frame);
  if (!url) { clearOverlay(); return; }
  if (blitCachedMask(frame)) return;
  // Frame-guard the blit: by the time the bitmap resolves, the user may have
  // scrubbed away. Without the guard, an old frame's mask would paint over
  // the current overlay and visibly flicker during fast drag.
  loadMaskBitmap(frame, url).then((bm) => {
    if (!bm) { if (currentFrame() === frame) clearOverlay(); return; }
    if (currentFrame() === frame) blitCachedMask(frame);
  });
}

// Single source of truth for "redraw the overlay for the current frame given
// current active segment + state". Call this from any state-change site that
// doesn't naturally trigger a frame change (chip activation, rehydrate after
// reload, SSE mask delivery on the visible frame, optimistic seek snap).
// loadMaskForFrame's blitCachedMask already handles the seed-frame crosshair
// fallback when a mask exists; the else branch here covers no-mask + crosshair.
function repaintOverlayForCurrentFrame() {
  const f = currentFrame();
  if (maskUrlForFrame(f) != null) {
    loadMaskForFrame(f);
    return;
  }
  clearOverlay();
  drawAllSeedCrosshairs(f);
}

async function prefetchMasks(slug) {
  const myToken = ++state.prefetchAbort;
  // Defer until overlay matches the source-pixel backing-store size — blits
  // built before resizeOverlay would land on a 300×150 default canvas and
  // paint as a top-left patch on the real overlay.
  const targetW = state.sourceWidth;
  if (!targetW || el.overlay.width !== targetW) {
    setTimeout(() => prefetchMasks(slug), 50);
    return;
  }
  const allEntries = [];
  for (const m of state.propMaskUrlsBySeg.values()) {
    for (const [frame, url] of m) allEntries.push([frame, url]);
  }
  const entries = allEntries
    .sort((a, b) => a[0] - b[0])
    .filter(([frame]) => !state.bitmapCache.has(frame));
  // createImageBitmap decodes off-thread, so concurrency mostly buys network
  // pipelining — 4 is enough on LAN; more just thrashes the LRU.
  const concurrency = 4;
  let cursor = 0;
  const worker = async () => {
    while (cursor < entries.length) {
      if (state.current !== slug || myToken !== state.prefetchAbort) return;
      const [frame, url] = entries[cursor++];
      await loadMaskBitmap(frame, url);
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
  if (!item.width || !item.height) {
    showError(`item ${item.slug}: width/height missing — run migrate_extract_proxies.py`);
    return false;
  }
  state.fps = item.fps;
  state.totalFrames = item.total_frames || 0;
  state.sourceWidth = item.width;
  state.sourceHeight = item.height;
  el.itemSlug.textContent = item.slug;
  updateSidebarActive();
  el.scrubber.max = String(Math.max(0, state.totalFrames - 1));
  el.scrubber.value = "0";
  // Lazy: <video preload="metadata"> only fetches enough to expose duration.
  // No frame decode happens until togglePlay() lands on this clip.
  el.video.src = `${API_BASE}/clip/${item.slug}.mp4`;
  el.video.playbackRate = parseFloat(el.playRate.value) || 1;
  state.lastDisplayedFrame = 0;
  enterScrubMode();
  state.scrubPaintToken++;
  clearSeedMask();
  clearPropMasks();
  clearDoneFills();
  clearOverlay();
  state.ptsTable = null;
  state.pendingTargetFrame = null;
  state.isSeeking = false;
  state.lastSeekTarget = null;
  state.pendingSeedClick = null;
  state.seedComputingSeg = null;
  state.seedComputeStartMs = null;
  state.propPhase = null;
  state.propDoneCount = 0;
  state.propExpected = 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = null;
  if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
  state.pendingBadIn = null;
  resizeOverlay();
  renderSegmentsStrip();
  renderBadRangesStrip();
  renderApproveCheckbox();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  startSse();
  fetchPts(item.slug);
  rehydrateMasks(item.slug);
  // Start at frame 0 immediately. fetchPts → may auto-jump to first seg's
  // seed_frame once the dense pts list arrives (handled in fetchPts).
  scheduleScrubPaint(0);
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
    const rv = reviewOf(it);
    let cls = "item-card";
    if (it.slug === state.current) cls += " active";
    if (rv.approved) cls += " approved";
    card.className = cls;
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
    if (rv.approved) {
      const badge = document.createElement("span");
      badge.className = "item-card-approved";
      badge.textContent = "✓";
      badge.title = `approved${rv.approved_at ? " " + rv.approved_at : ""}`;
      nameDiv.appendChild(badge);
    }
    nameDiv.appendChild(document.createTextNode(it.slug));
    if (rv.bad_ranges && rv.bad_ranges.length > 0) {
      const flag = document.createElement("span");
      flag.className = "item-card-bad-count";
      flag.textContent = `⚑${rv.bad_ranges.length}`;
      flag.title = `${rv.bad_ranges.length} bad-range(s) flagged`;
      nameDiv.appendChild(flag);
    }
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
    state.sourceWidth = 0; state.sourceHeight = 0;
    el.frameImg.removeAttribute("src");
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
    // No active gate — every seg's mask paints. doneFills is union across segs.
    if (!state.doneFrames.has(frame)) addDoneFill(frame);
    state.propDoneCount += 1;
    if (el.overlay.width && el.overlay.height) {
      loadMaskBitmap(frame, maskUrl).then(() => {
        if (state.current === capturedSlug && currentFrame() === frame) blitCachedMask(frame);
      });
    } else if (frame === currentFrame()) {
      repaintOverlayForCurrentFrame();
    }
    updateStatus();
  });
  es.addEventListener("phase", (ev) => {
    if (state.current !== capturedSlug) return;
    let payload;
    try { payload = JSON.parse(ev.data); } catch (_) { return; }
    // Show phase for any running seg — there's only one running at a time
    // per slug since SAM2 propagator is serial.
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
    if (state.current === capturedSlug) {
      state.propPhase = "done";
      if (typeof payload.elapsed_s === "number") state.propPhaseElapsed = payload.elapsed_s;
      if (state.propTickHandle) { clearInterval(state.propTickHandle); state.propTickHandle = null; }
      updatePropagateBtn();
      updateStatus();
      prefetchMasks(capturedSlug);
    }
  });
  es.addEventListener("segment_cleared", (ev) => {
    if (state.current !== capturedSlug) return;
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
    const segId = payload.seg_id;
    if (typeof segId !== "string") return;
    applySegmentCleared(segId);
  });
  es.addEventListener("error", (ev) => {
    let payload = {};
    try { payload = JSON.parse(ev.data); } catch (_) {}
    const segId = payload.seg_id;
    if (typeof segId === "string") {
      updateSidebarStatus(capturedSlug, segId, "failed");
    }
    if (state.current === capturedSlug && payload.msg) {
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

// Pick the seg [/]/N edits target. Decision tree (no manual selection):
//   1. Frame is inside an idle complete seg → edit that seg (extends in/out).
//   2. There's an open seg (in xor out) → fill in the missing endpoint.
//   3. Otherwise create a new seg.
// Done/running segs are never picked — overwriting their range silently
// invalidates masks that took ~30s+ to compute.
async function pickInOutTarget(f) {
  const inside = segContainingFrame(f);
  if (inside && inside.propagate_status !== "done" && inside.propagate_status !== "running") {
    return inside;
  }
  const open = openSegment();
  if (open && open.propagate_status !== "done" && open.propagate_status !== "running") {
    return open;
  }
  return await createSegment();
}

async function markIn() {
  if (!state.current) return;
  const f = currentFrame();
  const seg = await pickInOutTarget(f);
  if (!seg) return;
  seg.in_frame = f;
  // Maintain in < out invariant — if user marks in past existing out, drop out.
  if (seg.out_frame != null && seg.out_frame <= seg.in_frame) seg.out_frame = null;
  updateMarkers();
  renderSegmentsStrip();
  updatePropagateBtn();
  updateStatus();
  if (seg.out_frame != null) sendTrim(seg);
}

async function markOut() {
  if (!state.current) return;
  const f = currentFrame();
  const seg = await pickInOutTarget(f);
  if (!seg) return;
  seg.out_frame = f;
  if (seg.in_frame != null && seg.in_frame >= seg.out_frame) seg.in_frame = null;
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
  } catch (e) {
    if (state.current === capturedSlug) showError(`trim failed: ${e}`);
  }
}

async function markSeed() {
  if (!state.current) return;
  const f = currentFrame();
  // Seed disambiguation = the seg whose [in,out] contains f. Self-consistent:
  // user must scrub into a marked range before seeding. No fallback create.
  const seg = segContainingFrame(f);
  if (!seg) {
    showError(`frame ${f} is not inside any segment range — Mark In/Out first`);
    return;
  }
  if (seg.propagate_status === "running") {
    showError(`segment ${seg.id} is propagating — Cancel first to reseed`);
    return;
  }
  seg.seed_frame = f;
  seg.seed_point = null;
  state.pendingSeedClick = { segId: seg.id, frame: f };
  clearSeedMask(seg.id);
  clearOverlay();
  drawAllSeedCrosshairs(f);
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
  clearSeedMask(segId);
  clearPropMasks(segId);
  // Rebuild doneFills from the union across remaining segs.
  rebuildDoneFills();
  renderSegmentsStrip();
  renderSidebar();
  updateMarkers();
  updatePropagateBtn();
  updateStatus();
  scheduleScrubPaint(state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0);
}

async function clearSegmentResults(segId) {
  if (!confirm(`Clear propagation results for ${segId}?\n\nSeed will be kept; you can re-Propagate with one click.`)) return;
  const capturedSlug = state.current;
  const r = await postJson(`/api/items/${encodeURIComponent(capturedSlug)}/segments/${segId}/clear`);
  if (!r.ok) { showError(`clear segment failed: HTTP ${r.status}`); return; }
  if (state.current !== capturedSlug) return;
  // SSE `segment_cleared` will also fire and run the same cleanup; idempotent.
  applySegmentCleared(segId);
}

function applySegmentCleared(segId) {
  const it = currentItem();
  if (!it) return;
  const seg = (it.segments || []).find(s => s.id === segId);
  if (!seg) return;
  // Drop bitmap cache + prop URLs for this seg's range. Seed URL/bitmap are
  // re-derived from the surviving on-disk PNG via /mask URL after
  // rebuildDoneFills repopulates state from server-side propMaskUrlsBySeg —
  // but we explicitly preserve the seed entry below.
  const seedFrame = seg.seed_frame;
  const propMap = state.propMaskUrlsBySeg.get(segId);
  if (propMap) {
    for (const [f, url] of propMap.entries()) {
      if (f === seedFrame) continue;
      const bm = state.bitmapCache.get(f);
      if (bm) { try { bm.close(); } catch (_) {} }
      state.bitmapCache.delete(f);
      if (typeof url === "string" && url.startsWith("blob:")) URL.revokeObjectURL(url);
    }
    // Keep only the seed entry in the prop map.
    const surviving = new Map();
    if (seedFrame != null && propMap.has(seedFrame)) {
      surviving.set(seedFrame, propMap.get(seedFrame));
    }
    state.propMaskUrlsBySeg.set(segId, surviving);
  }
  seg.propagate_status = "idle";
  rebuildDoneFills();
  renderSegmentsStrip();
  updatePropagateBtn();
  updateStatus();
  scheduleScrubPaint(state.lastDisplayedFrame >= 0 ? state.lastDisplayedFrame : 0);
}

function rebuildDoneFills() {
  state.doneFrames.clear();
  el.fills.innerHTML = "";
  clearAllBitmaps();
  state.prefetchAbort++;
  for (const m of state.propMaskUrlsBySeg.values()) {
    for (const f of m.keys()) addDoneFill(f);
  }
  state.propDoneCount = state.doneFrames.size;
}

async function sendSeed(segId, frameIndex, x, y) {
  const capturedSlug = state.current;
  const capturedSegId = segId;
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
      // + stale propagate masks). Mirror that purge: drop this seg's prop
      // map + bitmapCache for its frames. Order matters — do destructive
      // cleanup BEFORE creating the new blob URL.
      const oldMap = state.propMaskUrlsBySeg.get(capturedSegId);
      if (oldMap) {
        for (const f of oldMap.keys()) {
          const bm = state.bitmapCache.get(f);
          if (bm) { try { bm.close(); } catch (_) {} }
          state.bitmapCache.delete(f);
        }
      }
      clearPropMasks(capturedSegId);
      const blobUrl = URL.createObjectURL(blob);
      state.seedMaskUrls.set(capturedSegId, blobUrl);
      const segMap = new Map();
      state.propMaskUrlsBySeg.set(capturedSegId, segMap);
      segMap.set(frameIndex, blobUrl);
      // Rebuild doneFills since this seg's old masks just got nuked.
      rebuildDoneFills();
      if (currentFrame() === frameIndex) {
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
  const seg = pickPropagateTarget();
  if (!seg) return;
  const capturedSegId = seg.id;
  seg.propagate_status = "running";
  updateSidebarStatus(capturedSlug, capturedSegId, "running");
  state.propPhase = "starting";
  state.propExpected = (seg.out_frame - seg.in_frame + 1) || 0;
  state.propPhaseElapsed = 0;
  state.propStartMs = performance.now();
  // Wipe THIS segment's prior masks so a re-propagate doesn't show stale data.
  clearPropMasks(capturedSegId);
  rebuildDoneFills();
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
      if (state.current === capturedSlug) {
        showError(`propagate failed: HTTP ${r.status} ${text}`);
        clearInterval(state.propTickHandle);
        state.propTickHandle = null;
        updatePropagateBtn();
        updateStatus();
      }
    }
  } catch (e) {
    updateSidebarStatus(capturedSlug, capturedSegId, "failed");
    if (state.current === capturedSlug) {
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
    state.pendingSeedClick = null;
    updateStatus();
    return;
  }
  const seg = segWithRunningProp();
  if (!seg || !state.current) return;
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
  if (state.current === capturedSlug) {
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
  // Snap lastDisplayedFrame so currentFrame() reads the new frame even before
  // scheduleScrubPaint's async paintAtomic lands.
  state.lastDisplayedFrame = target;
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
      // Reload UX: pts arrives async; if there's a seeded seg and we're still
      // sitting at frame 0, jump to its seed_frame now (proxy <img> swap is
      // ~free). Guard so we never yank a user who already moved.
      const it = currentItem();
      const firstSeeded = it && (it.segments || []).find(s => s.seed_frame != null);
      if (firstSeeded && state.lastDisplayedFrame === 0) {
        jumpToFrame(firstSeeded.seed_frame);
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
    // Render fills for ALL segs (union, since every mask is on screen).
    rebuildDoneFills();
    // propExpected: show the contextual seg's range total (status bar uses it).
    const ctx = contextSegment();
    if (ctx && ctx.in_frame != null && ctx.out_frame != null) {
      state.propExpected = ctx.out_frame - ctx.in_frame + 1;
    }
    repaintOverlayForCurrentFrame();
    // Re-evaluate Propagate button: isSegReadyForPropagate needs the seed
    // frame's URL to be in propMaskUrlsBySeg, which we just populated. Without
    // this, the button stays disabled from the syncFromItem() call that fired
    // before this fetch resolved.
    updatePropagateBtn();
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
  const pending = state.pendingSeedClick;
  if (!pending) { togglePlay(); return; }
  const it = currentItem();
  const seg = it && (it.segments || []).find(s => s.id === pending.segId);
  if (!seg || seg.seed_frame == null) {
    state.pendingSeedClick = null;
    updateStatus();
    return;
  }
  const nativeW = state.sourceWidth || el.video.videoWidth;
  const nativeH = state.sourceHeight || el.video.videoHeight;
  if (!nativeW || !nativeH) { showError("source dims not loaded"); return; }
  const disp = videoDisplayRect();
  if (!disp) { showError("video display rect unresolved"); return; }
  if (e.clientX < disp.left || e.clientX > disp.left + disp.width ||
      e.clientY < disp.top || e.clientY > disp.top + disp.height) {
    console.warn("click outside video frame area, ignored");
    return;
  }
  const x = Math.round((e.clientX - disp.left) * (nativeW / disp.width));
  const y = Math.round((e.clientY - disp.top) * (nativeH / disp.height));
  console.log("seed click", { seg: seg.id, client: [e.clientX, e.clientY], disp, native: [x, y] });
  seg.seed_point = [x, y];
  state.pendingSeedClick = null;
  updateStatus();
  sendSeed(seg.id, seg.seed_frame, x, y);
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
    const seg = contextSegment();
    jumpToFrame(seg && seg.in_frame != null ? seg.in_frame : 0);
  } else if (e.key === "End") {
    e.preventDefault();
    const seg = contextSegment();
    jumpToFrame(seg && seg.out_frame != null ? seg.out_frame : state.totalFrames - 1);
  } else if (e.key === "[") {
    if (e.altKey) markBadIn(); else markIn();
  } else if (e.key === "]") {
    if (e.altKey) markBadOut(); else markOut();
  }
  else if (e.key === "s" || e.key === "S") markSeed();
  else if (e.key === "n" || e.key === "N") createSegment();
  else if (e.key === "Enter") propagate();
  else if (e.key === "Escape") cancelOrEscape();
  else if (e.key === "h" || e.key === "H") toggleSeedMarker();
}

function toggleSeedMarker() {
  state.showSeedMarker = !state.showSeedMarker;
  updateSeedMarkerBtn();
  repaintOverlayForCurrentFrame();
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
    // Observe the wrap (the layout container) — its size is what scales
    // both <img> and <video>. The previous observer on el.video missed
    // resizes that happened while in scrub mode (video element hidden).
    new ResizeObserver(() => resizeOverlay()).observe(el.videoWrap);
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
  if (el.btnBadIn) el.btnBadIn.addEventListener("click", markBadIn);
  if (el.btnBadOut) el.btnBadOut.addEventListener("click", markBadOut);
  if (el.chkApprove) el.chkApprove.addEventListener("change", () => setApproved(el.chkApprove.checked));

  document.querySelectorAll("#mask-color-swatches .swatch").forEach(s => {
    s.addEventListener("click", () => setMaskColor(s.dataset.color));
  });
  updateColorSwatchActive();

  document.addEventListener("keydown", onKeydown);
}

bindUi();
bootstrap();
