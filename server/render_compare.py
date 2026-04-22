"""Shared real/virtual compare helpers for server-rendered pages."""
from __future__ import annotations

import html


LIVE_COMPARE_CSS = """
.preview-panel, .virt-cell {
  position: relative;
  border: 1px solid var(--border-l);
  border-radius: calc(var(--r) + 2px);
  overflow: hidden;
  aspect-ratio: 16 / 9;
}
.preview-panel {
  background: #120F0D;
}
.virt-cell {
  background: #1A1714;
}
.preview-panel img,
.virt-cell canvas,
.preview-panel .preview-overlay {
  display: block;
  width: 100%;
  height: 100%;
}
.preview-panel img {
  position: absolute;
  inset: 0;
  object-fit: cover;
  background: #120F0D;
}
.virt-cell canvas {
  position: absolute;
  inset: 0;
}
.preview-panel .preview-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.preview-overlay polygon {
  fill: rgba(202, 61, 47, 0.08);
  stroke: rgba(202, 61, 47, 0.92);
  stroke-width: 2.4;
  stroke-dasharray: 9 7;
}
.preview-overlay .marker-dot {
  stroke: rgba(26, 23, 20, 0.9);
  stroke-width: 1.5;
}
.preview-overlay .marker-tag {
  stroke: rgba(26, 23, 20, 0.7);
  stroke-width: 1;
  rx: 6;
  ry: 6;
}
.preview-overlay .marker-text {
  fill: #171411;
  font-family: "JetBrains Mono", monospace;
  font-size: 13px;
  font-weight: 700;
  text-anchor: middle;
  dominant-baseline: central;
}
.preview-overlay .is-selected .marker-tag,
.preview-overlay .is-selected .marker-dot {
  stroke: #F8F7F4;
  stroke-width: 1.6;
}
.preview-panel .placeholder,
.virt-cell .placeholder {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  pointer-events: none;
}
.preview-panel .placeholder {
  color: rgba(255, 255, 255, 0.55);
}
.virt-cell .placeholder {
  color: rgba(219, 214, 205, 0.55);
}
.virt-cell.ready .placeholder {
  display: none;
}
.compare-cell-label {
  position: absolute;
  left: 12px;
  top: 12px;
  z-index: 3;
  padding: 5px 9px;
  border-radius: var(--r);
  background: rgba(26, 23, 20, 0.84);
  border: 1px solid rgba(255, 255, 255, 0.12);
  color: #F8F7F4;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.compare-cell-label.real { border-color: rgba(202, 61, 47, 0.32); }
.compare-cell-label.virt { border-color: rgba(219, 214, 205, 0.18); }
"""


PLATE_WORLD_JS = """
const PLATE_WORLD = [
  [-0.216, 0.0,   0.0],
  [ 0.216, 0.0,   0.0],
  [ 0.216, 0.216, 0.0],
  [ 0.0,   0.432, 0.0],
  [-0.216, 0.216, 0.0],
];
"""


PROJECTION_JS = """
function projectWorldToPixel(P, cam) {
  const R = cam.R_wc, t = cam.t_wc;
  if (!R || !t) return null;
  const Xc = R[0]*P[0] + R[1]*P[1] + R[2]*P[2] + t[0];
  const Yc = R[3]*P[0] + R[4]*P[1] + R[5]*P[2] + t[1];
  const Zc = R[6]*P[0] + R[7]*P[1] + R[8]*P[2] + t[2];
  if (Zc <= 0.01) return null;
  const xn = Xc / Zc, yn = Yc / Zc;
  const d = cam.distortion || [0, 0, 0, 0, 0];
  const k1 = d[0] || 0, k2 = d[1] || 0, p1 = d[2] || 0, p2 = d[3] || 0, k3 = d[4] || 0;
  const r2 = xn*xn + yn*yn, r4 = r2*r2, r6 = r4*r2;
  const radial = 1 + k1*r2 + k2*r4 + k3*r6;
  const xd = xn*radial + 2*p1*xn*yn + p2*(r2 + 2*xn*xn);
  const yd = yn*radial + p1*(r2 + 2*yn*yn) + 2*p2*xn*yn;
  return { u: cam.fx * xd + cam.cx, v: cam.fy * yd + cam.cy, z: Zc };
}
"""


