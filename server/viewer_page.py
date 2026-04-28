from __future__ import annotations

from dataclasses import dataclass
import json as _json
from pathlib import Path

from cam_view_ui import CAM_VIEW_CONTENT_CSS, CAM_VIEW_RUNTIME_JS
from overlays_ui import OVERLAYS_RUNTIME_JS
from reconstruct import Scene
from render_compare import (
    DRAW_VIRTUAL_BASE_JS,
    PLATE_WORLD_JS,
    PROJECTION_JS,
)
from render_scene_theme import (
    _ACCENT,
    _BG,
    _BORDER_BASE,
    _BORDER_L,
    _CAMERA_AXIS_LEN_M,
    _CAMERA_COLORS,
    _CAMERA_FORWARD_ARROW_M,
    _CONTRA,
    _DEV,
    _DUAL,
    _FALLBACK_CAMERA_COLOR,
    _INK,
    _INK_40,
    _OK,
    _PENDING,
    _SUB,
    _SURFACE,
)
from viewer_fragments import (
    failure_strip_html,
    health_nav_strip_html,
    video_cell_html,
)


@dataclass(frozen=True)
class ViewerPageContext:
    scene_json: str
    layout_json: str
    static_traces_json: str
    camera_colors_json: str
    fallback_color_json: str
    accent_color_json: str
    # Camera diamond + axis geometry is rendered dynamically from JS (see
    # `camMarkerTracesFor` in the viewer) so it follows the per-pipeline
    # pills. These constants mirror render_scene_theme so the dashboard's
    # static figure and the viewer's dynamic figure agree on sizes/colors.
    scene_theme_json: str
    videos_json: str
    has_triangulated: bool
    scene_flex: str
    videos_flex: str
    layout_mode: str
    health_strip_html: str
    health_failure_html: str
    video_cells_html: str
    session_id: str
    server_post_ran: bool
    can_run_server: bool


def build_viewer_page_context(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
    *,
    build_figure,
) -> ViewerPageContext:
    fig = build_figure(scene)
    fig_json = _json.loads(fig.to_json())
    layout_json = _json.dumps(fig_json.get("layout", {}))
    static_traces = [
        t for t in fig_json.get("data", [])
        if not (t.get("meta") or {}).get("trace_kind")
    ]
    has_triangulated = bool(scene.triangulated)
    # Default split is 50/50 so both halves read equally; operators who
    # want more scene or more camera grid drag the #col-resizer (persisted
    # to localStorage).
    scene_flex = "1 1 0"
    videos_flex = "1 1 0"

    videos_by_cam = {cam: (url, off) for cam, url, off, _fps, _fr in videos if url}
    other_cam = {"A": "B", "B": "A"}
    cams_by_id = {c.camera_id: c for c in scene.cameras}
    video_cells = "".join(
        video_cell_html(
            cam,
            videos_by_cam.get(cam),
            never_coming=(
                cam not in videos_by_cam
                and other_cam[cam] in videos_by_cam
                and not health["cameras"][cam]["received"]
            ),
            image_width_px=(cams_by_id[cam].image_width_px if cam in cams_by_id else None),
            image_height_px=(cams_by_id[cam].image_height_px if cam in cams_by_id else None),
            cx=(cams_by_id[cam].cx if cam in cams_by_id else None),
            cy=(cams_by_id[cam].cy if cam in cams_by_id else None),
        )
        for cam in ("A", "B")
    )

    cam_a_received = health["cameras"]["A"]["received"]
    cam_b_received = health["cameras"]["B"]["received"]
    if cam_a_received and cam_b_received:
        layout_mode = "paired"
    elif cam_a_received or cam_b_received:
        layout_mode = "single-cam"
    else:
        layout_mode = "empty"

    # Server-post status: has it run on any received camera? Uploaded MOV
    # is the prereq for running it, and the viewer has a MOV iff `mode ==
    # "camera_only"` (set by the health builder when a video is on disk).
    def _server_post_count(cam_key: str) -> int:
        cam = health["cameras"].get(cam_key) or {}
        counts = (cam.get("counts_by_path") or {}).get("server_post") or {}
        return int(counts.get("total") or 0)

    server_post_ran = any(_server_post_count(c) > 0 for c in ("A", "B"))
    can_run_server = health.get("mode") == "camera_only" and not server_post_ran

    return ViewerPageContext(
        scene_json=_json.dumps(scene.to_dict()),
        layout_json=layout_json,
        static_traces_json=_json.dumps(static_traces),
        camera_colors_json=_json.dumps(_CAMERA_COLORS),
        fallback_color_json=_json.dumps(_FALLBACK_CAMERA_COLOR),
        accent_color_json=_json.dumps(_ACCENT),
        scene_theme_json=_json.dumps(
            {
                "cam_axis_len_m": _CAMERA_AXIS_LEN_M,
                "cam_fwd_len_m": _CAMERA_FORWARD_ARROW_M,
                "axis_color_right": _DEV,
                "axis_color_up": _INK_40,
            }
        ),
        videos_json=_json.dumps(
            [
                {
                    "camera_id": cam,
                    "url": url,
                    "t_rel_offset_s": off,
                    "fps": fps,
                    "frames": frames,
                }
                for (cam, url, off, fps, frames) in videos
            ]
        ),
        has_triangulated=has_triangulated,
        scene_flex=scene_flex,
        videos_flex=videos_flex,
        layout_mode=layout_mode,
        health_strip_html=health_nav_strip_html(health),
        health_failure_html=failure_strip_html(health),
        video_cells_html=video_cells,
        session_id=scene.session_id,
        server_post_ran=server_post_ran,
        can_run_server=can_run_server,
    )


# Phase 6: viewer's vid-cell uses cam-view via data-cam-view (no
# .cam-view class — viewer owns its own video + cell layout). These
# rules glue the runtime canvas onto the existing .vid-media and
# render the per-cam layer toolbar as a slim footer bar beneath the
# video, NOT floating absolute over the video. The toolbar inherits
# pill / slider styling from the CAM_VIEW_CONTENT_CSS bucket; the
# CAM_VIEW_BOX_CSS bucket (with .cam-view aspect-ratio + absolute
# toolbar positioning) is intentionally NOT pulled in here — viewer
# already owns its container layout.
_VIEWER_CAM_VIEW_OVERRIDES = (
    CAM_VIEW_CONTENT_CSS
    + """
.vid-cell[data-cam-view] .vid-media { position: relative; }
.vid-cell[data-cam-view] canvas[data-cam-canvas] {
  position: absolute; inset: 0; width: 100%; height: 100%; display: block;
}
.vid-cell[data-cam-view] .cam-view-toolbar {
  margin-top: var(--s-2, 6px);
  padding: 5px 8px;
  background: var(--surface, #FCFBFA);
  border: 1px solid var(--border-base, #DBD6CD);
  border-radius: var(--r, 4px);
  flex-wrap: wrap;
}
"""
)


