"""Renderer for `/markers` — dual-camera marker registry workspace."""
from __future__ import annotations

import json
from typing import Any

from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    DRAW_PLATE_OVERLAY_JS,
    LIVE_COMPARE_CSS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
    render_live_compare_camera,
)
from render_dashboard import _CSS


_MARKERS_CSS = """
.main-markers {
  max-width: 1500px; margin: 0 auto;
  padding: calc(var(--nav-h) + var(--s-4)) var(--s-4) var(--s-5) var(--s-4);
  display: flex; flex-direction: column; gap: var(--s-3);
}
.markers-hero {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: var(--s-3);
  padding: 20px 24px;
  flex-wrap: wrap;
}
.hero-copy {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hero-kicker {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--sub); margin: 0;
}
.hero-title {
  font-family: var(--mono); font-size: 28px; line-height: 1.05; letter-spacing: 0.02em;
  color: var(--ink); margin: 0;
}
.hero-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.markers-grid {
  display: grid; grid-template-columns: minmax(640px, 1.35fr) minmax(340px, 0.9fr);
  gap: var(--s-3); align-items: start;
}
.compare-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--s-3);
}
.camera-compare {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.camera-compare-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.compare-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s-2);
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.compare-title {
  margin: 0; font-family: var(--mono); font-size: 13px;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--ink);
}
""" + LIVE_COMPARE_CSS + """
.camera-compare .preview-panel .placeholder {
  display: none;
}
.markers-right { display: flex; flex-direction: column; gap: var(--s-3); }
.controls-row {
  display: flex; gap: var(--s-2); align-items: end; flex-wrap: wrap;
}
.field {
  display: flex; flex-direction: column; gap: 6px; min-width: 92px;
}
.field label {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.12em;
  text-transform: uppercase; color: var(--sub);
}
.field input, .field select {
  border: 1px solid var(--border-base); border-radius: var(--r);
  background: var(--surface); color: var(--ink); padding: 8px 10px;
  font-family: var(--mono); font-size: 12px;
}
.field.checkbox {
  flex-direction: row; align-items: center; gap: 8px; padding-top: 23px;
}
.field.checkbox label { margin: 0; }
.muted-note {
  font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em;
  color: var(--sub); text-transform: uppercase;
}
#markers-plot {
  width: 100%; height: 620px;
  background: var(--surface-hover); border: 1px solid var(--border-l);
  border-radius: var(--r);
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.section-head .card-title { margin: 0; }
.list-table {
  width: 100%; border-collapse: collapse;
  font-family: var(--mono); font-size: 11px;
}
.list-table th, .list-table td {
  border-top: 1px solid var(--border-l); padding: 8px 0; text-align: left;
  vertical-align: top;
}
.list-table th {
  font-size: 10px; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--sub); font-weight: 500;
}
.list-table tr:first-child th, .list-table tr:first-child td { border-top: 0; }
.marker-row-active td { background: rgba(230,179,0,.08); }
.marker-inline {
  display: flex; gap: var(--s-2); align-items: center; flex-wrap: wrap;
}
.pill-note {
  display: inline-block; padding: 2px 6px; border: 1px solid var(--border-base);
  border-radius: var(--r); font-family: var(--mono); font-size: 10px;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--sub);
}
.stack {
  display: flex; flex-direction: column; gap: var(--s-2);
}
.split-fields {
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s-2);
}
.split-fields.three {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.empty-state {
  border: 1px dashed var(--border-base); border-radius: var(--r);
  padding: var(--s-4); color: var(--sub); font-family: var(--mono);
  font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
}
.status-banner {
  min-height: 18px; font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.08em; color: var(--sub); text-transform: uppercase;
}
.status-banner.error { color: var(--failed); }
.status-banner.ok { color: var(--passed); }
.candidate-check { margin-top: 10px; }
.warning-text { color: var(--failed); }
.good-text { color: var(--passed); }
.subtle-text { color: var(--sub); }
@media (max-width: 1100px) {
  .compare-grid { grid-template-columns: 1fr; }
  .camera-compare-grid { grid-template-columns: 1fr; }
  .markers-grid { grid-template-columns: 1fr; }
  #markers-plot { height: 480px; }
  .hero-title { font-size: 22px; }
}
"""


