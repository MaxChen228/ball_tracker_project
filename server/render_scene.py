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
    _GHOST_FLICKER,
    _GHOST_JUMP,
    _GHOST_LINE_WIDTH,
    _GHOST_OPACITY,
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


# filter_status → (display color, display name suffix, opacity, line width, meta tag).
# Kept items use the per-camera color; rejected items use semantic ghost colors.
_GHOST_STYLES = {
    "rejected_flicker": (_GHOST_FLICKER, "flicker", _GHOST_OPACITY, _GHOST_LINE_WIDTH, "flicker"),
    "rejected_jump":    (_GHOST_JUMP,    "jump",    _GHOST_OPACITY, _GHOST_LINE_WIDTH, "jump"),
}


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

    # Split rays by (camera_id, filter_status) so kept detections render in
    # the camera color and rejected ones render in the ghost palette. Each
    # bucket becomes its own Plotly trace so the legend lets operators toggle
    # categories individually, and the "Hide rejected" updatemenu hides all
    # rejected buckets at once.
    rays_by_key: dict[tuple[str, str | None], list] = {}
    for r in scene.rays:
        status = getattr(r, "filter_status", None)
        rays_by_key.setdefault((r.camera_id, status), []).append(r)

    rejected_trace_indices: list[int] = []
    # Stable order: kept first (per cam), then rejected categories.
    def _status_key(k: tuple[str, str | None]) -> tuple[str, int]:
        _, st = k
        order = {None: 0, "kept": 0, "rejected_flicker": 1, "rejected_jump": 2}
        return (k[0], order.get(st, 9))

    for (cam_id, status) in sorted(rays_by_key.keys(), key=_status_key):
        rays = rays_by_key[(cam_id, status)]
        xs: list[float | None] = []
        ys: list[float | None] = []
        zs: list[float | None] = []
        for r in rays:
            xs.extend([r.origin[0], r.endpoint[0], None])
            ys.extend([r.origin[1], r.endpoint[1], None])
            zs.extend([r.origin[2], r.endpoint[2], None])
        if status in _GHOST_STYLES:
            color, suffix, opacity, width, tag = _GHOST_STYLES[status]
            name = f"Rays {cam_id} · {suffix} ({len(rays)})"
        else:
            color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
            opacity = 0.35
            width = 2
            tag = "kept"
            name = f"Rays {cam_id} ({len(rays)})"
        traces.append(
            go.Scatter3d(
                x=xs,
                y=ys,
                z=zs,
                mode="lines",
                line=dict(color=color, width=width),
                opacity=opacity,
                name=name,
                hoverinfo="skip",
                meta=dict(trace_kind="ray", camera_id=cam_id, filter_status=tag),
            )
        )
        if tag in ("flicker", "jump"):
            rejected_trace_indices.append(len(traces) - 1)

    # Same split for ground traces.
    gt_by_key: dict[tuple[str, str | None], list] = {}
    for cam_id, trace in scene.ground_traces.items():
        for p in trace:
            status = p.get("filter_status")
            gt_by_key.setdefault((cam_id, status), []).append(p)

    dashed = bool(scene.triangulated)
    for (cam_id, status) in sorted(gt_by_key.keys(), key=_status_key):
        pts = gt_by_key[(cam_id, status)]
        if not pts:
            continue
        pts = sorted(pts, key=lambda p: p["t_rel_s"])
        xs = [p["x"] for p in pts]
        ys = [p["y"] for p in pts]
        zs = [p["z"] for p in pts]
        ts = [p["t_rel_s"] for p in pts]
        if status in _GHOST_STYLES:
            color, suffix, opacity, _width, tag = _GHOST_STYLES[status]
            name = f"Ground {cam_id} · {suffix} ({len(pts)} pts)"
            marker_size = 2
            # Ghost ground-trace: connecting a line through rejected points
            # would imply motion continuity that the rejection argues against.
            # Draw as scatter dots only.
            mode = "markers"
            line_kw = None
        else:
            color = _CAMERA_COLORS.get(cam_id, _FALLBACK_CAMERA_COLOR)
            opacity = 0.7 if not dashed else 0.45
            name = f"Ground trace {cam_id} ({len(pts)} pts)"
            marker_size = 3
            mode = "lines+markers"
            line_kw = dict(color=color, width=3, dash="dash" if dashed else "solid")
            tag = "kept"
        kw = dict(
            x=xs, y=ys, z=zs,
            mode=mode,
            marker=dict(size=marker_size, color=color),
            opacity=opacity,
            name=name,
            hovertemplate=(
                f"Cam {cam_id} ground"
                "<br>t=%{customdata:.3f}s"
                "<br>x=%{x:.2f} m"
                "<br>y=%{y:.2f} m<extra></extra>"
            ),
            customdata=ts,
            meta=dict(trace_kind="ground_trace", camera_id=cam_id, filter_status=tag),
        )
        if line_kw is not None:
            kw["line"] = line_kw
        traces.append(go.Scatter3d(**kw))
        if tag in ("flicker", "jump"):
            rejected_trace_indices.append(len(traces) - 1)

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

    # Ballistic RANSAC fit overlays (one per detection path). Dashed line
    # to differentiate from the raw triangulated polyline; line-only (no
    # markers) since the samples are interpolated. Colors are distinct
    # from the per-camera ray palette + the _ACCENT triangulated line:
    # server_post → _DEV (red), live → _CONTRA (blue). Legendgroup lets
    # the viewer toggle both curves together.
    _BALLISTIC_STYLES = {
        "server_post": (_DEV, "server"),
        "live": (_CONTRA, "live"),
    }
    for path_value in sorted(scene.ballistic_curves.keys()):
        curve = scene.ballistic_curves[path_value]
        if not curve:
            continue
        color, suffix = _BALLISTIC_STYLES.get(path_value, (_ACCENT, path_value))
        xs = [pt[1] for pt in curve]
        ys = [pt[2] for pt in curve]
        zs = [pt[3] for pt in curve]
        ts = [pt[0] for pt in curve]
        traces.append(
            go.Scatter3d(
                x=xs, y=ys, z=zs,
                mode="lines",
                line=dict(color=color, width=6, dash="dash"),
                opacity=0.85,
                name=f"Ballistic fit · {suffix} ({len(curve)} pts)",
                legendgroup="ballistic",
                hovertemplate=(
                    f"Ballistic {suffix}"
                    "<br>t=%{customdata:.3f}s"
                    "<br>x=%{x:.2f} m<br>y=%{y:.2f} m<br>z=%{z:.2f} m<extra></extra>"
                ),
                customdata=ts,
                meta=dict(trace_kind="ballistic_fit", path=path_value),
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
    # Button (top-right) that collapses every rejected trace to legend-only at
    # once. Second click brings them back. Only rendered when something is
    # actually rejected — no point showing a disabled button otherwise.
    updatemenus = []
    if rejected_trace_indices:
        n_traces = len(traces)
        hide_visible: list[object] = [True] * n_traces
        for i in rejected_trace_indices:
            hide_visible[i] = "legendonly"
        show_visible: list[object] = [True] * n_traces
        updatemenus.append(dict(
            type="buttons",
            direction="left",
            showactive=True,
            x=1.0,
            xanchor="right",
            y=1.08,
            yanchor="bottom",
            bgcolor=_SURFACE,
            bordercolor=_BORDER_BASE,
            borderwidth=1,
            font=dict(family="JetBrains Mono, monospace", size=10, color=_INK),
            pad=dict(l=6, r=6, t=2, b=2),
            buttons=[dict(
                label="Hide rejected",
                method="restyle",
                args=[{"visible": hide_visible}],
                args2=[{"visible": show_visible}],
            )],
        ))
    fig.update_layout(
        updatemenus=updatemenus,
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
