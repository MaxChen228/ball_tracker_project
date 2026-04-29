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
    _STRIKE_ZONE_COLOR,
    _STRIKE_ZONE_FILL_OPACITY,
    _STRIKE_ZONE_LINE_WIDTH,
    _STRIKE_ZONE_X_HALF_M,
    _STRIKE_ZONE_Y_BACK_M,
    _STRIKE_ZONE_Y_FRONT_M,
    _STRIKE_ZONE_Z_BOTTOM_M,
    _STRIKE_ZONE_Z_TOP_M,
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
    *,
    cost_threshold: float | None = None,
    gap_threshold_m: float | None = None,
) -> str:
    from viewer_page import render_viewer_html as _render_viewer_html

    return _render_viewer_html(
        scene,
        videos,
        health,
        build_figure=_build_figure,
        cost_threshold=cost_threshold,
        gap_threshold_m=gap_threshold_m,
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

    # Strike zone — a rectangular prism above the plate. 12-edge wireframe
    # plus a low-opacity Mesh3d fill so it reads as a volume rather than
    # eight floating verts. Sits on the same scene as plate / cameras /
    # rays so it appears in both the dashboard canvas and the per-session
    # viewer with no extra wiring.
    sx = _STRIKE_ZONE_X_HALF_M
    syf = _STRIKE_ZONE_Y_FRONT_M
    syb = _STRIKE_ZONE_Y_BACK_M
    szb = _STRIKE_ZONE_Z_BOTTOM_M
    szt = _STRIKE_ZONE_Z_TOP_M
    # 8 corners, indexed bottom CCW (0-3) then top CCW (4-7):
    _sz_corners = [
        (-sx, syf, szb), (+sx, syf, szb), (+sx, syb, szb), (-sx, syb, szb),
        (-sx, syf, szt), (+sx, syf, szt), (+sx, syb, szt), (-sx, syb, szt),
    ]
    _sz_edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),  # bottom rectangle
        (4, 5), (5, 6), (6, 7), (7, 4),  # top rectangle
        (0, 4), (1, 5), (2, 6), (3, 7),  # vertical struts
    ]
    sz_xs: list[float | None] = []
    sz_ys: list[float | None] = []
    sz_zs: list[float | None] = []
    for i, j in _sz_edges:
        sz_xs += [_sz_corners[i][0], _sz_corners[j][0], None]
        sz_ys += [_sz_corners[i][1], _sz_corners[j][1], None]
        sz_zs += [_sz_corners[i][2], _sz_corners[j][2], None]
    traces.append(
        go.Scatter3d(
            x=sz_xs,
            y=sz_ys,
            z=sz_zs,
            mode="lines",
            line=dict(color=_STRIKE_ZONE_COLOR, width=_STRIKE_ZONE_LINE_WIDTH),
            name="strike zone",
            hoverinfo="skip",
            showlegend=False,
            # `feature` (not `trace_kind`) so the viewer's static-trace
            # filter — which drops anything with trace_kind set — keeps
            # the zone in STATIC. The strike-zone toggle reads this on
            # the JS side to hide the wireframe + fill in lock-step.
            meta=dict(feature="strike_zone"),
        )
    )
    # Translucent solid: 6 faces, 12 triangles (i/j/k point into the
    # 8-corner array above).
    traces.append(
        go.Mesh3d(
            x=[c[0] for c in _sz_corners],
            y=[c[1] for c in _sz_corners],
            z=[c[2] for c in _sz_corners],
            i=[0, 0, 4, 4, 0, 0, 1, 1, 0, 0, 3, 3],
            j=[1, 2, 5, 6, 1, 5, 2, 6, 4, 3, 4, 7],
            k=[2, 3, 6, 7, 5, 4, 6, 5, 3, 7, 7, 4],
            color=_STRIKE_ZONE_COLOR,
            opacity=_STRIKE_ZONE_FILL_OPACITY,
            flatshading=True,
            hoverinfo="skip",
            showlegend=False,
            name="strike zone fill",
            meta=dict(feature="strike_zone"),
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

    # Group rays per camera; segmenter handles outlier rejection
    # downstream so the viewer doesn't pre-classify per-frame anymore.
    rays_by_cam: dict[str, list] = {}
    for r in scene.rays:
        rays_by_cam.setdefault(r.camera_id, []).append(r)

    for cam_id in sorted(rays_by_cam.keys()):
        rays = rays_by_cam[cam_id]
        xs: list[float | None] = []
        ys: list[float | None] = []
        zs: list[float | None] = []
        for r in rays:
            xs.extend([r.origin[0], r.endpoint[0], None])
            ys.extend([r.origin[1], r.endpoint[1], None])
            zs.extend([r.origin[2], r.endpoint[2], None])
        color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
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

    # Ground traces — single trace per camera, no per-status splitting.
    dashed = bool(scene.triangulated)
    for cam_id in sorted(scene.ground_traces.keys()):
        pts = scene.ground_traces[cam_id]
        if not pts:
            continue
        pts = sorted(pts, key=lambda p: p["t_rel_s"])
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        zs = [p["z"] for p in pts]
        ts = [p["t_rel_s"] for p in pts]
        color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
        opacity = 0.7 if not dashed else 0.45
        traces.append(go.Scatter3d(
            x=xs, y=ys, z=zs,
            mode="lines+markers",
            marker=dict(size=3, color=color),
            line=dict(color=color, width=3, dash="dash" if dashed else "solid"),
            opacity=opacity,
            name=f"Ground trace {cam_id} ({len(pts)} pts)",
            hovertemplate=(
                f"Cam {cam_id} ground"
                "<br>t=%{customdata:.3f}s"
                "<br>x=%{x:.2f} m"
                "<br>y=%{y:.2f} m<extra></extra>"
            ),
            customdata=ts,
            meta=dict(trace_kind="ground_trace", camera_id=cam_id),
        ))

    if scene.triangulated:
        pts = scene.triangulated
        traces.append(
            go.Scatter3d(
                x=[p["x"] for p in pts],
                y=[p["y"] for p in pts],
                z=[p["z"] for p in pts],
                mode="lines+markers",
                line=dict(color=_ACCENT, width=4),
                marker=dict(size=4, color=_ACCENT),
                name=f"3D trajectory ({len(pts)} pts)",
                hovertemplate=(
                    "x=%{x:.2f} m<br>y=%{y:.2f} m<br>z=%{z:.2f} m<extra></extra>"
                ),
                meta=dict(trace_kind="triangulated"),
            )
        )

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
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        scene=dict(
            xaxis=axis("X (left/right, m)"),
            yaxis=axis("Y (depth, m)"),
            zaxis=axis("Z (up, m)"),
            bgcolor=_BG,
            aspectmode="data",
            # ISO preset baked into the figure so first paint matches
            # the toolbar's default-active ISO chip. Centre = strike-zone
            # centroid (X=0, Y=0.216, Z=0.76) so the box sits at the
            # frame middle; every preset shares this centre. Keep in
            # sync with VIEW_PRESETS.iso in 75_view_presets.js.
            camera=dict(
                eye=dict(x=1.6, y=1.816, z=1.56),
                up=dict(x=0, y=0, z=1),
                center=dict(x=0, y=0.216, z=0.76),
            ),
            uirevision="viewer-scene",
        ),
        margin=dict(l=0, r=0, t=8, b=0),
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
