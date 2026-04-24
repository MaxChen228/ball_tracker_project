from __future__ import annotations

from dataclasses import dataclass
import json as _json

from reconstruct import Scene
from render_compare import (
    DRAW_PLATE_OVERLAY_JS,
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
    health_banner_html,
    video_cell_html,
    virtual_cell_html,
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
    health_html: str
    video_cells_html: str
    virtual_cells_html: str
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
    virt_cells = "".join(
        virtual_cell_html(
            cam,
            pose_available=(cam in cams_by_id),
            image_width_px=(cams_by_id[cam].image_width_px if cam in cams_by_id else None),
            image_height_px=(cams_by_id[cam].image_height_px if cam in cams_by_id else None),
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
        health_html=health_banner_html(health),
        video_cells_html=video_cells,
        virtual_cells_html=virt_cells,
        session_id=scene.session_id,
        server_post_ran=server_post_ran,
        can_run_server=can_run_server,
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
</style>
</head><body>
<div class="viewer">
  <div class="nav">
    <span class="brand"><span class="dot"></span>BALL_TRACKER</span>
    <span class="nav-spacer"></span>
    {action_html}
    <a class="back" href="/">&larr; dashboard</a>
  </div>
  {ctx.health_html}
  <div class="work" data-mode="{ctx.layout_mode}">
    <div class="scene-col">
      <div id="scene"></div>
      <div class="scene-toolbar" role="toolbar" aria-label="Scene controls">
        <button id="scene-reset" class="reset" type="button" title="Reset 3D view">&#x21BA;</button>
        <div class="divider" aria-hidden="true"></div>
        <button id="mode-all" class="active" type="button" role="tab" title="Show full trajectory">All</button>
        <button id="mode-playback" type="button" role="tab" title="Cut trace at playback time">Playback</button>
      </div>
    </div>
    <div class="col-resizer" id="col-resizer" role="separator" aria-orientation="vertical" aria-label="Resize 3D scene vs cameras" tabindex="0" title="Drag to resize"></div>
    <div class="videos-col">{ctx.video_cells_html}{ctx.virtual_cells_html}</div>
  </div>
  <div class="timeline">
    <div class="tl-row">
      <div class="scrubber-wrap">
        <div class="strip-legend" aria-hidden="true">
          <span>detection:</span>
          <span><span class="sw" style="background:var(--contra);border-color:var(--contra);"></span>A detected</span>
          <span><span class="sw" style="background:var(--dual);border-color:var(--dual);"></span>B detected</span>
          <span><span class="sw" style="background:rgba(122,117,108,0.35);"></span>missed</span>
          <span><span class="sw" style="background:rgba(232,228,219,0.6);"></span>no frame</span>
          <span><span class="sw" style="background:var(--accent);border-color:var(--accent);"></span>chirp anchor</span>
          <span class="layer-toggles" id="layer-toggles" aria-label="Layer visibility">
            <span class="layer-label">show:</span>
            <span class="layer-group" data-layer="traj">
              <span class="layer-name">Traj</span>
              <button type="button" class="layer-pill" data-layer="traj" data-path="live" aria-pressed="false" disabled title="live stream carries no triangulation">live</button>
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
        <div class="strip-note" id="strip-note-multi" hidden>
          兩條 detection pipeline 獨立：LIVE 是 iOS 端在 raw BGRA 上跑；SVR 是 server 解碼後在 BGR 上跑。共用同一 chirp anchor，色塊差異 = pipeline 差異、不是時間錯位。
        </div>
      </div>
      <span id="frame-label" class="frame-label">
        frame <input id="frame-input" type="number" min="0" max="0" value="0" step="1" title="Type a frame index to jump" /> / <span id="frame-total">0</span>
        <span class="sub" id="frame-sub">t=0.000s</span>
      </span>
    </div>
    <div class="tl-row">
      <div class="transport" role="group" aria-label="transport">
        <button id="step-first" type="button" title="First frame (Home)">&#x23ee;</button>
        <button id="step-back" type="button" title="Prev frame (,)">&#x23ea;</button>
        <button id="play-btn" class="play-btn" type="button" title="Play/pause (Space)">Play</button>
        <button id="step-fwd" type="button" title="Next frame (.)">&#x23e9;</button>
        <button id="step-last" type="button" title="Last frame (End)">&#x23ed;</button>
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
  .nav .nav-spacer {{ flex:1 1 auto; }}
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
  .health {{ flex:0 0 auto; background:var(--surface);
    border-bottom:1px solid var(--border-base);
    padding:var(--s-3) var(--s-5);
    display:flex; flex-direction:column; gap:var(--s-2); }}
  .health-row {{ display:grid; grid-template-columns:1fr 1fr;
    gap:var(--s-3); align-items:stretch; }}
  .cam-stack {{ display:flex; flex-direction:column; gap:var(--s-2); }}
  .hero-card {{ border:1px solid var(--border-base); border-radius:var(--r);
    padding:var(--s-2) var(--s-4); background:var(--bg); display:flex;
    flex-direction:column; justify-content:center; gap:var(--s-1); }}
  .hero-card.ok {{ background:var(--surface); border-color:var(--accent); }}
  .hero-title {{ font-family:var(--mono); font-size:10px;
    letter-spacing:0.12em; text-transform:uppercase; color:var(--sub); }}
  .hero-tri {{ font-family:var(--mono);
    font-size:clamp(30px, 3.2vh, 40px); font-weight:500;
    line-height:1; color:var(--accent); letter-spacing:0.02em; }}
  .hero-tri.zero {{ color:var(--sub); }}
  .hero-note {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.04em; color:var(--sub); }}
  .hero-sub {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.04em; color:var(--sub); margin-top:var(--s-1);
    border-top:1px solid var(--border-l); padding-top:6px; }}
  .cam-card {{ border:1px solid var(--border-base); border-radius:var(--r);
    padding:6px 10px; background:var(--bg);
    display:flex; flex-direction:column; gap:var(--s-1); }}
  .cam-card.received {{ background:var(--surface); }}
  .cam-card.missing {{ opacity:0.85; flex-direction:row;
    align-items:center; gap:10px; }}
  .cam-head {{ display:flex; align-items:center; gap:10px;
    flex-wrap:wrap; }}
  .cam-badge {{ font-family:var(--mono); font-weight:600; font-size:11px;
    letter-spacing:0.18em; padding:2px 8px; border:1px solid;
    border-radius:var(--r); }}
  .cam-badge.A {{ color:var(--contra); border-color:var(--contra); }}
  .cam-badge.B {{ color:var(--dual); border-color:var(--dual); }}
  .cam-state {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.08em; text-transform:uppercase; }}
  .cam-state.ok {{ color:var(--ok); }}
  .cam-state.fail {{ color:var(--dev); }}
  .cam-note {{ font-family:var(--mono); font-size:11px; color:var(--sub);
    letter-spacing:0.02em; }}
  .cam-checks {{ display:inline-flex; flex-wrap:wrap; gap:4px 12px;
    margin-left:auto; }}
  .check {{ font-family:var(--mono); font-size:11px;
    letter-spacing:0.04em; color:var(--sub);
    display:inline-flex; align-items:center; gap:6px; }}
  .check .mark {{ font-weight:700; width:12px; display:inline-block;
    text-align:center; }}
  .check.pass {{ color:var(--ink); }}
  .check.pass .mark {{ color:var(--ok); }}
  .check.fail .mark {{ color:var(--dev); }}
  .cam-rate {{ display:flex; align-items:center; gap:10px; }}
  .cam-stats {{ font-family:var(--mono); font-size:12px; color:var(--ink);
    letter-spacing:0.02em; white-space:nowrap; display:inline-flex; gap:8px; align-items:center; }}
  .cam-stats .n {{ font-weight:500; }}
  .cam-stats .of {{ color:var(--sub); }}
  .path-stat {{ display:inline-flex; align-items:baseline; gap:3px;
    font-family:var(--mono); font-size:11px; letter-spacing:0.02em;
    padding:1px 5px; border:1px solid var(--border-l); border-radius:var(--r); }}
  .path-stat .lbl {{ font-size:9px; letter-spacing:0.12em; color:var(--sub);
    text-transform:uppercase; }}
  .path-stat .val {{ font-variant-numeric:tabular-nums; color:var(--ink); }}
  .path-stat .fps {{ font-variant-numeric:tabular-nums; color:var(--sub);
    font-size:10px; border-left:1px solid var(--border-l); padding-left:4px;
    margin-left:2px; }}
  .path-stat.off {{ opacity:0.45; }}
  .path-stat.off .val {{ color:var(--sub); }}
  .rate-bar {{ flex:1 1 auto; min-width:60px; height:4px;
    background:var(--border-l); border-radius:var(--r); overflow:hidden;
    display:inline-block; }}
  .rate-fill {{ display:block; height:100%; transition:width .3s; }}
  .rate-fill.ok {{ background:var(--ok); }}
  .rate-fill.pending {{ background:var(--pending); }}
  .rate-fill.fail {{ background:var(--dev); }}
  .rate-empty {{ font-family:var(--mono); font-size:12px;
    color:var(--sub); flex:1; }}
  .fail-strip {{ font-family:var(--mono); font-size:12px;
    letter-spacing:0.02em; padding:var(--s-2) var(--s-3); border-radius:var(--r);
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
    grid-template-columns:1fr 1fr; grid-template-rows:1fr 1fr; gap:1px;
    background:var(--border-base); }}
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
  .virt-cell {{ background:var(--surface); padding:var(--s-2) var(--s-3);
    display:flex; flex-direction:column; gap:var(--s-1); min-height:0;
    min-width:0; position:relative; }}
  .virt-frame {{ flex:1 1 auto; min-height:0; min-width:0; width:100%; max-width:100%;
    position:relative; overflow:hidden; background:transparent; }}
  .virt-media {{ position:absolute; inset:0; margin:auto;
    max-width:100%; max-height:100%; background:#1A1714;
    border:1px solid var(--border-base); border-radius:var(--r); overflow:hidden; }}
  .virt-media canvas {{ display:block; width:100%; height:100%; }}
  .virt-frame.empty {{ display:flex; align-items:center; justify-content:center;
    color:var(--sub); font-family:var(--mono); font-size:11px; letter-spacing:0.12em;
    text-transform:uppercase; background:var(--bg);
    border:1px dashed var(--border-base); border-radius:var(--r); }}
  .vid-label.virt {{ color:var(--sub); border-color:var(--border-base); }}
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
  .vid-media video {{ display:block; width:100%; height:100%; object-fit:cover; }}
  .plate-overlay-real {{ position:absolute; inset:0; width:100%; height:100%;
    pointer-events:none; z-index:1; }}
  .plate-overlay-real polygon {{ fill:none; stroke:rgba(217,59,59,0.92);
    stroke-width:1.8; stroke-dasharray:8 5; stroke-linejoin:round; }}
  .pp-cross {{ position:absolute; width:14px; height:14px;
    transform:translate(-50%, -50%); pointer-events:none; z-index:2; }}
  .pp-cross::before, .pp-cross::after {{ content:""; position:absolute;
    background:rgba(255,255,255,0.7); box-shadow:0 0 2px rgba(0,0,0,0.85); }}
  .pp-cross::before {{ left:0; right:0; top:50%; height:1px; transform:translateY(-0.5px); }}
  .pp-cross::after {{ top:0; bottom:0; left:50%; width:1px; transform:translateX(-0.5px); }}
  .vid-frame.empty {{ background:var(--bg); border:1px dashed var(--border-base);
    color:var(--sub); font-family:var(--mono); font-size:11px;
    letter-spacing:0.12em; text-transform:uppercase; border-radius:var(--r); }}
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
  .layer-toggles {{ margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  .layer-toggles .layer-label {{ color:var(--sub); letter-spacing:0.08em; font-size:10px; }}
  .layer-toggles .layer-group {{ display:inline-flex; align-items:center; gap:4px; padding:2px 4px 2px 6px;
    border:1px solid var(--border-base); border-radius:var(--r); }}
  .layer-toggles .layer-name {{ font-size:10px; letter-spacing:0.08em; color:var(--ink); text-transform:uppercase;
    padding-right:2px; display:inline-flex; align-items:center; gap:4px; }}
  .layer-toggles .layer-name .swatch {{ width:8px; height:8px; display:inline-block; border:1px solid rgba(0,0,0,0.12); }}
  .layer-toggles .layer-pill {{ font:inherit; font-size:9px; letter-spacing:0.06em; padding:1px 6px;
    background:transparent; color:var(--sub); border:1px solid var(--border-base); border-radius:2px;
    cursor:pointer; text-transform:uppercase; transition:background 0.12s, color 0.12s, border-color 0.12s; }}
  .layer-toggles .layer-pill[aria-pressed="true"] {{ background:var(--ink); color:var(--surface); border-color:var(--ink); }}
  .layer-toggles .layer-pill:hover {{ border-color:var(--ink); }}
  .layer-toggles .layer-pill[hidden] {{ display:none; }}
  .tl-row .frame-label {{ min-width:300px; text-align:right; color:var(--ink); font-weight:500; font-size:10px;
    letter-spacing:0.02em; white-space:nowrap; font-variant-numeric:tabular-nums;
    display:inline-flex; align-items:center; justify-content:flex-end; gap:4px; }}
  .tl-row .frame-label .sub {{ color:var(--sub); font-weight:400; }}
  .tl-row .frame-label .det {{ color:var(--contra); font-weight:500; }}
  .tl-row .frame-label .det.no {{ color:var(--sub); }}
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
  .scene-col .scene-toolbar {{ position:absolute; top:var(--s-2); right:var(--s-2); z-index:5;
    display:inline-flex; align-items:stretch; border:1px solid var(--border-base);
    border-radius:var(--r); overflow:hidden; background:var(--surface); }}
  .scene-col .scene-toolbar button {{ padding:5px 12px; border:none; background:transparent; color:var(--sub);
    cursor:pointer; min-width:auto; border-radius:0; font:inherit; font-size:11px; letter-spacing:0.1em;
    text-transform:uppercase; font-weight:400; line-height:1; }}
  .scene-col .scene-toolbar button.active {{ background:var(--ink); color:var(--surface); font-weight:500; }}
  .scene-col .scene-toolbar .reset {{ font-size:14px; padding:4px 12px; }}
  .scene-col .scene-toolbar .divider {{ width:1px; background:var(--border-base); align-self:stretch; }}
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
    .health {{ padding:var(--s-2) var(--s-5); gap:6px; }}
    .health-row {{ gap:10px; }}
    .cam-stack {{ gap:6px; }}
    .hero-card {{ padding:6px 10px; }}
    .hero-tri {{ font-size:28px; }}
    .hero-note, .hero-sub, .cam-note, .cam-state, .check, .cam-stats {{
      font-size:10px;
    }}
    .timeline {{ gap:6px; padding:6px var(--s-5); }}
    .scrubber-wrap canvas {{ height:16px; }}
    .scrubber-wrap .strip-row canvas.strip-canvas {{ height:24px; }}
    .strip-row .strip-label {{ min-width:42px; flex-basis:42px; }}
    .strip-row .strip-sublabels {{ height:24px; font-size:7px; }}
    .strip-note {{ padding-left:48px; }}
    .scene-col .scene-toolbar {{ top:6px; right:6px; }}
  }}
