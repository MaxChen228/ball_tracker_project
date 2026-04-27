"""Shared single-pane camera view runtime.

Replaces the legacy 2-pane (real preview + virtual canvas side-by-side)
layout used across dashboard / setup / markers pages with a single AR-
style merged pane: real MJPEG as base, virtual reprojection drawn as a
semi-transparent canvas overlay. Calibration correctness is read off
visually as overlay-vs-image alignment.

`window.BallTrackerCamView` is the single source of truth — every page
mounts the same component and chooses which sub-layers (plate / axes /
marker footprints / etc) are active. Sub-layer renderers are registered
on the runtime so per-page code can plug in extras (e.g. markers page
adds a footprint layer) without forking the base.

Usage on the Python side:

    from cam_view_ui import (
        CAM_VIEW_CSS, CAM_VIEW_RUNTIME_JS, render_cam_view,
    )

    body = render_cam_view(
        "A",
        preview_src="/camera/A/preview?t=0",
        layers=["plate", "axes"],
        default_opacity=70,
    )

The rendered DOM exposes `[data-cam-view="A"]` containing `[data-cam-img]`
(MJPEG <img>) + `[data-cam-canvas]` (overlay <canvas>) + a status badge
slot + a layer-toggle pill bar. The runtime mounts on DOMContentLoaded.
"""
from __future__ import annotations

import html
import json

from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
)


CAM_VIEW_CSS = """
/* === Box / positioning model — applies only when caller opts in via
   the `.cam-view` class. Dashboard / setup / markers wrap each cam in
   a 16:9 box with the canvas absolute-positioned over an MJPEG <img>;
   viewer skips the .cam-view class and arranges its own video + cell
   layout, but still uses the data-cam-view attribute below. ============ */

.cam-view {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  border: 1px solid var(--border-l);
  border-radius: calc(var(--r) + 2px);
  overflow: hidden;
  background: #120F0D;
}
.cam-view img[data-cam-img],
.cam-view canvas[data-cam-canvas] {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
}
.cam-view img[data-cam-img] {
  object-fit: cover;
  background: #120F0D;
}
.cam-view.is-offline img[data-cam-img] { opacity: 0.15; }
.cam-view .cam-view-toolbar {
  position: absolute;
  right: 10px;
  top: 10px;
  z-index: 3;
  background: rgba(26, 23, 20, 0.78);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: var(--r);
  padding: 5px 8px;
  color: #F8F7F4;
}
.cam-view .cam-view-badges {
  position: absolute;
  left: 12px;
  top: 12px;
  z-index: 3;
  display: flex;
  flex-direction: column;
  gap: 6px;
  pointer-events: none;
}
.cam-view .cam-view-extra {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 3;
  display: flex;
  gap: 6px;
  pointer-events: none;
}

/* === Content styling — works regardless of `.cam-view` class. The
   data-cam-view attribute is the contract; pages that want a different
   container layout (viewer's vid-cell) get the same pill / badge /
   slider styling for free. ============================================ */

[data-cam-view] canvas[data-cam-canvas] {
  pointer-events: none;
  /* opacity controlled by runtime via inline style */
}
[data-cam-view].has-click canvas[data-cam-canvas] {
  pointer-events: auto;
  cursor: crosshair;
}
[data-cam-view] .cam-view-toolbar {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--mono, monospace);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
[data-cam-view] .cv-layer {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border-radius: var(--r, 4px);
  border: 1px solid var(--border-base, rgba(120, 120, 120, 0.4));
  background: transparent;
  color: var(--sub, rgba(120, 120, 120, 0.7));
  cursor: pointer;
  font: inherit;
  text-transform: inherit;
  letter-spacing: inherit;
}
[data-cam-view] .cv-layer.on {
  background: var(--ink, #2A2520);
  border-color: var(--ink, #2A2520);
  color: var(--surface, #FCFBFA);
}
[data-cam-view] .cv-opacity {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--sub, rgba(120, 120, 120, 0.7));
}
[data-cam-view] .cv-opacity input[type=range] {
  width: 70px;
  accent-color: var(--ink, #2A2520);
}

/* Inside the dashboard / setup / markers `.cam-view` box, override pill
   colors to a dark-theme palette (yellow accent on near-black bg). */
.cam-view .cv-layer {
  border-color: rgba(255, 255, 255, 0.14);
  color: rgba(248, 247, 244, 0.66);
}
.cam-view .cv-layer.on {
  background: rgba(255, 200, 0, 0.18);
  border-color: rgba(255, 200, 0, 0.55);
  color: #FFE08A;
}
.cam-view .cv-opacity { color: #F8F7F4; }
.cam-view .cv-opacity input[type=range] { accent-color: #FFD86A; }

[data-cam-view] .cam-view-badge {
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
[data-cam-view] .cam-view-badge.cam-id { border-color: rgba(202, 61, 47, 0.32); }
[data-cam-view] .cam-view-badge.status-offline { border-color: rgba(202, 61, 47, 0.6); color: #F8C8C0; }
[data-cam-view] .cam-view-badge.status-uncal { border-color: rgba(255, 200, 0, 0.5); color: #FFD86A; }
[data-cam-view] .cam-view-badge.rms { border-color: rgba(120, 200, 140, 0.45); color: #C4F0CD; }
"""


