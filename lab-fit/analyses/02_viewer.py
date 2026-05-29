"""Single-page Three.js viewer with live filter sliders + time playback.

v1 segmenter is run once per (session, path) with WIDE-OPEN gates so we
get a candidate superset. Per-segment metadata is computed
(duration, speed, n_points, path_length, rmse, az_fit). The HTML page
filters this superset live based on slider values; defaults match v1's
production parameters so the default view ≈ v1 output.

az_fit is recovered by refitting each segment with az free (not pinned
to -9.81). Real ballistic → az_fit ≈ -9.81. Stationary / rolling →
az_fit ≈ 0. Displayed per-segment in the info panel.

Overlap dedupe: after slider-bar filters pass, segments that
time-overlap are compared. The longer path_length wins
(secondary tiebreaks: more points, lower rmse). Lower-quality member
is dropped.

Output: reports/02_viewer/index.html (single self-contained file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_loader import list_sessions, load_result  # noqa: E402
from algo import segmenter as v1  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "reports" / "02_viewer"
G = np.array([0.0, 0.0, -9.81])

# v1 production defaults — slider defaults mirror these so the default
# view reproduces v1 behavior on whatever segments the permissive run
# discovered.
V1_DEFAULTS = {
    "min_seg_len": 5,
    "v_min_mps": 5.0,
    "v_max_mps": 60.0,
    "min_displacement_m": 0.30,  # used as default for "min path length"
    "min_duration_s": 0.020,
}

# Permissive run: gather superset of candidates so sliders can reveal
# segments that v1 defaults would have rejected.
PERMISSIVE_KWARGS = dict(
    min_seg_len=4,
    v_min_mps=0.1,
    v_max_mps=200.0,
    min_displacement_m=0.0,
)


class _Pt:
    __slots__ = ("t_rel_s", "x_m", "y_m", "z_m", "residual_m")
    def __init__(self, t, x, y, z, r):
        self.t_rel_s = t; self.x_m = x; self.y_m = y; self.z_m = z; self.residual_m = r


def _to_pts(raw, gap_thr):
    return [_Pt(p["t_rel_s"], p["x_m"], p["y_m"], p["z_m"], p["residual_m"])
            for p in raw if p["residual_m"] <= gap_thr]


def _refit_free_az(pts_arr, idx):
    """Refit z with az free; return az_fit (m/s²)."""
    sub = pts_arr[sorted(idx)]
    t_anchor = float(sub[0, 0])
    tau = sub[:, 0] - t_anchor
    A = np.column_stack([np.ones_like(tau), tau, 0.5 * tau * tau])
    coef, *_ = np.linalg.lstsq(A, sub[:, 3], rcond=None)
    return float(coef[2])


def _path_length(pts_arr, idx):
    sub = pts_arr[sorted(idx)][:, 1:4]
    if sub.shape[0] < 2:
        return 0.0
    diffs = np.diff(sub, axis=0)
    return float(np.sum(np.linalg.norm(diffs, axis=1)))


def _seg_curve(seg, n=80):
    ts = np.linspace(seg.t_start, seg.t_end, n)
    tau = ts - seg.t_anchor
    out = np.empty((n, 3))
    for axis in range(3):
        out[:, axis] = seg.p0[axis] + seg.v0[axis] * tau + 0.5 * G[axis] * tau * tau
    return out.tolist()


def _segs_payload(segs, pts_arr, t0):
    out = []
    for s in segs:
        members = pts_arr[sorted(s.indices)]
        out.append({
            "t_start": round(s.t_start - t0, 4),
            "t_end": round(s.t_end - t0, 4),
            "t_anchor": round(s.t_anchor - t0, 4),
            "p0": [round(float(x), 4) for x in s.p0],
            "v0": [round(float(x), 4) for x in s.v0],
            "n": len(s.indices),
            "duration_s": round(s.t_end - s.t_start, 4),
            "speed_mps": round(float(np.linalg.norm(s.v0)), 3),
            "rmse_mm": round(s.rmse_m * 1000, 1),
            "path_length_m": round(_path_length(pts_arr, s.indices), 3),
            "az_fit": round(_refit_free_az(pts_arr, s.indices), 2),
            "z_min": round(float(members[:, 3].min()), 3),
            "z_max": round(float(members[:, 3].max()), 3),
            "curve": [[round(p[0], 3), round(p[1], 3), round(p[2], 3)]
                      for p in _seg_curve(s)],
            "members": [
                [round(float(m[0]) - t0, 4),
                 round(float(m[1]), 3), round(float(m[2]), 3), round(float(m[3]), 3)]
                for m in members
            ],
        })
    return out


def build_payload():
    rows = []
    for sid in list_sessions():
        try:
            r = load_result(sid)
        except Exception:
            continue
        gap_thr = r.get("gap_threshold_m") or 0.2
        for path in ("server_post", "live"):
            from data_loader import algorithm_id_for_path
            alg = algorithm_id_for_path(r, path)
            raw = r.get("triangulated_by_algorithm", {}).get(alg, []) if alg else []
            pts = _to_pts(raw, gap_thr)
            if len(pts) < 4:
                continue
            try:
                segs, arr = v1.find_segments(pts, **PERMISSIVE_KWARGS)
            except Exception:
                continue
            t0 = float(arr[0, 0])
            t_end = float(arr[-1, 0])
            n = arr.shape[0]
            if n > 4000:
                idx = np.linspace(0, n - 1, 4000).astype(int)
                bg = arr[idx]
            else:
                bg = arr
            rows.append({
                "id": f"{sid}__{path}",
                "session_id": sid,
                "path": path,
                "n_points": int(n),
                "n_segments": len(segs),
                "t0": round(t0, 4),
                "t_max": round(t_end - t0, 4),
                "background": [
                    [round(float(p[0]) - t0, 4),
                     round(float(p[1]), 3), round(float(p[2]), 3), round(float(p[3]), 3)]
                    for p in bg
                ],
                "segments": _segs_payload(segs, arr, t0),
            })
    return {"rows": rows, "v1_defaults": V1_DEFAULTS}


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Segmenter viewer — filters + playback</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; background: #141414; color: #e8e8e8; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; overflow: hidden; }
  #wrap { position: relative; height: 100vh; width: 100vw; }
  #top, #filters, #scrubber, #scene, #info { position: absolute; left: 0; right: 0; box-sizing: border-box; }
  #top      { top: 0;      height: 38px; }
  #filters  { top: 38px;   height: 50px; }
  #scrubber { top: 88px;   height: 32px; }
  #scene    { top: 120px;  bottom: 240px; }
  #info     { bottom: 0;   height: 240px; }
  #top { padding: 8px 12px; background: #1f1f1f; border-bottom: 1px solid #333; display: flex; align-items: center; gap: 12px; flex-wrap: nowrap; overflow: hidden; }
  #top label { color: #888; }
  select, button, input[type=range] { background: #2c2c2c; color: #e8e8e8; border: 1px solid #444; border-radius: 3px; font-family: inherit; font-size: 12px; }
  select { padding: 4px 8px; min-width: 280px; }
  button { padding: 4px 10px; cursor: pointer; }
  button:hover { background: #3a3a3a; }
  button.active { background: #3a5; border-color: #4c6; }
  .meta strong { color: #fff; }
  #filters { padding: 6px 12px; background: #1a1a1a; border-bottom: 1px solid #2a2a2a; display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px 24px; }
  .slider { display: flex; flex-direction: column; gap: 2px; }
  .slider .row { display: flex; align-items: center; gap: 6px; }
  .slider label { font-size: 10.5px; color: #aaa; text-transform: uppercase; letter-spacing: 0.5px; }
  .slider input[type=range] { flex: 1; }
  .slider .val { color: #fff; min-width: 70px; text-align: right; font-variant-numeric: tabular-nums; }
  #scrubber { padding: 6px 12px; background: #181818; border-bottom: 1px solid #2a2a2a; display: flex; align-items: center; gap: 10px; }
  #scrubber input[type=range] { flex: 1; }
  #scrubber .t { font-variant-numeric: tabular-nums; min-width: 130px; color: #fff; }
  #scene canvas { display: block; width: 100%; height: 100%; }
  #info { overflow-y: auto; padding: 6px 12px; background: #181818; border-top: 1px solid #2a2a2a; font-size: 11px; }
  #info table { width: 100%; border-collapse: collapse; }
  #info th, #info td { text-align: left; padding: 3px 7px; border-bottom: 1px solid #242424; white-space: nowrap; }
  #info th { color: #888; font-weight: normal; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.5px; }
  #info tr.dropped { opacity: 0.4; }
  #info tr.dropped td.tag { color: #888; text-decoration: line-through; }
  #info td.az.bad { color: #f76; }
  #info td.az.good { color: #6c6; }
  .swatch { display: inline-block; width: 12px; height: 9px; margin-right: 5px; vertical-align: middle; border-radius: 2px; }
</style>
</head>
<body>
<div id="wrap">
  <div id="top">
    <label>session: <select id="sess"></select></label>
    <span class="meta">visible: <strong id="m_visible">0</strong>/<strong id="m_total">0</strong></span>
    <span class="meta">dropped: <strong id="m_dropped">0</strong></span>
    <button id="reset">reset filters</button>
    <span style="flex:1"></span>
    <button id="play">▶ play</button>
    <label>speed: <select id="speed"><option>0.25</option><option>0.5</option><option selected>1</option><option>2</option><option>4</option></select>×</label>
  </div>
  <div id="filters">
    <div class="slider"><label>min duration (ms)</label>
      <div class="row"><input type="range" id="f_dur"><span class="val" id="v_dur"></span></div></div>
    <div class="slider"><label>min speed (m/s)</label>
      <div class="row"><input type="range" id="f_vmin"><span class="val" id="v_vmin"></span></div></div>
    <div class="slider"><label>max speed (m/s)</label>
      <div class="row"><input type="range" id="f_vmax"><span class="val" id="v_vmax"></span></div></div>
    <div class="slider"><label>min n_points</label>
      <div class="row"><input type="range" id="f_n"><span class="val" id="v_n"></span></div></div>
    <div class="slider"><label>min path length (m)</label>
      <div class="row"><input type="range" id="f_path"><span class="val" id="v_path"></span></div></div>
  </div>
  <div id="scrubber">
    <button id="rewind">⏮</button>
    <input type="range" id="t_slider" min="0" max="1000" value="0">
    <span class="t" id="t_label">t = 0.00 / 0.00 s</span>
  </div>
  <div id="scene"></div>
  <div id="info">
    <table><thead><tr>
      <th>seg</th><th>t_start</th><th>t_end</th><th>n</th><th>dur</th><th>|v0|</th>
      <th>path_len</th><th>rmse</th><th>az_fit</th><th>z range</th>
    </tr></thead><tbody id="seglist"></tbody></table>
  </div>
</div>

<script type="importmap">
{ "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.min.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
} }
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";

const PAYLOAD = __PAYLOAD__;
const V1 = PAYLOAD.v1_defaults;
const G = -9.81;
const SEG_PALETTE = [
  0xff3b30, 0xff9500, 0xffcc00, 0x34c759, 0x00d4ff, 0x007aff,
  0xaf52de, 0xff2d92, 0x5ac8fa, 0xa2845e, 0xffd60a, 0x32d74b,
];

// --- scene setup ---
const sceneDiv = document.getElementById("scene");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
sceneDiv.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x141414);
const camera = new THREE.PerspectiveCamera(55, 1, 0.05, 200);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
const grid = new THREE.GridHelper(10, 20, 0x444, 0x282828);
grid.rotation.x = Math.PI / 2; scene.add(grid);
scene.add(new THREE.AxesHelper(0.5));
const dataGroup = new THREE.Group(); scene.add(dataGroup);
const playheadGroup = new THREE.Group(); scene.add(playheadGroup);

// Tracking ball — interpolated through raw triangulated points.
const trackBallGeom = new THREE.SphereGeometry(0.06, 16, 12);
const trackBallMat  = new THREE.MeshBasicMaterial({color: 0xffffff});
const trackBall     = new THREE.Mesh(trackBallGeom, trackBallMat);
trackBall.visible   = false;
scene.add(trackBall);
let bgTimeline = []; // sorted [t, x, y, z] for current row

function clearGroup(g) {
  while (g.children.length) {
    const c = g.children[0]; g.remove(c);
    if (c.geometry) c.geometry.dispose();
    if (c.material) {
      if (Array.isArray(c.material)) c.material.forEach(m=>m.dispose());
      else c.material.dispose();
    }
  }
}
function pointsCloud(pts, color, size) {
  const arr = new Float32Array(pts.length * 3);
  for (let i = 0; i < pts.length; ++i) {
    arr[i*3]=pts[i][0]; arr[i*3+1]=pts[i][1]; arr[i*3+2]=pts[i][2];
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(arr, 3));
  return new THREE.Points(g, new THREE.PointsMaterial({color, size, sizeAttenuation:true, transparent:true, opacity:0.85}));
}
function thickLine(curve, color, size) {
  const flat = []; for (const p of curve) flat.push(p[0],p[1],p[2]);
  const g = new LineGeometry(); g.setPositions(flat);
  const m = new LineMaterial({color, linewidth:5, worldUnits:false});
  m.resolution.set(size.x, size.y);
  const line = new Line2(g, m); line.computeLineDistances();
  line._labMat = m;
  return line;
}
function autoFrame(box) {
  box.expandByScalar(0.3);
  const c = new THREE.Vector3(); box.getCenter(c);
  const s = new THREE.Vector3(); box.getSize(s);
  const dist = Math.max(s.x,s.y,s.z,1) * 1.7;
  camera.position.copy(c).addScaledVector(new THREE.Vector3(1,-1,0.7).normalize(), dist);
  controls.target.copy(c); camera.lookAt(c); camera.updateProjectionMatrix();
}

// --- state ---
let currentRow = null;
let allSegs = [];
let visibleSegs = [];
let currentT = 0;
let playing = false;
let lastFrameT = performance.now();

// --- sliders ---
const sliders = [
  {id:"f_dur",  label:"v_dur",  min:0, max:2000, step:5,    def:V1.min_duration_s*1000,  fmt:v=>`${Math.round(v)} ms`,  key:"durationMs"},
  {id:"f_vmin", label:"v_vmin", min:0, max:30,   step:0.1,  def:V1.v_min_mps,             fmt:v=>`${(+v).toFixed(1)}`,  key:"vmin"},
  {id:"f_vmax", label:"v_vmax", min:5, max:100,  step:0.5,  def:V1.v_max_mps,             fmt:v=>`${(+v).toFixed(1)}`,  key:"vmax"},
  {id:"f_n",    label:"v_n",    min:4, max:40,   step:1,    def:V1.min_seg_len,           fmt:v=>`${Math.round(v)}`,    key:"nmin"},
  {id:"f_path", label:"v_path", min:0, max:5,    step:0.05, def:V1.min_displacement_m,    fmt:v=>`${(+v).toFixed(2)} m`,key:"pathmin"},
];
const filterValues = {};
for (const s of sliders) {
  const el = document.getElementById(s.id);
  el.min = s.min; el.max = s.max; el.step = s.step; el.value = s.def;
  filterValues[s.key] = +s.def;
  document.getElementById(s.label).textContent = s.fmt(+s.def);
  el.addEventListener("input", () => {
    filterValues[s.key] = +el.value;
    document.getElementById(s.label).textContent = s.fmt(+el.value);
    applyFilters();
  });
}
document.getElementById("reset").addEventListener("click", () => {
  for (const s of sliders) {
    const el = document.getElementById(s.id);
    el.value = s.def;
    filterValues[s.key] = +s.def;
    document.getElementById(s.label).textContent = s.fmt(+s.def);
  }
  applyFilters();
});

// --- session selector ---
const sel = document.getElementById("sess");
PAYLOAD.rows.sort((a,b) => b.n_segments - a.n_segments);
for (const row of PAYLOAD.rows) {
  const opt = document.createElement("option");
  opt.value = row.id;
  opt.textContent = `${row.session_id} / ${row.path}   pts=${row.n_points} segs=${row.n_segments}`;
  sel.appendChild(opt);
}
sel.addEventListener("change", () => loadSession(PAYLOAD.rows.find(r=>r.id===sel.value)));

function loadSession(row) {
  if (!row) return;
  currentRow = row;
  clearGroup(dataGroup); clearGroup(playheadGroup);
  document.getElementById("m_total").textContent = row.n_segments;
  // Background
  const bgXYZ = row.background.map(p => [p[1], p[2], p[3]]);
  dataGroup.add(pointsCloud(bgXYZ, 0x666666, 0.022));
  const box = new THREE.Box3();
  for (const p of bgXYZ) box.expandByPoint(new THREE.Vector3(p[0], p[1], p[2]));
  if (box.isEmpty()) box.set(new THREE.Vector3(-1,-1,0), new THREE.Vector3(1,1,1));
  autoFrame(box);
  allSegs = row.segments.map((seg, i) => ({
    seg, color: SEG_PALETTE[i % SEG_PALETTE.length], idx: i,
    dropped: false, hidden: false
  }));
  // Build sorted timeline from raw bg points for the tracking ball.
  bgTimeline = row.background.map(p => [p[0], p[1], p[2], p[3]])
                              .sort((a,b) => a[0] - b[0]);
  // Clamp slider to the time window where data exists; if there are
  // segments, jump initial t to just before the first segment so the
  // user lands in the action.
  const tFirst = bgTimeline.length ? bgTimeline[0][0] : 0;
  const tLast  = bgTimeline.length ? bgTimeline[bgTimeline.length-1][0] : row.t_max;
  const tSliderEl = document.getElementById("t_slider");
  tSliderEl.min = Math.floor(tFirst * 1000);
  tSliderEl.max = Math.ceil(tLast * 1000);
  const firstSegT = row.segments.length
    ? Math.min(...row.segments.map(s=>s.t_start)) : tFirst;
  currentT = Math.max(tFirst, firstSegT - 0.1);
  tSliderEl.value = Math.round(currentT * 1000);
  setLabel();
  applyFilters();
}

// --- filters + dedupe ---
function compareForDedupe(a, b) {
  // User-specified: prefer LONGER PATH first.
  // Tiebreak: more points, then lower rmse.
  if (a.seg.path_length_m !== b.seg.path_length_m) return b.seg.path_length_m - a.seg.path_length_m;
  if (a.seg.n !== b.seg.n) return b.seg.n - a.seg.n;
  return a.seg.rmse_mm - b.seg.rmse_mm;
}
function applyFilters() {
  const f = filterValues;
  const passing = [];
  for (const obj of allSegs) {
    obj.dropped = false; obj.hidden = false;
    const s = obj.seg;
    if (s.duration_s * 1000 < f.durationMs) { obj.hidden = true; continue; }
    if (s.speed_mps < f.vmin)    { obj.hidden = true; continue; }
    if (s.speed_mps > f.vmax)    { obj.hidden = true; continue; }
    if (s.n < f.nmin)            { obj.hidden = true; continue; }
    if (s.path_length_m < f.pathmin) { obj.hidden = true; continue; }
    passing.push(obj);
  }
  // Time-overlap dedupe — drop the worse of any overlapping pair.
  passing.sort(compareForDedupe);
  const kept = [];
  for (const cand of passing) {
    let conflict = false;
    for (const k of kept) {
      const A = cand.seg, B = k.seg;
      if (A.t_start < B.t_end && B.t_start < A.t_end) { conflict = true; break; }
    }
    if (conflict) cand.dropped = true; else kept.push(cand);
  }
  visibleSegs = kept;
  rebuildSceneSegments();
  rebuildInfoTable();
  document.getElementById("m_visible").textContent = visibleSegs.length;
  document.getElementById("m_dropped").textContent = passing.length - kept.length;
}

function rebuildSceneSegments() {
  // Keep first child (background); remove the rest.
  while (dataGroup.children.length > 1) {
    const c = dataGroup.children[dataGroup.children.length - 1];
    dataGroup.remove(c);
    if (c.geometry) c.geometry.dispose();
    if (c.material) c.material.dispose();
  }
  const sz = new THREE.Vector2(); renderer.getSize(sz);
  for (const obj of visibleSegs) {
    dataGroup.add(thickLine(obj.seg.curve, obj.color, sz));
    const mp = obj.seg.members.map(m => [m[1], m[2], m[3]]);
    dataGroup.add(pointsCloud(mp, obj.color, 0.05));
  }
  updatePlayhead();
}
function rebuildInfoTable() {
  const tbody = document.getElementById("seglist");
  tbody.innerHTML = "";
  const ordered = [...visibleSegs, ...allSegs.filter(o => o.dropped && !o.hidden)];
  for (const obj of ordered) {
    const s = obj.seg;
    const colorHex = "#" + obj.color.toString(16).padStart(6, "0");
    const azGood = Math.abs(s.az_fit - G) / Math.abs(G) <= 0.5;
    tbody.insertAdjacentHTML("beforeend",
      `<tr class="${obj.dropped?'dropped':''}">
        <td class="tag"><span class="swatch" style="background:${colorHex}"></span>seg#${obj.idx}</td>
        <td>${s.t_start.toFixed(3)}s</td><td>${s.t_end.toFixed(3)}s</td>
        <td>${s.n}</td><td>${(s.duration_s*1000).toFixed(0)}ms</td>
        <td>${s.speed_mps.toFixed(2)} m/s</td>
        <td>${s.path_length_m.toFixed(2)} m</td>
        <td>${s.rmse_mm.toFixed(1)} mm</td>
        <td class="az ${azGood?'good':'bad'}">${s.az_fit.toFixed(2)} m/s²</td>
        <td>[${s.z_min.toFixed(2)}, ${s.z_max.toFixed(2)}]</td>
      </tr>`);
  }
}

// --- playback ---
function predictAt(seg, t) {
  if (t < seg.t_start || t > seg.t_end) return null;
  const tau = t - seg.t_anchor;
  return [
    seg.p0[0] + seg.v0[0]*tau,
    seg.p0[1] + seg.v0[1]*tau,
    seg.p0[2] + seg.v0[2]*tau + 0.5*G*tau*tau,
  ];
}
function updatePlayhead() {
  clearGroup(playheadGroup);
  for (const obj of visibleSegs) {
    const p = predictAt(obj.seg, currentT);
    if (!p) continue;
    const geom = new THREE.SphereGeometry(0.05, 16, 10);
    const mat = new THREE.MeshBasicMaterial({color: obj.color});
    const mesh = new THREE.Mesh(geom, mat);
    mesh.position.set(p[0], p[1], p[2]);
    playheadGroup.add(mesh);
  }
  // Tracking ball — find nearest two bg points by time and interpolate.
  if (bgTimeline.length >= 2) {
    let lo = 0, hi = bgTimeline.length - 1;
    if (currentT <= bgTimeline[0][0])      { trackBall.position.set(bgTimeline[0][1], bgTimeline[0][2], bgTimeline[0][3]); }
    else if (currentT >= bgTimeline[hi][0]){ trackBall.position.set(bgTimeline[hi][1], bgTimeline[hi][2], bgTimeline[hi][3]); }
    else {
      while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (bgTimeline[mid][0] <= currentT) lo = mid; else hi = mid;
      }
      const a = bgTimeline[lo], b = bgTimeline[hi];
      const dt = b[0] - a[0];
      // Only interpolate if neighbours are within 100ms; else snap to nearer.
      if (dt < 0.1) {
        const u = (currentT - a[0]) / dt;
        trackBall.position.set(a[1] + (b[1]-a[1])*u, a[2] + (b[2]-a[2])*u, a[3] + (b[3]-a[3])*u);
      } else {
        const near = (currentT - a[0]) < (b[0] - currentT) ? a : b;
        trackBall.position.set(near[1], near[2], near[3]);
      }
    }
    trackBall.visible = true;
  } else {
    trackBall.visible = false;
  }
}
function tBounds() {
  const lo = +tSlider.min / 1000, hi = +tSlider.max / 1000;
  return [lo, hi];
}
function setLabel() {
  const [lo, hi] = tBounds();
  document.getElementById("t_label").textContent = `t = ${currentT.toFixed(2)} / ${hi.toFixed(2)} s`;
}
const tSlider = document.getElementById("t_slider");
tSlider.addEventListener("input", () => {
  currentT = +tSlider.value / 1000;
  setLabel();
  updatePlayhead();
});
document.getElementById("rewind").addEventListener("click", () => {
  const [lo] = tBounds();
  currentT = lo; tSlider.value = +tSlider.min;
  setLabel();
  updatePlayhead();
});
const playBtn = document.getElementById("play");
playBtn.addEventListener("click", () => {
  playing = !playing;
  playBtn.textContent = playing ? "⏸ pause" : "▶ play";
  playBtn.classList.toggle("active", playing);
  lastFrameT = performance.now();
});

function tick(now) {
  if (playing && currentRow) {
    const speed = +document.getElementById("speed").value;
    const dt = (now - lastFrameT) / 1000 * speed;
    lastFrameT = now;
    currentT += dt;
    const [lo, hi] = tBounds();
    if (currentT > hi) currentT = lo;
    tSlider.value = Math.round(currentT * 1000);
    setLabel();
    updatePlayhead();
  } else {
    lastFrameT = now;
  }
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}
function resize() {
  const r = sceneDiv.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return;
  renderer.setSize(r.width, r.height);
  camera.aspect = r.width / r.height; camera.updateProjectionMatrix();
  dataGroup.traverse(obj => { if (obj._labMat) obj._labMat.resolution.set(r.width, r.height); });
}
window.addEventListener("resize", resize);
resize();
if (PAYLOAD.rows.length) {
  sel.value = PAYLOAD.rows[0].id;
  loadSession(PAYLOAD.rows[0]);
}
requestAnimationFrame(tick);
</script>
</body>
</html>
"""


def main():
    print("Building corpus payload (v1 with permissive gates)...")
    payload = build_payload()
    print(f"  {len(payload['rows'])} (session, path) rows")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "index.html"
    out.write_text(HTML.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":"))), encoding="utf-8")
    print(f"\nWrote {out}  ({out.stat().st_size/1e6:.2f} MB)")
    print(f"Open with: open {out}")


if __name__ == "__main__":
    main()
