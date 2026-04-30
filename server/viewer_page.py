from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as _datetime
import json as _json
import re
from pathlib import Path

from cam_view_ui import CAM_VIEW_CONTENT_CSS, CAM_VIEW_RUNTIME_JS
from overlays_ui import OVERLAYS_RUNTIME_JS
from scene_runtime import (
    fit_extension_seconds_slider_html,
    fit_line_width_slider_html,
    layer_chip_with_popover_html,
    point_size_slider_html,
    view_presets_toolbar_html,
)
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
    cam_view_shared_toolbar_html,
    failure_strip_html,
    health_nav_strip_html,
    session_tuning_strip_html,
    video_cell_html,
)


@dataclass(frozen=True)
class ViewerPageContext:
    scene_json: str
    camera_colors_json: str
    fallback_color_json: str
    accent_color_json: str
    segments_json: str
    segments_by_path_json: str
    # Camera diamond + axis geometry is rendered by the Three.js viewer
    # layers module (`static/threejs/viewer_layers.js`) using these
    # constants, mirroring render_scene_theme so dashboard + viewer
    # agree on sizes/colours.
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
    # Server-detection processing state at render time. None = idle;
    # "processing" / "queued" / "canceled" mirror state.processing's
    # session_summary. Drives the pre-seeded scene-pending-overlay so
    # an operator opening the viewer mid-decode sees the overlay
    # immediately instead of waiting for the next SSE progress tick.
    processing_state: str | None


def build_viewer_page_context(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
    *,
    cost_threshold: float | None = None,
    gap_threshold_m: float | None = None,
    segments: list | None = None,
    segments_by_path: dict[str, list] | None = None,
) -> ViewerPageContext:
    # Pre-Three.js this used a Plotly `build_figure(scene)` callable to
    # extract the static trace list + layout block; the viewer JS then
    # composed `[...static, ...dynamic]` for `Plotly.react`. Three.js
    # builds its static layers (ground / plate / strike zone / world
    # axes) client-side from the JSON theme payload — no server-side
    # trace extraction needed.
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

    # Pre-seed processing_state so an operator opening the viewer
    # mid-decode sees the overlay immediately (don't wait for the next
    # SSE progress tick). state.processing.session_summary returns
    # ("processing"|"queued"|"canceled", resumable) or (None, _).
    from main import state as _global_state
    processing_state, _resumable = _global_state.processing.session_summary(
        scene.session_id,
    )

    # SegmentRecord-only contract: callers (route + reprocess) pass
    # SegmentRecord instances. Tests construct them too. No dict
    # affordance — keeps wire shape canonical.
    seg_dicts: list[dict] = [
        s.model_dump() for s in ([] if segments is None else segments)
    ]
    segs_by_path_dicts: dict[str, list[dict]] = {
        path: [s.model_dump() for s in segs]
        for path, segs in (({} if segments_by_path is None else segments_by_path).items())
    }

    return ViewerPageContext(
        scene_json=_json.dumps(scene.to_dict()),
        camera_colors_json=_json.dumps(_CAMERA_COLORS),
        fallback_color_json=_json.dumps(_FALLBACK_CAMERA_COLOR),
        accent_color_json=_json.dumps(_ACCENT),
        segments_json=_json.dumps(seg_dicts),
        segments_by_path_json=_json.dumps(segs_by_path_dicts),
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
        processing_state=processing_state,
    )