_DRAW_AXES_JS = r"""
function drawAxesLayer(ctx, sx, sy, cam) {
  // 0.3 m world axes anchored at plate centre (0, 0.216, 0). X red, Y green, Z blue.
  const origin = [0.0, 0.216, 0.0];
  const tips = {
    x: [origin[0] + 0.3, origin[1], origin[2]],
    y: [origin[0], origin[1] + 0.3, origin[2]],
    z: [origin[0], origin[1], origin[2] + 0.3],
  };
  const o = projectWorldToPixel(origin, cam);
  if (!o) return;
  const ox = o.u * sx, oy = o.v * sy;
  const colors = { x: '#E07A6B', y: '#9DD68F', z: '#7AB8E0' };
  for (const k of ['x', 'y', 'z']) {
    const tip = projectWorldToPixel(tips[k], cam);
    if (!tip) continue;
    ctx.strokeStyle = colors[k];
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    ctx.moveTo(ox, oy);
    ctx.lineTo(tip.u * sx, tip.v * sy);
    ctx.stroke();
  }
}
"""


CAM_VIEW_RUNTIME_JS = (
    PLATE_WORLD_JS
    + PROJECTION_JS
    + DRAW_VIRTUAL_BASE_JS
    + _DRAW_AXES_JS
    + r"""
(function () {
  if (window.BallTrackerCamView) return;

  const camMeta = new Map();    // cam_id -> {fx, fy, cx, cy, R_wc, t_wc, image_width_px, image_height_px, distortion}
  const camExtras = new Map();  // cam_id -> { rms_px?: number, ... }
  const camStatus = new Map();  // cam_id -> { online: bool, calibrated: bool }
  const layerState = new Map(); // cam_id -> { plate: bool, axes: bool, ... }
  const opacityState = new Map(); // cam_id -> 0..100
  const layerRenderers = new Map(); // key -> fn(ctx, sx, sy, meta, extras)

  // Built-in layer renderers — register here so plate/axes work out of the box
  // and callers can registerLayer('marker_footprints', ...) to plug in extras.
  // registerLayer silently overrides — pages that want to customise plate
  // rendering can replace this. Reserved keys: plate, axes.
  layerRenderers.set('plate', function (ctx, sx, sy, cam) {
    // Plate pentagon + principal-point cross. Bundled together because
    // they're both calibration-alignment indicators — toggle 'plate' off
    // gives the operator a clean image with no overlay annotations.
    const cxPx = cam.cx * sx;
    const cyPx = cam.cy * sy;
    ctx.strokeStyle = 'rgba(219, 214, 205, 0.45)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cxPx - 6, cyPx); ctx.lineTo(cxPx + 6, cyPx);
    ctx.moveTo(cxPx, cyPx - 6); ctx.lineTo(cxPx, cyPx + 6);
    ctx.stroke();
    const proj = PLATE_WORLD.map(P => projectWorldToPixel(P, cam));
    if (!proj.every(Boolean)) return;
    ctx.strokeStyle = 'rgba(255, 200, 0, 0.85)';
    ctx.fillStyle = 'rgba(255, 200, 0, 0.10)';
    ctx.lineWidth = 1.6;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    for (let i = 0; i < proj.length; i++) {
      const x = proj[i].u * sx, y = proj[i].v * sy;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
  });
  layerRenderers.set('axes', drawAxesLayer);

  function ensureLayerState(camId) {
    if (!layerState.has(camId)) layerState.set(camId, {});
    return layerState.get(camId);
  }

  function ensureOpacity(camId) {
    if (!opacityState.has(camId)) {
      // Read default opacity off the root element if present, else 70.
      const root = document.querySelector(`[data-cam-view="${camId}"]`);
      const def = root ? Number(root.dataset.defaultOpacity || '70') : 70;
      opacityState.set(camId, def);
    }
    return opacityState.get(camId);
  }

  function applyCanvasOpacity(camId) {
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (!root) return;
    const canvas = root.querySelector('[data-cam-canvas]');
    if (!canvas) return;
    canvas.style.opacity = String(ensureOpacity(camId) / 100);
  }

  function applyStatusBadges(camId) {
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (!root) return;
    const status = camStatus.get(camId) || { online: true, calibrated: true };
    const meta = camMeta.get(camId);
    const calibrated = !!(meta && meta.fx != null && meta.R_wc && meta.t_wc);
    root.classList.toggle('is-offline', !status.online);
    const badges = root.querySelector('.cam-view-badges');
    if (!badges) return;
    let off = badges.querySelector('.status-offline');
    if (!status.online) {
      if (!off) {
        off = document.createElement('span');
        off.className = 'cam-view-badge status-offline';
        off.textContent = 'offline';
        badges.appendChild(off);
      }
    } else if (off) {
      off.remove();
    }
    let unc = badges.querySelector('.status-uncal');
    if (status.online && !calibrated) {
      if (!unc) {
        unc = document.createElement('span');
        unc.className = 'cam-view-badge status-uncal';
        unc.textContent = 'uncalibrated';
        badges.appendChild(unc);
      }
    } else if (unc) {
      unc.remove();
    }
    const extras = camExtras.get(camId) || {};
    let rms = badges.querySelector('.rms');
    if (extras.rms_px != null && calibrated) {
      if (!rms) {
        rms = document.createElement('span');
        rms.className = 'cam-view-badge rms';
        badges.appendChild(rms);
      }
      rms.textContent = `rms ${Number(extras.rms_px).toFixed(2)} px`;
    } else if (rms) {
      rms.remove();
    }
  }

  function paintOne(root) {
    const camId = root.dataset.camView;
    if (!camId) return;
    const canvas = root.querySelector('[data-cam-canvas]');
    if (!canvas) return;
    const meta = camMeta.get(camId);
    // skipBuiltins: cam-view runtime owns plate + principal-point as
    // toggleable layers. Otherwise drawVirtualBase double-paints them.
    const base = drawVirtualBase(canvas, meta, { background: 'transparent', skipBuiltins: true });
    if (!base) return;
    const { ctx, sx, sy } = base;
    const layers = ensureLayerState(camId);
    for (const [key, on] of Object.entries(layers)) {
      if (!on) continue;
      const fn = layerRenderers.get(key);
      if (!fn) continue;
      try {
        fn(ctx, sx, sy, meta, camExtras.get(camId) || {});
      } catch (e) {
        // Per-layer failures must not break the whole canvas — log and move on.
        if (window.console && console.warn) console.warn('cam-view layer error', key, e);
      }
    }
  }

  function redrawAll() {
    for (const root of document.querySelectorAll('[data-cam-view]')) paintOne(root);
  }

  function redraw(camId) {
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (root) paintOne(root);
  }

  function setMeta(camId, meta) {
    if (meta == null) camMeta.delete(camId);
    else camMeta.set(camId, meta);
    applyStatusBadges(camId);
    redraw(camId);
  }

  function setExtras(camId, extras) {
    camExtras.set(camId, extras || {});
    applyStatusBadges(camId);
    redraw(camId);
  }

  function setStatus(camId, status) {
    // status = { online: bool }. Calibration badge is derived from
    // setMeta payload, not from this status arg — keeping calibration
    // truth in one place avoids two callers disagreeing.
    camStatus.set(camId, Object.assign({ online: true }, status || {}));
    applyStatusBadges(camId);
  }

  function listCams() {
    return Array.from(camMeta.keys());
  }

  function setLayer(camId, layerKey, on) {
    const ls = ensureLayerState(camId);
    ls[layerKey] = !!on;
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (root) {
      const btn = root.querySelector(`.cv-layer[data-layer="${layerKey}"]`);
      if (btn) btn.classList.toggle('on', !!on);
    }
    redraw(camId);
  }

  function setOpacity(camId, value) {
    const v = Math.max(0, Math.min(100, Number(value) || 0));
    opacityState.set(camId, v);
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (root) {
      const slider = root.querySelector('.cv-opacity input[type=range]');
      if (slider && Number(slider.value) !== v) slider.value = String(v);
    }
    applyCanvasOpacity(camId);
  }

  function registerLayer(key, fn) {
    if (typeof fn === 'function') layerRenderers.set(key, fn);
  }

  const clickHandlers = new Map(); // cam_id -> [fn(eventInfo), ...]
  const resizeObservers = new Map(); // cam_id -> ResizeObserver
  const previewPollers = new Map(); // cam_id -> Set<intervalId>  (cleared by forgetCam)

  function onCanvasClick(camId, fn) {
    if (typeof fn !== 'function') return;
    if (!clickHandlers.has(camId)) clickHandlers.set(camId, []);
    clickHandlers.get(camId).push(fn);
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (root) root.classList.add('has-click');
  }

  function _emitCanvasClick(camId, ev) {
    const handlers = clickHandlers.get(camId);
    if (!handlers || handlers.length === 0) return;
    const meta = camMeta.get(camId);
    const target = ev.currentTarget;
    const rect = target.getBoundingClientRect();
    const cssX = ev.clientX - rect.left;
    const cssY = ev.clientY - rect.top;
    // Map css px -> image-space pixels using meta.image_*. Without meta,
    // fall back to css coords so the handler at least sees the click.
    let u = cssX, v = cssY;
    if (meta && meta.image_width_px && meta.image_height_px && rect.width > 0 && rect.height > 0) {
      u = cssX * (meta.image_width_px / rect.width);
      v = cssY * (meta.image_height_px / rect.height);
    }
    const info = { camId, u, v, cssX, cssY, meta, event: ev };
    for (const fn of handlers) {
      try { fn(info); } catch (e) {
        if (window.console && console.warn) console.warn('cam-view click handler error', e);
      }
    }
  }

  function mount(root) {
    const camId = root.dataset.camView;
    if (!camId) return;
    // Initialise layer state from data-layers="plate,axes" + data-layers-on="plate,axes".
    // Re-mount preserves existing user-toggled state — only seed keys we
    // haven't seen before. Otherwise tickCalibration's renderDevices
    // innerHTML rebuild would silently undo every toggle.
    const all = (root.dataset.layers || '').split(',').map(s => s.trim()).filter(Boolean);
    const onSet = new Set((root.dataset.layersOn || '').split(',').map(s => s.trim()).filter(Boolean));
    const ls = ensureLayerState(camId);
    for (const k of all) if (!(k in ls)) ls[k] = onSet.has(k);
    // Wire per-layer toggle buttons.
    root.querySelectorAll('.cv-layer').forEach(btn => {
      const key = btn.dataset.layer;
      btn.classList.toggle('on', !!ls[key]);
      btn.addEventListener('click', () => setLayer(camId, key, !ls[key]));
    });
    // Wire opacity slider.
    const slider = root.querySelector('.cv-opacity input[type=range]');
    if (slider) {
      slider.value = String(ensureOpacity(camId));
      slider.addEventListener('input', () => setOpacity(camId, slider.value));
    }
    // Wire canvas click (no-op until onCanvasClick registers a handler).
    const canvas = root.querySelector('[data-cam-canvas]');
    if (canvas) {
      canvas.addEventListener('click', (ev) => _emitCanvasClick(camId, ev));
    }
    if (clickHandlers.has(camId)) root.classList.add('has-click');
    // ResizeObserver catches sidebar/grid reflow that doesn't trigger
    // window 'resize' (e.g. dashboard side card collapse). Track per
    // camId so renderDevices' innerHTML rebuild can disconnect the
    // stranded observer on the discarded root before we attach a new one.
    if (typeof ResizeObserver !== 'undefined') {
      const prev = resizeObservers.get(camId);
      if (prev) prev.disconnect();
      const obs = new ResizeObserver(() => {
        const r = document.querySelector(`[data-cam-view="${camId}"]`);
        if (r) paintOne(r);
      });
      obs.observe(root);
      resizeObservers.set(camId, obs);
    }
    applyCanvasOpacity(camId);
    applyStatusBadges(camId);
    paintOne(root);
  }

  function mountAll() {
    document.querySelectorAll('[data-cam-view]').forEach(mount);
  }

  function forgetCam(camId) {
    // Drop every cam-keyed bit of state. After this call the runtime
    // behaves as if the cam had never been registered, so re-mounting
    // the same camId starts fresh — no stale layer toggles, no
    // observer leaks, no ghost preview pollers. setMeta(null) is a
    // softer "decalibrated but still here" signal; forgetCam is the
    // hard "this cam is gone" signal.
    camMeta.delete(camId);
    camExtras.delete(camId);
    camStatus.delete(camId);
    layerState.delete(camId);
    opacityState.delete(camId);
    clickHandlers.delete(camId);
    const obs = resizeObservers.get(camId);
    if (obs) obs.disconnect();
    resizeObservers.delete(camId);
    const pollers = previewPollers.get(camId);
    if (pollers) {
      for (const id of pollers) clearInterval(id);
      previewPollers.delete(camId);
    }
    const root = document.querySelector(`[data-cam-view="${camId}"]`);
    if (root) {
      root.classList.remove('has-click');
      root.classList.remove('is-offline');
      const badges = root.querySelector('.cam-view-badges');
      if (badges) badges.querySelectorAll('.cam-view-badge').forEach(el => el.remove());
      const canvas = root.querySelector('[data-cam-canvas]');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    }
  }

  function startPreviewPolling(camId, opts) {
    // Cache-busting GET on the cam's <img data-cam-img>. Multipart MJPEG
    // via <img> is too flaky across browsers (Chrome silently aborts when
    // the first boundary doesn't land within a short window), so we
    // simulate streaming by bumping a query-string. Default 200 ms ≈ 5 fps.
    // Gated on .is-offline so cams with preview disabled don't pin the
    // network with 404s — same gating dashboard always had, now uniformly
    // available to /setup and /markers.
    const o = opts || {};
    const intervalMs = o.intervalMs || 200;
    const gateOffline = o.gateOffline !== false;
    const urlBuilder = o.urlBuilder
      || (cam => '/camera/' + encodeURIComponent(cam) + '/preview?t=' + Date.now());
    const tick = () => {
      const root = document.querySelector(`[data-cam-view="${camId}"]`);
      if (!root) return;
      if (gateOffline && root.classList.contains('is-offline')) return;
      const img = root.querySelector(`img[data-cam-img]`);
      if (!img) return;
      img.src = urlBuilder(camId);
    };
    const id = setInterval(tick, intervalMs);
    if (!previewPollers.has(camId)) previewPollers.set(camId, new Set());
    previewPollers.get(camId).add(id);
    return () => {
      clearInterval(id);
      const set = previewPollers.get(camId);
      if (set) set.delete(id);
    };
  }

  function startCalibrationPolling(opts) {
    // Periodic GET /calibration/state. setMeta the cams the server's
    // scene reports; setMeta(null) the cams we've seen before but the
    // server didn't return — that flips them to 'uncalibrated' badge
    // immediately rather than waiting for next page load. For "cam fully
    // gone" semantics callers want forgetCam, but dashboard / setup /
    // markers all keep their EXPECTED set fixed so setMeta(null) is the
    // right default here.
    const o = opts || {};
    const intervalMs = o.intervalMs || 5000;
    const endpoint = o.endpoint || '/calibration/state';
    const onPayload = typeof o.onPayload === 'function' ? o.onPayload : null;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const r = await fetch(endpoint, { cache: 'no-store' });
        if (!r.ok) return;
        const payload = await r.json();
        const cams = (payload.scene && payload.scene.cameras) || [];
        const live = new Set();
        for (const c of cams) {
          if (!c || !c.camera_id) continue;
          setMeta(c.camera_id, c);
          live.add(c.camera_id);
        }
        for (const cam of listCams()) {
          if (!live.has(cam)) setMeta(cam, null);
        }
        if (onPayload) {
          try { onPayload(payload); } catch (_) {}
        }
      } catch (_) { /* silent retry */ }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => { stopped = true; clearInterval(id); };
  }

  window.addEventListener('resize', redrawAll);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountAll);
  } else {
    mountAll();
  }

  window.BallTrackerCamView = {
    mount, mountAll, redraw, redrawAll,
    setMeta, setExtras, setStatus,
    setLayer, setOpacity, registerLayer,
    onCanvasClick, listCams,
    forgetCam, startPreviewPolling, startCalibrationPolling,
    _internal: { camMeta, camExtras, camStatus, layerState, opacityState, layerRenderers, clickHandlers, resizeObservers, previewPollers },
  };
})();
"""
)


