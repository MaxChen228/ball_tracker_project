"""Plotly 3D scene renderer for /viewer/{session_id} and the dashboard
canvas — extracted from viewer.py.

Returns a self-contained HTML string with the ground plane, world axes,
per-camera marker + local RGB triad + forward arrow, ray bundles, and
the triangulated trajectory coloured by time. Loads Plotly.js from CDN
so the file stays tiny and opens in any modern browser without a build
step. `reconstruct.Scene` is the stable input hand-off.

Colour palette mirrors the `PHYSICS_LAB` design system (warm neutrals +
semantic roles) — `contra` for camera A, `dual` for camera B, `accent`
for the triangulated trajectory, `borderL` for the plate mesh, `ink`
at 40% for world axes.
"""
from __future__ import annotations

from reconstruct import Scene

# Design-system tokens reused across the dashboard and this scene so the
# 3D canvas visually belongs to the same page as the sidebar.
_BG = "#F8F7F4"
_SURFACE = "#FCFBFA"
_INK = "#2A2520"
_INK_40 = "rgba(42, 37, 32, 0.4)"
_SUB = "#7A756C"
_BORDER_BASE = "#DBD6CD"
_BORDER_L = "#E8E4DB"
_CONTRA = "#4A6B8C"   # Cam A — semantic blue
_DUAL = "#D35400"     # Cam B — semantic warm
_DEV = "#C0392B"      # semantic red (axis X / error)
_ACCENT = "#E6B300"   # interactive / triangulated trajectory highlight

_CAMERA_COLORS = {
    "A": _CONTRA,
    "B": _DUAL,
}
_FALLBACK_CAMERA_COLOR = _SUB
_GROUND_HALF_EXTENT_M = 1.5   # ground mesh drawn from (-1.5, -1.5) to (+1.5, +1.5)
_WORLD_AXIS_LEN_M = 0.3
_CAMERA_AXIS_LEN_M = 0.25
_CAMERA_FORWARD_ARROW_M = 0.5

# Home-plate pentagon (world frame, Z=0). Vertices match the iOS
# CalibrationViewController's `markerWorldPoints`: front edge is at Y=0
# facing the pitcher, back tip is toward the catcher at Y=+0.432 m.
# Drawn with a filled mesh + outlined pentagon so it's clearly readable
# on top of the dim ground plane.
_PLATE_WIDTH_M = 0.432
_PLATE_SHOULDER_Y_M = 0.216
_PLATE_TIP_Y_M = 0.432
_PLATE_X = [
    -_PLATE_WIDTH_M / 2,   # FL
    +_PLATE_WIDTH_M / 2,   # FR
    +_PLATE_WIDTH_M / 2,   # RS
    0.0,                   # BT
    -_PLATE_WIDTH_M / 2,   # LS
]
_PLATE_Y = [
    0.0,
    0.0,
    _PLATE_SHOULDER_Y_M,
    _PLATE_TIP_Y_M,
    _PLATE_SHOULDER_Y_M,
]


def render_scene_html(scene: Scene) -> str:
    return _build_figure(scene).to_html(include_plotlyjs="cdn", full_html=True)


def render_scene_div(scene: Scene, div_id: str = "canvas-scene") -> str:
    """HTML fragment (no <html> wrapper) for embedding the 3D scene inside
    the dashboard shell. Plotly.js is assumed to be loaded once at the top
    of the host document — each fragment only ships its trace data."""
    return _build_figure(scene).to_html(
        include_plotlyjs=False, full_html=False, div_id=div_id
    )