_MARKERS_JS = r"""
(function () {
  const INITIAL = __INITIAL_STATE__;
  const plotEl = document.getElementById('markers-plot');
  const candidatesBody = document.getElementById('candidate-body');
  const storedBody = document.getElementById('stored-body');
  const detailsBody = document.getElementById('details-body');
  const statusEl = document.getElementById('markers-status');
  const scanBtn = document.getElementById('scan-btn');
  const saveCandidatesBtn = document.getElementById('save-candidates-btn');
  const clearBtn = document.getElementById('clear-markers-btn');
  const camAEl = document.getElementById('camera-a');
  const camBEl = document.getElementById('camera-b');
  const compareRoot = document.getElementById('compare-root');

  const state = {
    markers: INITIAL.markers || [],
    candidates: [],
    scanMeta: INITIAL.scanMeta || null,
    scene: INITIAL.scene || {},
    compareMarkers: INITIAL.compare_markers || [],
    selectedKind: null,
    selectedId: null,
  };

  """ + PLATE_WORLD_JS + """
  const virtCamMeta = new Map((state.scene.cameras || []).map(cam => [cam.camera_id, cam]));
  """ + PROJECTION_JS + """
  """ + DRAW_VIRTUAL_BASE_JS + """
  """ + DRAW_PLATE_OVERLAY_JS + """

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }

  function fmt(n, digits = 3) {
    const x = Number(n);
    if (!Number.isFinite(x)) return '—';
    return x.toFixed(digits);
  }

  function setStatus(msg, cls) {
    statusEl.className = 'status-banner' + (cls ? (' ' + cls) : '');
    statusEl.textContent = msg || '';
  }

  function markerKey(kind, markerId) { return `${kind}:${markerId}`; }

  function currentSelection() {
    if (!state.selectedKind) return null;
    const rows = state.selectedKind === 'candidate' ? state.candidates : state.markers;
    return rows.find(r => Number(r.marker_id) === Number(state.selectedId)) || null;
  }

  function syncSelectionFallback() {
    const sel = currentSelection();
    if (!sel) {
      state.selectedKind = null;
      state.selectedId = null;
    }
  }

  function renderPlot() {
    const storedPlane = state.markers.filter(m => m.on_plate_plane);
    const storedFree = state.markers.filter(m => !m.on_plate_plane);
    const candidates = state.candidates;
    const traces = [];

    const plate = state.scene.plate || [];
    if (plate.length) {
      const closed = plate.concat([plate[0]]);
      traces.push({
        type: 'scatter3d',
        mode: 'lines',
        name: 'Home plate',
        x: closed.map(p => p.x),
        y: closed.map(p => p.y),
        z: closed.map(p => p.z),
        line: { color: '#2A2520', width: 6 },
        hoverinfo: 'skip',
      });
      traces.push({
        type: 'mesh3d',
        name: 'Plate plane',
        x: plate.map(p => p.x),
        y: plate.map(p => p.y),
        z: plate.map(p => p.z),
        opacity: 0.08,
        color: '#7A756C',
        hoverinfo: 'skip',
        showscale: false,
      });
    }

    (state.scene.cameras || []).forEach(cam => {
      const c = cam.center_world || [0, 0, 0];
      const f = cam.axis_forward_world || [0, 0, 1];
      const r = cam.axis_right_world || [1, 0, 0];
      const u = cam.axis_up_world || [0, 1, 0];
      const scale = 0.24;
      traces.push({
        type: 'scatter3d',
        mode: 'markers+text',
        name: `Camera ${cam.camera_id}`,
        x: [c[0]], y: [c[1]], z: [c[2]],
        text: [`CAM ${cam.camera_id}`],
        textposition: 'bottom center',
        marker: { size: 6, color: cam.camera_id === 'A' ? '#C0392B' : '#D35400' },
        hovertemplate: `Camera ${cam.camera_id}<br>x=%{x:.2f}<br>y=%{y:.2f}<br>z=%{z:.2f}<extra></extra>`,
      });
      [
        { vec: f, color: '#256246', name: `Camera ${cam.camera_id} forward` },
        { vec: r, color: '#4A6B8C', name: `Camera ${cam.camera_id} right` },
        { vec: u, color: '#A7372A', name: `Camera ${cam.camera_id} up` },
      ].forEach(axis => {
        traces.push({
          type: 'scatter3d',
          mode: 'lines',
          name: axis.name,
          x: [c[0], c[0] + axis.vec[0] * scale],
          y: [c[1], c[1] + axis.vec[1] * scale],
          z: [c[2], c[2] + axis.vec[2] * scale],
          line: { color: axis.color, width: 4 },
          hoverinfo: 'skip',
          showlegend: false,
        });
      });
    });

    function pushTrace(rows, name, color, symbol, kind) {
      if (!rows.length) return;
      traces.push({
        type: 'scatter3d',
        mode: 'markers+text',
        name,
        x: rows.map(r => r.x_m),
        y: rows.map(r => r.y_m),
        z: rows.map(r => r.z_m),
        text: rows.map(r => String(r.marker_id)),
        textposition: 'top center',
        marker: { size: 6, color, symbol, line: { color: '#2A2520', width: 1 } },
        customdata: rows.map(r => [kind, r.marker_id]),
        hovertemplate:
          'ID %{text}<br>x=%{x:.3f} m<br>y=%{y:.3f} m<br>z=%{z:.3f} m<extra>' + name + '</extra>',
      });
    }

    pushTrace(storedPlane, 'Stored · plate plane', '#256246', 'square', 'stored');
    pushTrace(storedFree, 'Stored · free 3D', '#9B6B16', 'circle', 'stored');
    pushTrace(candidates, 'Scanned candidates', '#A7372A', 'diamond', 'candidate');

    const layout = {
      margin: { l: 0, r: 0, t: 10, b: 0 },
      paper_bgcolor: '#F8F7F4',
      plot_bgcolor: '#F8F7F4',
      showlegend: true,
      legend: { orientation: 'h', y: 1.05, x: 0 },
      scene: {
        bgcolor: '#F8F7F4',
        xaxis: { title: 'X (m)', gridcolor: '#E8E4DB', zerolinecolor: '#DBD6CD' },
        yaxis: { title: 'Y (m)', gridcolor: '#E8E4DB', zerolinecolor: '#DBD6CD' },
        zaxis: { title: 'Z (m)', gridcolor: '#E8E4DB', zerolinecolor: '#DBD6CD' },
        aspectmode: 'data',
        uirevision: 'markers-plot',
        camera: { eye: { x: 1.5, y: -1.6, z: 0.9 } },
      },
    };
    Plotly.react(plotEl, traces, layout, { displayModeBar: false, responsive: true });
    plotEl.on('plotly_click', ev => {
      const row = ev && ev.points && ev.points[0] ? ev.points[0].customdata : null;
      if (!row) return;
      state.selectedKind = row[0] === 'candidate' ? 'candidate' : 'stored';
      state.selectedId = Number(row[1]);
      renderAll();
    });
  }

  function compareRows() {
    const rows = [];
    (state.compareMarkers || []).forEach(row => rows.push({ ...row, origin: 'known' }));
    state.markers.forEach(row => {
      if (!rows.find(existing => Number(existing.marker_id) === Number(row.marker_id))) {
        rows.push({ ...row, kind: 'stored', origin: 'stored' });
      }
    });
    state.candidates.forEach(row => {
      const idx = rows.findIndex(existing => Number(existing.marker_id) === Number(row.marker_id));
      const next = { ...row, kind: 'candidate', origin: 'candidate' };
      if (idx >= 0) rows[idx] = next;
      else rows.push(next);
    });
    return rows;
  }

  function markerColor(row) {
    if (row.kind === 'plate') return '#256246';
    if (row.kind === 'candidate') return '#A7372A';
    return row.on_plate_plane ? '#256246' : '#9B6B16';
  }

  function markerDash(row) {
    if (row.kind === 'candidate') return [5, 3];
    if (row.kind === 'plate') return [3, 2];
    return row.on_plate_plane ? [4, 3] : [8, 4];
  }

  function drawMarkerFootprint(ctx, row, cam, sx, sy, selected) {
    const half = Number(row.side_m || 0.08) / 2.0;
    const x = Number(row.x_m || 0);
    const y = Number(row.y_m || 0);
    const z = Number(row.z_m || 0);
    const quad = [
      [x - half, y - half, z],
      [x + half, y - half, z],
      [x + half, y + half, z],
      [x - half, y + half, z],
    ].map(P => projectWorldToPixel(P, cam));
    const centroid = projectWorldToPixel([x, y, z], cam);
    if (!centroid) return;
    const color = markerColor(row);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = selected ? 2.4 : 1.5;
    ctx.setLineDash(markerDash(row));
    if (quad.every(Boolean)) {
      ctx.beginPath();
      for (let i = 0; i < quad.length; i++) {
        const px = quad[i].u * sx;
        const py = quad[i].v * sy;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.stroke();
    } else {
      const cx = centroid.u * sx;
      const cy = centroid.v * sy;
      const box = selected ? 18 : 14;
      ctx.strokeRect(cx - box / 2, cy - box / 2, box, box);
    }
    ctx.setLineDash([]);
    const lx = centroid.u * sx;
    const ly = centroid.v * sy;
    const label = `ID ${row.marker_id}`;
    ctx.font = '11px "JetBrains Mono", monospace';
    const tw = ctx.measureText(label).width;
    const pad = 4;
    const bh = 18;
    ctx.fillStyle = color;
    ctx.fillRect(lx - tw / 2 - pad, ly - 22, tw + pad * 2, bh);
    ctx.fillStyle = '#1A1714';
    ctx.fillText(label, lx - tw / 2, ly - 9);
  }

  function drawCompareVirtual(canvas, camId) {
    const cam = virtCamMeta.get(camId);
    const base = drawVirtualBase(canvas, cam, {
      plateStroke: 'rgba(202, 61, 47, 0.95)',
      plateFill: 'rgba(202, 61, 47, 0.10)',
    });
    if (!base) {
      return false;
    }
    const { ctx, sx, sy } = base;
    const selected = currentSelection();
    compareRows().forEach(row => {
      drawMarkerFootprint(
        ctx,
        row,
        cam,
        sx,
        sy,
        selected && Number(selected.marker_id) === Number(row.marker_id),
      );
    });
    ctx.restore();
    return true;
  }

  function svgNode(name, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, String(v)));
    return el;
  }

  function labelWidth(text) {
    return Math.max(18, 10 + String(text).length * 8);
  }

  function drawPreviewOverlay(svg, camId) {
    const meta = virtCamMeta.get(camId);
    const ok = redrawPlateOverlay(svg, meta);
    let layer = svg.querySelector('[data-role="marker-layer"]');
    if (!layer) {
      layer = svgNode('g', { 'data-role': 'marker-layer' });
      svg.appendChild(layer);
    }
    layer.replaceChildren();
    if (!ok) return;
    const selected = currentSelection();
    compareRows().forEach(row => {
      const point = projectWorldToPixel([row.x_m, row.y_m, row.z_m], meta);
      if (!point) return;
      const text = String(row.marker_id);
      const width = labelWidth(text);
      const x = point.u;
      const y = point.v;
      const g = svgNode('g', {
        class: `preview-marker${selected && Number(selected.marker_id) === Number(row.marker_id) ? ' is-selected' : ''}`,
      });
      g.appendChild(svgNode('circle', {
        cx: x,
        cy: y,
        r: 4.5,
        fill: markerColor(row),
        class: 'marker-dot',
      }));
      g.appendChild(svgNode('rect', {
        x: x - width / 2,
        y: y - 28,
        width,
        height: 18,
        fill: markerColor(row),
        class: 'marker-tag',
      }));
      const textEl = svgNode('text', {
        x,
        y: y - 19,
        class: 'marker-text',
      });
      textEl.textContent = text;
      g.appendChild(textEl);
      layer.appendChild(g);
    });
  }

  function redrawCompareViews() {
    compareRoot.querySelectorAll('[data-markers-virt-canvas]').forEach(canvas => {
      const camId = canvas.dataset.markersVirtCanvas;
      const cell = canvas.closest('.virt-cell');
      const ok = drawCompareVirtual(canvas, camId);
      if (cell) cell.classList.toggle('ready', ok);
    });
    compareRoot.querySelectorAll('[data-preview-overlay]').forEach(svg => {
      drawPreviewOverlay(svg, svg.dataset.previewOverlay);
    });
  }

  async function tickPreviewRefresh() {
    const cams = ['A', 'B'];
    await Promise.all(cams.map(async cam => {
      try {
        await fetch('/camera/' + encodeURIComponent(cam) + '/preview_request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: true }),
        });
      } catch (_) {}
    }));
  }

  function tickPreviewImages() {
    const t = Date.now();
    compareRoot.querySelectorAll('img[data-preview-img]').forEach(img => {
      const cam = img.dataset.previewImg;
      if (!cam) return;
      img.src = '/camera/' + encodeURIComponent(cam) + '/preview?t=' + t;
      img.style.opacity = 1;
    });
  }

  function renderCandidates() {
    if (!state.candidates.length) {
      candidatesBody.innerHTML = '<div class="empty-state">No pending scan. Run dual-camera scan to populate candidate markers.</div>';
      return;
    }
    const selectedKey = markerKey(state.selectedKind, state.selectedId);
    candidatesBody.innerHTML = `
      <table class="list-table">
        <tr><th>Save</th><th>ID</th><th>Label</th><th>Pose</th><th>Plane</th><th>Quality</th></tr>
        ${state.candidates.map(row => {
          const key = markerKey('candidate', row.marker_id);
          const qualityCls = row.residual_bucket === 'poor' ? 'warning-text'
                            : (row.residual_bucket === 'warn' ? 'warning-text' : 'good-text');
          const actionNote = row.update_action === 'conflict'
            ? `<div class="warning-text">conflict · Δ ${fmt(row.delta_existing_m, 3)} m</div>`
            : (row.update_action === 'refresh'
                ? `<div class="good-text">update · Δ ${fmt(row.delta_existing_m, 3)} m</div>`
                : `<div class="subtle-text">new marker</div>`);
          return `<tr class="${key === selectedKey ? 'marker-row-active' : ''}">
            <td><input class="candidate-check" type="checkbox" data-role="candidate-check" data-marker-id="${row.marker_id}" ${row.save !== false ? 'checked' : ''}></td>
            <td><button class="btn secondary small" data-role="select-candidate" data-marker-id="${row.marker_id}" type="button">ID ${row.marker_id}</button></td>
            <td><input data-role="candidate-label" data-marker-id="${row.marker_id}" value="${esc(row.label || row.existing_label || '')}" placeholder="optional">${actionNote}</td>
            <td>x ${fmt(row.x_m)}<br>y ${fmt(row.y_m)}<br>z ${fmt(row.z_m)}</td>
            <td><label class="marker-inline"><input type="checkbox" data-role="candidate-plane" data-marker-id="${row.marker_id}" ${row.on_plate_plane ? 'checked' : ''}> <span class="pill-note">${row.on_plate_plane ? 'plate plane' : 'free 3d'}</span></label></td>
            <td><div class="${qualityCls}">${esc(row.residual_bucket || '—')}</div><div>${fmt(row.residual_m, 4)} m</div></td>
          </tr>`;
        }).join('')}
      </table>`;
  }

  function renderStored() {
    if (!state.markers.length) {
      storedBody.innerHTML = '<div class="empty-state">No markers saved yet.</div>';
      return;
    }
    const selectedKey = markerKey(state.selectedKind, state.selectedId);
    storedBody.innerHTML = `
      <table class="list-table">
        <tr><th>ID</th><th>Label</th><th>Placement</th><th>Pose</th><th>Source</th></tr>
        ${state.markers.map(row => {
          const key = markerKey('stored', row.marker_id);
          return `<tr class="${key === selectedKey ? 'marker-row-active' : ''}">
            <td><button class="btn secondary small" data-role="select-stored" data-marker-id="${row.marker_id}" type="button">ID ${row.marker_id}</button></td>
            <td>${esc(row.label || '—')}</td>
            <td><span class="chip ${row.on_plate_plane ? 'calibrated' : 'partial'}">${row.on_plate_plane ? 'plate plane' : 'free 3d'}</span></td>
            <td>x ${fmt(row.x_m)}<br>y ${fmt(row.y_m)}<br>z ${fmt(row.z_m)}</td>
            <td>${(row.source_camera_ids || []).join(' + ') || '—'}</td>
          </tr>`;
        }).join('')}
      </table>`;
  }

  function renderDetails() {
    const row = currentSelection();
    if (!row) {
      detailsBody.innerHTML = '<div class="empty-state">Select a stored marker or a scanned candidate to inspect and edit its values.</div>';
      return;
    }
    if (state.selectedKind === 'candidate') {
      detailsBody.innerHTML = `
        <div class="stack">
          <div class="marker-inline"><span class="chip partial">Candidate</span><span class="pill-note">ID ${row.marker_id}</span></div>
          <div class="split-fields three">
            <div class="field"><label>X (m)</label><input value="${fmt(row.x_m)}" readonly></div>
            <div class="field"><label>Y (m)</label><input value="${fmt(row.y_m)}" readonly></div>
            <div class="field"><label>Z (m)</label><input value="${fmt(row.z_m)}" readonly></div>
          </div>
          <div class="split-fields">
            <div class="field"><label>Residual (m)</label><input value="${fmt(row.residual_m, 4)}" readonly></div>
            <div class="field"><label>Placement</label><input value="${row.on_plate_plane ? 'plate plane' : 'free 3d'}" readonly></div>
          </div>
          <div class="split-fields">
            <div class="field"><label>Update mode</label><input value="${esc(row.update_action || 'new')}" readonly></div>
            <div class="field"><label>Seen by</label><input value="${(row.detected_in || []).join(' + ') || '—'}" readonly></div>
          </div>
        </div>`;
      return;
    }
    detailsBody.innerHTML = `
      <div class="stack">
        <div class="marker-inline"><span class="chip calibrated">Stored</span><span class="pill-note">ID ${row.marker_id}</span></div>
        <div class="split-fields">
          <div class="field" style="grid-column: 1 / -1;"><label>Label</label><input id="detail-label" value="${esc(row.label || '')}" placeholder="optional"></div>
        </div>
        <div class="split-fields three">
          <div class="field"><label>X (m)</label><input id="detail-x" type="number" step="0.001" value="${fmt(row.x_m)}"></div>
          <div class="field"><label>Y (m)</label><input id="detail-y" type="number" step="0.001" value="${fmt(row.y_m)}"></div>
          <div class="field"><label>Z (m)</label><input id="detail-z" type="number" step="0.001" value="${fmt(row.z_m)}"></div>
        </div>
        <div class="field checkbox">
          <input id="detail-plane" type="checkbox" ${row.on_plate_plane ? 'checked' : ''}>
          <label for="detail-plane">Marker lies on the plate plane (snap Z = 0)</label>
        </div>
        <div class="marker-inline">
          <button class="btn" type="button" id="detail-save-btn">Save marker</button>
          <button class="btn danger" type="button" id="detail-delete-btn">Delete marker</button>
        </div>
      </div>`;

    document.getElementById('detail-save-btn').onclick = async function () {
      const payload = {
        label: document.getElementById('detail-label').value,
        x_m: Number(document.getElementById('detail-x').value),
        y_m: Number(document.getElementById('detail-y').value),
        z_m: Number(document.getElementById('detail-z').value),
        on_plate_plane: document.getElementById('detail-plane').checked,
        snap_to_plate_plane: document.getElementById('detail-plane').checked,
      };
      const r = await fetch('/markers/' + encodeURIComponent(row.marker_id), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const body = await r.json();
      if (!r.ok) {
        setStatus(body.detail || 'failed to save marker', 'error');
        return;
      }
      const idx = state.markers.findIndex(m => Number(m.marker_id) === Number(row.marker_id));
      if (idx >= 0) state.markers[idx] = body.marker;
      setStatus(`Saved marker ${row.marker_id}.`, 'ok');
      renderAll();
    };
    document.getElementById('detail-delete-btn').onclick = async function () {
      const r = await fetch('/markers/' + encodeURIComponent(row.marker_id), { method: 'DELETE' });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setStatus(body.detail || 'failed to delete marker', 'error');
        return;
      }
      state.markers = state.markers.filter(m => Number(m.marker_id) !== Number(row.marker_id));
      state.selectedKind = null;
      state.selectedId = null;
      setStatus(`Deleted marker ${row.marker_id}.`, 'ok');
      renderAll();
    };
  }

  function bindTableActions() {
    candidatesBody.querySelectorAll('[data-role="select-candidate"]').forEach(el => {
      el.onclick = () => {
        state.selectedKind = 'candidate';
        state.selectedId = Number(el.dataset.markerId);
        renderAll();
      };
    });
    candidatesBody.querySelectorAll('[data-role="candidate-check"]').forEach(el => {
      el.onchange = () => {
        const row = state.candidates.find(r => Number(r.marker_id) === Number(el.dataset.markerId));
        if (row) row.save = !!el.checked;
      };
    });
    candidatesBody.querySelectorAll('[data-role="candidate-label"]').forEach(el => {
      el.oninput = () => {
        const row = state.candidates.find(r => Number(r.marker_id) === Number(el.dataset.markerId));
        if (row) row.label = el.value;
      };
    });
    candidatesBody.querySelectorAll('[data-role="candidate-plane"]').forEach(el => {
      el.onchange = () => {
        const row = state.candidates.find(r => Number(r.marker_id) === Number(el.dataset.markerId));
        if (!row) return;
        row.on_plate_plane = !!el.checked;
        if (row.on_plate_plane) row.z_m = 0;
        renderAll();
      };
    });
    storedBody.querySelectorAll('[data-role="select-stored"]').forEach(el => {
      el.onclick = () => {
        state.selectedKind = 'stored';
        state.selectedId = Number(el.dataset.markerId);
        renderAll();
      };
    });
  }

  function renderAll() {
    syncSelectionFallback();
    renderPlot();
    renderCandidates();
    renderStored();
    renderDetails();
    bindTableActions();
    redrawCompareViews();
  }

  scanBtn.onclick = async function () {
    setStatus('Scanning markers from both cameras…', '');
    const qs = new URLSearchParams({ camera_a_id: camAEl.value, camera_b_id: camBEl.value });
    const r = await fetch('/markers/scan?' + qs.toString(), { method: 'POST' });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(body.detail || 'marker scan failed', 'error');
      return;
    }
    state.scanMeta = {
      camera_a_id: camAEl.value,
      camera_b_id: camBEl.value,
      ...(body.visibility || {}),
    };
    state.candidates = (body.candidates || []).map(row => ({
      ...row,
      label: row.existing_label || '',
      on_plate_plane: !!row.suggest_on_plate_plane,
      save: row.update_action !== 'conflict',
    }));
    if (state.candidates.length) {
      state.selectedKind = 'candidate';
      state.selectedId = state.candidates[0].marker_id;
      const poor = state.candidates.filter(r => r.residual_bucket === 'poor').length;
      const conflicts = state.candidates.filter(r => r.update_action === 'conflict').length;
      const suffix = [
        poor ? `${poor} poor residual` : '',
        conflicts ? `${conflicts} conflict` : '',
      ].filter(Boolean).join(' · ');
      setStatus(`Scanned ${state.candidates.length} candidate marker(s).${suffix ? ' ' + suffix + '.' : ''}`, poor || conflicts ? 'error' : 'ok');
    } else {
      setStatus('No shared non-plate markers were visible to both cameras.', 'error');
    }
    renderAll();
  };

  saveCandidatesBtn.onclick = async function () {
    const rows = state.candidates.filter(r => r.save !== false);
    if (!rows.length) {
      setStatus('No candidate markers selected for save.', 'error');
      return;
    }
    const payload = {
      markers: rows.map(r => ({
        marker_id: r.marker_id,
        x_m: r.x_m,
        y_m: r.y_m,
        z_m: r.z_m,
        label: r.label || null,
        on_plate_plane: !!r.on_plate_plane,
        snap_to_plate_plane: !!r.on_plate_plane,
        residual_m: r.residual_m,
        source_camera_ids: r.source_camera_ids || [],
      })),
    };
    const r = await fetch('/markers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(body.detail || 'failed to save markers', 'error');
      return;
    }
    const byId = new Map(state.markers.map(m => [Number(m.marker_id), m]));
    (body.markers || []).forEach(row => byId.set(Number(row.marker_id), row));
    state.markers = Array.from(byId.values()).sort((a, b) => a.marker_id - b.marker_id);
    const savedIds = new Set((body.markers || []).map(r => Number(r.marker_id)));
    state.candidates = state.candidates.filter(r => !savedIds.has(Number(r.marker_id)));
    state.selectedKind = 'stored';
    state.selectedId = body.markers && body.markers[0] ? body.markers[0].marker_id : null;
    setStatus(`Saved ${savedIds.size} marker(s).`, 'ok');
    renderAll();
  };

  clearBtn.onclick = async function () {
    if (!confirm('Clear all saved markers?')) return;
    const r = await fetch('/markers/clear', { method: 'POST' });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(body.detail || 'failed to clear markers', 'error');
      return;
    }
    state.markers = [];
    state.selectedKind = null;
    state.selectedId = null;
    setStatus(`Cleared ${body.cleared_count || 0} marker(s).`, 'ok');
    renderAll();
  };

  window.addEventListener('resize', redrawCompareViews);
  tickPreviewRefresh();
  tickPreviewImages();
  setInterval(tickPreviewRefresh, 2000);
  setInterval(tickPreviewImages, 250);
  renderAll();
})();
"""