def render_viewer_html(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
    *,
    build_figure,
) -> str:
    ctx = build_viewer_page_context(
        scene,
        videos,
        health,
        build_figure=build_figure,
    )
    if ctx.can_run_server:
        action_html = (
            f'<form method="POST" action="/sessions/{ctx.session_id}/run_server_post">'
            f'<button class="action" type="submit">Run server detection</button>'
            f'</form>'
        )
    elif ctx.server_post_ran:
        action_html = '<span class="action-chip">server done</span>'
    else:
        action_html = ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Session {scene.session_id}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
{_viewer_css(ctx.scene_flex, ctx.videos_flex)}
{_VIEWER_CAM_VIEW_OVERRIDES}
</style>
</head><body>
<div class="viewer">
  <div class="nav">
    <span class="brand"><span class="dot"></span>BALL_TRACKER</span>
    {ctx.health_strip_html}
    {action_html}
    <a class="back" href="/">&larr; dashboard</a>
  </div>
  {ctx.health_failure_html}
  <div class="work" data-mode="{ctx.layout_mode}">
    <div class="scene-col">
      <div id="scene"></div>
      <div class="scene-views" role="toolbar" aria-label="Camera presets">
        <button class="view-preset active" type="button" data-view="iso" title="Isometric overview (default)">ISO</button>
        <button class="view-preset" type="button" data-view="catch" title="Catcher's view — strike zone front-on (X/Z plane)">CATCH</button>
        <button class="view-preset" type="button" data-view="side" title="1B-side view — trajectory arc (Y/Z plane)">SIDE</button>
        <button class="view-preset" type="button" data-view="top" title="Top-down — horizontal break (X/Y plane)">TOP</button>
        <button class="view-preset" type="button" data-view="pitcher" title="Pitcher's view — looking back at catcher">PITCHER</button>
      </div>
      <div class="scene-toolbar" role="toolbar" aria-label="Scene controls">
        <button id="mode-all" class="active" type="button" role="tab" title="Show full trajectory">All</button>
        <button id="mode-playback" type="button" role="tab" title="Cut trace at playback time">Playback</button>
      </div>
      <div id="fit-info" class="fit-info" hidden aria-live="polite"></div>
      <div id="speed-bars" class="speed-bars" hidden aria-label="Per-segment speed"></div>
    </div>
    <div class="col-resizer" id="col-resizer" role="separator" aria-orientation="vertical" aria-label="Resize 3D scene vs cameras" tabindex="0" title="Drag to resize"></div>
    <div class="videos-col">{ctx.video_cells_html}</div>
  </div>
  <div class="timeline">
    <div class="tl-row">
      <div class="scrubber-wrap">
        <div class="strip-legend"
             title="Strip colors: A detected (orange) · B detected (brown) · missed (grey) · no frame (pale) · chirp anchor (accent)"
             role="group" aria-label="Layer visibility + filters">
          <span class="layer-toggles" id="layer-toggles" aria-label="Layer visibility">
            <span class="layer-group" data-layer="traj">
              <span class="layer-name">Traj</span>
              <button type="button" class="layer-pill" data-layer="traj" data-path="live" aria-pressed="false">live</button>
              <button type="button" class="layer-pill" data-layer="traj" data-path="server_post" aria-pressed="true">svr</button>
            </span>
            <span class="layer-group" data-layer="camA">
              <span class="layer-name"><span class="swatch" data-cam="A"></span>Rays A</span>
              <button type="button" class="layer-pill" data-layer="camA" data-path="live" aria-pressed="true">live</button>
              <button type="button" class="layer-pill" data-layer="camA" data-path="server_post" aria-pressed="true">svr</button>
            </span>
            <span class="layer-group" data-layer="camB">
              <span class="layer-name"><span class="swatch" data-cam="B"></span>Rays B</span>
              <button type="button" class="layer-pill" data-layer="camB" data-path="live" aria-pressed="true">live</button>
              <button type="button" class="layer-pill" data-layer="camB" data-path="server_post" aria-pressed="true">svr</button>
            </span>
            <span class="layer-divider" aria-hidden="true"></span>
            <span class="layer-group" data-layer="residual" id="residual-filter-group" title="Drop triangulated points whose ray-midpoint residual exceeds this threshold. Real ball pairs sit at sub-cm residual; static-target false pairs blow up to metres.">
              <span class="layer-name">Residual</span>
              <input type="range" id="residual-filter-slider" min="0" max="200" step="1" value="200" aria-label="Residual filter threshold (cm)">
              <span id="residual-filter-readout" class="readout">off</span>
            </span>
            <span class="layer-group" data-layer="fitres" id="fitres-filter-group" title="Spatial isolation outlier rejection. For each triangulated point, compute the mean distance to its 3 nearest neighbours in 3D — cluster points sit a few cm apart, isolated outliers sit ≥ 1 m away. Reject points whose isolation > median + κ·MAD. κ = slider value; off = no rejection. Does NOT fit a curve first, so it survives the LSQ leverage problem where one outlier warps the fit.">
              <span class="layer-name">Outlier</span>
              <input type="range" id="fitres-filter-slider" min="10" max="60" step="1" value="60" aria-label="Outlier rejection (κ · MAD; 60 = off)">
              <span id="fitres-filter-readout" class="readout">off</span>
            </span>
            <span class="layer-divider" aria-hidden="true"></span>
            <span class="layer-group" data-layer="fit" title="Overlay a ballistic fit curve (per-axis quadratic, gravity free) on the filtered points. Same overlay flag as the dashboard.">
              <label class="layer-checkbox">
                <input type="checkbox" id="fit-toggle">
                <span class="layer-name">Fit</span>
              </label>
              <span class="layer-source-group" role="group" aria-label="Fit source pipeline">
                <button id="fit-src-svr" class="fit-src-pill" type="button" data-src="server_post" aria-pressed="true" title="Fit using server_post triangulated points">svr</button>
                <button id="fit-src-live" class="fit-src-pill" type="button" data-src="live" aria-pressed="false" title="Fit using live triangulated points">live</button>
              </span>
            </span>
            <span class="layer-group" data-layer="speed" title="Colour each trajectory segment by instantaneous speed (m/s). Adds a colorbar and a per-segment 2D bar chart below the scene.">
              <label class="layer-checkbox">
                <input type="checkbox" id="speed-toggle">
                <span class="layer-name">Speed</span>
              </label>
            </span>
            <span class="layer-group" data-layer="strike-zone" title="Toggle the strike-zone wireframe in the 3D scene. Default on.">
              <label class="layer-checkbox">
                <input type="checkbox" id="strike-zone-toggle" checked>
                <span class="layer-name">Strike zone</span>
              </label>
            </span>
          </span>
        </div>
        <div class="strip-row strip-row-scrubber">
          <span class="strip-label" aria-hidden="true"></span>
          <input id="scrubber" class="strip-canvas" type="range" min="0" max="1" value="0" step="1" />
        </div>
        <div class="strip-row" id="strip-row-live" hidden
             title="LIVE — iOS on-device detection streamed over WS while the session was armed. Runs on raw BGRA frames pre-encode; earliest signal available.">
          <span class="strip-label">LIVE</span>
          <span class="strip-sublabels" aria-hidden="true"><span>A</span><span>B</span></span>
          <canvas id="detection-canvas-live" class="strip-canvas" height="28" aria-hidden="true"></canvas>
        </div>
        <div class="strip-row" id="strip-row-server-post" hidden
             title="SVR — server-side detection on the H.264-decoded MOV. Independent from the iOS paths; H.264 quantization typically costs a few frames at detection edges.">
          <span class="strip-label">SVR</span>
          <span class="strip-sublabels" aria-hidden="true"><span>A</span><span>B</span></span>
          <canvas id="detection-canvas-server-post" class="strip-canvas" height="28" aria-hidden="true"></canvas>
        </div>
        <div class="strip-note" id="strip-note-multi" hidden></div>
      </div>
      <div id="frame-label" class="frame-label" role="group" aria-label="Playback position">
        <div class="frame-label-head">
          <span class="primary" id="frame-primary">t=0.000s</span>
          <span class="frame-meta">
            <input id="frame-input" type="number" min="0" max="0" value="0" step="1" title="Type a frame index to jump" />
            <span class="frame-slash">/</span>
            <span id="frame-total" class="frame-total">0</span>
          </span>
        </div>
        <div class="frame-label-body" id="frame-sub"></div>
      </div>
    </div>
    <div class="tl-row">
      <div class="transport" role="group" aria-label="transport">
        <button id="step-first" type="button" title="First frame (Home)" aria-label="First frame">
          <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="3" width="1.6" height="10" fill="currentColor"/><path d="M14 3 L14 13 L6.4 8 Z" fill="currentColor"/></svg>
        </button>
        <button id="step-back" type="button" title="Prev frame (,)" aria-label="Previous frame">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M14 3 L14 13 L8 8 Z" fill="currentColor"/><path d="M8 3 L8 13 L2 8 Z" fill="currentColor"/></svg>
        </button>
        <button id="play-btn" class="play-btn" type="button" title="Play/pause (Space)">Play</button>
        <button id="step-fwd" type="button" title="Next frame (.)" aria-label="Next frame">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3 L2 13 L8 8 Z" fill="currentColor"/><path d="M8 3 L8 13 L14 8 Z" fill="currentColor"/></svg>
        </button>
        <button id="step-last" type="button" title="Last frame (End)" aria-label="Last frame">
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 3 L2 13 L9.6 8 Z" fill="currentColor"/><rect x="11.4" y="3" width="1.6" height="10" fill="currentColor"/></svg>
        </button>
      </div>
      <div class="speed-group" id="speed-group" role="group" aria-label="playback speed">
        <button data-rate="0.1" type="button">0.1&times;</button>
        <button data-rate="0.25" type="button">0.25&times;</button>
        <button data-rate="0.5" type="button">0.5&times;</button>
        <button data-rate="1" class="active" type="button">1&times;</button>
        <button data-rate="2" type="button">2&times;</button>
      </div>
      <button id="hint-btn" class="hint-btn" type="button" title="Keyboard shortcuts (?)" aria-haspopup="dialog">?</button>
    </div>
    <div id="hint-overlay" class="hint-overlay" role="dialog" aria-label="Keyboard shortcuts">
      <h4>Keyboard shortcuts</h4>
      <table><tbody>
        <tr><td>Space</td><td>Play / pause</td></tr>
        <tr><td>, &nbsp;.</td><td>Prev / next frame</td></tr>
        <tr><td>Shift+, &nbsp;.</td><td>&plusmn;10 frames</td></tr>
        <tr><td>&larr; &nbsp;&rarr;</td><td>&plusmn;0.5 second</td></tr>
        <tr><td>Home / End</td><td>First / last frame</td></tr>
        <tr><td>D &nbsp;F</td><td>Prev / next ball-detected frame</td></tr>
        <tr><td>1 &ndash; 5</td><td>Speed presets</td></tr>
        <tr><td>?</td><td>Toggle this help</td></tr>
        <tr><td>Esc</td><td>Close</td></tr>
      </tbody></table>
    </div>
  </div>
