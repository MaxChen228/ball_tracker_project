"""Plotly 3D scene renderer for the dashboard canvas and session viewer."""
from __future__ import annotations

from reconstruct import Scene
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
    _FALLBACK_CAMERA_COLOR,
    _GROUND_HALF_EXTENT_M,
    _INK,
    _INK_40,
    _PLATE_X,
    _PLATE_Y,
    _SUB,
    _SURFACE,
    _WORLD_AXIS_LEN_M,
)


def render_scene_html(scene: Scene) -> str:
    return _build_figure(scene).to_html(include_plotlyjs="cdn", full_html=True)


def render_scene_div(scene: Scene, div_id: str = "canvas-scene") -> str:
    return _build_figure(scene).to_html(
        include_plotlyjs=False,
        full_html=False,
        div_id=div_id,
    )


def render_viewer_html(
    scene: Scene,
    videos: list[tuple[str, str, float, float, dict[str, list]]],
    health: dict,
) -> str:
    from viewer_page import render_viewer_html as _render_viewer_html

    return _render_viewer_html(
        scene,
        videos,
        health,
        build_figure=_build_figure,
    )


def _build_figure(scene: Scene):
    import plotly.graph_objects as go

    traces: list = []
    g = _GROUND_HALF_EXTENT_M
    traces.append(
        go.Mesh3d(
            x=[-g, g, g, -g],
            y=[-g, -g, g, g],
            z=[0.0, 0.0, 0.0, 0.0],
            i=[0, 0],
            j=[1, 2],
            k=[2, 3],
            color=_BORDER_L,
            opacity=0.18,
            name="ground (Z=0)",
            hoverinfo="skip",
            showlegend=False,
        )
    )
    traces.append(
        go.Mesh3d(
            x=_PLATE_X,
            y=_PLATE_Y,
            z=[0.0] * 5,
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
                textfont=dict(
                    family="JetBrains Mono, monospace",
                    size=11,
                    color=_INK,
                ),
                line=dict(color=color, width=4),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    for cam in scene.cameras:
        color = _CAMERA_COLORS.get(cam.camera_id, _FALLBACK_CAMERA_COLOR)
        cx, cy, cz = cam.center_world
        traces.append(
            go.Scatter3d(
                x=[cx],
                y=[cy],
                z=[cz],
                mode="markers+text",
                marker=dict(size=8, color=color, symbol="diamond"),
                text=[f"Cam {cam.camera_id}"],
                textposition="top center",
                showlegend=False,
                meta=dict(trace_kind="camera", camera_id=cam.camera_id),
                hovertemplate=(
                    f"Camera {cam.camera_id}"
                    "<br>x=%{x:.2f} m"
                    "<br>y=%{y:.2f} m"
                    "<br>z=%{z:.2f} m<extra></extra>"
                ),
            )
        )
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
                    meta=dict(trace_kind="camera_axis", camera_id=cam.camera_id),
                )
            )

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
                x=xs,
                y=ys,
                z=zs,
                mode="lines",
                line=dict(color=color, width=2),
                opacity=0.35,
                name=f"Rays {cam_id} ({len(rays)})",
                hoverinfo="skip",
                meta=dict(trace_kind="ray", camera_id=cam_id),
            )
        )

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
                x=xs,
                y=ys,
                z=zs,
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

    if scene.triangulated:
        ts = [p["t_rel_s"] for p in scene.triangulated]
        xs = [p["x"] for p in scene.triangulated]
        ys = [p["y"] for p in scene.triangulated]
        zs = [p["z"] for p in scene.triangulated]
        traces.append(
            go.Scatter3d(
                x=xs,
                y=ys,
                z=zs,
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

    n_cams = len(scene.cameras)
    # Break the ray count out by DetectionPath so the viewer title mirrors
    # the three independent pills. "live" / "post" / "svr" map 1-to-1 to
    # live / ios_post / server_post on the backend. The reconstruct.py
    # source tag uses the older strings ("on_device", "server"), hence the
    # translation below.
    ray_counts = {"live": 0, "ios_post": 0, "server_post": 0}
    for r in scene.rays:
        src = getattr(r, "source", None) or "server"
        if src == "on_device":
            ray_counts["ios_post"] += 1
        elif src == "live":
            ray_counts["live"] += 1
        else:
            ray_counts["server_post"] += 1
    path_label = [("live", "live"), ("ios_post", "post"), ("server_post", "svr")]
    ray_parts = [f"{ray_counts[p]} {lbl}" for p, lbl in path_label if ray_counts[p]]
    ray_str = " / ".join(ray_parts) if ray_parts else f"{len(scene.rays)} rays"
    subtitle = f"{n_cams} cam · {ray_str}"
    triag_parts = []
    if scene.triangulated:
        triag_parts.append(f"{len(scene.triangulated)} svr")
    if scene.triangulated_on_device:
        triag_parts.append(f"{len(scene.triangulated_on_device)} post")
    if triag_parts:
        subtitle += " · " + " + ".join(triag_parts) + " 3D"

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
            camera=dict(
                eye=dict(x=1.5, y=1.5, z=1.0),
                up=dict(x=0, y=0, z=1),
                center=dict(x=0, y=0.2, z=0.3),
            ),
            uirevision="viewer-scene",
        ),
        margin=dict(l=0, r=0, t=36, b=0),
        legend=dict(
            itemsizing="constant",
            bgcolor=_SURFACE,
            bordercolor=_BORDER_BASE,
            borderwidth=1,
            font=dict(family="JetBrains Mono, monospace", size=10, color=_INK),
            orientation="h",
            x=0.0,
            xanchor="left",
            y=-0.02,
            yanchor="top",
        ),
        font=dict(family="Noto Sans TC, sans-serif", color=_INK),
    )
    return fig