def render_viewer_html(
    scene: Scene,
    videos: list[tuple[str, str, float]],
    health: dict,
) -> str:
    """Full /viewer/{sid} post-mortem page.

    Layout (top to bottom):

      1. Header bar — BALL_TRACKER brand + session id + back link.
      2. Health banner — the page's first-class citizen. Per-camera rows
         answer "did A/B reach here, calibrated, time-synced, how many
         frames, how many detections?", plus a session-level triangulation
         chip + explicit failure-reason strip when something stopped the
         pipeline short. This is what makes the viewer a diagnostic tool
         rather than a 3D novelty.
      3. Main work area — two-column flex: 3D scene on the left, CAM A
         stacked above CAM B on the right. Width split is adaptive: a
         session with a triangulated trajectory gives the 3D scene more
         room (since that's the payoff visual); a session without one
         shrinks the scene so the videos — the only surviving evidence —
         dominate.
      4. Shared timeline footer — ALL/PLAYBACK toggle, play/pause, single
         scrubber that drives both videos and 3D playback together.

    `videos` is `[(camera_id, url, t_rel_offset_s), ...]` where
    `t_rel_offset_s = video_start_pts_s − sync_anchor_timestamp_s` for
    that camera. Each video's `currentTime = t_rel − t_rel_offset_s`, so
    A and B stay locked to the chirp anchor even when their phones
    started recording at different wall-clock moments.
    """
    import json as _json

    fig = _build_figure(scene)
    fig_json = _json.loads(fig.to_json())
    layout_json = _json.dumps(fig_json.get("layout", {}))
    static_traces = [
        t for t in fig_json.get("data", [])
        if not (t.get("meta") or {}).get("trace_kind")
    ]
    static_traces_json = _json.dumps(static_traces)

    scene_json = _json.dumps(scene.to_dict())
    camera_colors_json = _json.dumps(_CAMERA_COLORS)
    fallback_color_json = _json.dumps(_FALLBACK_CAMERA_COLOR)
    accent_color_json = _json.dumps(_ACCENT)
    videos_json = _json.dumps(
        [{"camera_id": cam, "url": url, "t_rel_offset_s": off}
         for (cam, url, off) in videos]
    )
    has_triangulated = bool(scene.triangulated)

    # Adaptive split: triangulation succeeded → 3D gets more (it's the
    # whole point of the two-phone rig); no triangulation → videos get
    # more (they're the only evidence left).
    scene_flex = "3 1 0" if has_triangulated else "2 1 0"
    videos_flex = "2 1 0" if has_triangulated else "3 1 0"

    videos_by_cam = {cam: (url, off) for cam, url, off in videos}
    video_cells = "".join(
        _video_cell_html(cam, videos_by_cam.get(cam))
        for cam in ("A", "B")
    )

    health_html = _health_banner_html(health)
    header_meta = _header_meta_text(health)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Session {scene.session_id}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: {_BG}; --surface: {_SURFACE}; --ink: {_INK}; --sub: {_SUB};
    --border-base: {_BORDER_BASE}; --border-l: {_BORDER_L};
    --contra: {_CONTRA}; --dual: {_DUAL}; --dev: {_DEV}; --accent: {_ACCENT};
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; padding:0; height:100%; background:var(--bg);
    color:var(--ink); font-family:var(--sans); font-weight:300; line-height:1.6;
    -webkit-font-smoothing:antialiased; }}
  .viewer {{ display:flex; flex-direction:column; min-height:100vh; }}

  /* --- Header (52px brand bar) --- */
  .nav {{ height:52px; flex:0 0 52px; background:var(--surface);
    border-bottom:1px solid var(--border-base); display:flex;
    align-items:center; padding:0 24px; gap:20px; }}
  .nav .brand {{ font-family:var(--mono); font-weight:700; font-size:14px;
    letter-spacing:0.16em; color:var(--ink); }}
  .nav .brand .dot {{ display:inline-block; width:7px; height:7px;
    background:var(--ink); margin-right:10px; vertical-align:middle; }}
  .nav .meta {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.08em; text-transform:uppercase; color:var(--sub);
    display:flex; gap:16px; }}
  .nav .meta .v {{ color:var(--ink); font-weight:500; }}
  .nav .back {{ margin-left:auto; font-family:var(--mono); font-size:11px;
    letter-spacing:0.12em; text-transform:uppercase; color:var(--sub);
    text-decoration:none; }}
  .nav .back:hover {{ color:var(--ink); }}

  /* --- Health banner --- */
  .health {{ flex:0 0 auto; background:var(--surface);
    border-bottom:1px solid var(--border-base); padding:18px 24px;
    display:flex; flex-direction:column; gap:12px; }}
  .health-row {{ display:grid; grid-template-columns:1fr 1fr 220px;
    gap:16px; align-items:stretch; }}
  .cam-card {{ border:1px solid var(--border-base); border-radius:4px;
    padding:12px 16px; background:var(--bg);
    display:flex; flex-direction:column; gap:8px; }}
  .cam-card.received {{ background:var(--surface); }}
  .cam-card.missing {{ opacity:0.75; }}
  .cam-head {{ display:flex; align-items:center; gap:10px; }}
  .cam-badge {{ font-family:var(--mono); font-weight:600; font-size:11px;
    letter-spacing:0.18em; padding:3px 10px; border:1px solid;
    border-radius:2px; }}
  .cam-badge.A {{ color:var(--contra); border-color:var(--contra); }}
  .cam-badge.B {{ color:var(--dual); border-color:var(--dual); }}
  .cam-state {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.08em; text-transform:uppercase; }}
  .cam-state.ok {{ color:var(--ink); }}
  .cam-state.bad {{ color:var(--dev); }}
  .cam-checks {{ display:flex; flex-wrap:wrap; gap:6px 14px; }}
  .check {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.04em; color:var(--sub);
    display:inline-flex; align-items:center; gap:6px; }}
  .check .mark {{ font-weight:700; width:12px; display:inline-block;
    text-align:center; }}
  .check.pass {{ color:var(--ink); }}
  .check.pass .mark {{ color:var(--contra); }}
  .check.fail .mark {{ color:var(--dev); }}
  .cam-stats {{ font-family:var(--mono); font-size:12px; color:var(--ink);
    letter-spacing:0.02em; }}
  .cam-stats .n {{ font-weight:500; }}
  .cam-stats .of {{ color:var(--sub); }}

  .tri-card {{ border:1px solid var(--border-base); border-radius:4px;
    padding:12px 16px; background:var(--bg); display:flex;
    flex-direction:column; justify-content:center; gap:4px; }}
  .tri-card.ok {{ background:var(--surface); border-color:var(--accent); }}
  .tri-title {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.18em; text-transform:uppercase; color:var(--sub); }}
  .tri-count {{ font-family:var(--mono); font-size:28px; font-weight:500;
    color:var(--ink); line-height:1; letter-spacing:0.02em; }}
  .tri-count.zero {{ color:var(--sub); }}
  .tri-note {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.04em; color:var(--sub); }}

  .fail-strip {{ font-family:var(--mono); font-size:12px;
    letter-spacing:0.02em; padding:8px 12px; border-radius:2px;
    border:1px solid var(--dev); color:var(--dev);
    background:rgba(192, 57, 43, 0.06); display:flex;
    align-items:center; gap:10px; }}
  .fail-strip .icon {{ font-weight:700; }}

  /* --- Main work area --- */
  .work {{ flex:1 1 auto; display:flex; min-height:460px;
    border-bottom:1px solid var(--border-base); }}
  .scene-col {{ flex:{scene_flex}; min-width:420px; position:relative;
    border-right:1px solid var(--border-base); background:var(--bg); }}
  #scene {{ position:absolute; inset:0; }}
  .videos-col {{ flex:{videos_flex}; min-width:320px; display:flex;
    flex-direction:column; gap:1px; background:var(--border-base); }}
  .vid-cell {{ flex:1 1 0; background:var(--surface); padding:10px 14px;
    display:flex; flex-direction:column; gap:6px; min-height:0; }}
  .vid-head {{ display:flex; align-items:center; gap:10px; }}
  .vid-label {{ font-family:var(--mono); font-size:10px; font-weight:600;
    letter-spacing:0.18em; border:1px solid; padding:2px 8px;
    border-radius:2px; }}
  .vid-hint {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.06em; color:var(--sub); text-transform:uppercase; }}
  .vid-frame {{ flex:1 1 auto; min-height:0; display:flex;
    align-items:center; justify-content:center; background:#000;
    border-radius:2px; overflow:hidden; }}
  .vid-frame video {{ width:100%; height:100%; object-fit:contain;
    display:block; }}
  .vid-frame.empty {{ background:var(--bg); border:1px dashed var(--border-base);
    color:var(--sub); font-family:var(--mono); font-size:11px;
    letter-spacing:0.12em; text-transform:uppercase; }}

  /* --- Timeline footer --- */
  .timeline {{ flex:0 0 56px; background:var(--surface);
    display:flex; align-items:center; gap:14px; padding:0 24px;
    font-family:var(--mono); font-size:12px; color:var(--sub); }}
  .timeline button {{ padding:5px 14px; font:inherit; font-size:11px;
    letter-spacing:0.12em; text-transform:uppercase;
    border:1px solid var(--border-base); background:var(--bg);
    color:var(--ink); border-radius:2px; cursor:pointer; min-width:60px; }}
  .timeline input[type=range] {{ flex:1 1 auto; accent-color:var(--ink); }}
  .timeline .tlabel {{ min-width:140px; text-align:right; color:var(--ink);
    font-weight:500; }}
  .mode-toggle {{ display:inline-flex; border:1px solid var(--border-base);
    border-radius:2px; overflow:hidden; }}
  .mode-toggle button {{ padding:5px 12px; border:none; background:transparent;
    color:var(--sub); cursor:pointer; min-width:auto; border-radius:0; }}
  .mode-toggle button.active {{ background:var(--ink); color:var(--surface); }}
