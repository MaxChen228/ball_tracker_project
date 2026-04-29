from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as _datetime
import json as _json
from pathlib import Path

import html as _html

from cam_view_ui import CAM_VIEW_CONTENT_CSS, CAM_VIEW_RUNTIME_JS
from overlays_ui import OVERLAYS_RUNTIME_JS
from presets import PRESETS
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
    session_tuning_strip_html,
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
    session_tuning_html: str
    cost_threshold: float | None
    gap_threshold_m: float | None
    video_cells_html: str
    session_id: str
    server_post_ran: bool
    can_run_server: bool
    server_post_ran_at: float | None


def build_viewer_page_context(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
    *,
    build_figure,
    cost_threshold: float | None = None,
    gap_threshold_m: float | None = None,
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
    # Drop the `not server_post_ran` gate — the operator may want to
    # rerun after tweaking HSV / shape gate / selector tuning. The button
    # label flips to "Rerun" once a previous run is detected.
    can_run_server = health.get("mode") == "camera_only"
    # SessionResult.server_post_ran_at is the per-session aggregate (max
    # of A/B). None when nothing has run yet for this session.
    server_post_ran_at = health.get("server_post_ran_at")

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
        session_tuning_html=session_tuning_strip_html(
            cost_threshold, gap_threshold_m, scene.session_id,
        ),
        cost_threshold=cost_threshold,
        gap_threshold_m=gap_threshold_m,
        video_cells_html=video_cells,
        session_id=scene.session_id,
        server_post_ran=server_post_ran,
        can_run_server=can_run_server,
        server_post_ran_at=server_post_ran_at,
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
    cost_threshold: float | None = None,
    gap_threshold_m: float | None = None,
) -> str:
    ctx = build_viewer_page_context(
        scene,
        videos,
        health,
        build_figure=build_figure,
        cost_threshold=cost_threshold,
        gap_threshold_m=gap_threshold_m,
    )
    if ctx.can_run_server:
        # Operator may rerun after tweaking HSV / shape gate / selector;
        # label flips to "Rerun" once a previous server_post detection
        # has populated frames for either cam. Timestamp surfaces next
        # to the button so the operator sees how stale the current
        # results are before deciding to rerun.
        label = "Rerun server" if ctx.server_post_ran else "Run server detection"
        ts_html = ""
        if ctx.server_post_ran_at is not None:
            iso = _datetime.fromtimestamp(ctx.server_post_ran_at).strftime("%Y-%m-%d %H:%M:%S")
            ts_html = (
                f'<span class="action-ts" title="Server detection last completed at {iso}">'
                f'{iso}</span>'
            )
        # Detection-config picker: 'live' is the current dashboard
        # config (mutating effect — same as the events row "Run srv"
        # button); 'frozen' replays the per-pitch *_used snapshot for
        # bit-exact reproduction (409 if any cam in the session lacks
        # the snapshot, e.g. pre-PR #93 pitches); 'preset:<name>' uses
        # the canonical HSV from `presets.PRESETS` without touching
        # disk — the research-compare path. Default selection is
        # 'live' so a casual operator click matches today's behavior.
        # Defensive escape: today's labels ("Tennis" / "Blue ball") are
        # safe, but `presets.PRESETS` is the single registry for future
        # presets — a label introduced later containing `<`/`&`/`"`
        # would inject without this. Same defensive posture as the
        # dashboard preset buttons (`render_dashboard_session.py`).
        preset_options = "".join(
            f'<option value="preset:{_html.escape(name)}">'
            f'Preset: {_html.escape(preset.label)}</option>'
            for name, preset in PRESETS.items()
        )
        action_html = (
            f'<form method="POST" action="/sessions/{ctx.session_id}/run_server_post" class="action-form">'
            f'<select class="action-select" name="source"'
            f' title="Detection config to run with. \'Live\' = current dashboard config'
            f' (mutates if you tweak after). \'Frozen\' = the config this pitch was'
            f' originally detected with. \'Preset\' = canonical HSV without disturbing'
            f' the live dashboard config — for research compares.">'
            f'<option value="live" selected>Live (current dashboard config)</option>'
            f'<option value="frozen">Original (frozen at detection time)</option>'
            f'{preset_options}'
            f'</select>'
            f'<button class="action" type="submit">{label}</button>'
            f'{ts_html}'
            f'</form>'
        )
    else:
        action_html = ""
    fit_link_html = (
        f'<a class="fit-link" href="/fit/{ctx.session_id}"'
        f' title="Independent fit page — multi-segment ballistic extraction (auto-picks server_post / live)">'
        f'Fit&nbsp;&rarr;</a>'
    )
    progress_html = (
        '<span class="srv-progress" id="srv-progress" hidden'
        ' aria-live="polite"></span>'
    )
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
    {ctx.session_tuning_html}
    {progress_html}
    {action_html}
    {fit_link_html}
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
        <tr><td>&larr; &nbsp;&rarr;</td><td>Prev / next frame</td></tr>
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
window.VIEWER_INITIAL_COST_THRESHOLD = {1.0 if ctx.cost_threshold is None else float(ctx.cost_threshold)};
// Initial gap in METRES (None → 2.0m = effectively off; route's max).
// 50_canvas.js converts to client-side residualCapM (Infinity when ≥ 2.0).
window.VIEWER_INITIAL_GAP_THRESHOLD_M = {2.0 if ctx.gap_threshold_m is None else float(ctx.gap_threshold_m)};
window._applyTuning = function(btn) {{
  const cost = parseFloat(document.querySelector('[data-session-cost-threshold]').value);
  // Slider value is centimetres (0–200); ship metres to the route. 200 = 2.0m
  // = the route's max ("off" semantically — every cartesian pair survives
  // the gap gate, so what you Apply matches what the slider shows).
  const gap_m = parseFloat(document.querySelector('[data-session-gap-threshold]').value) / 100;
  const sid = btn.getAttribute('data-session-id');
  btn.disabled = true;
  btn.textContent = 'Recomputing…';
  fetch('/sessions/' + encodeURIComponent(sid) + '/recompute', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ cost_threshold: cost, gap_threshold_m: gap_m }}),
  }}).then(function(r) {{
    if (!r.ok) throw new Error('HTTP ' + r.status);
    // Simplest reload — full re-render with fresh SessionResult.
    // Future optimization: patch in place via the new result JSON.
    window.location.reload();
  }}).catch(function(err) {{
    btn.disabled = false;
    btn.textContent = 'Apply';
    alert('Recompute failed: ' + err);
  }});
}};
</script>
<script>
{OVERLAYS_RUNTIME_JS}
</script>
<script>
{CAM_VIEW_RUNTIME_JS}
</script>
<script>
{_viewer_js()}
</script>
<script>
(function() {{
  if (!window.EventSource) return;
  const VIEWER_SID = {_json.dumps(ctx.session_id)};
  const el = document.getElementById('srv-progress');
  if (!el) return;
  const slots = {{}};  // cam → {{ done, total }}
  function render() {{
    const cams = Object.keys(slots).sort();
    if (cams.length === 0) {{ el.hidden = true; el.textContent = ''; return; }}
    el.hidden = false;
    el.textContent = cams.map(function(c) {{
      const s = slots[c];
      const tot = (s.total != null) ? s.total : '?';
      return 'svr ' + c + ' ' + s.done + '/' + tot;
    }}).join(' · ');
  }}
  const es = new EventSource('/stream');
  es.addEventListener('server_post_progress', function(evt) {{
    try {{
      const d = JSON.parse(evt.data);
      if (d.sid !== VIEWER_SID) return;
      slots[d.cam] = {{
        done: Number(d.frames_done || 0),
        total: d.frames_total != null ? Number(d.frames_total) : null,
      }};
      render();
    }} catch (_) {{}}
  }});
  es.addEventListener('server_post_done', function(evt) {{
    try {{
      const d = JSON.parse(evt.data);
      if (d.sid !== VIEWER_SID) return;
      delete slots[d.cam];
      render();
      // Authoritative refresh once the last cam wraps so the page picks
      // up the new triangulated points + path_status without a manual
      // reload. autorefresh.js already polls /events and reload()s on
      // detected change; we just nudge it forward.
      if (Object.keys(slots).length === 0) {{
        setTimeout(function() {{ location.reload(); }}, 800);
      }}
    }} catch (_) {{}}
  }});
}})();
</script>
</body></html>"""


_VIEWER_CSS_PATH = Path(__file__).parent / "static" / "viewer" / "viewer.css"


def _resolve_viewer_css_template() -> str:
    """Load `static/viewer/viewer.css` and substitute the per-theme color
    tokens (`{BG}`, `{INK}`, …). Same pattern as
    `_resolve_viewer_js_template`: literal `{NAME}` placeholders, NOT
    f-string fields, so resolve via `str.replace`. The remaining
    `{SCENE_FLEX}` / `{VIDEOS_FLEX}` slots are filled per-page in
    `_viewer_css`."""
    css = _VIEWER_CSS_PATH.read_text(encoding="utf-8")
    css = css.replace("{BG}", _BG).replace("{SURFACE}", _SURFACE)
    css = css.replace("{INK}", _INK).replace("{SUB}", _SUB)
    css = css.replace("{BORDER_BASE}", _BORDER_BASE).replace("{BORDER_L}", _BORDER_L)
    css = css.replace("{CONTRA}", _CONTRA).replace("{DUAL}", _DUAL)
    css = css.replace("{DEV}", _DEV).replace("{ACCENT}", _ACCENT)
    css = css.replace("{OK}", _OK).replace("{PENDING}", _PENDING)
    return css


_VIEWER_CSS_TEMPLATE = _resolve_viewer_css_template()


def _viewer_css(scene_flex: str, videos_flex: str) -> str:
    return (
        "\n"
        + _VIEWER_CSS_TEMPLATE
            .replace("{SCENE_FLEX}", scene_flex)
            .replace("{VIDEOS_FLEX}", videos_flex)
    )




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