</div>
<script id="viewer-data" type="application/json">{{
  "scene": {ctx.scene_json},
  "layout": {ctx.layout_json},
  "static_traces": {ctx.static_traces_json},
  "camera_colors": {ctx.camera_colors_json},
  "fallback_color": {ctx.fallback_color_json},
  "accent_color": {ctx.accent_color_json},
  "scene_theme": {ctx.scene_theme_json},
  "videos": {ctx.videos_json},
  "has_triangulated": {str(ctx.has_triangulated).lower()}
}}</script>
<script>
{OVERLAYS_RUNTIME_JS}
</script>
<script>
{CAM_VIEW_RUNTIME_JS}
</script>
<script>
{_viewer_js()}
</script>
</body></html>"""


def _viewer_css(scene_flex: str, videos_flex: str) -> str:
    return f"""
  :root {{
    --bg: {_BG}; --surface: {_SURFACE}; --surface-hover: #F3F0EA;
    --ink: {_INK}; --sub: {_SUB};
    --ink-light: #5A544C;
    --border-base: {_BORDER_BASE}; --border-l: {_BORDER_L};
    --contra: {_CONTRA}; --dual: {_DUAL}; --dev: {_DEV}; --accent: {_ACCENT};
    --ok: {_OK}; --pending: {_PENDING};
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px;
    --r: 3px;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0; padding:0; height:100%; overflow:hidden;
    background:var(--bg); color:var(--ink); font-family:var(--sans);
    font-weight:300; line-height:1.6; -webkit-font-smoothing:antialiased; }}
  .viewer {{ display:grid; grid-template-rows:52px auto minmax(0, 1fr) auto;
    height:100vh; min-height:0; overflow:hidden; }}
  .nav {{ height:52px; flex:0 0 52px; background:var(--surface);
    border-bottom:1px solid var(--border-base); display:flex;
    align-items:center; padding:0 var(--s-5); gap:var(--s-4); }}
  .nav .brand {{ font-family:var(--mono); font-weight:700; font-size:14px;
    letter-spacing:0.16em; color:var(--ink); }}
  .nav .brand .dot {{ display:inline-block; width:7px; height:7px;
    background:var(--ink); margin-right:var(--s-2); vertical-align:middle; }}
  .nav .back {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.12em; text-transform:uppercase; color:var(--sub);
    text-decoration:none; }}
  .nav .back:hover {{ color:var(--ink); }}
  .nav form {{ margin:0; }}
  .nav .action {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.10em; text-transform:uppercase; color:var(--ink);
    background:var(--surface); border:1px solid var(--border-base);
    border-radius:var(--r); padding:5px 10px; cursor:pointer;
    transition:border-color 0.15s, color 0.15s, background 0.15s; }}
  .nav .action:hover {{ border-color:var(--passed); color:var(--passed);
    background:var(--surface); }}
  .nav .action:disabled {{ opacity:0.55; cursor:not-allowed;
    color:var(--sub); }}
  .nav .action-chip {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.10em; text-transform:uppercase; color:var(--passed);
    border:1px solid var(--passed); background:var(--passed-bg);
    padding:3px 8px; border-radius:var(--r); }}
  .health-strip {{ flex:1 1 auto; min-width:0; display:flex;
    align-items:center; gap:var(--s-3); flex-wrap:wrap;
    font-family:var(--mono); font-size:11px; color:var(--sub);
    letter-spacing:0.04em; overflow:hidden; }}
  .health-strip .hs-tri {{ display:inline-flex; align-items:baseline; gap:4px;
    padding:3px 8px; border:1px solid var(--accent); border-radius:var(--r);
    background:var(--surface); }}
  .health-strip .hs-tri.zero {{ border-color:var(--border-base); }}
  .health-strip .hs-tri-n {{ font-size:14px; font-weight:600; color:var(--accent);
    line-height:1; font-variant-numeric:tabular-nums; }}
  .health-strip .hs-tri.zero .hs-tri-n {{ color:var(--sub); }}
  .health-strip .hs-tri-lbl {{ font-size:9px; letter-spacing:0.1em;
    text-transform:uppercase; color:var(--sub); }}
  .health-strip .hs-meta {{ display:inline-flex; gap:6px; align-items:baseline;
    color:var(--sub); }}
  .health-strip .hs-meta .hs-sid {{ color:var(--ink); font-weight:500; }}
  .health-strip .hs-meta .hs-dur {{ font-variant-numeric:tabular-nums; }}
  .health-strip .hs-meta > span + span::before {{ content:"·"; margin-right:6px;
    color:var(--border-base); }}
  .health-strip .hs-cams {{ display:inline-flex; gap:var(--s-2); flex-wrap:wrap; }}
  .health-strip .hs-cam {{ display:inline-flex; align-items:center; gap:6px;
    padding:2px 6px; border:1px solid var(--border-base); border-radius:var(--r);
    background:var(--bg); }}
  .health-strip .hs-cam.received {{ background:var(--surface); }}
  .health-strip .hs-cam.missing {{ border-color:var(--dev);
    background:rgba(192, 57, 43, 0.04); }}
  .health-strip .hs-fail {{ color:var(--dev); font-size:10px;
    text-transform:uppercase; letter-spacing:0.08em; }}
  .health-strip .hs-checks {{ display:inline-flex; gap:2px; }}
  .health-strip .hs-check {{ display:inline-flex; align-items:center;
    justify-content:center; width:14px; height:14px; font-size:10px;
    font-weight:700; border-radius:2px; }}
  .health-strip .hs-check.pass {{ color:var(--ok); }}
  .health-strip .hs-check.fail {{ color:var(--dev); }}
  .health-strip .hs-paths {{ display:inline-flex; gap:4px; }}
  .cam-badge {{ font-family:var(--mono); font-weight:600; font-size:11px;
    letter-spacing:0.18em; padding:2px 8px; border:1px solid;
    border-radius:var(--r); }}
  .cam-badge.A {{ color:var(--contra); border-color:var(--contra); }}
  .cam-badge.B {{ color:var(--dual); border-color:var(--dual); }}
  .path-stat {{ display:inline-flex; align-items:baseline; gap:3px;
    font-family:var(--mono); font-size:10px; letter-spacing:0.02em;
    padding:1px 5px; border:1px solid var(--border-l); border-radius:var(--r);
    background:transparent; color:inherit; }}
  .path-stat .lbl {{ font-size:9px; letter-spacing:0.12em; color:var(--sub);
    text-transform:uppercase; }}
  .path-stat .val {{ font-variant-numeric:tabular-nums; color:var(--ink); }}
  .path-stat .fps {{ font-variant-numeric:tabular-nums; color:var(--sub);
    font-size:9px; border-left:1px solid var(--border-l); padding-left:4px;
    margin-left:2px; }}
  .path-stat.off {{ opacity:0.45; }}
  .path-stat.off .val {{ color:var(--sub); }}
  .path-stat[data-rate-klass="ok"] {{ border-color:var(--ok); }}
  .path-stat[data-rate-klass="pending"] {{ border-color:var(--pending); }}
  .path-stat[data-rate-klass="fail"] {{ border-color:var(--dev); }}
  .fail-strip {{ font-family:var(--mono); font-size:12px;
    letter-spacing:0.02em; margin:var(--s-2) var(--s-5);
    padding:var(--s-2) var(--s-3); border-radius:var(--r);
    border:1px solid var(--dev); color:var(--dev);
    background:rgba(192, 57, 43, 0.06); display:flex;
    align-items:center; gap:var(--s-2); }}
  .fail-strip .icon {{ font-weight:700; }}
  .work {{ flex:1 1 auto; display:flex; min-height:0;
    border-bottom:1px solid var(--border-base); }}
  .scene-col {{ flex:{scene_flex}; min-width:280px; position:relative;
    background:var(--bg); }}
  #scene {{ position:absolute; inset:0; }}
  .videos-col {{ flex:{videos_flex}; min-width:320px; display:grid;
    grid-template-rows:1fr 1fr; gap:1px;
    background:var(--border-base); }}
  .work[data-mode="single-cam"] .videos-col {{ grid-template-rows:1fr; }}
  .work[data-mode="single-cam"] .videos-col {{ min-width:280px; }}
  .col-resizer {{ flex:0 0 6px; cursor:col-resize; position:relative;
    background:var(--border-base);
    transition:background 0.12s; user-select:none; touch-action:none; }}
  .col-resizer::before {{ content:""; position:absolute; left:2px; right:2px;
    top:50%; height:28px; transform:translateY(-50%);
    border-left:1px solid var(--sub); border-right:1px solid var(--sub);
    opacity:0.45; transition:opacity 0.12s; }}
  .col-resizer:hover, .col-resizer.dragging {{ background:var(--ink); }}
  .col-resizer:hover::before, .col-resizer.dragging::before {{ opacity:0; }}
  .col-resizer:focus-visible {{ outline:2px solid var(--ink); outline-offset:-2px; }}
  body.col-resizing {{ cursor:col-resize; user-select:none; }}
  body.col-resizing * {{ cursor:col-resize !important; }}
  .vid-cell {{ background:var(--surface); padding:var(--s-2) var(--s-3);
    display:flex; flex-direction:column; gap:var(--s-1); min-height:0;
    min-width:0; }}
  .vid-cell.collapsed {{ padding:var(--s-2) var(--s-3);
    flex-direction:row; align-items:center; gap:var(--s-2); }}
  .vid-head {{ display:flex; align-items:center; gap:var(--s-2); }}
  .vid-label {{ font-family:var(--mono); font-size:10px; font-weight:600;
    letter-spacing:0.18em; border:1px solid; padding:2px 8px; border-radius:var(--r); }}
  .vid-hint {{ font-family:var(--mono); font-size:10px; letter-spacing:0.06em;
    color:var(--sub); text-transform:uppercase; }}
  .vid-frame {{ flex:1 1 auto; min-height:0; min-width:0; width:100%; max-width:100%;
    position:relative; overflow:hidden; background:transparent; }}
  .vid-media {{ position:absolute; inset:0; margin:auto;
    max-width:100%; max-height:100%; background:#000;
    border-radius:var(--r); overflow:hidden; }}
  /* `contain` (was `cover`) keeps the video's pixel grid aligned with
     the canvas overlay — the cam-view's projection assumes the canvas
     covers the same image region as the video. `cover` would crop the
     video if calibration aspect ≠ video aspect (e.g. 4:3 calibration
     vs 16:9 video), making the projected dot float off the real ball.
     Letterbox is acceptable because the container already locks
     aspect-ratio to the calibration dims, so any mismatch is rare. */
  .vid-media video {{ display:block; width:100%; height:100%; object-fit:contain; }}
  /* Per-cam frame HUD: mirror of timeline label scoped to one cam, drawn
     over the video as a DOM layer (not canvas) so it stays legible at
     OVL=0. Same dark-on-light palette as cam-view-toolbar so the two
     read as a coherent pair. */
  .vid-media .vid-hud {{ position:absolute; top:8px; left:8px; z-index:4;
    background:rgba(26,23,20,0.78); border:1px solid rgba(255,255,255,0.12);
    border-radius:var(--r); padding:4px 7px;
    font-family:var(--mono); font-size:11px; line-height:1.35;
    letter-spacing:0.02em; font-variant-numeric:tabular-nums;
    color:#F8F7F4; pointer-events:none; }}
  .vid-media .vid-hud:empty {{ display:none; }}
  .vid-media .vid-hud .hud-row {{ display:flex; gap:8px; align-items:baseline; }}
  .vid-media .vid-hud .hud-row + .hud-row {{ margin-top:2px; }}
  .vid-media .vid-hud .hud-path {{ color:#9b948b; min-width:30px;
    letter-spacing:0.08em; font-size:9px; text-transform:uppercase; }}
  .vid-media .vid-hud .hud-fidx {{ color:#9b948b; font-size:9px; }}
  .vid-media .vid-hud .hud-mark {{ font-weight:600; }}
  .vid-media .vid-hud .hud-mark-kept {{ color:#7eb8a8; }}
  .vid-media .vid-hud .hud-mark-unscored {{ color:#7eb8a8; opacity:0.6; }}
  .vid-media .vid-hud .hud-mark-flicker {{ color:#e3b66f; }}
  .vid-media .vid-hud .hud-mark-jump {{ color:#e08177; }}
  .vid-media .vid-hud .hud-mark-no {{ color:#6e6863; font-weight:400; }}
  .plate-overlay-real {{ position:absolute; inset:0; width:100%; height:100%;
    pointer-events:none; z-index:1; }}
  .plate-overlay-real polygon {{ fill:none; stroke:rgba(217,59,59,0.92);
    stroke-width:1.8; stroke-dasharray:8 5; stroke-linejoin:round; }}
  /* Awaiting-upload placeholder. Mirrors `.vid-media`'s 16:9 (or
     calibration-aspect) box so the cell occupies the same footprint as
     a populated cell — keeps the work-row from reflowing when the MOV
     upload lands and the auto-refresh swaps in the real video. */
  .vid-media.empty {{ background:var(--bg); border:1px dashed var(--border-base);
    display:flex; align-items:center; justify-content:center;
    border-radius:var(--r); }}
  .vid-media.empty .vid-empty-msg {{ color:var(--sub); font-family:var(--mono);
    font-size:11px; letter-spacing:0.12em; text-transform:uppercase; }}
  .timeline {{ flex:0 0 auto; background:var(--surface); display:flex;
    flex-direction:column; gap:var(--s-2); padding:var(--s-2) var(--s-5);
    font-family:var(--mono); font-size:12px; color:var(--sub); position:relative; }}
  .tl-row {{ display:flex; align-items:center; gap:10px; }}
  .scrubber-wrap {{ flex:1 1 auto; display:flex; flex-direction:column;
    gap:3px; min-width:0; }}
  .scrubber-wrap input[type=range] {{ width:100%; accent-color:var(--ink); height:16px; margin:0; }}
  .scrubber-wrap canvas {{ display:block; width:100%; height:18px; border:1px solid var(--border-base);
    border-radius:var(--r); background:var(--bg); image-rendering:pixelated; }}
  .scrubber-wrap .strip-row canvas.strip-canvas {{ height:28px; }}
  .strip-row {{ display:flex; align-items:center; gap:6px; }}
  .strip-row .strip-label {{ font-family:var(--mono); font-size:9px; letter-spacing:0.1em;
    color:var(--sub); min-width:46px; text-align:right; flex:0 0 46px; }}
  .strip-row .strip-sublabels {{ display:flex; flex-direction:column; justify-content:space-around;
    font-family:var(--mono); font-size:8px; letter-spacing:0.05em; color:var(--sub);
    height:28px; line-height:1; flex:0 0 auto; padding:0 2px 0 0; text-align:right; }}
  .strip-row .strip-canvas {{ flex:1 1 auto; min-width:0; }}
  .strip-row[hidden] {{ display:none; }}
  .strip-row.is-reference {{ opacity:0.6; }}
  .strip-row.is-reference .strip-label::after {{ content:" · REF"; color:var(--sub); font-weight:400; }}
  .strip-note {{ font-size:9px; color:var(--sub); letter-spacing:0.04em; font-style:italic;
    padding:2px 0 0 52px; line-height:1.35; }}
  .strip-legend {{ font-size:10px; color:var(--sub); letter-spacing:0.06em; display:flex;
    gap:10px; align-items:center; flex-wrap:wrap; text-transform:uppercase; }}
  .strip-legend .sw {{ display:inline-block; width:10px; height:10px; vertical-align:middle;
    margin-right:4px; border:1px solid var(--border-base); }}
  .layer-toggles {{ margin-left:auto; display:flex; gap:6px; align-items:stretch; flex-wrap:wrap;
    padding:0; }}
  .layer-toggles .layer-label {{ color:var(--sub); letter-spacing:0.08em; font-size:10px;
    align-self:center; padding-right:4px; }}
  .layer-toggles .layer-group {{ display:inline-flex; align-items:center; gap:6px;
    padding:3px 8px; height:26px; box-sizing:border-box;
    border:1px solid var(--border-base); border-radius:var(--r); background:var(--surface); }}
  .layer-toggles .layer-group[data-layer="residual"],
  .layer-toggles .layer-group[data-layer="fitres"] {{ border-color:var(--ink-light, #7a756c); }}
  .layer-toggles .layer-name {{ font-size:10px; letter-spacing:0.1em; color:var(--ink); text-transform:uppercase;
    display:inline-flex; align-items:center; gap:4px; font-weight:500; }}
  .layer-toggles .layer-name .swatch {{ width:8px; height:8px; display:inline-block; border:1px solid rgba(0,0,0,0.12); }}
  .layer-toggles .layer-pill {{ font:inherit; font-size:9px; letter-spacing:0.08em; padding:2px 8px;
    background:transparent; color:var(--sub); border:1px solid var(--border-base); border-radius:2px;
    cursor:pointer; text-transform:uppercase; line-height:1;
    transition:background 0.12s, color 0.12s, border-color 0.12s; }}
  .layer-toggles .layer-pill[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); }}
  .layer-toggles .layer-pill:hover {{ border-color:var(--ink); }}
  .layer-toggles .layer-pill[hidden] {{ display:none; }}
  .layer-toggles .layer-checkbox {{ display:inline-flex; align-items:center; gap:5px; cursor:pointer; height:100%; }}
  .layer-toggles .layer-checkbox input {{ accent-color:var(--ink); cursor:pointer; width:13px; height:13px; margin:0; vertical-align:middle; }}
  .layer-toggles .layer-divider {{ width:1px; align-self:stretch;
    background:var(--border-base); margin:2px 2px; }}
  .layer-toggles input[type="range"] {{ -webkit-appearance:none; appearance:none;
    width:96px; height:2px; background:var(--border-base); border-radius:2px;
    margin:0; accent-color:var(--ink); cursor:pointer; }}
  .layer-toggles input[type="range"]::-webkit-slider-thumb {{ -webkit-appearance:none;
    width:12px; height:12px; background:var(--ink); border-radius:50%; cursor:pointer;
    border:1px solid var(--ink); }}
  .layer-toggles input[type="range"]::-moz-range-thumb {{ width:12px; height:12px; background:var(--ink);
    border-radius:50%; cursor:pointer; border:1px solid var(--ink); }}
  .layer-toggles .layer-group .readout {{ font:inherit; font-size:9px; letter-spacing:0.06em;
    color:var(--sub); min-width:52px; display:inline-block; text-align:right;
    font-variant-numeric:tabular-nums; }}
  /* --- Fixed-width playback info card ---
     Pinned width so scrubber doesn't reflow as per-path stats change. */
  .tl-row .frame-label {{ flex:0 0 auto; width:260px; padding:6px 10px;
    border:1px solid var(--border-base); border-radius:var(--r); background:var(--surface);
    color:var(--ink); font-size:10px; letter-spacing:0.02em;
    font-variant-numeric:tabular-nums; display:flex; flex-direction:column; gap:4px;
    align-self:stretch; justify-content:center; }}
  .tl-row .frame-label .frame-label-head {{ display:flex; justify-content:space-between;
    align-items:baseline; gap:8px; }}
  .tl-row .frame-label .primary {{ color:var(--ink); font-weight:600; font-size:14px;
    font-variant-numeric:tabular-nums; letter-spacing:0.04em; }}
  .tl-row .frame-label .frame-meta {{ display:inline-flex; align-items:baseline; gap:2px;
    color:var(--sub); }}
  .tl-row .frame-label .frame-slash {{ color:var(--sub); padding:0 1px; }}
  .tl-row .frame-label .frame-total {{ color:var(--sub); font-weight:400; }}
  .tl-row .frame-label .frame-label-body {{ display:flex; flex-direction:column; gap:2px;
    border-top:1px solid var(--border-base); padding-top:4px; }}
  .tl-row .frame-label .frame-label-body:empty {{ display:none; }}
  .tl-row .frame-label .fl-row {{ display:grid; grid-template-columns:40px 1fr 1fr;
    gap:8px; align-items:baseline; color:var(--ink); }}
  .tl-row .frame-label .fl-pathlabel {{ color:var(--sub); letter-spacing:0.08em;
    text-transform:uppercase; font-size:9px; }}
  .tl-row .frame-label .fl-cell {{ display:inline-flex; align-items:baseline; gap:3px;
    color:var(--ink); font-weight:500; }}
  .tl-row .frame-label .fl-cell-blank {{ color:var(--sub); font-weight:400; }}
  .tl-row .frame-label .fl-det {{ color:var(--contra); font-weight:600; }}
  .tl-row .frame-label .fl-det-no {{ color:var(--sub); font-weight:400; }}
  .tl-row .frame-label .fl-det-warn {{ color:var(--pending); font-weight:600; }}
  .tl-row .frame-label .fl-det-bad {{ color:var(--dev); font-weight:600; }}
  .tl-row .frame-label .fl-det-unscored {{ opacity:0.55; }}
  .tl-row .frame-label .fl-fidx {{ color:var(--sub); font-size:9px;
    letter-spacing:0; padding:0 1px; }}
  #frame-input {{ width:58px; font:inherit; font-size:10px; background:var(--bg);
    border:1px solid var(--border-base); color:var(--ink); padding:1px 4px; text-align:center;
    font-variant-numeric:tabular-nums; border-radius:var(--r); }}
  #frame-input:focus {{ outline:none; border-color:var(--ink); }}
  #frame-input::-webkit-inner-spin-button,
  #frame-input::-webkit-outer-spin-button {{ opacity:0.4; }}
  .timeline button {{ padding:5px 12px; font:inherit; font-size:11px; letter-spacing:0.08em;
    text-transform:uppercase; border:1px solid var(--border-base); background:var(--bg); color:var(--ink);
    border-radius:var(--r); cursor:pointer; min-width:42px;
    transition:border-color 0.15s, background 0.15s, color 0.15s; }}
  .timeline button:hover {{ border-color:var(--ink); }}
  .timeline button:disabled {{ opacity:0.4; cursor:not-allowed; }}
  .timeline .transport {{ display:inline-flex; align-items:stretch; gap:0; padding:0;
    background:var(--surface); border:1px solid var(--border-base); border-radius:var(--r);
    overflow:hidden; height:30px; }}
  .timeline .transport button {{ border:none; border-left:1px solid var(--border-base);
    background:transparent; width:32px; height:100%; min-width:32px; padding:0;
    font-size:12px; color:var(--sub); border-radius:0;
    display:inline-flex; align-items:center; justify-content:center; letter-spacing:0;
    transition:background 0.12s, color 0.12s; }}
  .timeline .transport button:first-child {{ border-left:none; }}
  .timeline .transport button:hover:not(:disabled) {{ background:var(--surface-hover); color:var(--ink); }}
  .timeline .transport button svg {{ width:14px; height:14px; display:block; }}
  .timeline .transport .play-btn {{ min-width:72px; width:auto; height:100%; padding:0 16px;
    background:var(--ink); color:var(--surface); font-weight:500; font-size:10px;
    letter-spacing:0.12em; text-transform:uppercase; border-radius:0; margin:0; }}
  .timeline .transport .play-btn:hover:not(:disabled) {{ background:var(--ink-light); color:var(--surface); }}
  .speed-group {{ display:inline-flex; border:1px solid var(--border-base); border-radius:var(--r);
    overflow:hidden; background:var(--surface); height:30px; }}
  .speed-group button {{ border:none; background:transparent; color:var(--sub); padding:0 12px;
    min-width:auto; border-radius:0; font-size:10px; letter-spacing:0.06em; height:100%;
    border-right:1px solid var(--border-l); font-variant-numeric:tabular-nums; }}
  .speed-group button:last-child {{ border-right:none; }}
  .speed-group button.active {{ background:var(--ink); color:var(--surface); font-weight:500; }}
  .scene-col .scene-toolbar {{ position:absolute; top:var(--s-4); right:var(--s-3); z-index:5;
    display:inline-flex; align-items:stretch; flex-wrap:nowrap; white-space:nowrap;
    border:1px solid var(--border-base);
    border-radius:var(--r); overflow:hidden; background:var(--surface); }}
  .scene-col .scene-toolbar button {{ padding:5px 12px; border:none; background:transparent; color:var(--sub);
    cursor:pointer; min-width:auto; border-radius:0; font:inherit; font-size:11px; letter-spacing:0.1em;
    text-transform:uppercase; font-weight:400; line-height:1; }}
  .scene-col .scene-toolbar button.active {{ background:var(--ink); color:var(--surface); font-weight:500; }}
  .scene-col .scene-toolbar button[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); font-weight:500; }}
  /* Camera preset picker (top-left). Mirrors .scene-toolbar styling so
     the two pills read as one design language, just sitting on opposite
     corners. `active` state means the camera currently matches the
     preset; first user-drag (plotly_relayouting) clears it because the
     view is no longer pinned. */
  .scene-col .scene-views {{ position:absolute; top:var(--s-4); left:var(--s-3); z-index:5;
    display:inline-flex; align-items:stretch; flex-wrap:nowrap; white-space:nowrap;
    border:1px solid var(--border-base); border-radius:var(--r);
    overflow:hidden; background:var(--surface); }}
  .scene-col .scene-views .view-preset {{ padding:5px 10px; border:none; background:transparent;
    color:var(--sub); cursor:pointer; min-width:auto; border-radius:0; font:inherit;
    font-family:var(--mono); font-size:10px; letter-spacing:0.12em; text-transform:uppercase;
    font-weight:500; line-height:1; }}
  .scene-col .scene-views .view-preset + .view-preset {{ border-left:1px solid var(--border-l); }}
  .scene-col .scene-views .view-preset:hover {{ color:var(--ink); }}
  .scene-col .scene-views .view-preset.active {{ background:var(--ink); color:var(--surface); }}
  .layer-source-group {{ display:inline-flex; align-items:center; margin-left:6px; height:100%;
    border:1px solid var(--border-base); border-radius:var(--r); overflow:hidden; }}
  /* Source pills go dormant when Fit is off — picking svr/live without
     a visible Fit overlay does nothing user-observable. Stronger than
     plain opacity: drop saturation so the pressed-state black bg fades
     to grey, and gate pointer-events so a click can't silently change
     the dormant source. Pressed state still tracks the user's choice
     so re-enabling Fit shows them what they had. */
  .layer-source-group.is-off {{ opacity:0.4; filter:saturate(0.15); pointer-events:none; }}
  .fit-src-pill {{ padding:2px 8px; font-family:var(--mono); font-size:9px;
    letter-spacing:0.06em; background:var(--surface); border:0; color:var(--sub);
    cursor:pointer; line-height:1; }}
  .fit-src-pill[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); }}
  .fit-src-pill + .fit-src-pill {{ border-left:1px solid var(--border-base); }}
  .fit-src-pill[disabled] {{ opacity:0.35; cursor:not-allowed; }}
  .scene-col .fit-info {{ position:absolute; top:54px; right:var(--s-3); z-index:6;
    background:var(--surface); border:1px solid var(--border-base); border-radius:var(--r);
    padding:8px 12px; font:inherit; font-size:11px; line-height:1.55; color:var(--ink);
    min-width:220px; max-width:300px; pointer-events:none; }}
  .scene-col .fit-info[hidden] {{ display:none; }}
  .scene-col .fit-info .fit-row {{ display:flex; justify-content:space-between; gap:var(--s-3);
    font-variant-numeric:tabular-nums; }}
  .scene-col .fit-info .fit-row .k {{ color:var(--sub); letter-spacing:0.04em; }}
  .scene-col .fit-info .fit-row .v {{ font-weight:500; }}
  .scene-col .fit-info h4 {{ margin:0 0 6px 0; font:inherit; font-size:10px; letter-spacing:0.1em;
    text-transform:uppercase; color:var(--sub); font-weight:500; }}
  .scene-col .fit-info .fit-warn {{ color:#A7372A; font-size:10px; margin-top:6px; }}
  .scene-col .speed-bars {{ position:absolute; left:var(--s-3); right:var(--s-3); bottom:var(--s-3);
    height:120px; z-index:3; background:var(--surface); border:1px solid var(--border-base);
    border-radius:var(--r); padding:4px 8px; pointer-events:none; }}
  /* Re-enable hover/click ONLY for the Plotly chart inside, so the
     bottom 120 px of the 3D scene still accepts orbit drags everywhere
     except directly over a bar. */
  .scene-col .speed-bars > .js-plotly-plot {{ pointer-events:auto; }}
  .scene-col .speed-bars[hidden] {{ display:none; }}
  .hint-btn {{ font:inherit; font-size:12px; padding:0; width:26px; height:26px; border:1px solid var(--border-base);
    background:var(--surface); color:var(--sub); border-radius:50%; cursor:pointer; margin-left:auto;
    min-width:auto; font-weight:600; letter-spacing:0; display:inline-flex; align-items:center; justify-content:center; }}
  .hint-overlay {{ position:absolute; bottom:60px; right:var(--s-5); background:var(--surface);
    border:1px solid var(--border-base); padding:var(--s-3) var(--s-4); font:inherit; font-size:11px;
    color:var(--ink); display:none; z-index:10; border-radius:var(--r); min-width:240px; }}
  .hint-overlay.open {{ display:block; }}
  .hint-overlay h4 {{ margin:0 0 8px; font-family:var(--mono); font-size:10px; letter-spacing:0.18em;
    text-transform:uppercase; color:var(--sub); font-weight:600; }}
  .hint-overlay table {{ border-collapse:collapse; width:100%; }}
  .hint-overlay td {{ padding:2px 8px; vertical-align:top; }}
  .hint-overlay td:first-child {{ color:var(--sub); font-family:var(--mono); white-space:nowrap; }}
  @media (max-height: 980px) {{
    .health-strip {{ gap:var(--s-2); font-size:10px; }}
    .health-strip .hs-tri-n {{ font-size:12px; }}
    .health-strip .hs-cam {{ padding:1px 5px; }}
    .timeline {{ gap:6px; padding:6px var(--s-5); }}
    .scrubber-wrap canvas {{ height:16px; }}
    .scrubber-wrap .strip-row canvas.strip-canvas {{ height:24px; }}
    .strip-row .strip-label {{ min-width:42px; flex-basis:42px; }}
    .strip-row .strip-sublabels {{ height:24px; font-size:7px; }}
    .strip-note {{ padding-left:48px; }}
    .scene-col .scene-toolbar {{ top:10px; right:8px; }}
    .scene-col .scene-views {{ top:10px; left:8px; }}
    .scene-col .fit-info {{ top:48px; }}
  }}
"""


_VIEWER_STATIC_DIR = Path(__file__).parent / "static" / "viewer"


def _resolve_viewer_js_template() -> str:
    """Concatenate `static/viewer/*.js` (alphabetical order) and substitute
    the shared virt-canvas helpers. Same pattern as
    `render_dashboard_client._resolve_js_template`: the placeholders are
    literal `{NAME}` tokens (NOT f-string fields — the JS body is full of
    real braces that would explode `.format()`), so resolve via
    `str.replace` before embedding."""
    js = "".join(
        f.read_text(encoding="utf-8")
        for f in sorted(_VIEWER_STATIC_DIR.glob("*.js"))
    )
    js = js.replace("{PLATE_WORLD_JS}", PLATE_WORLD_JS)
    js = js.replace("{PROJECTION_JS}", PROJECTION_JS)
    js = js.replace("{DRAW_VIRTUAL_BASE_JS}", DRAW_VIRTUAL_BASE_JS)
    return js


_VIEWER_JS_TEMPLATE = _resolve_viewer_js_template()


def _viewer_js() -> str:
    # Leading "\n" preserves the byte-for-byte HTML rendering from when
    # this used to be `return f"""\n(() => {...}})();\n"""`.
    return "\n" + _VIEWER_JS_TEMPLATE
