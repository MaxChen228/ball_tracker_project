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


def render_viewer_html(scene: Scene, videos: list[tuple[str, str]]) -> str:
    """Full /viewer/{sid} page: 3D scene on top, A/B MOV playback below.

    `videos` is a sorted list of `(camera_id, url)` pairs — typically
    `[("A", "/videos/session_..._A.mov"), ("B", "/videos/session_..._B.mov")]`.
    Videos are rendered side-by-side with a shared play/pause control so
    the operator can scrub A and B in lock-step when reviewing a cycle.
    """
    scene_fragment = _build_figure(scene).to_html(
        include_plotlyjs="cdn", full_html=False, div_id="scene"
    )
    video_cells = "".join(
        f"""
        <div class="vid-cell">
          <div class="vid-label" style="border-color:{_camera_color(cam)};color:{_camera_color(cam)};">CAM {cam}</div>
          <video data-cam="{cam}" controls preload="metadata" playsinline src="{url}"></video>
        </div>
        """
        for cam, url in videos
    )
    if not video_cells:
        video_cells = (
            '<div class="vid-empty">no clips on disk for this session</div>'
        )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Session {scene.session_id}</title>
<style>
  html, body {{ margin:0; padding:0; background:{_BG}; color:{_INK};
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .viewer {{ display:flex; flex-direction:column; min-height:100vh; }}
  header {{ padding:16px 24px; border-bottom:1px solid {_BORDER_BASE};
    background:{_SURFACE}; display:flex; align-items:baseline; gap:16px; }}
  header h1 {{ margin:0; font-size:18px; font-weight:600; letter-spacing:0.02em; }}
  header .sid {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color:{_SUB}; font-size:14px; }}
  header a {{ margin-left:auto; color:{_SUB}; font-size:13px; text-decoration:none; }}
  header a:hover {{ color:{_INK}; }}
  .scene-wrap {{ flex:1 1 auto; min-height:420px; }}
  .scene-wrap > div {{ height:100%; }}
  .videos {{ display:grid; grid-template-columns:1fr 1fr; gap:1px;
    background:{_BORDER_BASE}; border-top:1px solid {_BORDER_BASE}; }}
  .vid-cell {{ background:{_SURFACE}; padding:12px; display:flex;
    flex-direction:column; gap:8px; }}
  .vid-label {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size:11px; font-weight:600; letter-spacing:0.1em;
    border:1px solid; padding:2px 8px; align-self:flex-start; border-radius:2px; }}
  .vid-cell video {{ width:100%; max-height:50vh; background:#000; }}
  .vid-empty {{ grid-column:1 / -1; padding:32px; text-align:center;
    color:{_SUB}; font-size:14px; background:{_SURFACE}; }}
  .sync-note {{ padding:8px 24px; font-size:12px; color:{_SUB};
    background:{_SURFACE}; border-top:1px solid {_BORDER_BASE}; }}
</style>
</head><body>
<div class="viewer">
  <header>
    <h1>Session Viewer</h1>
    <span class="sid">{scene.session_id}</span>
    <a href="/">&larr; dashboard</a>
  </header>
  <div class="scene-wrap">{scene_fragment}</div>
  <div class="videos">{video_cells}</div>
  <div class="sync-note">Playback: press play on either video — both A and B start together and scrub-sync on seek.</div>
</div>
<script>
(() => {{
  const vids = Array.from(document.querySelectorAll("video[data-cam]"));
  if (vids.length < 2) return;
  let syncing = false;
  const run = (fn) => {{ if (syncing) return; syncing = true; fn(); syncing = false; }};
  vids.forEach((src) => {{
    src.addEventListener("play",  () => run(() => vids.forEach((v) => v !== src && v.paused && v.play())));
    src.addEventListener("pause", () => run(() => vids.forEach((v) => v !== src && !v.paused && v.pause())));
    src.addEventListener("seeked", () => run(() => vids.forEach((v) => {{ if (v !== src && Math.abs(v.currentTime - src.currentTime) > 0.05) v.currentTime = src.currentTime; }})));
    src.addEventListener("ratechange", () => run(() => vids.forEach((v) => v !== src && (v.playbackRate = src.playbackRate))));
  }});
}})();
</script>
</body></html>"""


def _camera_color(camera_id: str) -> str:
    return _CAMERA_COLORS.get(camera_id, _FALLBACK_CAMERA_COLOR)


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
