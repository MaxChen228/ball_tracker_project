// Shared fit-curve primitives for dashboard + viewer 3D scenes.
// Both surfaces render parabolic SegmentRecord fits as Three.js fat
// lines (`Line2 + LineMaterial`) so the operator can dial in line width
// without being capped by `LineBasicMaterial.linewidth = 1` on every
// browser. Centralising the sampling, hover-tooltip raycaster, and
// dashed-extension logic here so dashboard_layers.js + viewer_layers.js
// don't drift.
//
// SegmentRecord wire shape (server/schemas.py):
//   { p0:[x,y,z], v0:[vx,vy,vz], t_anchor, t_start, t_end, ... }
// Closed-form trajectory: p(τ) = p0 + v0·τ + ½·g·τ², g = (0, 0, -9.81).
// `instantaneousSpeedKph(seg, t)` therefore needs no fit-poly inversion.

import * as THREE from "three";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";

const G_Z = -9.81;

// Sampling resolution for the core segment polyline. 80 points across a
// typical ~0.5 s flight = ~6 ms per sample, well below visual detection
// threshold even at high zoom. Hover τ-from-faceIndex inversion error
// scales as (t_end - t_start) / (n - 1).
export const FIT_CORE_SAMPLES = 80;

// Default LineMaterial.linewidth (screen-space px). Wide enough that
// active-segment highlight (×1.6) reads as a step, narrow enough that
// 8 overlapping segments don't merge.
export const FIT_LINE_WIDTH_PX_DEFAULT = 2.0;
export const FIT_LINE_WIDTH_PX_MIN = 1.0;
export const FIT_LINE_WIDTH_PX_MAX = 8.0;
export const FIT_LINE_WIDTH_PX_STEP = 0.5;
export const FIT_LINE_WIDTH_LS_KEY = "ball_tracker_fit_line_width_px";

// Default dashed-extension padding on each side of a segment, seconds.
export const FIT_EXTENSION_SEC_DEFAULT = 0.10;
export const FIT_EXTENSION_SEC_MIN = 0.0;
export const FIT_EXTENSION_SEC_MAX = 0.5;
export const FIT_EXTENSION_SEC_STEP = 0.02;
export const FIT_EXTENSION_LS_KEY = "ball_tracker_fit_extension_sec";

// Dashed extension cosmetic constants. dashSize/gapSize are in world
// metres because LineMaterial.dashScale defaults to 1 and we run with
// world-space distances on the geometry; ~0.04/0.03 reads as a fine
// dotted line at the working camera distance.
const FIT_EXT_OPACITY = 0.65;
const FIT_EXT_WIDTH_RATIO = 0.7;
const FIT_EXT_DASH_SIZE = 0.04;
const FIT_EXT_GAP_SIZE = 0.03;

// Hover tooltip raycast threshold (screen-space px around the hit ray).
const FIT_HOVER_PX_THRESHOLD = 8;

// Inverse-trajectory: instantaneous speed in km/h at world-time `t`.
// Uses the closed-form derivative of the SegmentRecord parabola — no
// numerical differentiation, no sample lookup.
export function instantaneousSpeedKph(seg, t) {
  const tau = t - seg.t_anchor;
  const vx = seg.v0[0];
  const vy = seg.v0[1];
  const vz = seg.v0[2] + G_Z * tau;
  return Math.hypot(vx, vy, vz) * 3.6;
}

// Sample positions for one stretch of the parabola. Returns a flat
// Float32Array of length 3*n. `tA` / `tB` are absolute times (i.e. the
// caller has already added pre/post pad to t_start/t_end as needed).
function sampleParabola(seg, tA, tB, n) {
  const out = new Float32Array(n * 3);
  const ta = seg.t_anchor;
  const p0 = seg.p0, v0 = seg.v0;
  for (let i = 0; i < n; ++i) {
    const t = tA + (tB - tA) * (i / (n - 1));
    const tau = t - ta;
    out[i * 3 + 0] = p0[0] + v0[0] * tau;
    out[i * 3 + 1] = p0[1] + v0[1] * tau;
    out[i * 3 + 2] = p0[2] + v0[2] * tau + 0.5 * G_Z * tau * tau;
  }
  return out;
}