def _pending_overlay_html(processing_state: str | None) -> str:
    """Server-detection pending overlay. Pre-seeded visible when the
    operator opens the viewer mid-decode (state.processing.session_summary
    returned 'queued' / 'processing'); the SSE handler in the inline
    `<script>` block keeps it in sync afterwards."""
    visible = processing_state in ("queued", "processing")
    hidden_attr = "" if visible else " hidden"
    title = (
        "Queued — server detection"
        if processing_state == "queued"
        else "Decoding MOV…"
    )
    counts_seed = (
        "waiting for first frame…"
        if processing_state == "processing"
        else ""
    )
    return (
        f'<div class="scene-pending-overlay" id="scene-pending-overlay"'
        f'{hidden_attr} role="status" aria-live="polite">'
        f'<div class="spo-title">{title}</div>'
        f'<div class="spo-counts" id="scene-pending-counts">{counts_seed}</div>'
        f'<div class="spo-hint">Server detection running. Page will refresh on completion.</div>'
        f'</div>'
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
.cam-view-shared-toolbar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin-bottom: var(--s-2, 6px);
  padding: 5px 8px;
  background: var(--surface, #FCFBFA);
  border: 1px solid var(--border-base, #DBD6CD);
  border-radius: var(--r, 4px);
}
.cam-view-shared-toolbar .cv-layer {
  font: inherit; font-size: 9px; letter-spacing: 0.08em;
  padding: 3px 8px; background: transparent;
  color: var(--sub, #7a756c);
  border: 1px solid var(--border-base, #DBD6CD); border-radius: 2px;
  cursor: pointer; text-transform: uppercase;
}
.cam-view-shared-toolbar .cv-layer.on {
  background: var(--ink, #2A2520); color: #F8F7F4;
  border-color: var(--ink, #2A2520);
}
.cam-view-shared-toolbar .cv-path-group {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 6px;
  border: 1px solid var(--border-base, #DBD6CD); border-radius: 3px;
}
.cam-view-shared-toolbar .cv-path-lbl {
  font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--sub, #7a756c); font-weight: 500;
}
.cam-view-shared-toolbar .cv-opacity {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 9px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--sub, #7a756c);
}
.cam-view-shared-toolbar .cv-opacity input[type=range] { width: 80px; }
"""
)


def render_viewer_html(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
    *,
    strike_zone: dict | None = None,
    cost_threshold: float | None = None,
    gap_threshold_m: float | None = None,
    segments: list | None = None,
    segments_by_path: dict[str, list] | None = None,
) -> str:
    from pairing_tuning import PairingTuning
    _pt_default = PairingTuning.default()
    ctx = build_viewer_page_context(
        scene,
        videos,
        health,
        cost_threshold=cost_threshold,
        gap_threshold_m=gap_threshold_m,
        segments=[] if segments is None else segments,
        segments_by_path={} if segments_by_path is None else segments_by_path,
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
        # Operator picks which preset to detect under. Default selection
        # is the dashboard's current active preset (matches what the iOS
        # live path is using right now), but the operator can pick any
        # named preset on disk — the server resolves the file at request
        # time and stamps the chosen name onto
        # `SessionResult.server_post_preset_name`. Re-running with a
        # different preset overwrites both detection results and the
        # field; there is no per-session history of past server_post
        # runs.
        from main import state as _state
        from html import escape as _esc
        active = _state.detection_config().preset
        options = "".join(
            f'<option value="{_esc(p.name)}"'
            f'{" selected" if p.name == active else ""}>'
            f'{_esc(p.label)} ({_esc(p.name)})</option>'
            for p in _state.list_presets()
        )
        action_html = (
            f'<form method="POST" action="/sessions/{ctx.session_id}/run_server_post" class="action-form">'
            f'<select class="action-select" name="preset_name" '
            f'title="Detection preset to run server-side">'
            f'{options}</select>'
            f'<button class="action" type="submit">{label}</button>'
            f'{ts_html}'
            f'</form>'
        )
    else:
        action_html = ""
    progress_html = (
        '<span class="srv-progress" id="srv-progress" hidden'
        ' aria-live="polite"></span>'
    )
    # Three.js scene runtime injection — importmap + theme JSON +
    # boot module that mounts the scene onto `#scene` and sets up the
    # viewer-specific layers. Polled mount with bounded retry (matches
    # dashboard) so a WebGL failure surfaces instead of hanging.
    from scene_runtime import scene_runtime_html as _scene_runtime_html
    scene_runtime_fragment = _scene_runtime_html(
        container_id="scene",
        strike_zone=strike_zone,
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Session {scene.session_id}</title>
{scene_runtime_fragment}
<style>
{_viewer_css(ctx.scene_flex, ctx.videos_flex)}
{_VIEWER_CAM_VIEW_OVERRIDES}
</style>
</head><body>
<div class="viewer">
  <div class="nav">
    <span class="brand"><span class="dot"></span>BALL_TRACKER</span>
    {ctx.health_strip_html}
    {progress_html}
    {action_html}
    <a class="back" href="/">&larr; dashboard</a>
  </div>
  <div class="nav-tuning" role="region" aria-label="Per-session pairing tuning">
    {ctx.session_tuning_html}
  </div>
  {ctx.health_failure_html}
  <div class="work" data-mode="{ctx.layout_mode}">
    <div class="scene-col">
      <div class="latest-pitch-badge" id="viewer-speed-badge" hidden>
        <span class="lpb-speed" id="viewer-lpb-speed">—</span>
        <span class="lpb-units">kph</span>
        <span class="lpb-meta" id="viewer-lpb-meta"></span>
      </div>
      <div id="scene"></div>
      {_pending_overlay_html(ctx.processing_state)}
      {view_presets_toolbar_html()}
      <div class="scene-toolbar" role="toolbar" aria-label="Scene controls">
        <button id="mode-all" class="active" type="button" role="tab" title="Show full trajectory">All</button>
        <button id="mode-playback" type="button" role="tab" title="Cut trace at playback time">Playback</button>
      </div>
    </div>
    <div class="col-resizer" id="col-resizer" role="separator" aria-orientation="vertical" aria-label="Resize 3D scene vs cameras" tabindex="0" title="Drag to resize"></div>
    <div class="videos-col">{cam_view_shared_toolbar_html()}{ctx.video_cells_html}</div>
  </div>
  <div class="timeline">
    <div class="tl-row">
      <div class="scrubber-wrap">
        <div class="strip-legend"
             title="Strip colors: A detected (orange) · B detected (brown) · missed (grey) · no frame (pale) · chirp anchor (accent)"
             role="group" aria-label="Layer visibility + filters">
            <span class="layer-toggles" id="layer-toggles" aria-label="Layer visibility">
            <span class="layer-group" data-path-group role="radiogroup" aria-label="Active path">
              <span class="layer-name">Path</span>
              <button type="button" class="layer-pill" data-path="live" role="radio" aria-checked="false"><span class="layer-pill-label">live</span><span class="layer-pill-count" data-path-count="live"></span></button>
              <button type="button" class="layer-pill" data-path="server_post" role="radio" aria-checked="false"><span class="layer-pill-label">svr</span><span class="layer-pill-count" data-path-count="server_post"></span></button>
            </span>
            <span class="layer-divider" aria-hidden="true"></span>
            <span class="layer-group" data-layer-group="rays">
              <label class="layer-checkbox">
                <input type="checkbox" class="layer-checkbox" data-layer="rays" checked>
                <span class="layer-name">Rays</span>
              </label>
            </span>
            {layer_chip_with_popover_html(
                group_key="traj",
                label="Traj",
                layer_data_attr="traj",
                checked=True,
                popover_id="viewer-traj-popover",
                title="Trajectory points — click ▾ for display settings",
                popover_inner_html=point_size_slider_html(slot_id="viewer-point-size"),
            )}
            {layer_chip_with_popover_html(
                group_key="fit",
                label="Fit",
                layer_data_attr="fit",
                checked=True,
                popover_id="viewer-fit-popover",
                title="Fit curves — click ▾ for display settings (line width, dashed extension)",
                popover_inner_html=(
                    fit_line_width_slider_html(slot_id="viewer-fit-line-width")
                    + fit_extension_seconds_slider_html(slot_id="viewer-fit-extension")
                ),
            )}
            <span class="layer-divider" aria-hidden="true"></span>
            <span class="layer-group" data-layer-group="strike-zone" title="Toggle the strike-zone wireframe in the 3D scene. Default on.">
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
             title="LIVE — iOS on-device detection streamed over WS while the session was armed. Runs on raw BGRA frames pre-encode; earliest signal available. Top two bands = cam A / cam B per-frame detection; bottom band = ballistic fit segments (SegmentRecord t_start..t_end), coloured by the same palette as the 3D fit curves.">
          <span class="strip-label">LIVE</span>
          <span class="strip-sublabels" aria-hidden="true"><span>A</span><span>B</span><span>S</span></span>
          <canvas id="detection-canvas-live" class="strip-canvas" height="32" aria-hidden="true"></canvas>
        </div>
        <div class="strip-row" id="strip-row-server-post" hidden
             title="SVR — server-side detection on the H.264-decoded MOV. Independent from the iOS paths; H.264 quantization typically costs a few frames at detection edges. Top two bands = cam A / cam B per-frame detection; bottom band = fit segments for this path.">
          <span class="strip-label">SVR</span>
          <span class="strip-sublabels" aria-hidden="true"><span>A</span><span>B</span><span>S</span></span>
          <canvas id="detection-canvas-server-post" class="strip-canvas" height="32" aria-hidden="true"></canvas>
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
  "camera_colors": {ctx.camera_colors_json},
  "fallback_color": {ctx.fallback_color_json},
  "accent_color": {ctx.accent_color_json},
  "scene_theme": {ctx.scene_theme_json},
  "videos": {ctx.videos_json},
  "segments": {ctx.segments_json},
  "segments_by_path": {ctx.segments_by_path_json},
  "has_triangulated": {str(ctx.has_triangulated).lower()}
}}</script>
<script>
window.VIEWER_INITIAL_COST_THRESHOLD = {float(_pt_default.cost_threshold) if ctx.cost_threshold is None else float(ctx.cost_threshold)};
// Initial gap in METRES (None → PairingTuning.default().gap_threshold_m).
// 50_canvas.js converts to client-side residualCapM as a finite metres
// value; the slider's 200cm position is just `2.0m`, no Infinity special
// case.
window.VIEWER_INITIAL_GAP_THRESHOLD_M = {float(_pt_default.gap_threshold_m) if ctx.gap_threshold_m is None else float(ctx.gap_threshold_m)};
window._applyTuning = function(btn) {{
  const costInput = document.querySelector('[data-session-cost-threshold]');
  const gapInput = document.querySelector('[data-session-gap-threshold]');
  const costValueEl = document.querySelector('[data-session-cost-value]');
  const gapValueEl = document.querySelector('[data-session-gap-value]');
  const cost = parseFloat(costInput.value);
  // Slider value is centimetres (0–200); ship metres to the route.
  // 200cm = 2.0m = the route's max — every cartesian pair survives the
  // gap gate at that setting.
  const gap_m = parseFloat(gapInput.value) / 100;
  const sid = btn.getAttribute('data-session-id');
  // In-flight guard: button + sliders disabled together, recomputing
  // class drives the spinner border. The slider oninput handlers check
  // data-recomputing before re-enabling the button so a mid-flight
  // wiggle can't sneak past the disabled state.
  btn.disabled = true;
  btn.dataset.recomputing = '1';
  btn.classList.add('recomputing');
  btn.textContent = 'Recomputing…';
  costInput.disabled = true;
  gapInput.disabled = true;
  fetch('/sessions/' + encodeURIComponent(sid) + '/recompute', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ cost_threshold: cost, gap_threshold_m: gap_m }}),
  }}).then(function(r) {{
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }}).then(function(body) {{
    if (!body || !body.result) throw new Error('recompute response missing `result`');
    const r = body.result;
    if (!window.BallTrackerViewerScene) {{
      throw new Error('Three.js viewer scene not mounted — cannot patch');
    }}
    window.BallTrackerViewerScene.setSessionData({{
      points: r.points || [],
      triangulated_by_path: r.triangulated_by_path || {{}},
      segments: r.segments || [],
      segments_by_path: r.segments_by_path || {{}},
    }});
    if (window._viewerPatchSegmentsState) {{
      window._viewerPatchSegmentsState(
        r.segments || [],
        r.segments_by_path || {{}},
      );
    }}
    // Reflect server-persisted values back onto the sliders + tick
    // labels — operator sees what was actually applied, not whatever
    // they happened to drag to before clicking. Subsequent drags
    // diff from this new baseline.
    if (typeof r.cost_threshold === 'number') {{
      window.VIEWER_INITIAL_COST_THRESHOLD = r.cost_threshold;
      costInput.value = r.cost_threshold.toFixed(2);
      if (costValueEl) costValueEl.textContent = r.cost_threshold.toFixed(2);
    }}
    if (typeof r.gap_threshold_m === 'number') {{
      window.VIEWER_INITIAL_GAP_THRESHOLD_M = r.gap_threshold_m;
      const cm = Math.round(r.gap_threshold_m * 100);
      gapInput.value = String(cm);
      if (gapValueEl) gapValueEl.textContent = '≤ ' + cm + ' cm';
    }}
    btn.textContent = 'Apply';
    btn.disabled = true;
    btn.classList.remove('recomputing');
    delete btn.dataset.recomputing;
    costInput.disabled = false;
    gapInput.disabled = false;
  }}).catch(function(err) {{
    btn.disabled = false;
    btn.textContent = 'Apply';
    btn.classList.remove('recomputing');
    delete btn.dataset.recomputing;
    costInput.disabled = false;
    gapInput.disabled = false;
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
<script type="module">
import {{ setupViewerLayers }} from "/static/threejs/viewer_layers.js";
// Bounded poll for the scene runtime to mount, mirroring the
// dashboard's boot pattern. The viewer-specific layer module
// reads `window.VIEWER_DATA` (set below by the IIFE) for the
// scene/segments/traj payload.
let _attempts = 0;
function _hookup() {{
  if (window.BallTrackerScene && window.VIEWER_DATA) {{
    const d = window.VIEWER_DATA;
    setupViewerLayers(window.BallTrackerScene, {{
      SCENE: d.SCENE,
      SEGMENTS: d.SEGMENTS,
      SEGMENTS_BY_PATH: d.SEGMENTS_BY_PATH,
      TRAJ_BY_PATH: d.TRAJ_BY_PATH,
      HAS_TRIANGULATED: d.HAS_TRIANGULATED,
      fallbackColor: d.FALLBACK_COLOR,
      tInitial: d.tMin || 0,
      mode: 'all',
      layerVisibility: d.layerVisibility,
    }});
    return;
  }}
  if (++_attempts > 50) {{
    const root = document.getElementById('scene');
    const sceneOk = !!window.BallTrackerScene;
    const dataOk = !!window.VIEWER_DATA;
    const reason = !sceneOk && !dataOk
      ? "neither BallTrackerScene nor VIEWER_DATA appeared"
      : !sceneOk ? "BallTrackerScene runtime never mounted (WebGL context issue?)"
      : "VIEWER_DATA never set (classic IIFE failed mid-init?)";
    if (root) root.innerHTML =
      "<div style=\\"padding:24px;font-family:monospace;color:#C0392B;\\">"
      + "3D scene failed to mount — " + reason + ". "
      + "Check the browser console for the actual error.</div>";
    console.error('Viewer scene mount failed:', reason);
    return;
  }}
  setTimeout(_hookup, 50);
}}
_hookup();
</script>
<script>
{_viewer_js()}
</script>
<script>
(function() {{
  if (!window.EventSource) {{
    throw new Error("viewer SSE init: EventSource missing");
  }}
  const VIEWER_SID = {_json.dumps(ctx.session_id)};
  const navChip = document.getElementById('srv-progress');
  const overlay = document.getElementById('scene-pending-overlay');
  const counts = document.getElementById('scene-pending-counts');
  if (!navChip || !overlay || !counts) {{
    throw new Error("viewer SSE init: progress DOM missing");
  }}
  const slots = {{}};  // cam → {{ done, total }}
  function render() {{
    const cams = Object.keys(slots).sort();
    if (cams.length === 0) {{
      navChip.hidden = true; navChip.textContent = '';
      overlay.hidden = true; counts.textContent = '';
      return;
    }}
    const summary = cams.map(function(c) {{
      const s = slots[c];
      const tot = (s.total != null) ? s.total : '?';
      return c + ' ' + s.done + '/' + tot;
    }}).join('  ·  ');
    navChip.hidden = false;
    navChip.textContent = 'svr ' + summary;
    overlay.hidden = false;
    counts.textContent = summary;
  }}
  const es = new EventSource('/stream');
  es.addEventListener('server_post_progress', function(evt) {{
    const d = JSON.parse(evt.data);
    if (d.sid !== VIEWER_SID) return;
    slots[d.cam] = {{
      done: Number(d.frames_done),
      total: d.frames_total != null ? Number(d.frames_total) : null,
    }};
    render();
  }});
  es.addEventListener('server_post_done', function(evt) {{
    const d = JSON.parse(evt.data);
    if (d.sid !== VIEWER_SID) return;
    delete slots[d.cam];
    render();
    // Authoritative refresh once the last cam wraps so the page picks
    // up the new triangulated points + path_status without a manual
    // reload. The fit SSE handler in 85_sse_fit.js patches segments
    // in place, but server_post finishing also rewrites frames_server_post
    // which the IIFE seeded at first paint — easier to reload than to
    // surgically rebuild every per-cam frame index.
    if (Object.keys(slots).length === 0) {{
      setTimeout(function() {{ location.reload(); }}, 800);
    }}
  }});
}})();
</script>
</body></html>"""


_VIEWER_STATIC_DIR = Path(__file__).parent / "static" / "viewer"


def _resolve_viewer_css_template() -> str:
    """Load `static/viewer/viewer.css` and substitute the per-theme color
    tokens (`{BG}`, `{INK}`, …). Same pattern as
    `_resolve_viewer_js_template`: literal `{NAME}` placeholders, NOT
    f-string fields, so resolve via `str.replace`. The remaining
    `{SCENE_FLEX}` / `{VIDEOS_FLEX}` slots are filled per-page in
    `_viewer_css`."""
    css = (_VIEWER_STATIC_DIR / "viewer.css").read_text(encoding="utf-8")
    css = css.replace("{BG}", _BG).replace("{SURFACE}", _SURFACE)
    css = css.replace("{INK}", _INK).replace("{SUB}", _SUB)
    css = css.replace("{BORDER_BASE}", _BORDER_BASE).replace("{BORDER_L}", _BORDER_L)
    css = css.replace("{CONTRA}", _CONTRA).replace("{DUAL}", _DUAL)
    css = css.replace("{DEV}", _DEV).replace("{ACCENT}", _ACCENT)
    css = css.replace("{OK}", _OK).replace("{PENDING}", _PENDING)
    # Catch any placeholder we forgot to wire up — failure mode is silent
    # otherwise (browser ignores invalid CSS, dashboard renders unstyled).
    # Mirrors the 2026-04-22 dashboard incident where an unresolved
    # `{PLATE_WORLD_JS}` token blew up the IIFE in runtime; here the
    # symptom would be even quieter, so fail loud at module load instead.
    leftover = re.findall(r"\{[A-Z_]+\}", css)
    per_call = {"{SCENE_FLEX}", "{VIDEOS_FLEX}"}
    unresolved = [t for t in leftover if t not in per_call]
    if unresolved:
        raise RuntimeError(
            f"viewer.css has unresolved placeholders: {sorted(set(unresolved))}"
        )
    return css


_VIEWER_CSS_TEMPLATE = _resolve_viewer_css_template()


def _viewer_css(scene_flex: str, videos_flex: str) -> str:
    return (
        "\n"
        + _VIEWER_CSS_TEMPLATE
            .replace("{SCENE_FLEX}", scene_flex)
            .replace("{VIDEOS_FLEX}", videos_flex)
    )


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