</style>
</head><body>
<div class="viewer">
  <div class="nav">
    <span class="brand"><span class="dot"></span>BALL_TRACKER</span>
    <span class="meta">{header_meta}</span>
    <a class="back" href="/">&larr; dashboard</a>
  </div>
  {health_html}
  <div class="work">
    <div class="scene-col"><div id="scene"></div></div>
    <div class="videos-col">{video_cells}</div>
  </div>
  <div class="timeline">
    <button id="play-btn" type="button">Play</button>
    <input id="scrubber" type="range" min="0" max="1000" value="0" step="1" />
    <span id="time-label" class="tlabel">t=0.000s</span>
    <div class="mode-toggle" role="tablist">
      <button id="mode-all" class="active" type="button">All</button>
      <button id="mode-playback" type="button">Playback</button>
    </div>
  </div>
</div>
<script id="viewer-data" type="application/json">{{
  "scene": {scene_json},
  "layout": {layout_json},
  "static_traces": {static_traces_json},
  "camera_colors": {camera_colors_json},
  "fallback_color": {fallback_color_json},
  "accent_color": {accent_color_json},
  "videos": {videos_json},
  "has_triangulated": {str(has_triangulated).lower()}
}}</script>
<script>
(() => {{
  const DATA = JSON.parse(document.getElementById("viewer-data").textContent);
  const SCENE = DATA.scene;
  const STATIC = DATA.static_traces || [];
  const LAYOUT = DATA.layout;
  const CAM_COLOR = DATA.camera_colors || {{}};
  const FALLBACK = DATA.fallback_color;
  const ACCENT = DATA.accent_color;
  const VIDEO_META = DATA.videos || [];
  const HAS_TRIANGULATED = DATA.has_triangulated;

  const sceneDiv = document.getElementById("scene");
  const playBtn = document.getElementById("play-btn");
  const scrubber = document.getElementById("scrubber");
  const tLabel = document.getElementById("time-label");
  const modeAll = document.getElementById("mode-all");
  const modePlayback = document.getElementById("mode-playback");

  const vids = Array.from(document.querySelectorAll("video[data-cam]"));
  const offsetByCam = Object.fromEntries(VIDEO_META.map(v => [v.camera_id, v.t_rel_offset_s]));

  // Master timeline is anchor-relative (t_rel_s). Span it from the
  // earliest observed data point to the latest; fall back to a trivial
  // 1 s window if the scene is empty.
  const allTs = [];
  for (const arr of Object.values(SCENE.ground_traces || {{}})) {{
    for (const p of arr) allTs.push(p.t_rel_s);
  }}
  for (const r of SCENE.rays || []) allTs.push(r.t_rel_s);
  for (const p of SCENE.triangulated || []) allTs.push(p.t_rel_s);
  let tMin = allTs.length ? Math.min(...allTs) : 0.0;
  let tMax = allTs.length ? Math.max(...allTs) : 1.0;
  if (tMax - tMin < 0.05) tMax = tMin + 0.05;

  let mode = "all";
  let currentT = tMin;
  let rafPending = false;

  // --- Dynamic trace builders. Each takes the master time cutoff and
  //     returns Scatter3d-shaped objects (plain JS objects; Plotly
  //     accepts them as trace defs). In "all" mode cutoff is +inf.

  function ballDetectedRaysUpTo(rays, t) {{
    const xs = [], ys = [], zs = [];
    for (const r of rays) {{
      if (r.t_rel_s > t) continue;
      xs.push(r.origin[0], r.endpoint[0], null);
      ys.push(r.origin[1], r.endpoint[1], null);
      zs.push(r.origin[2], r.endpoint[2], null);
    }}
    return {{xs, ys, zs}};
  }}

  function buildDynamicTraces(cutoff) {{
    const out = [];

    // Rays per camera.
    const raysByCam = {{}};
    for (const r of (SCENE.rays || [])) {{
      (raysByCam[r.camera_id] = raysByCam[r.camera_id] || []).push(r);
    }}
    for (const [cam, rays] of Object.entries(raysByCam)) {{
      const color = CAM_COLOR[cam] || FALLBACK;
      const {{xs, ys, zs}} = ballDetectedRaysUpTo(rays, cutoff);
      if (!xs.length) continue;
      out.push({{
        type: "scatter3d",
        x: xs, y: ys, z: zs,
        mode: "lines",
        line: {{color: color, width: 2}},
        opacity: 0.35,
        name: `Rays ${{cam}} (${{Math.floor(xs.length / 3)}})`,
        hoverinfo: "skip",
      }});
    }}

    // Ground traces per camera — line + markers, dashed if a triangulated
    // trajectory is also shown so the 3D trajectory visually dominates.
    for (const [cam, trace] of Object.entries(SCENE.ground_traces || {{}})) {{
      const filtered = trace.filter(p => p.t_rel_s <= cutoff);
      if (!filtered.length) continue;
      const color = CAM_COLOR[cam] || FALLBACK;
      out.push({{
        type: "scatter3d",
        x: filtered.map(p => p.x),
        y: filtered.map(p => p.y),
        z: filtered.map(p => p.z),
        mode: "lines+markers",
        line: {{color: color, width: 3, dash: HAS_TRIANGULATED ? "dash" : "solid"}},
        marker: {{size: 3, color: color}},
        opacity: HAS_TRIANGULATED ? 0.45 : 0.7,
        name: `Ground trace ${{cam}} (${{filtered.length}} pts)`,
      }});
    }}

    // Triangulated trajectory (A+B paired).
    const triPts = (SCENE.triangulated || []).filter(p => p.t_rel_s <= cutoff);
    if (triPts.length) {{
      const ts = triPts.map(p => p.t_rel_s);
      out.push({{
        type: "scatter3d",
        x: triPts.map(p => p.x),
        y: triPts.map(p => p.y),
        z: triPts.map(p => p.z),
        mode: "lines+markers",
        line: {{color: ACCENT, width: 4}},
        marker: {{size: 4, color: ts, colorscale: "Cividis", showscale: true,
          colorbar: {{title: "t (s)"}}}},
        name: `3D trajectory (${{triPts.length}} pts)`,
      }});
    }}

    return out;
  }}

  function drawScene() {{
    const cutoff = mode === "all" ? Infinity : currentT;
    Plotly.react(sceneDiv, [...STATIC, ...buildDynamicTraces(cutoff)], LAYOUT, {{displayModeBar: false, responsive: true}});
  }}

  // --- Video sync via anchor-relative time ---

  function syncVideosToT(t) {{
    for (const v of vids) {{
      const off = offsetByCam[v.dataset.cam] ?? 0;
      const want = t - off;
      // Only seek if the gap is > 50 ms — avoids feedback loops when
      // the browser fires its own timeupdate after our seek.
      if (isFinite(want) && Math.abs(v.currentTime - want) > 0.05) {{
        try {{ v.currentTime = Math.max(0, want); }} catch (e) {{}}
      }}
    }}
  }}

  function readMasterTFromVideo() {{
    // Pick the first ready video; else fall back to current scrubber.
    for (const v of vids) {{
      if (!isNaN(v.currentTime)) {{
        const off = offsetByCam[v.dataset.cam] ?? 0;
        return v.currentTime + off;
      }}
    }}
    return currentT;
  }}

  function setT(t, {{ seekVideos = true }} = {{}}) {{
    currentT = Math.max(tMin, Math.min(tMax, t));
    scrubber.value = String(((currentT - tMin) / (tMax - tMin)) * 1000 | 0);
    tLabel.textContent = `t=${{currentT.toFixed(3)}}s`;
    if (seekVideos) syncVideosToT(currentT);
    if (mode === "playback") drawScene();
  }}

  function onVideoTimeUpdate() {{
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {{
      rafPending = false;
      const t = readMasterTFromVideo();
      setT(t, {{ seekVideos: false }});
    }});
  }}

  // Play-button toggles all videos together.
  playBtn.addEventListener("click", () => {{
    const anyPaused = vids.some(v => v.paused);
    if (anyPaused) {{
      vids.forEach(v => {{ try {{ v.play(); }} catch (e) {{}} }});
    }} else {{
      vids.forEach(v => v.pause());
    }}
  }});

  function updatePlayBtnLabel() {{
    playBtn.textContent = vids.every(v => v.paused) ? "PLAY" : "PAUSE";
  }}

  vids.forEach(v => {{
    v.addEventListener("play",  updatePlayBtnLabel);
    v.addEventListener("pause", updatePlayBtnLabel);
    v.addEventListener("timeupdate", onVideoTimeUpdate);
    v.addEventListener("seeked",     onVideoTimeUpdate);
    v.addEventListener("ratechange", () => {{
      // Keep all rates in lockstep with whichever one changed.
      const r = v.playbackRate;
      for (const other of vids) {{ if (other !== v && other.playbackRate !== r) other.playbackRate = r; }}
    }});
  }});

  scrubber.addEventListener("input", () => {{
    const frac = Number(scrubber.value) / 1000;
    setT(tMin + frac * (tMax - tMin));
  }});

  function setMode(next) {{
    mode = next;
    modeAll.classList.toggle("active", next === "all");
    modePlayback.classList.toggle("active", next === "playback");
    drawScene();
  }}
  modeAll.addEventListener("click", () => setMode("all"));
  modePlayback.addEventListener("click", () => setMode("playback"));

  // Initial render.
  setT(tMin, {{ seekVideos: true }});
  drawScene();
  updatePlayBtnLabel();
}})();
</script>
</body></html>"""


def _camera_color(camera_id: str) -> str:
    return _CAMERA_COLORS.get(camera_id, _FALLBACK_CAMERA_COLOR)


def _video_cell_html(cam: str, entry: tuple[str, float] | None) -> str:
    """One vid-cell per camera slot. `entry` is None when the slot has no
    clip on disk — rendered as an explicit placeholder so the operator
    sees "no clip" rather than a silent gap."""
    color = _camera_color(cam)
    if entry is None:
        # Keep the 'no clips on disk' literal when BOTH cameras are missing
        # (test_viewer_endpoint_without_clips_still_renders checks for it);
        # a per-slot placeholder is fine and still contains the phrase.
        body = '<div class="vid-frame empty">no clips on disk</div>'
        hint = "awaiting upload"
    else:
        url, _ = entry
        body = (
            f'<div class="vid-frame">'
            f'<video data-cam="{cam}" preload="auto" playsinline muted '
            f'src="{url}"></video></div>'
        )
        hint = "synced to chirp"
    return (
        f'<div class="vid-cell">'
        f'<div class="vid-head">'
        f'<span class="vid-label" style="color:{color};border-color:{color};">'
        f'CAM {cam}</span>'
        f'<span class="vid-hint">{hint}</span>'
        f'</div>'
        f'{body}'
        f'</div>'
    )


def _header_meta_text(health: dict) -> str:
    """Monospaced status strip for the nav bar: session id, duration,
    upload time. Each value is tagged `.v` so the design-system CSS
    colours it as 'ink' while the labels stay 'sub'."""
    import datetime as _dt

    parts: list[str] = []
    parts.append(
        f'<span>SESSION <span class="v">{health["session_id"]}</span></span>'
    )
    dur = health.get("duration_s")
    if dur is not None:
        parts.append(f'<span>DURATION <span class="v">{dur:.2f}s</span></span>')
    rx = health.get("received_at")
    if rx is not None:
        ts = _dt.datetime.fromtimestamp(rx).strftime("%m-%d %H:%M")
        parts.append(f'<span>RECEIVED <span class="v">{ts}</span></span>')
    return "".join(parts)


def _health_banner_html(health: dict) -> str:
    """Per-camera diagnostic cards + triangulation summary + explicit
    failure strip. The banner is the page's answer to "what actually
    happened during this session?" — every failure mode the pipeline can
    hit (B never uploaded, no time sync, missing calibration, triangulation
    skipped) has a visible surface here, so the operator never has to
    infer from an empty 3D scene."""
    cards: list[str] = []
    for cam_id in ("A", "B"):
        cam = health["cameras"][cam_id]
        cards.append(_cam_card_html(cam_id, cam))

    tri_n = health.get("triangulated_count", 0)
    if tri_n > 0:
        tri_block = (
            f'<div class="tri-card ok">'
            f'<div class="tri-title">3D Trajectory</div>'
            f'<div class="tri-count">{tri_n}</div>'
            f'<div class="tri-note">points triangulated</div>'
            f'</div>'
        )
    else:
        tri_block = (
            f'<div class="tri-card">'
            f'<div class="tri-title">3D Trajectory</div>'
            f'<div class="tri-count zero">—</div>'
            f'<div class="tri-note">no triangulation</div>'
            f'</div>'
        )

    fail_strip = _failure_strip_html(health)

    return (
        f'<div class="health">'
        f'<div class="health-row">{cards[0]}{cards[1]}{tri_block}</div>'
        f'{fail_strip}'
        f'</div>'
    )


def _cam_card_html(cam_id: str, cam: dict) -> str:
    if not cam["received"]:
        return (
            f'<div class="cam-card missing">'
            f'<div class="cam-head">'
            f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
            f'<span class="cam-state bad">not uploaded</span>'
            f'</div>'
            f'<div class="cam-stats" style="color:var(--sub);">'
            f'this phone never reached the server for this session'
            f'</div>'
            f'</div>'
        )

    checks = [
        ("calibrated", cam["calibrated"], "intrinsics + homography"),
        ("time synced", cam["time_synced"], "chirp anchor"),
    ]
    checks_html = "".join(
        f'<span class="check {"pass" if ok else "fail"}" title="{tip}">'
        f'<span class="mark">{"✓" if ok else "✗"}</span>{label}'
        f'</span>'
        for (label, ok, tip) in checks
    )

    n_det = cam["n_detected"]
    n_frames = cam["n_frames"]
    stats_html = (
        f'<span class="n">{n_det}</span>'
        f'<span class="of"> detected / {n_frames} frames</span>'
    )

    return (
        f'<div class="cam-card received">'
        f'<div class="cam-head">'
        f'<span class="cam-badge {cam_id}">CAM {cam_id}</span>'
        f'<span class="cam-state ok">uploaded</span>'
        f'</div>'
        f'<div class="cam-checks">{checks_html}</div>'
        f'<div class="cam-stats">{stats_html}</div>'
        f'</div>'
    )


def _failure_strip_html(health: dict) -> str:
    """Surface the first blocking failure explicitly. Order matters —
    show the earliest pipeline step that broke, because fixing a later
    step before the earlier one is wasted effort. `None` return means
    the pipeline completed cleanly, so no strip is rendered."""
    cams = health["cameras"]
    tri_n = health.get("triangulated_count", 0)
    server_err = health.get("error")

    reasons: list[str] = []
    missing = [c for c in ("A", "B") if not cams[c]["received"]]
    if missing:
        reasons.append(
            f"{' + '.join('Cam ' + c for c in missing)} never uploaded "
            f"— triangulation skipped"
        )
    else:
        uncal = [c for c in ("A", "B") if not cams[c]["calibrated"]]
        if uncal:
            reasons.append(
                f"{' + '.join('Cam ' + c for c in uncal)} missing calibration "
                f"(intrinsics or homography) — run Calibration screen"
            )
        unsyn = [c for c in ("A", "B") if not cams[c]["time_synced"]]
        if unsyn:
            reasons.append(
                f"{' + '.join('Cam ' + c for c in unsyn)} has no chirp anchor "
                f"— re-run 時間校正 before arming"
            )
        if server_err:
            reasons.append(f"server error: {server_err}")
        elif tri_n == 0 and all(cams[c]["received"] for c in ("A", "B")):
            no_detect = [c for c in ("A", "B") if cams[c]["n_detected"] == 0]
            if no_detect:
                reasons.append(
                    f"{' + '.join('Cam ' + c for c in no_detect)} detected no ball "
                    f"in any frame — check lighting / HSV range"
                )
            else:
                reasons.append(
                    "triangulation produced no points — check A/B pairing "
                    "window or frame timing"
                )

    if not reasons:
        return ""
    body = "<br>".join(reasons)
    return (
        f'<div class="fail-strip">'
        f'<span class="icon">!</span>'
        f'<span>{body}</span>'
        f'</div>'
    )


def _build_figure(scene: Scene):
    import plotly.graph_objects as go

    traces: list = []

    # --- Ground plane (Z=0). Very dim so the pentagon reads cleanly on
    #     top of it; the ground is purely a reference surface, not content.
    g = _GROUND_HALF_EXTENT_M
    traces.append(
        go.Mesh3d(
            x=[-g, g, g, -g],
            y=[-g, -g, g, g],
            z=[0.0, 0.0, 0.0, 0.0],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color=_BORDER_L,
            opacity=0.18,
            name="ground (Z=0)",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    # --- Home plate (Z=0). Filled pentagon in `surface` with an ink
    #     outline — high enough contrast against the ground that the
    #     canvas always reads as "the plate is the anchor" even before
    #     any camera exists.
    traces.append(
        go.Mesh3d(
            x=_PLATE_X,
            y=_PLATE_Y,
            z=[0.0] * 5,
            # Fan triangulation from vertex 0 (FL): (0,1,2), (0,2,3), (0,3,4).
            i=[0, 0, 0],
            j=[1, 2, 3],
            k=[2, 3, 4],
            color=_SURFACE,
            opacity=0.95,
            flatshading=True,
            name="home plate",
            hoverinfo="skip",
            showlegend=False,
        )
    )
    traces.append(
        go.Scatter3d(
            x=_PLATE_X + [_PLATE_X[0]],
            y=_PLATE_Y + [_PLATE_Y[0]],
            z=[0.0] * 6,
            mode="lines",
            line=dict(color=_INK, width=3),
            hoverinfo="skip",
            showlegend=False,
        )
    )

    # --- World axes at origin. Ink-tinted instead of full-saturation RGB
    #     so the axes recede behind the semantic camera colours — matches
    #     the design-system rule that semantic colour is meaning, not
    #     decoration.
    for direction, color, label in (
        ((1.0, 0.0, 0.0), _DEV, "X"),
        ((0.0, 1.0, 0.0), _CONTRA, "Y"),
        ((0.0, 0.0, 1.0), _INK_40, "Z"),
    ):
        dx, dy, dz = direction
        traces.append(
            go.Scatter3d(
                x=[0.0, _WORLD_AXIS_LEN_M * dx],
                y=[0.0, _WORLD_AXIS_LEN_M * dy],
                z=[0.0, _WORLD_AXIS_LEN_M * dz],
                mode="lines+text",
                text=["", label],
                textposition="top center",
                textfont=dict(family="JetBrains Mono, monospace", size=11, color=_INK),
                line=dict(color=color, width=4),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    # --- Cameras: marker + local RGB triad + forward arrow ---
    for cam in scene.cameras:
        color = _CAMERA_COLORS.get(cam.camera_id, _FALLBACK_CAMERA_COLOR)
        cx, cy, cz = cam.center_world

        traces.append(
            go.Scatter3d(
                x=[cx], y=[cy], z=[cz],
                mode="markers+text",
                marker=dict(size=8, color=color, symbol="diamond"),
                text=[f"Cam {cam.camera_id}"],
                textposition="top center",
                name=f"Camera {cam.camera_id}",
                hovertemplate=(
                    f"Camera {cam.camera_id}"
                    "<br>x=%{x:.2f} m"
                    "<br>y=%{y:.2f} m"
                    "<br>z=%{z:.2f} m<extra></extra>"
                ),
            )
        )

        # Local axes: forward (cam+Z) in the camera's own tinted colour
        # (so A/B stay visually distinct at a glance), right (+X) and
        # up (-image_down) in muted ink so they don't compete with the
        # forward vector for the eye.
        for axis, axis_color, length in (
            (cam.axis_forward_world, color, _CAMERA_FORWARD_ARROW_M),
            (cam.axis_right_world, _DEV, _CAMERA_AXIS_LEN_M),
            (cam.axis_up_world, _INK_40, _CAMERA_AXIS_LEN_M),
        ):
            traces.append(
                go.Scatter3d(
                    x=[cx, cx + length * axis[0]],
                    y=[cy, cy + length * axis[1]],
                    z=[cz, cz + length * axis[2]],
                    mode="lines",
                    line=dict(color=axis_color, width=4),
                    hoverinfo="skip",
                    showlegend=False,
                )
            )

    # --- Rays per camera (one trace each, with None separators) ---
    rays_by_cam: dict[str, list] = {}
    for r in scene.rays:
        rays_by_cam.setdefault(r.camera_id, []).append(r)

    for cam_id, rays in rays_by_cam.items():
        color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
        xs: list[float | None] = []
        ys: list[float | None] = []
        zs: list[float | None] = []
        for r in rays:
            xs.extend([r.origin[0], r.endpoint[0], None])
            ys.extend([r.origin[1], r.endpoint[1], None])
            zs.extend([r.origin[2], r.endpoint[2], None])
        traces.append(
            go.Scatter3d(
                x=xs, y=ys, z=zs,
                mode="lines",
                line=dict(color=color, width=2),
                opacity=0.35,
                name=f"Rays {cam_id} ({len(rays)})",
                hoverinfo="skip",
                meta=dict(trace_kind="ray", camera_id=cam_id),
            )
        )

    # --- Ground-plane trace per camera (single-camera trajectory proxy).
    #     Connect every ball-detected ray's Z=0 intersection by time. For
    #     a paired session this is redundant with `triangulated`, so draw
    #     it semi-transparent and as a dashed line so the 3D trajectory
    #     still reads as the authoritative curve.
    for cam_id, trace in scene.ground_traces.items():
        if not trace:
            continue
        color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
        xs = [p["x"] for p in trace]
        ys = [p["y"] for p in trace]
        zs = [p["z"] for p in trace]
        ts = [p["t_rel_s"] for p in trace]
        dashed = bool(scene.triangulated)
        traces.append(
            go.Scatter3d(
                x=xs, y=ys, z=zs,
                mode="lines+markers",
                line=dict(
                    color=color,
                    width=3,
                    dash="dash" if dashed else "solid",
                ),
                marker=dict(size=3, color=color),
                opacity=0.7 if not dashed else 0.45,
                name=f"Ground trace {cam_id} ({len(trace)} pts)",
                hovertemplate=(
                    f"Cam {cam_id} ground"
                    "<br>t=%{customdata:.3f}s"
                    "<br>x=%{x:.2f} m"
                    "<br>y=%{y:.2f} m<extra></extra>"
                ),
                customdata=ts,
                meta=dict(trace_kind="ground_trace", camera_id=cam_id),
            )
        )

    # --- Triangulated trajectory (if paired) ---
    if scene.triangulated:
        ts = [p["t_rel_s"] for p in scene.triangulated]
        xs = [p["x"] for p in scene.triangulated]
        ys = [p["y"] for p in scene.triangulated]
        zs = [p["z"] for p in scene.triangulated]
        traces.append(
            go.Scatter3d(
                x=xs, y=ys, z=zs,
                mode="lines+markers",
                line=dict(color=_ACCENT, width=4),
                marker=dict(
                    size=4,
                    color=ts,
                    colorscale="Cividis",
                    showscale=True,
                    colorbar=dict(
                        title=dict(text="t (s)", font=dict(color=_INK, size=11)),
                        tickfont=dict(color=_INK, size=10),
                        outlinecolor=_BORDER_BASE,
                        outlinewidth=1,
                    ),
                ),
                name=f"3D trajectory ({len(ts)} pts)",
                hovertemplate=(
                    "t=%{marker.color:.3f}s"
                    "<br>x=%{x:.2f} m"
                    "<br>y=%{y:.2f} m"
                    "<br>z=%{z:.2f} m<extra></extra>"
                ),
                meta=dict(trace_kind="triangulated"),
            )
        )

    n_rays = len(scene.rays)
    n_cams = len(scene.cameras)
    subtitle = f"{n_cams} cam · {n_rays} rays"
    if scene.triangulated:
        subtitle += f" · {len(scene.triangulated)} 3D pts"

    axis_font = dict(family="JetBrains Mono, monospace", size=11, color=_INK)
    axis_style = dict(
        backgroundcolor=_BG,
        gridcolor=_BORDER_L,
        zerolinecolor=_BORDER_BASE,
        linecolor=_BORDER_BASE,
        tickfont=dict(family="JetBrains Mono, monospace", size=10, color=_SUB),
    )

    def axis(title_text: str) -> dict:
        return dict(title=dict(text=title_text, font=axis_font), **axis_style)

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=dict(
            text=f"Session {scene.session_id}  —  {subtitle}",
            font=dict(family="JetBrains Mono, monospace", size=13, color=_INK),
            x=0.02,
        ),
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        scene=dict(
            xaxis=axis("X (left/right, m)"),
            yaxis=axis("Y (depth, m)"),
            zaxis=axis("Z (up, m)"),
            bgcolor=_BG,
            aspectmode="data",
        ),
        margin=dict(l=0, r=0, t=36, b=0),
        legend=dict(
            itemsizing="constant",
            bgcolor=_SURFACE,
            bordercolor=_BORDER_BASE,
            borderwidth=1,
            font=dict(family="JetBrains Mono, monospace", size=11, color=_INK),
        ),
        font=dict(family="Noto Sans TC, sans-serif", color=_INK),
    )
    return fig