// Convert a flat XYZ Float32Array into the [x0,y0,z0,x1,y1,z1,...] form
// LineGeometry expects (it's the same memory layout, but
// `setPositions` actually wants a plain Array or Float32Array of 3*N
// floats — the format matches, so we pass through).
function _fatLine(positions, color, opts) {
  const geom = new LineGeometry();
  geom.setPositions(positions);
  const material = new LineMaterial({
    color: new THREE.Color(color).getHex(),
    linewidth: opts.linewidth ?? FIT_LINE_WIDTH_PX_DEFAULT,
    transparent: opts.opacity != null && opts.opacity < 1.0,
    opacity: opts.opacity ?? 1.0,
    dashed: !!opts.dashed,
    dashSize: opts.dashSize ?? FIT_EXT_DASH_SIZE,
    gapSize: opts.gapSize ?? FIT_EXT_GAP_SIZE,
    depthWrite: opts.depthWrite ?? true,
  });
  // Resolution must be set or the linewidth uniform stays default-zero
  // (visible width = 1 px regardless of slider). Caller passes a getter
  // so we can re-read on resize via applyResolution(group, vec2).
  if (opts.resolution) {
    material.resolution.copy(opts.resolution);
  }
  const line = new Line2(geom, material);
  if (opts.dashed) line.computeLineDistances();
  // Line2 uses InstancedInterleavedBuffer; raycasting needs the world
  // matrix to be current. updateMatrix is no-op until the line is added
  // to a scene, so leave to caller.
  return line;
}

// Build all fit-segment lines (core + optional pre/post dashed
// extensions) into a freshly-named Group. Caller addLayer's it.
//
// `opts`:
//   groupName (string)              required; addLayer key
//   palette ((segIdx) => colorHex)  required
//   sampleCount (int = 80)
//   prePadSec / postPadSec (float)  zero ⇒ skip dashed extensions
//   lineWidthPx (float)
//   extensionWidthScale (float)     core_width × this = dashed width
//   activeHighlight (bool)          if true, caller will mutate linewidth
//                                   per active-segment via applyActiveHighlight
//   resolution (THREE.Vector2)      LineMaterial resolution at build time
//   onLineCreated (line, segIdx, kind) => void (optional)
//
// Each line gets userData:
//   { segIdx, kind: 'core'|'prePad'|'postPad', tA, tB }
// — `kind`/`tA`/`tB` let the hover code reverse-map a faceIndex to
// absolute time without re-sampling.
export function buildFitSegmentLines(segments, opts) {
  if (!opts || typeof opts.groupName !== "string") {
    throw new Error("buildFitSegmentLines: opts.groupName required");
  }
  if (typeof opts.palette !== "function") {
    throw new Error("buildFitSegmentLines: opts.palette required");
  }
  const group = new THREE.Group();
  group.name = opts.groupName;
  const n = opts.sampleCount ?? FIT_CORE_SAMPLES;
  const pre = Math.max(0, opts.prePadSec ?? 0);
  const post = Math.max(0, opts.postPadSec ?? 0);
  const widthCore = opts.lineWidthPx ?? FIT_LINE_WIDTH_PX_DEFAULT;
  const widthExt = widthCore * (opts.extensionWidthScale ?? FIT_EXT_WIDTH_RATIO);
  const resolution = opts.resolution || new THREE.Vector2(1, 1);

  for (let i = 0; i < (segments || []).length; ++i) {
    const seg = segments[i];
    const color = opts.palette(i);
    // Core segment.
    const corePos = sampleParabola(seg, seg.t_start, seg.t_end, n);
    const core = _fatLine(corePos, color, {
      linewidth: widthCore,
      opacity: 1.0,
      resolution,
    });
    core.userData = { segIdx: i, kind: "core", tA: seg.t_start, tB: seg.t_end };
    core.name = `fit_seg_${i}_core`;
    group.add(core);
    if (typeof opts.onLineCreated === "function") {
      opts.onLineCreated(core, i, "core");
    }
    // Pre-pad dashed extension.
    if (pre > 0) {
      const tA = seg.t_start - pre;
      const pos = sampleParabola(seg, tA, seg.t_start, n);
      const line = _fatLine(pos, color, {
        linewidth: widthExt,
        opacity: FIT_EXT_OPACITY,
        dashed: true,
        resolution,
      });
      line.userData = { segIdx: i, kind: "prePad", tA, tB: seg.t_start };
      line.name = `fit_seg_${i}_prePad`;
      group.add(line);
      if (typeof opts.onLineCreated === "function") {
        opts.onLineCreated(line, i, "prePad");
      }
    }
    // Post-pad dashed extension.
    if (post > 0) {
      const tB = seg.t_end + post;
      const pos = sampleParabola(seg, seg.t_end, tB, n);
      const line = _fatLine(pos, color, {
        linewidth: widthExt,
        opacity: FIT_EXT_OPACITY,
        dashed: true,
        resolution,
      });
      line.userData = { segIdx: i, kind: "postPad", tA: seg.t_end, tB };
      line.name = `fit_seg_${i}_postPad`;
      group.add(line);
      if (typeof opts.onLineCreated === "function") {
        opts.onLineCreated(line, i, "postPad");
      }
    }
  }
  return group;
}