_LAYER_LABELS = {
    "plate": "PLATE",
    "axes": "AXES",
    "marker_footprints": "MARKERS",
    "ball_crosshair": "BALL",
    "reproj_check": "REPROJ",
    "detection_live": "LIVE",
    "detection_svr": "SVR",
}


def render_cam_view(
    cam_id: str,
    *,
    preview_src: str,
    layers: list[str],
    layers_on: list[str] | None = None,
    default_opacity: int = 70,
    cam_label: str | None = None,
    show_opacity: bool = True,
    extra_html: str = "",
) -> str:
    """Emit a single-pane merged camera view.

    `layers` declares which sub-layers are user-toggleable (rendered as
    pill buttons in the toolbar). `layers_on` defaults to `layers` (all
    on). `default_opacity` 0..100 sets initial canvas alpha. `extra_html`
    is injected into the bottom-left slot for page-specific affordances.
    """
    cam = html.escape(cam_id)
    label = html.escape(cam_label or f"Cam {cam_id}")
    on_set = set(layers_on) if layers_on is not None else set(layers)
    pills: list[str] = []
    for key in layers:
        cls = "cv-layer on" if key in on_set else "cv-layer"
        text = html.escape(_LAYER_LABELS.get(key, key.upper()))
        pills.append(
            f'<button type="button" class="{cls}" data-layer="{html.escape(key)}">{text}</button>'
        )
    layers_csv = html.escape(",".join(layers))
    layers_on_csv = html.escape(",".join(k for k in layers if k in on_set))
    opacity_html = ""
    if show_opacity:
        opacity_html = (
            '<span class="cv-opacity">OVL'
            f'<input type="range" min="0" max="100" step="1" value="{int(default_opacity)}" aria-label="Overlay opacity">'
            "</span>"
        )
    return (
        f'<div class="cam-view" data-cam-view="{cam}" '
        f'data-layers="{layers_csv}" data-layers-on="{layers_on_csv}" '
        f'data-default-opacity="{int(default_opacity)}">'
        f'<img data-cam-img="{cam}" src="{html.escape(preview_src)}" alt="preview {cam}">'
        f'<canvas data-cam-canvas="{cam}"></canvas>'
        f'<div class="cam-view-badges">'
        f'<span class="cam-view-badge cam-id">{label}</span>'
        f'</div>'
        f'<div class="cam-view-toolbar">{"".join(pills)}{opacity_html}</div>'
        f'<div class="cam-view-extra">{extra_html}</div>'
        f'</div>'
    )


def assert_cam_view_present(html_text: str) -> None:
    """Smoke-check that a rendered page actually injected the runtime
    before the page's main script. Mirrors `assert_overlays_present`."""
    if "BallTrackerCamView" not in html_text:
        raise AssertionError("BallTrackerCamView runtime missing from page")
    if "PLATE_WORLD" not in html_text:
        raise AssertionError("PLATE_WORLD constants missing from page")
    if "projectWorldToPixel" not in html_text:
        raise AssertionError("projectWorldToPixel helper missing from page")


def cam_view_runtime_self_check() -> None:
    """Best-effort string self-check used by tests / module import smoke."""
    js = CAM_VIEW_RUNTIME_JS
    for needle in (
        "BallTrackerCamView",
        "PLATE_WORLD",
        "projectWorldToPixel",
        "drawVirtualBase",
        "drawAxesLayer",
        "registerLayer",
        "setMeta",
        "setLayer",
        "setOpacity",
        "data-cam-view",
    ):
        if needle not in js:
            raise AssertionError(f"cam-view runtime missing {needle!r}")
    # Defensive: ensure JSON encoder for layer config can round-trip.
    json.loads(json.dumps({"plate": True, "axes": False}))