"""


def _viewer_js() -> str:
    return f"""
(() => {{
  const DATA = JSON.parse(document.getElementById("viewer-data").textContent);
  const SCENE = DATA.scene;
  const STATIC = DATA.static_traces || [];
  const LAYOUT = DATA.layout;
  const CAM_COLOR = DATA.camera_colors || {{}};
  const FALLBACK = DATA.fallback_color;
  const ACCENT = DATA.accent_color;
  // Two detection pipelines. Their string IDs match
  // server/schemas.py::DetectionPath so we never have to translate.
  const PATHS = ["live", "server_post"];
  const PATH_LABEL = {{ live: "live", server_post: "svr" }};
  // reconstruct.py still tags rays with the older source strings; map here
  // once so the rest of the JS speaks in DetectionPath IDs exclusively.
  function sourceToPath(source) {{
    if (source === "live") return "live";
    return "server_post";
  }}
  // Per-path hue (source = colour), A/B shade within each hue (camera =
  // lightness). Replaces the earlier solid/dash/dot distinction — users
  // read hue faster than dash patterns in a dense 3D scene.
  const PATH_COLORS = {{
    live:        {{ A: "#B8451F", B: "#E08B5F" }},
    server_post: {{ A: "#4A6B8C", B: "#89A5BD" }},
  }};
  function colorForCamPath(cam, path) {{
    const bucket = PATH_COLORS[path];
    if (bucket && bucket[cam]) return bucket[cam];
    return CAM_COLOR[cam] || FALLBACK;
  }}
  const PATH_DASH = {{ live: "solid", server_post: "solid" }};
  const PATH_OPACITY = {{ live: 0.55, server_post: 0.55 }};
  const PATH_MARKER_SYMBOL = {{ live: "circle", server_post: "circle" }};
  const SCENE_THEME = DATA.scene_theme || {{
    cam_axis_len_m: 0.25, cam_fwd_len_m: 0.5,
    axis_color_right: "#C0392B", axis_color_up: "rgba(42, 37, 32, 0.4)",
  }};
  const VIDEO_META = DATA.videos || [];
  const HAS_TRIANGULATED = DATA.has_triangulated;
  const sceneDiv = document.getElementById("scene");
  const playBtn = document.getElementById("play-btn");
  const scrubber = document.getElementById("scrubber");
  const frameInput = document.getElementById("frame-input");
  const frameTotal = document.getElementById("frame-total");
  const frameSub = document.getElementById("frame-sub");
  const modeAll = document.getElementById("mode-all");
  const modePlayback = document.getElementById("mode-playback");
  const stepFirstBtn = document.getElementById("step-first");
  const stepBackBtn = document.getElementById("step-back");
  const stepFwdBtn = document.getElementById("step-fwd");
  const stepLastBtn = document.getElementById("step-last");
  const speedGroup = document.getElementById("speed-group");
  const sceneResetBtn = document.getElementById("scene-reset");
  const hintBtn = document.getElementById("hint-btn");
  const hintOverlay = document.getElementById("hint-overlay");
  const DEFAULT_CAMERA = (LAYOUT && LAYOUT.scene && LAYOUT.scene.camera)
    ? JSON.parse(JSON.stringify(LAYOUT.scene.camera))
    : {{eye: {{x: 1.5, y: 1.5, z: 1.0}}, up: {{x: 0, y: 0, z: 1}}, center: {{x: 0, y: 0.2, z: 0.3}}}};
  const vids = Array.from(document.querySelectorAll("video[data-cam]"));
  const offsetByCam = Object.fromEntries(VIDEO_META.map(v => [v.camera_id, v.t_rel_offset_s]));
  const fpsByCam = Object.fromEntries(VIDEO_META.map(v => [v.camera_id, v.fps]));
  function pickMasterVideo() {{
    if (!vids.length) return null;
    let master = vids[0];
    let masterCount = -1;
    for (const v of vids) {{
      // Prefer the video whose own camera has the richest *any-path* detection
      // history — that's the one we want RVFC to drive the scrubber off.
      let n = 0;
      for (const path of PATHS) {{
        n += (framesByPath[path][v.dataset.cam]?.t_rel_s || []).length;
      }}
      if (n > masterCount) {{ master = v; masterCount = n; }}
    }}
    return master;
  }}
  // framesByPath[path][cam] = {{t_rel_s, detected, px, py}}. Three entries
  // always present (even if empty) so the rest of the JS can iterate PATHS
  // without null checks.
  const framesByPath = {{ live: {{}}, server_post: {{}} }};
  for (const v of VIDEO_META) {{
    const f = v.frames || {{}};
    for (const path of PATHS) {{
      const stream = f[path] || {{ t_rel_s: [], detected: [], px: [], py: [] }};
      framesByPath[path][v.camera_id] = {{
        t_rel_s: stream.t_rel_s || [],
        detected: stream.detected || [],
        px: stream.px || [],
        py: stream.py || [],
      }};
    }}
  }}
  const camsWithFramesByPath = {{}};
  for (const path of PATHS) {{
    camsWithFramesByPath[path] = Object.keys(framesByPath[path])
      .filter(c => (framesByPath[path][c].t_rel_s || []).length)
      .sort();
  }}
  // Did any camera produce rays / points / frames on this pipeline? Used to
  // hide inapplicable pills (so a live-only session doesn't show dead SVR /
  // POST toggles).
  const HAS_PATH = {{
    live: camsWithFramesByPath.live.length > 0
      || (SCENE.rays || []).some(r => sourceToPath(r.source || "server") === "live"),
    server_post: camsWithFramesByPath.server_post.length > 0
      || Object.keys(SCENE.ground_traces || {{}}).length > 0
      || (SCENE.triangulated || []).length > 0,
  }};
  // Per-cam applicability: single-camera sessions must not light up the
  // other cam's pills as dead buttons. Falls back to HAS_PATH for any cam
  // we don't enumerate here.
  const HAS_PATH_PER_CAM = {{}};
  for (const cam of ["A", "B"]) {{
    const raySrc = (p) => (SCENE.rays || []).some(r => r.camera_id === cam && sourceToPath(r.source || "server") === p);
    HAS_PATH_PER_CAM[cam] = {{
      live: camsWithFramesByPath.live.includes(cam) || raySrc("live"),
      server_post: camsWithFramesByPath.server_post.includes(cam)
        || !!(SCENE.ground_traces && SCENE.ground_traces[cam])
        || raySrc("server_post"),
    }};
  }}
  const TRAJ_BY_PATH = SCENE.triangulated_by_path || {{}};
  const HAS_TRAJ_PATH = {{
    live: (TRAJ_BY_PATH.live || []).length > 0,
    server_post: (TRAJ_BY_PATH.server_post || []).length > 0
      || (SCENE.triangulated || []).length > 0,
  }};
  function hasPathForLayer(layer, path) {{
    if (layer === "traj") return HAS_TRAJ_PATH[path];
    const cam = layer.startsWith("cam") ? layer.slice(3) : null;
    if (cam && HAS_PATH_PER_CAM[cam]) return HAS_PATH_PER_CAM[cam][path];
    return HAS_PATH[path];
  }}
  // Key is bumped from _layer_visibility → _layer_visibility_v2 because the
  // schema changed: old flat shape is not migrate-able
  // without losing the new `live` axis. Users get the default (all paths on
  // for pipelines that have data) on first post-upgrade load.
  const LAYER_VIS_KEY = "ball_tracker_viewer_layer_visibility_v3";
  const layerVisibility = {{
    traj: {{ live: HAS_TRAJ_PATH.live, server_post: HAS_TRAJ_PATH.server_post }},
    camA: {{ live: HAS_PATH_PER_CAM.A.live, server_post: HAS_PATH_PER_CAM.A.server_post }},
    camB: {{ live: HAS_PATH_PER_CAM.B.live, server_post: HAS_PATH_PER_CAM.B.server_post }},
  }};
  try {{
    const saved = JSON.parse(localStorage.getItem(LAYER_VIS_KEY) || "null");
    if (saved && typeof saved === "object") {{
      for (const k of ["traj", "camA", "camB"]) {{
        if (saved[k]) {{
          for (const path of PATHS) {{
            if (typeof saved[k][path] === "boolean") {{
              // Respect the saved choice BUT clamp to what's applicable for
              // this session. A stale "traj.live=true" from an old localStorage
              // entry must not resurrect a non-existent toggle.
              const applicable = hasPathForLayer(k, path);
              layerVisibility[k][path] = saved[k][path] && applicable;
            }}
          }}
        }}
      }}
    }}
  }} catch {{}}
  function persistLayerVisibility() {{
    try {{ localStorage.setItem(LAYER_VIS_KEY, JSON.stringify(layerVisibility)); }} catch {{}}
  }}
  function isLayerVisible(layer, path) {{
    return !!(layerVisibility[layer] && layerVisibility[layer][path]);
  }}
  // Flat cams-present views used by the frame scrubber / label renderer —
  // we scrub across the UNION of all three streams so the timeline reflects
  // everything the session captured.
  const MASTER_FPS = Math.max(60, ...Object.values(fpsByCam).filter(f => isFinite(f) && f > 0));
  const QUANT = 10000;
  const timeMap = new Map();
  for (const path of PATHS) {{
    for (const cam of camsWithFramesByPath[path]) {{
      for (const t of framesByPath[path][cam].t_rel_s) {{
        const q = Math.round(t * QUANT);
        if (!timeMap.has(q)) timeMap.set(q, t);
      }}
    }}
  }}
  if (timeMap.size === 0) {{
    for (const r of SCENE.rays || []) timeMap.set(Math.round(r.t_rel_s * QUANT), r.t_rel_s);
    for (const p of SCENE.triangulated || []) timeMap.set(Math.round(p.t_rel_s * QUANT), p.t_rel_s);
    for (const path of Object.keys(TRAJ_BY_PATH)) {{
      for (const p of TRAJ_BY_PATH[path] || []) timeMap.set(Math.round(p.t_rel_s * QUANT), p.t_rel_s);
    }}
  }}
  const unionTimes = Array.from(timeMap.values()).sort((a, b) => a - b);
  if (unionTimes.length === 0) {{ unionTimes.push(0); unionTimes.push(0.05); }}
  const TOTAL_FRAMES = unionTimes.length;
  let tMin = unionTimes[0];
  let tMax = unionTimes[TOTAL_FRAMES - 1];
  // Window used to pick "current" rays in playback mode. We want the
  // near-frame match so the 3D view shows an instantaneous ray pair, not
  // a cumulative fan. 0.75 of the nominal inter-frame gap gives a bit of
  // slack for A/B jitter without pulling in neighbouring frames.
  const PLAYBACK_RAY_TOL = TOTAL_FRAMES > 1
    ? Math.max(0.004, (tMax - tMin) / (TOTAL_FRAMES - 1) * 0.75)
    : 0.010;
  function buildCamIndexFor(frameMap, cam) {{
    const f = frameMap[cam];
    const ts = f.t_rel_s, det = f.detected;
    const out = new Array(TOTAL_FRAMES).fill(null);
    if (!ts.length) return out;
    const tol = 0.010;
    let j = 0;
    for (let i = 0; i < TOTAL_FRAMES; ++i) {{
      const t = unionTimes[i];
      if (t < ts[0] - tol || t > ts[ts.length - 1] + tol) continue;
      while (j + 1 < ts.length && Math.abs(ts[j + 1] - t) <= Math.abs(ts[j] - t)) j++;
      out[i] = {{ idx: j, t: ts[j], detected: !!det[j] }};
    }}
    return out;
  }}
  // One (cam → frameIndex → {{idx, t, detected}}) table per pipeline.
  // Three fully-independent tables so a missed detection in SVR does not
  // suppress LIVE's head-indicator, etc.
  const camAtFrameByPath = {{ live: {{}}, server_post: {{}} }};
  for (const path of PATHS) {{
    for (const cam of camsWithFramesByPath[path]) {{
      camAtFrameByPath[path][cam] = buildCamIndexFor(framesByPath[path], cam);
    }}
  }}
  let mode = "all";
  let currentFrame = 0;
  let currentT = tMin;
  let rvfcEnabled = false;
  let seekRafPending = false;
  let sceneDrawRaf = null;
  let virtualDrawRaf = null;
  let isScrubbing = false;
  let suppressVideoFeedbackUntilMs = 0;
  const masterVideo = pickMasterVideo();
  const HARD_SYNC_THRESHOLD_S = 0.040;
  const SOFT_SYNC_THRESHOLD_S = 0.008;
  const MAX_RATE_NUDGE = 0.12;
  scrubber.max = String(TOTAL_FRAMES - 1);
  scrubber.step = "1";
  frameInput.max = String(TOTAL_FRAMES - 1);
  frameTotal.textContent = String(TOTAL_FRAMES - 1);
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
  // Playback: pick the single ray closest to currentT (within tol) rather
  // than the cumulative fan. Keeps the scene readable as an instantaneous
  // snapshot tied to the bottom player.
  function raysAtT(rays, t, tol) {{
    let best = null, bestDt = Infinity;
    for (const r of rays) {{
      const dt = Math.abs(r.t_rel_s - t);
      if (dt <= tol && dt < bestDt) {{ best = r; bestDt = dt; }}
    }}
    if (!best) return {{xs: [], ys: [], zs: []}};
    return {{
      xs: [best.origin[0], best.endpoint[0], null],
      ys: [best.origin[1], best.endpoint[1], null],
      zs: [best.origin[2], best.endpoint[2], null],
    }};
  }}
  // Camera diamond + 3-axis triad is data the user should be able to hide
  // in lock-step with that camera's ray pills. When every path for a given
  // camera is off, the camera itself disappears too — no orphaned diamonds.
  // Emitted BEFORE rays so Plotly's autoscale sees the camera centre up
  // front and the initial viewport always frames the rig rather than just
  // the plate.
  function camMarkerTracesFor(c) {{
    const color = CAM_COLOR[c.camera_id] || FALLBACK;
    const [cx, cy, cz] = c.center_world;
    const mkLine = (axis, axisColor, length) => ({{
      type: "scatter3d",
      x: [cx, cx + length * axis[0]],
      y: [cy, cy + length * axis[1]],
      z: [cz, cz + length * axis[2]],
      mode: "lines",
      line: {{color: axisColor, width: 4}},
      hoverinfo: "skip",
      showlegend: false,
    }});
    return [
      {{
        type: "scatter3d",
        x: [cx], y: [cy], z: [cz],
        mode: "markers+text",
        marker: {{size: 8, color: color, symbol: "diamond"}},
        text: [`Cam ${{c.camera_id}}`],
        textposition: "top center",
        textfont: {{family: "JetBrains Mono, monospace", size: 11, color: "#2A2520"}},
        showlegend: false,
        hovertemplate: `Camera ${{c.camera_id}}<br>x=%{{x:.2f}} m<br>y=%{{y:.2f}} m<br>z=%{{z:.2f}} m<extra></extra>`,
      }},
      mkLine(c.axis_forward_world, color, SCENE_THEME.cam_fwd_len_m),
      mkLine(c.axis_right_world, SCENE_THEME.axis_color_right, SCENE_THEME.cam_axis_len_m),
      mkLine(c.axis_up_world, SCENE_THEME.axis_color_up, SCENE_THEME.cam_axis_len_m),
    ];
  }}
  function cameraIsAnyPathVisible(camera_id) {{
    const group = layerVisibility[`cam${{camera_id}}`];
    if (!group) return false;
    return PATHS.some(p => group[p] && HAS_PATH[p]);
  }}
  function buildDynamicTraces(cutoff, playback) {{
    const out = [];
    // --- cameras (diamond + axis triad), gated on the per-cam pipeline pills ---
    for (const c of (SCENE.cameras || [])) {{
      if (!cameraIsAnyPathVisible(c.camera_id)) continue;
      for (const t of camMarkerTracesFor(c)) out.push(t);
    }}
    // --- rays: one trace per (camera × path), each with its own visibility ---
    const raysByKey = {{}};
    for (const r of (SCENE.rays || [])) {{
      const path = sourceToPath(r.source || "server");
      const camKey = `cam${{r.camera_id}}`;
      if (!isLayerVisible(camKey, path)) continue;
      const key = `${{r.camera_id}}|${{path}}`;
      (raysByKey[key] = raysByKey[key] || []).push(r);
    }}
    for (const [key, rays] of Object.entries(raysByKey)) {{
      const [cam, path] = key.split("|");
      const color = colorForCamPath(cam, path);
      const {{xs, ys, zs}} = playback
        ? raysAtT(rays, currentT, PLAYBACK_RAY_TOL)
        : ballDetectedRaysUpTo(rays, cutoff);
      if (!xs.length) continue;
      out.push({{ type: "scatter3d", x: xs, y: ys, z: zs, mode: "lines",
        line: {{color: color, width: playback ? 3 : 2, dash: PATH_DASH[path]}},
        opacity: playback ? 0.95 : PATH_OPACITY[path],
        name: `Rays ${{cam}} (${{PATH_LABEL[path]}}, ${{Math.floor(xs.length / 3)}})`,
        hoverinfo: "skip", showlegend: false }});
    }}
    // --- ground traces: each scene bucket → exactly one path ---
    const GROUND_BUCKETS = [
      {{ path: "server_post", traces: SCENE.ground_traces || {{}} }},
      {{ path: "live", traces: SCENE.ground_traces_live || {{}} }},
    ];
    for (const {{path, traces}} of GROUND_BUCKETS) {{
      for (const [cam, trace] of Object.entries(traces)) {{
        if (!isLayerVisible(`cam${{cam}}`, path)) continue;
        const filtered = trace.filter(p => p.t_rel_s <= cutoff);
        if (!filtered.length) continue;
        const color = colorForCamPath(cam, path);
        // When ANY triangulation path has produced 3D points, de-emphasise
        // ground traces so the trajectory reads as the primary result.
        const dimmed = HAS_TRIANGULATED;
        out.push({{ type: "scatter3d",
          x: filtered.map(p => p.x), y: filtered.map(p => p.y), z: filtered.map(p => p.z),
          mode: "lines+markers",
          line: {{color: color, width: path === "live" ? 2 : 3, dash: PATH_DASH[path]}},
          marker: {{size: 3, color: color, symbol: PATH_MARKER_SYMBOL[path]}},
          opacity: dimmed ? 0.40 : PATH_OPACITY[path],
          name: `Ground trace ${{cam}} (${{PATH_LABEL[path]}}, ${{filtered.length}} pts)`,
          showlegend: false }});
      }}
    }}
    // --- 3D trajectory: server_post ---
    if (isLayerVisible("traj", "server_post")) {{
      const svrPts = (TRAJ_BY_PATH.server_post && TRAJ_BY_PATH.server_post.length)
        ? TRAJ_BY_PATH.server_post : (SCENE.triangulated || []);
      const triPts = svrPts.filter(p => p.t_rel_s <= cutoff);
      if (triPts.length) {{
        const t0 = triPts[0].t_rel_s;
        const ts = triPts.map(p => p.t_rel_s - t0);
        out.push({{ type: "scatter3d", x: triPts.map(p => p.x), y: triPts.map(p => p.y), z: triPts.map(p => p.z),
          mode: "lines+markers", line: {{color: ACCENT, width: 4}},
          marker: {{size: 4, color: ts, colorscale: "Cividis", showscale: true,
            colorbar: {{ title: {{text: "flight t (s)", font: {{size: 10}}}}, thickness: 10, len: 0.45, x: 1.02, y: 0.5, tickfont: {{size: 9}} }}}},
          name: `3D trajectory (svr, ${{triPts.length}} pts)` }});
        if (playback) {{
          const head = triPts[triPts.length - 1];
          out.push({{ type: "scatter3d", x: [head.x], y: [head.y], z: [head.z],
            mode: "markers", marker: {{size: 9, color: ACCENT, symbol: "circle",
              line: {{color: "#2A2520", width: 1}}}},
            hoverinfo: "skip", showlegend: false }});
        }}
      }}
    }}
    // --- 3D trajectory: live ---
    if (isLayerVisible("traj", "live")) {{
      const livePts = (TRAJ_BY_PATH.live || []).filter(p => p.t_rel_s <= cutoff);
      if (livePts.length) {{
        out.push({{ type: "scatter3d", x: livePts.map(p => p.x), y: livePts.map(p => p.y), z: livePts.map(p => p.z),
          mode: "lines+markers",
          line: {{color: "#4A6B8C", width: 3, dash: "dot"}},
          marker: {{size: 3, color: "#4A6B8C", opacity: 0.7}},
          name: `3D trajectory (live, ${{livePts.length}} pts)` }});
        if (playback) {{
          const head = livePts[livePts.length - 1];
          out.push({{ type: "scatter3d", x: [head.x], y: [head.y], z: [head.z],
            mode: "markers", marker: {{size: 7, color: "#4A6B8C", symbol: "diamond",
              line: {{color: "#2A2520", width: 1}}}},
            hoverinfo: "skip", showlegend: false }});
        }}
      }}
    }}
    return out;
  }}
  {PLATE_WORLD_JS}
  {PROJECTION_JS}
  {DRAW_VIRTUAL_BASE_JS}
  {DRAW_PLATE_OVERLAY_JS}
  const VIRT_CANVASES = [];
  const REAL_OVERLAYS = [];
  for (const c of (SCENE.cameras || [])) {{
    if (c.fx == null || c.R_wc == null || c.t_wc == null || c.image_width_px == null || c.image_height_px == null) continue;
    const canvas = document.getElementById(`virt-canvas-${{c.camera_id}}`);
    if (!canvas) continue;
    VIRT_CANVASES.push({{cam: c.camera_id, canvas, meta: c}});
    const overlay = document.getElementById(`real-plate-overlay-${{c.camera_id}}`);
    if (overlay) REAL_OVERLAYS.push({{cam: c.camera_id, overlay, meta: c}});
  }}
  function drawVirtCanvas(entry) {{
    const {{canvas, meta}} = entry;
    const base = drawVirtualBase(canvas, meta, {{ plateStroke: "rgba(219, 214, 205, 0.55)", plateFill: "rgba(219, 214, 205, 0.08)", plateLineWidth: 1, plateDash: [4, 3] }});
    if (!base) return;
    const {{ctx, sx, sy}} = base;
    function drawCurrentDetection(framesForThisCam, opts) {{
      if (!framesForThisCam) return;
      const ts = framesForThisCam.t_rel_s || [];
      const det = framesForThisCam.detected || [];
      const pxArr = framesForThisCam.px || [];
      const pyArr = framesForThisCam.py || [];
      if (!ts.length) return;
      let lo = 0, hi = ts.length - 1;
      while (lo + 1 < hi) {{
        const mid = (lo + hi) >> 1;
        if (ts[mid] <= currentT) lo = mid; else hi = mid;
      }}
      const iLo = Math.abs(ts[lo] - currentT) <= Math.abs(ts[hi] - currentT) ? lo : hi;
      const tol = 0.020;
      if (Math.abs(ts[iLo] - currentT) > tol || !det[iLo]) return;
      const px = pxArr[iLo], py = pyArr[iLo];
      if (px == null || py == null) return;
      const x = px * sx, y = py * sy;
      ctx.fillStyle = "rgba(255, 255, 255, 0.9)";
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = opts.color;
      ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
    }}
    const cam = meta.camera_id;
    const camLayer = `cam${{cam}}`;
    // Draw the per-path detection dot independently. If the session only has
    // `live` data (camera_only + no chirp flow + no server post-pass), only
    // the LIVE dot should appear; same symmetry for the other two paths.
    // Drawing order: deepest → highlighted, so svr/post (more definitive)
    // sits on top of live.
    const DOT_COLOR = {{
      live: colorForCamPath(cam, "live"),
      server_post: ACCENT,
    }};
    const PATH_ORDER = ["live", "server_post"];
    for (const path of PATH_ORDER) {{
      if (!isLayerVisible(camLayer, path)) continue;
      const frames = framesByPath[path][cam];
      if (!frames || !(frames.t_rel_s || []).length) continue;
      drawCurrentDetection(frames, {{color: DOT_COLOR[path]}});
    }}
    ctx.restore();
  }}
  function drawVirtuals() {{ for (const entry of VIRT_CANVASES) drawVirtCanvas(entry); }}
  function drawRealPlateOverlays() {{ for (const entry of REAL_OVERLAYS) redrawPlateOverlay(entry.overlay, entry.meta); }}
  function drawScene() {{
    const playback = mode !== "all";
    const cutoff = playback ? currentT : Infinity;
    Plotly.react(sceneDiv, [...STATIC, ...buildDynamicTraces(cutoff, playback)], LAYOUT, {{displayModeBar: false, responsive: true}});
    drawVirtuals();
    drawRealPlateOverlays();
  }}
  function scheduleSceneDraw() {{
    if (sceneDrawRaf !== null) return;
    sceneDrawRaf = requestAnimationFrame(() => {{ sceneDrawRaf = null; drawScene(); }});
  }}
  function scheduleVirtualDraw() {{
    if (virtualDrawRaf !== null) return;
    virtualDrawRaf = requestAnimationFrame(() => {{ virtualDrawRaf = null; drawVirtuals(); }});
  }}
  window.addEventListener("resize", () => {{ drawVirtuals(); drawRealPlateOverlays(); }});
  function markManualSeekWindow(ms = 180) {{
    suppressVideoFeedbackUntilMs = Math.max(suppressVideoFeedbackUntilMs, performance.now() + ms);
  }}
  function shouldIgnoreVideoFeedback() {{ return isScrubbing || performance.now() < suppressVideoFeedbackUntilMs; }}
  function beginTimelineInteraction() {{ pauseAllPlayback(); markManualSeekWindow(); updatePlayBtnLabel(); }}
  function resetVideoPlaybackRates() {{ for (const v of vids) if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate; }}
  function syncFollowerVideosToMaster(masterT) {{
    if (!masterVideo || !isFinite(masterT)) return;
    for (const v of vids) {{
      if (v === masterVideo) continue;
      const off = offsetByCam[v.dataset.cam] ?? 0;
      const want = Math.max(0, masterT - off);
      if (!isFinite(v.currentTime)) continue;
      const drift = v.currentTime - want;
      if (Math.abs(drift) >= HARD_SYNC_THRESHOLD_S) {{
        try {{ v.currentTime = want; }} catch {{}}
        if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate;
        continue;
      }}
      if (v.paused || Math.abs(drift) <= SOFT_SYNC_THRESHOLD_S) {{
        if (Math.abs(v.playbackRate - currentRate) > 0.001) v.playbackRate = currentRate;
        continue;
      }}
      const correction = Math.max(-MAX_RATE_NUDGE, Math.min(MAX_RATE_NUDGE, -drift * 6.0));
      const targetRate = Math.max(0.1, currentRate * (1 + correction));
      if (Math.abs(v.playbackRate - targetRate) > 0.001) v.playbackRate = targetRate;
    }}
  }}
  let seekTargetT = tMin;
  function syncVideosToT(t) {{
    if (!isFinite(t)) return;
    seekTargetT = t;
    markManualSeekWindow();
    if (seekRafPending) return;
    seekRafPending = true;
    requestAnimationFrame(() => {{
      seekRafPending = false;
      const tt = seekTargetT;
      for (const v of vids) {{
        const off = offsetByCam[v.dataset.cam] ?? 0;
        const want = Math.max(0, tt - off);
        if (Math.abs((v.currentTime || 0) - want) < 1e-4) continue;
        try {{ v.currentTime = want; }} catch {{}}
      }}
      resetVideoPlaybackRates();
    }});
  }}
  function readMasterTFromVideo() {{
    if (masterVideo && !isNaN(masterVideo.currentTime)) return masterVideo.currentTime + (offsetByCam[masterVideo.dataset.cam] ?? 0);
    for (const v of vids) if (!isNaN(v.currentTime)) return v.currentTime + (offsetByCam[v.dataset.cam] ?? 0);
    return currentT;
  }}
  function frameIndexForT(t) {{
    let lo = 0, hi = TOTAL_FRAMES - 1;
    if (t <= unionTimes[lo]) return lo;
    if (t >= unionTimes[hi]) return hi;
    while (lo + 1 < hi) {{
      const mid = (lo + hi) >> 1;
      if (unionTimes[mid] <= t) lo = mid; else hi = mid;
    }}
    return (t - unionTimes[lo]) <= (unionTimes[hi] - t) ? lo : hi;
  }}
  function renderFrameLabel() {{
    const v = String(currentFrame);
    if (document.activeElement !== frameInput && frameInput.value !== v) frameInput.value = v;
    const tRel = currentT - tMin;
    const parts = [];
    // Emit one (cam:idx ✓/·) pair per active path. Wrapped in a label so the
    // operator can tell at a glance which pipeline contributed the mark.
    for (const path of PATHS) {{
      const cams = camsWithFramesByPath[path];
      if (!cams.length) continue;
      const inner = [];
      for (const cam of cams) {{
        const entry = camAtFrameByPath[path][cam][currentFrame];
        if (entry === null) {{ inner.push(`<span class="sub">${{cam}}:—</span>`); continue; }}
        const cls = entry.detected ? "det" : "det no";
        const mark = entry.detected ? "✓" : "·";
        inner.push(`<span class="sub">${{cam}}:${{entry.idx}}</span><span class="${{cls}}">${{mark}}</span>`);
      }}
      parts.push(`<span class="sub">${{PATH_LABEL[path]}}</span> ${{inner.join(" ")}}`);
    }}
    parts.push(`<span class="sub">t=${{tRel.toFixed(3)}}s</span>`);
    frameSub.innerHTML = parts.join(" · ");
  }}
  function setFrame(f, {{ seekVideos = true }} = {{}}) {{
    currentFrame = Math.max(0, Math.min(TOTAL_FRAMES - 1, f | 0));
    currentT = unionTimes[currentFrame];
    scrubber.value = String(currentFrame);
    renderFrameLabel();
    renderDetectionStrip();
    if (seekVideos) syncVideosToT(currentT);
    if (mode === "playback") scheduleSceneDraw();
    if (mode !== "playback") scheduleVirtualDraw();
  }}
  let virtualRAF = null;
  let virtualLastPerfMs = 0;
  let virtualTime = 0;
  function virtualPlaying() {{ return virtualRAF !== null; }}
  function startVirtualClock() {{
    if (virtualRAF !== null) return;
    virtualLastPerfMs = performance.now();
    virtualTime = currentT;
    const tick = (now) => {{
      virtualRAF = requestAnimationFrame(tick);
      const dt = (now - virtualLastPerfMs) / 1000 * currentRate;
      virtualLastPerfMs = now;
      virtualTime += dt;
      if (virtualTime >= unionTimes[TOTAL_FRAMES - 1]) {{
        setFrame(TOTAL_FRAMES - 1);
        stopVirtualClock();
        updatePlayBtnLabel();
        return;
      }}
      setFrame(frameIndexForT(virtualTime));
    }};
    virtualRAF = requestAnimationFrame(tick);
  }}
  function stopVirtualClock() {{ if (virtualRAF !== null) {{ cancelAnimationFrame(virtualRAF); virtualRAF = null; }} }}
  function pauseAllPlayback() {{ vids.forEach(v => v.pause()); resetVideoPlaybackRates(); stopVirtualClock(); }}
  function stepFrames(delta) {{ beginTimelineInteraction(); setFrame(currentFrame + delta); }}
  function jumpDetection(dir) {{
    // Step to the next frame where *any* currently-visible pipeline reports
    // a detection. Respecting the pills means the hotkey follows what the
    // operator is actually looking at: hide LIVE and D/F will skip through
    // svr+post only.
    let i = currentFrame + dir;
    while (i >= 0 && i < TOTAL_FRAMES) {{
      for (const path of PATHS) {{
        for (const cam of camsWithFramesByPath[path]) {{
          if (!isLayerVisible(`cam${{cam}}`, path)) continue;
          const e = camAtFrameByPath[path][cam][i];
          if (e && e.detected) {{ beginTimelineInteraction(); setFrame(i); return; }}
        }}
      }}
      i += dir;
    }}
  }}
  function onVideoTimeUpdate() {{
    if (rvfcEnabled || seekRafPending || shouldIgnoreVideoFeedback()) return;
    requestAnimationFrame(() => {{
      if (shouldIgnoreVideoFeedback()) return;
      setFrame(frameIndexForT(readMasterTFromVideo()), {{ seekVideos: false }});
    }});
  }}
  playBtn.addEventListener("click", () => {{
    if (vids.length > 0) {{
      const anyPaused = vids.some(v => v.paused);
      if (anyPaused) {{
        syncFollowerVideosToMaster(readMasterTFromVideo());
        resetVideoPlaybackRates();
        vids.forEach(v => {{ try {{ v.play(); }} catch {{}} }});
      }} else vids.forEach(v => v.pause());
      return;
    }}
    if (virtualPlaying()) stopVirtualClock(); else startVirtualClock();
    updatePlayBtnLabel();
  }});
  function updatePlayBtnLabel() {{ playBtn.textContent = vids.length > 0 ? (vids.every(v => v.paused) ? "Play" : "Pause") : (virtualPlaying() ? "Pause" : "Play"); }}
  const hasRVFC = typeof HTMLVideoElement !== "undefined" && "requestVideoFrameCallback" in HTMLVideoElement.prototype;
  function driveWithRVFC() {{
    if (!masterVideo) return;
    rvfcEnabled = true;
    const master = masterVideo;
    const off = offsetByCam[master.dataset.cam] ?? 0;
    const onFrame = (_now, metadata) => {{
      if (shouldIgnoreVideoFeedback()) {{ master.requestVideoFrameCallback(onFrame); return; }}
      const mediaT = (metadata && typeof metadata.mediaTime === "number") ? metadata.mediaTime : master.currentTime;
      const t = mediaT + off;
      syncFollowerVideosToMaster(t);
      setFrame(frameIndexForT(t), {{ seekVideos: false }});
      master.requestVideoFrameCallback(onFrame);
    }};
    master.requestVideoFrameCallback(onFrame);
  }}
  vids.forEach(v => {{ v.addEventListener("play", updatePlayBtnLabel); v.addEventListener("pause", updatePlayBtnLabel); v.addEventListener("timeupdate", onVideoTimeUpdate); v.addEventListener("seeked", onVideoTimeUpdate); }});
  if (hasRVFC) driveWithRVFC();
  scrubber.addEventListener("pointerdown", () => {{ isScrubbing = true; beginTimelineInteraction(); }});
  const endScrub = () => {{ if (!isScrubbing) return; isScrubbing = false; markManualSeekWindow(120); }};
  scrubber.addEventListener("pointerup", endScrub);
  scrubber.addEventListener("pointercancel", endScrub);
  scrubber.addEventListener("blur", endScrub);
  window.addEventListener("pointerup", endScrub);
  scrubber.addEventListener("input", () => {{ beginTimelineInteraction(); setFrame(Number(scrubber.value)); }});
  scrubber.addEventListener("keydown", (ev) => {{
    switch (ev.key) {{
      case "ArrowLeft": ev.preventDefault(); stepFrames(-1); break;
      case "ArrowRight": ev.preventDefault(); stepFrames(+1); break;
      case "Home": ev.preventDefault(); beginTimelineInteraction(); setFrame(0); break;
      case "End": ev.preventDefault(); beginTimelineInteraction(); setFrame(TOTAL_FRAMES - 1); break;
      case "PageUp": ev.preventDefault(); stepFrames(-10); break;
      case "PageDown": ev.preventDefault(); stepFrames(+10); break;
    }}
  }});
  frameInput.addEventListener("change", () => {{
    const f = Number(frameInput.value);
    if (!isFinite(f)) {{ frameInput.value = String(currentFrame); return; }}
    beginTimelineInteraction();
    setFrame(f);
  }});
  frameInput.addEventListener("keydown", (ev) => {{ if (ev.key === "Enter") {{ ev.preventDefault(); frameInput.blur(); }} }});
  stepFirstBtn.addEventListener("click", () => stepFrames(-TOTAL_FRAMES));
  stepLastBtn.addEventListener("click", () => stepFrames(+TOTAL_FRAMES));
  stepBackBtn.addEventListener("click", () => stepFrames(-1));
  stepFwdBtn.addEventListener("click", () => stepFrames(+1));
  let currentRate = 1.0;
  speedGroup.addEventListener("click", (ev) => {{
    const btn = ev.target.closest("button[data-rate]");
    if (!btn) return;
    const r = parseFloat(btn.dataset.rate);
    if (!isFinite(r) || r <= 0) return;
    currentRate = r;
    resetVideoPlaybackRates();
    for (const b of speedGroup.querySelectorAll("button")) b.classList.toggle("active", b === btn);
  }});
  window.addEventListener("keydown", (ev) => {{
    if (ev.key === "Escape") {{
      if (hintOverlay.classList.contains("open")) {{ ev.preventDefault(); setHintOpen(false); }}
      return;
    }}
    const tag = (ev.target && ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    switch (ev.key) {{
      case " ": ev.preventDefault(); playBtn.click(); break;
      case ",": ev.preventDefault(); stepFrames(ev.shiftKey ? -10 : -1); break;
      case ".": ev.preventDefault(); stepFrames(ev.shiftKey ? +10 : +1); break;
      case "ArrowLeft": ev.preventDefault(); stepFrames(-Math.round(0.5 * MASTER_FPS)); break;
      case "ArrowRight": ev.preventDefault(); stepFrames(+Math.round(0.5 * MASTER_FPS)); break;
      case "Home": ev.preventDefault(); stepFrames(-TOTAL_FRAMES); break;
      case "End": ev.preventDefault(); stepFrames(+TOTAL_FRAMES); break;
      case "d": case "D": ev.preventDefault(); jumpDetection(-1); break;
      case "f": case "F": ev.preventDefault(); jumpDetection(+1); break;
      case "?": ev.preventDefault(); setHintOpen(!hintOverlay.classList.contains("open")); break;
      case "1": case "2": case "3": case "4": case "5": {{
        const idx = Number(ev.key) - 1;
        const buttons = speedGroup.querySelectorAll("button[data-rate]");
        if (buttons[idx]) {{ ev.preventDefault(); buttons[idx].click(); }}
        break;
      }}
    }}
  }});
  function setMode(next) {{ mode = next; modeAll.classList.toggle("active", next === "all"); modePlayback.classList.toggle("active", next === "playback"); scheduleSceneDraw(); }}
  modeAll.addEventListener("click", () => setMode("all"));
  modePlayback.addEventListener("click", () => setMode("playback"));
  sceneResetBtn.addEventListener("click", () => {{ Plotly.relayout(sceneDiv, {{ "scene.camera": DEFAULT_CAMERA }}); }});
  // Draggable divider between the 3D scene and the 2x2 camera panels.
  // Persists the chosen split so reload keeps the operator's layout.
  (() => {{
    const resizer = document.getElementById("col-resizer");
    if (!resizer) return;
    const work = resizer.parentElement;
    const sceneCol = work.querySelector(".scene-col");
    const videosCol = work.querySelector(".videos-col");
    const STORE_KEY = "viewer:col-split-frac";
    function applyFrac(frac) {{
      const clamped = Math.max(0.15, Math.min(0.85, frac));
      sceneCol.style.flex = `${{clamped}} 1 0`;
      videosCol.style.flex = `${{1 - clamped}} 1 0`;
      try {{ Plotly.Plots.resize(sceneDiv); }} catch (_) {{}}
    }}
    try {{
      const saved = parseFloat(localStorage.getItem(STORE_KEY));
      if (Number.isFinite(saved)) applyFrac(saved);
    }} catch (_) {{}}
    let dragging = false;
    function onMove(e) {{
      if (!dragging) return;
      const rect = work.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      const frac = x / rect.width;
      applyFrac(frac);
    }}
    function onUp() {{
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      const basis = parseFloat(sceneCol.style.flex);
      if (Number.isFinite(basis)) {{
        try {{ localStorage.setItem(STORE_KEY, String(basis)); }} catch (_) {{}}
      }}
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    }}
    resizer.addEventListener("pointerdown", (e) => {{
      e.preventDefault();
      dragging = true;
      resizer.classList.add("dragging");
      document.body.classList.add("col-resizing");
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    }});
    resizer.addEventListener("dblclick", () => {{
      try {{ localStorage.removeItem(STORE_KEY); }} catch (_) {{}}
      sceneCol.style.flex = "";
      videosCol.style.flex = "";
      try {{ Plotly.Plots.resize(sceneDiv); }} catch (_) {{}}
    }});
    resizer.addEventListener("keydown", (e) => {{
      const rect = work.getBoundingClientRect();
      const current = parseFloat(sceneCol.style.flex);
      const frac = Number.isFinite(current) ? current / (current + (parseFloat(videosCol.style.flex) || 1)) : 0.55;
      const step = e.shiftKey ? 0.08 : 0.02;
      if (e.key === "ArrowLeft") {{ e.preventDefault(); applyFrac(frac - step); }}
      else if (e.key === "ArrowRight") {{ e.preventDefault(); applyFrac(frac + step); }}
    }});
    window.addEventListener("resize", () => {{ try {{ Plotly.Plots.resize(sceneDiv); }} catch (_) {{}} }});
  }})();
  sceneDiv.addEventListener("wheel", (e) => {{
    if (!sceneDiv._fullLayout || !sceneDiv._fullLayout.scene) return;
    const cam = sceneDiv._fullLayout.scene.camera;
    if (!cam || !cam.eye) return;
    e.preventDefault();
    const mag = Math.min(0.5, Math.sqrt(Math.abs(e.deltaY)) * 0.04);
    const factor = e.deltaY > 0 ? (1 + mag) : (1 - mag);
    Plotly.relayout(sceneDiv, {{ "scene.camera.eye": {{ x: cam.eye.x * factor, y: cam.eye.y * factor, z: cam.eye.z * factor }} }});
  }}, {{ passive: false }});
  function setHintOpen(open) {{ hintOverlay.classList.toggle("open", open); hintBtn.classList.toggle("open", open); hintBtn.setAttribute("aria-expanded", open ? "true" : "false"); }}
  hintBtn.addEventListener("click", () => {{ setHintOpen(!hintOverlay.classList.contains("open")); }});
  // One strip-row per pipeline, each hidden until we have data for it. Row
  // id / canvas id pairs are static so the CSS and the JS agree without a
  // parallel config dict.
  const STRIP_ROWS = {{
    live: {{ row: document.getElementById("strip-row-live"), canvas: document.getElementById("detection-canvas-live") }},
    server_post: {{ row: document.getElementById("strip-row-server-post"), canvas: document.getElementById("detection-canvas-server-post") }},
  }};
  const layerToggles = document.getElementById("layer-toggles");
  const STRIP_MUTED = "rgba(122, 117, 108, 0.35)";
  const STRIP_EMPTY = "rgba(232, 228, 219, 0.6)";
  const STRIP_HEAD = "#2A2520";
  const STRIP_CHIRP = "rgba(230, 179, 0, 0.65)";
  let visibleStripCount = 0;
  for (const path of PATHS) {{
    if (HAS_PATH[path]) {{
      STRIP_ROWS[path].row.hidden = false;
      visibleStripCount += 1;
    }}
  }}
  // Surface the multi-pipeline disclaimer only when at least two strips are
  // on screen — otherwise the note is noise.
  const multiNote = document.getElementById("strip-note-multi");
  if (multiNote) multiNote.hidden = visibleStripCount < 2;
  function paintLayerPills() {{
    const pills = layerToggles.querySelectorAll(".layer-pill");
    for (const pill of pills) {{
      const layer = pill.dataset.layer;
      const path = pill.dataset.path;
      const applicable = hasPathForLayer(layer, path);
      if (!applicable) {{
        pill.hidden = true;
        pill.setAttribute("aria-pressed", "false");
        continue;
      }}
      pill.hidden = false;
      pill.setAttribute("aria-pressed", isLayerVisible(layer, path) ? "true" : "false");
    }}
    for (const sw of layerToggles.querySelectorAll(".layer-name .swatch")) {{
      sw.style.background = colorForCamPath(sw.dataset.cam, "server_post");
    }}
    // If every pill in a group is hidden, fold the group too — otherwise you
    // get a dangling "Traj" label with nothing under it.
    for (const group of layerToggles.querySelectorAll(".layer-group")) {{
      const anyPill = group.querySelector(".layer-pill:not([hidden])");
      group.hidden = !anyPill;
    }}
  }}
  paintLayerPills();
  layerToggles.addEventListener("click", (e) => {{
    const pill = e.target.closest(".layer-pill");
    if (!pill || pill.hidden || pill.disabled) return;
    const layer = pill.dataset.layer;
    const path = pill.dataset.path;
    // Refuse to turn off the last visible pipeline *within a cam group* —
    // an all-off group would just remove that camera entirely, which is
    // redundant with hiding the group and confusing as a click result.
    const group = layerVisibility[layer];
    if (!group) return;
    group[path] = !group[path];
    persistLayerVisibility();
    paintLayerPills();
    drawScene();
    renderDetectionStrip();
  }});
  function resizeOneCanvas(canvas) {{
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight || 28;
    const dpr = window.devicePixelRatio || 1;
    const pxW = Math.max(1, Math.floor(cssW * dpr));
    const pxH = Math.max(1, Math.floor(cssH * dpr));
    if (canvas.width !== pxW || canvas.height !== pxH) {{ canvas.width = pxW; canvas.height = pxH; }}
  }}
  // Every strip reserves one sub-track per cam, even when that cam has no
  // data on this pipeline — the empty row is load-bearing for single-camera
  // sessions (e.g. live-only A-only) so the operator can see "B is silent"
  // instead of misreading a full-width A track as both cams.
  const STRIP_CAMS = ["A", "B"];
  function drawStripInto(canvas, strips, path) {{
    const W = canvas.width, H = canvas.height;
    if (!W || !H) return;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, W, H);
    const rows = STRIP_CAMS.length;
    const rowH = Math.floor(H / rows);
    for (let ci = 0; ci < rows; ++ci) {{
      const cam = STRIP_CAMS[ci];
      const strip = strips[cam];
      const y = ci * rowH;
      ctx.fillStyle = STRIP_EMPTY;
      ctx.fillRect(0, y, W, rowH);
      if (!strip) continue;
      const muted = !isLayerVisible(`cam${{cam}}`, path);
      const detColor = muted ? STRIP_MUTED : colorForCamPath(cam, path);
      for (let x = 0; x < W; ++x) {{
        const i = TOTAL_FRAMES <= 1 ? 0 : Math.min(TOTAL_FRAMES - 1, Math.round(x * (TOTAL_FRAMES - 1) / (W - 1)));
        const e = strip[i];
        if (e === null || e === undefined) continue;
        ctx.fillStyle = e.detected ? detColor : STRIP_MUTED;
        ctx.fillRect(x, y, 1, rowH);
      }}
    }}
    if (tMin <= 0 && tMax >= 0 && tMax > tMin) {{
      const xChirp = Math.round((-tMin) * (W - 1) / (tMax - tMin));
      ctx.fillStyle = STRIP_CHIRP;
      ctx.fillRect(Math.max(0, xChirp - 1), 0, 2, H);
    }}
    const xHead = TOTAL_FRAMES <= 1 ? 0 : Math.round(currentFrame * (W - 1) / (TOTAL_FRAMES - 1));
    ctx.fillStyle = STRIP_HEAD;
    ctx.fillRect(Math.max(0, xHead - 1), 0, 2, H);
  }}
  function renderDetectionStrip() {{
    for (const path of PATHS) {{
      if (!HAS_PATH[path]) continue;
      drawStripInto(STRIP_ROWS[path].canvas, camAtFrameByPath[path], path);
    }}
  }}
  function resizeDetectionCanvas() {{
    for (const path of PATHS) {{
      if (!HAS_PATH[path]) continue;
      resizeOneCanvas(STRIP_ROWS[path].canvas);
    }}
    renderDetectionStrip();
  }}
  window.addEventListener("resize", resizeDetectionCanvas);
  setFrame(0, {{ seekVideos: true }});
  scheduleSceneDraw();
  updatePlayBtnLabel();
  requestAnimationFrame(resizeDetectionCanvas);
}})();
"""