// Walk a fit-curves group and refresh every LineMaterial.resolution on
// canvas resize. Without this the linewidth uniform is computed from a
// stale screen size and lines render at the wrong px width after the
// container reflows.
export function applyResolution(group, vec2) {
  if (!group) return;
  group.traverse((node) => {
    const mat = node && node.material;
    if (mat && mat.isLineMaterial) {
      mat.resolution.copy(vec2);
    }
  });
}

// Walk a fit-curves group and set every core line's linewidth (px) and
// every dashed extension's linewidth = px × extScale. Active-segment
// highlight is layered on top by applyActiveHighlight().
export function applyLineWidth(group, widthPx, extScale = FIT_EXT_WIDTH_RATIO) {
  if (!group) return;
  for (const line of group.children) {
    const mat = line.material;
    if (!mat || !mat.isLineMaterial) continue;
    const isCore = (line.userData && line.userData.kind === "core");
    mat.linewidth = isCore ? widthPx : widthPx * extScale;
  }
}

// Active-segment highlight. Plays on top of the base width — call
// applyLineWidth first if base width changed in the same frame. Bumps
// active segment's linewidth ×activeScale and leaves dashed extensions
// alone (they're cosmetic, never "active").
export function applyActiveHighlight(group, segments, t, mode, baseWidthPx, activeScale = 1.6) {
  if (!group) return;
  const playback = mode === "playback";
  for (const line of group.children) {
    const ud = line.userData;
    if (!ud || ud.kind !== "core") continue;
    const seg = segments[ud.segIdx];
    if (!seg) continue;
    const isActive = playback && t >= seg.t_start - 1e-3 && t <= seg.t_end + 1e-3;
    line.material.linewidth = isActive ? baseWidthPx * activeScale : baseWidthPx;
  }
}

// localStorage seed for line-width slider. Clamped to bounds; falls
// through to default on any read error or out-of-band value.
export function readPersistedFitLineWidth() {
  try {
    const raw = localStorage.getItem(FIT_LINE_WIDTH_LS_KEY);
    if (raw == null) return FIT_LINE_WIDTH_PX_DEFAULT;
    const v = Number(raw);
    if (!Number.isFinite(v)) return FIT_LINE_WIDTH_PX_DEFAULT;
    if (v < FIT_LINE_WIDTH_PX_MIN) return FIT_LINE_WIDTH_PX_MIN;
    if (v > FIT_LINE_WIDTH_PX_MAX) return FIT_LINE_WIDTH_PX_MAX;
    return v;
  } catch {
    return FIT_LINE_WIDTH_PX_DEFAULT;
  }
}

export function writePersistedFitLineWidth(v) {
  try { localStorage.setItem(FIT_LINE_WIDTH_LS_KEY, String(v)); }
  catch {}
}

export function readPersistedFitExtensionSeconds() {
  try {
    const raw = localStorage.getItem(FIT_EXTENSION_LS_KEY);
    if (raw == null) return FIT_EXTENSION_SEC_DEFAULT;
    const v = Number(raw);
    if (!Number.isFinite(v)) return FIT_EXTENSION_SEC_DEFAULT;
    if (v < FIT_EXTENSION_SEC_MIN) return FIT_EXTENSION_SEC_MIN;
    if (v > FIT_EXTENSION_SEC_MAX) return FIT_EXTENSION_SEC_MAX;
    return v;
  } catch {
    return FIT_EXTENSION_SEC_DEFAULT;
  }
}

export function writePersistedFitExtensionSeconds(v) {
  try { localStorage.setItem(FIT_EXTENSION_LS_KEY, String(v)); }
  catch {}
}

