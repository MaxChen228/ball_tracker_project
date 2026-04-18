"""Plotly 3D scene renderer for /viewer/{session_id} — extracted from viewer.py.

Returns a self-contained HTML string with the ground plane, world axes,
per-camera marker + local RGB triad + forward arrow, ray bundles, and
the triangulated trajectory coloured by time. Loads Plotly.js from CDN
so the file stays tiny and opens in any modern browser without a build
step. `reconstruct.Scene` is the stable input hand-off.
"""
from __future__ import annotations

from reconstruct import Scene

_CAMERA_COLORS = {
    "A": "royalblue",
    "B": "darkorange",
}
_FALLBACK_CAMERA_COLOR = "gray"
_GROUND_HALF_EXTENT_M = 1.5   # ground mesh drawn from (-1.5, -1.5) to (+1.5, +1.5)
_WORLD_AXIS_LEN_M = 0.3
_CAMERA_AXIS_LEN_M = 0.25
_CAMERA_FORWARD_ARROW_M = 0.5


def render_scene_html(scene: Scene) -> str:
    import plotly.graph_objects as go

    traces: list = []

    # --- Ground plane (Z=0) ---
    g = _GROUND_HALF_EXTENT_M
    traces.append(
        go.Mesh3d(
            x=[-g, g, g, -g],
            y=[-g, -g, g, g],
            z=[0.0, 0.0, 0.0, 0.0],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color="lightgray",
            opacity=0.25,
            name="ground (Z=0)",
            hoverinfo="skip",
            showlegend=False,
        )
    )

    # --- World axes at origin (RGB) ---
    for direction, color, label in (
        ((1.0, 0.0, 0.0), "crimson", "X"),
        ((0.0, 1.0, 0.0), "seagreen", "Y"),
        ((0.0, 0.0, 1.0), "royalblue", "Z_world"),
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
                line=dict(color=color, width=5),
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

        # Local axes: forward (cam+Z) long blue arrow, right (+X) short red,
        # up (-image_down) short green. Helps see orientation at a glance.
        for axis, axis_color, length in (
            (cam.axis_forward_world, color, _CAMERA_FORWARD_ARROW_M),
            (cam.axis_right_world, "crimson", _CAMERA_AXIS_LEN_M),
            (cam.axis_up_world, "seagreen", _CAMERA_AXIS_LEN_M),
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
                line=dict(color="limegreen", width=4),
                marker=dict(
                    size=4,
                    color=ts,
                    colorscale="Plasma",
                    showscale=True,
                    colorbar=dict(title="t (s)"),
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

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=f"Session {scene.session_id}  —  {subtitle}",
        scene=dict(
            xaxis=dict(title="X (left/right, m)"),
            yaxis=dict(title="Y (depth, m)"),
            zaxis=dict(title="Z (up, m)"),
            aspectmode="data",
        ),
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(itemsizing="constant"),
    )
    return fig.to_html(include_plotlyjs="cdn", full_html=True)