DRAW_VIRTUAL_BASE_JS = """
function drawVirtualBase(canvas, cam, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  if (!cssW || !cssH) return null;
  const pxW = Math.max(1, Math.floor(cssW * dpr));
  const pxH = Math.max(1, Math.floor(cssH * dpr));
  if (canvas.width !== pxW || canvas.height !== pxH) {
    canvas.width = pxW;
    canvas.height = pxH;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = opts.background || '#1A1714';
  ctx.fillRect(0, 0, cssW, cssH);
  if (!cam || cam.fx == null || !cam.R_wc || !cam.t_wc || !cam.image_width_px || !cam.image_height_px) {
    return null;
  }
  const sx = cssW / cam.image_width_px;
  const sy = cssH / cam.image_height_px;
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, cssW, cssH);
  ctx.clip();
  const cxPx = cam.cx * sx;
  const cyPx = cam.cy * sy;
  ctx.strokeStyle = opts.crossColor || 'rgba(219, 214, 205, 0.25)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cxPx - 6, cyPx); ctx.lineTo(cxPx + 6, cyPx);
  ctx.moveTo(cxPx, cyPx - 6); ctx.lineTo(cxPx, cyPx + 6);
  ctx.stroke();
  const plateProj = PLATE_WORLD.map(P => projectWorldToPixel(P, cam));
  if (plateProj.every(Boolean)) {
    ctx.strokeStyle = opts.plateStroke || 'rgba(219, 214, 205, 0.65)';
    ctx.fillStyle = opts.plateFill || 'rgba(219, 214, 205, 0.08)';
    ctx.lineWidth = opts.plateLineWidth || 1.5;
    ctx.setLineDash(opts.plateDash || [5, 3]);
    ctx.beginPath();
    for (let i = 0; i < plateProj.length; i++) {
      const x = plateProj[i].u * sx;
      const y = plateProj[i].v * sy;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
  }
  return { ctx, cssW, cssH, sx, sy };
}
"""


DRAW_PLATE_OVERLAY_JS = """
function redrawPlateOverlay(svg, cam) {
  const poly = svg && svg.querySelector ? svg.querySelector('polygon') : null;
  if (!svg || !poly || !cam || cam.image_width_px == null || cam.image_height_px == null) {
    if (poly) poly.setAttribute('points', '');
    if (svg) svg.removeAttribute('viewBox');
    return false;
  }
  const proj = PLATE_WORLD.map(P => projectWorldToPixel(P, cam));
  if (!proj.every(Boolean)) {
    poly.setAttribute('points', '');
    svg.removeAttribute('viewBox');
    return false;
  }
  svg.setAttribute('viewBox', `0 0 ${cam.image_width_px} ${cam.image_height_px}`);
  poly.setAttribute('points', proj.map(p => `${p.u.toFixed(2)},${p.v.toFixed(2)}`).join(' '));
  return true;
}
"""


def render_live_compare_camera(
    cam_id: str,
    *,
    preview_src: str,
    preview_img_attr: str = "data-preview-img",
    preview_overlay_attr: str = "data-preview-overlay",
    virt_canvas_attr: str = "data-virt-canvas",
    preview_panel_attr: str = "data-preview-panel",
    virt_cell_attr: str = "data-virt-cell",
    preview_placeholder: str = "Waiting for preview…",
    virt_placeholder: str = "Not calibrated",
    real_label: str | None = None,
    virt_label: str | None = None,
    preview_extra_class: str = "",
    preview_off: bool = False,
) -> str:
    cam = html.escape(cam_id)
    real = html.escape(real_label or f"Real · {cam_id}")
    virt = html.escape(virt_label or f"Virt · {cam_id}")
    off_cls = " off" if preview_off else ""
    extra_cls = f" {preview_extra_class.strip()}" if preview_extra_class.strip() else ""
    return (
        f'<div class="camera-compare">'
        f'<h3 class="compare-title">Camera {cam}</h3>'
        f'<div class="camera-compare-grid">'
        f'<div class="preview-panel{off_cls}{extra_cls}" {preview_panel_attr}="{cam}">'
        f'<span class="compare-cell-label real">{real}</span>'
        # Skip src when panel is off so the browser doesn't auto-fetch a
        # preview endpoint that's guaranteed to 404 (no buffered frame
        # because iOS isn't pushing).
        f'<img {preview_img_attr}="{cam}" src="{"" if preview_off else html.escape(preview_src)}" alt="preview {cam}">'
        f'<svg class="preview-overlay" {preview_overlay_attr}="{cam}" aria-hidden="true"><polygon></polygon></svg>'
        f'<div class="placeholder">{html.escape(preview_placeholder)}</div>'
        f'</div>'
        f'<div class="virt-cell" {virt_cell_attr}="{cam}">'
        f'<span class="compare-cell-label virt">{virt}</span>'
        f'<canvas {virt_canvas_attr}="{cam}"></canvas>'
        f'<div class="placeholder">{html.escape(virt_placeholder)}</div>'
        f'</div>'
        f'</div>'
        f'</div>'
    )