// Hover tooltip — Raycaster on the Line2 group. Inverts the hit
// faceIndex back to an absolute t along the parabola via the line's
// userData.tA/tB, then evaluates instantaneousSpeedKph(seg, t) to
// display "xxx.x km/h" in a DOM overlay.
//
// Caller responsibilities:
//   - tooltipParent must be `position: relative` (or absolute) so the
//     tooltip's translate() coords map to the correct origin.
//   - segmentsFn is invoked on every move so path switches don't stale.
//   - Returns { dispose() } to detach the listener + remove the DOM.
export function setupFitHoverTooltip({ scene, fitGroupGetter, segmentsFn, tooltipParent }) {
  if (!scene || !scene.renderer || !scene.camera) {
    throw new Error("setupFitHoverTooltip: scene + renderer + camera required");
  }
  if (!tooltipParent) {
    throw new Error("setupFitHoverTooltip: tooltipParent DOM required");
  }
  const tooltip = document.createElement("div");
  tooltip.className = "fit-hover-tooltip";
  tooltip.style.display = "none";
  tooltipParent.appendChild(tooltip);

  const raycaster = new THREE.Raycaster();
  // Line2 raycasting uses the Line2 namespace, NOT params.Line. Without
  // this threshold no hits register at all (default is undefined on
  // params.Line2). Threshold is in screen-space px.
  raycaster.params.Line2 = { threshold: FIT_HOVER_PX_THRESHOLD };
  const ndc = new THREE.Vector2();

  const onMove = (ev) => {
    const fitGroup = fitGroupGetter();
    if (!fitGroup || !fitGroup.visible || !fitGroup.children.length) {
      tooltip.style.display = "none";
      return;
    }
    const rect = scene.renderer.domElement.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const py = ev.clientY - rect.top;
    if (px < 0 || py < 0 || px > rect.width || py > rect.height) {
      tooltip.style.display = "none";
      return;
    }
    ndc.x = (px / rect.width) * 2 - 1;
    ndc.y = -(py / rect.height) * 2 + 1;
    raycaster.setFromCamera(ndc, scene.camera);
    const hits = raycaster.intersectObjects(fitGroup.children, false);
    if (!hits.length) {
      tooltip.style.display = "none";
      return;
    }
    const hit = hits[0];
    const ud = hit.object.userData || {};
    const segments = segmentsFn() || [];
    const seg = segments[ud.segIdx];
    if (!seg) {
      tooltip.style.display = "none";
      return;
    }
    // faceIndex on Line2 = the segment index along the polyline (0..n-2).
    // Recover absolute t by lerping between the line's userData tA/tB.
    // Fallback to point sample count − 1 if faceIndex is missing.
    const sampleSegs = (FIT_CORE_SAMPLES - 1);
    const f = (typeof hit.faceIndex === "number") ? hit.faceIndex : 0;
    const frac = Math.max(0, Math.min(1, f / sampleSegs));
    const tA = (typeof ud.tA === "number") ? ud.tA : seg.t_start;
    const tB = (typeof ud.tB === "number") ? ud.tB : seg.t_end;
    const t = tA + (tB - tA) * frac;
    const kph = instantaneousSpeedKph(seg, t);
    const parentRect = tooltipParent.getBoundingClientRect();
    const tx = ev.clientX - parentRect.left + 12;
    const ty = ev.clientY - parentRect.top - 6;
    tooltip.style.transform = `translate(${tx}px, ${ty}px)`;
    tooltip.textContent = `${kph.toFixed(1)} km/h`;
    tooltip.style.display = "block";
  };

  const onLeave = () => { tooltip.style.display = "none"; };
  const dom = scene.renderer.domElement;
  dom.addEventListener("pointermove", onMove);
  dom.addEventListener("pointerleave", onLeave);
  return {
    dispose() {
      dom.removeEventListener("pointermove", onMove);
      dom.removeEventListener("pointerleave", onLeave);
      if (tooltip.parentNode) tooltip.parentNode.removeChild(tooltip);
    },
  };
}

// Click-to-toggle wiring for the chip-popover scaffolding from
// `layer_chip_with_popover_html()`. One call per page wires every
// popover under `root` (usually `document`).
//
// Behaviour:
//   - Click on `[data-popover-target=...]` toggles its sibling popover.
//   - Click outside any open popover closes it.
//   - Pressing Escape closes the open popover.
//   - At most one popover open at a time (UX consistency with the
//     dropdown-style mini-toolbar this replaces).
export function bindLayerPopovers(root = document) {
  const toggles = root.querySelectorAll("[data-popover-target]");
  if (!toggles.length) return;
  const closeAll = (except = null) => {
    for (const t of toggles) {
      const popId = t.getAttribute("data-popover-target");
      const pop = root.getElementById ? root.getElementById(popId) : document.getElementById(popId);
      if (!pop) continue;
      if (pop === except) continue;
      pop.hidden = true;
      t.setAttribute("aria-expanded", "false");
      t.classList.remove("open");
    }
  };
  for (const toggle of toggles) {
    toggle.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const popId = toggle.getAttribute("data-popover-target");
      const pop = document.getElementById(popId);
      if (!pop) return;
      const willOpen = pop.hidden;
      closeAll(willOpen ? pop : null);
      pop.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", String(willOpen));
      toggle.classList.toggle("open", willOpen);
    });
  }
  document.addEventListener("click", (ev) => {
    // Click inside any popover or any toggle: leave alone.
    if (ev.target.closest("[data-popover]")) return;
    if (ev.target.closest("[data-popover-target]")) return;
    closeAll();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeAll();
  });
}