def render_markers_html(
    *,
    markers: list[dict[str, Any]],
    compare_markers: list[dict[str, Any]],
    scene: dict[str, Any],
    devices: list[dict[str, Any]],
    session: dict[str, Any] | None,
    calibrations: list[str],
) -> str:
    initial_state = json.dumps(
        {"markers": markers, "scene": scene, "compare_markers": compare_markers},
        ensure_ascii=False,
    )
    session_html = (
        f'<span class="val armed">{session.get("id", "—")}</span>'
        if session and session.get("armed")
        else '<span class="val idle">idle</span>'
    )
    devices_cls = "full" if len(devices) >= 2 else "partial"
    cal_cls = "full" if len(calibrations) >= 2 else "partial"
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>ball_tracker · markers</title>"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
        "<link href=\"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@300;500;700&display=swap\" rel=\"stylesheet\">"
        "<script src=\"https://cdn.plot.ly/plotly-2.35.2.min.js\" charset=\"utf-8\"></script>"
        f"<style>{_CSS}{_MARKERS_CSS}</style>"
        "</head><body data-page=\"markers\">"
        '<nav class="nav">'
        '<span class="brand"><span class="dot"></span>BALL_TRACKER</span>'
        '<div class="status-line">'
        f'<span class="pair"><span class="label">Devices</span><span class="val {devices_cls}">{len(devices)}/2</span></span>'
        f'<span class="pair"><span class="label">Calibrated</span><span class="val {cal_cls}">{len(calibrations)}/2</span></span>'
        f'<span class="pair"><span class="label">Session</span>{session_html}</span>'
        '<a class="nav-link" href="/setup">Setup</a>'
        '<a class="nav-link" href="/">Home</a>'
        '</div>'
        "</nav>"
        '<main class="main-markers">'
        '<section class="card markers-hero">'
        '<div class="hero-copy">'
        '<div class="hero-kicker">Dual-camera marker registry</div>'
        '<h1 class="hero-title">Marker Registry</h1>'
        '</div>'
        '<div class="hero-actions">'
        '<button class="btn" type="button" id="scan-btn">Scan</button>'
        '<button class="btn secondary" type="button" id="save-candidates-btn">Save Selected</button>'
        '<button class="btn danger" type="button" id="clear-markers-btn">Clear All</button>'
        '</div>'
        '</section>'
        '<section class="card">'
        '<div class="compare-heading">'
        '<h2 class="card-title">Camera Compare</h2>'
        '</div>'
        '<div id="compare-root" class="compare-grid">'
        f'{render_live_compare_camera("A", preview_src="/camera/A/preview?t=0", virt_canvas_attr="data-markers-virt-canvas")}'
        f'{render_live_compare_camera("B", preview_src="/camera/B/preview?t=0", virt_canvas_attr="data-markers-virt-canvas")}'
        '</div>'
        '</section>'
        '<section class="markers-grid">'
        '<div class="card">'
        '<div class="section-head"><h2 class="card-title">Spatial View</h2><div id="markers-status" class="status-banner"></div></div>'
        '<div class="controls-row">'
        '<div class="field"><label for="camera-a">Camera A</label><select id="camera-a"><option value="A">A</option><option value="B">B</option></select></div>'
        '<div class="field"><label for="camera-b">Camera B</label><select id="camera-b"><option value="B">B</option><option value="A">A</option></select></div>'
        '</div>'
        '<div id="markers-plot"></div>'
        '</div>'
        '<div class="markers-right">'
        '<div class="card"><h2 class="card-title">Scanned Candidates</h2><div id="candidate-body"></div></div>'
        '<div class="card"><h2 class="card-title">Marker Details</h2><div id="details-body"></div></div>'
        '<div class="card"><h2 class="card-title">Saved Markers</h2><div id="stored-body"></div></div>'
        '</div>'
        '</section>'
        '</main>'
        f"<script>{_MARKERS_JS.replace('__INITIAL_STATE__', initial_state)}</script>"
        "</body></html>"
    )
