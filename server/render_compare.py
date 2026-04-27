"""Projection-helper JS string library — consumed by `cam_view_ui.py`.

Hosted the legacy 2-pane `render_live_compare_camera` + `LIVE_COMPARE_CSS`
shape; both retired in Phase 5 once dashboard / setup / markers all
migrated to the merged single-pane component. Only the JS string library
remains so the runtime can compose its IIFE from these blocks.
"""
from __future__ import annotations


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
  // skipBuiltins lets the cam-view runtime own plate + principal-point
  // rendering as toggleable layers. The legacy 2-pane caller is gone
  // (Phase 5), so this branch is the only one taken in practice — kept
  // as opt-in to allow standalone canvas previews if ever needed.
  if (opts.skipBuiltins) {
    return { ctx, cssW, cssH, sx, sy };
  }
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
