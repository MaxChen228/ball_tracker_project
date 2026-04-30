"""Static scene traces — ground / plate / strike zone / world axes / cameras.

Single source of truth shared by `render_scene._build_figure` (dashboard +
viewer). Pre-extraction these ~150 lines were duplicated across the
viewer build path and the now-retired fit page; visual drift was a real
risk (zone colour change in one place, not the other).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from render_scene_theme import (
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
    _SURFACE,
    _WORLD_AXIS_LEN_M,
)

if TYPE_CHECKING:
    from reconstruct import Scene


def static_traces(scene: "Scene", *, tag_strike_zone: bool = True) -> list:
    """Return the universal static layer for any 3D scene render.

    `tag_strike_zone` controls whether strike zone traces carry the
    `meta.feature='strike_zone'` tag. Dashboard + viewer set this so the
    strike-zone visibility toggle's JS layer can find them; fit page
    doesn't have a toggle so the tag is harmless there but kept on for
    consistency."""
    import plotly.graph_objects as go

    traces: list = []

    # Ground plane.
    g = _GROUND_HALF_EXTENT_M
    traces.append(go.Mesh3d(
        x=[-g, g, g, -g], y=[-g, -g, g, g], z=[0.0, 0.0, 0.0, 0.0],
        i=[0, 0], j=[1, 2], k=[2, 3],
        color=_BORDER_L, opacity=0.18, name="ground (Z=0)",
        hoverinfo="skip", showlegend=False,
    ))

    # Home plate (filled pentagon + outline).
    traces.append(go.Mesh3d(
        x=_PLATE_X, y=_PLATE_Y, z=[0.0] * 5,
        i=[0, 0, 0], j=[1, 2, 3], k=[2, 3, 4],
        color=_SURFACE, opacity=0.95, flatshading=True,
        name="home plate", hoverinfo="skip", showlegend=False,
    ))
    traces.append(go.Scatter3d(
        x=_PLATE_X + [_PLATE_X[0]], y=_PLATE_Y + [_PLATE_Y[0]],
        z=[0.0] * 6, mode="lines",
        line=dict(color=_INK, width=3),
        hoverinfo="skip", showlegend=False,
    ))

    # Strike zone — wireframe + translucent fill. 8 corners indexed
    # bottom CCW (0-3) then top CCW (4-7).
    sx = _STRIKE_ZONE_X_HALF_M
    syf = _STRIKE_ZONE_Y_FRONT_M
    syb = _STRIKE_ZONE_Y_BACK_M
    szb = _STRIKE_ZONE_Z_BOTTOM_M
    szt = _STRIKE_ZONE_Z_TOP_M
    sz_corners = [
        (-sx, syf, szb), (+sx, syf, szb), (+sx, syb, szb), (-sx, syb, szb),
        (-sx, syf, szt), (+sx, syf, szt), (+sx, syb, szt), (-sx, syb, szt),
    ]
    sz_edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    sz_xs: list[float | None] = []
    sz_ys: list[float | None] = []
    sz_zs: list[float | None] = []
    for i, j in sz_edges:
        sz_xs += [sz_corners[i][0], sz_corners[j][0], None]
        sz_ys += [sz_corners[i][1], sz_corners[j][1], None]
        sz_zs += [sz_corners[i][2], sz_corners[j][2], None]
    sz_meta = dict(feature="strike_zone") if tag_strike_zone else None
    traces.append(go.Scatter3d(
        x=sz_xs, y=sz_ys, z=sz_zs, mode="lines",
        line=dict(color=_STRIKE_ZONE_COLOR, width=_STRIKE_ZONE_LINE_WIDTH),
        name="strike zone", hoverinfo="skip", showlegend=False,
        meta=sz_meta,
    ))
    traces.append(go.Mesh3d(
        x=[c[0] for c in sz_corners],
        y=[c[1] for c in sz_corners],
        z=[c[2] for c in sz_corners],
        i=[0, 0, 4, 4, 0, 0, 1, 1, 0, 0, 3, 3],
        j=[1, 2, 5, 6, 1, 5, 2, 6, 4, 3, 4, 7],
        k=[2, 3, 6, 7, 5, 4, 6, 5, 3, 7, 7, 4],
        color=_STRIKE_ZONE_COLOR,
        opacity=_STRIKE_ZONE_FILL_OPACITY, flatshading=True,
        hoverinfo="skip", showlegend=False, name="strike zone fill",
        meta=sz_meta,
    ))

    # World X/Y/Z axes.
    for direction, color, label in (
        ((1.0, 0.0, 0.0), _DEV, "X"),
        ((0.0, 1.0, 0.0), _CONTRA, "Y"),
        ((0.0, 0.0, 1.0), _INK_40, "Z"),
    ):
        dx, dy, dz = direction
        traces.append(go.Scatter3d(
            x=[0.0, _WORLD_AXIS_LEN_M * dx],
            y=[0.0, _WORLD_AXIS_LEN_M * dy],
            z=[0.0, _WORLD_AXIS_LEN_M * dz],
            mode="lines+text",
            text=["", label], textposition="top center",
            textfont=dict(family="JetBrains Mono, monospace", size=11, color=_INK),
            line=dict(color=color, width=4),
            hoverinfo="skip", showlegend=False,
        ))

    # Per-camera marker + 3-axis triad.
    for cam in scene.cameras:
        color = _CAMERA_COLORS.get(cam.camera_id, _FALLBACK_CAMERA_COLOR)
        cx, cy, cz = cam.center_world
        traces.append(go.Scatter3d(
            x=[cx], y=[cy], z=[cz], mode="markers+text",
            marker=dict(size=8, color=color, symbol="diamond"),
            text=[f"Cam {cam.camera_id}"], textposition="top center",
            showlegend=False,
            meta=dict(trace_kind="camera", camera_id=cam.camera_id),
            hovertemplate=(
                f"Camera {cam.camera_id}"
                "<br>x=%{x:.2f} m<br>y=%{y:.2f} m<br>z=%{z:.2f} m<extra></extra>"
            ),
        ))
        for axis, axis_color, length in (
            (cam.axis_forward_world, color, _CAMERA_FORWARD_ARROW_M),
            (cam.axis_right_world, _DEV, _CAMERA_AXIS_LEN_M),
            (cam.axis_up_world, _INK_40, _CAMERA_AXIS_LEN_M),
        ):
            traces.append(go.Scatter3d(
                x=[cx, cx + length * axis[0]],
                y=[cy, cy + length * axis[1]],
                z=[cz, cz + length * axis[2]],
                mode="lines",
                line=dict(color=axis_color, width=4),
                hoverinfo="skip", showlegend=False,
                meta=dict(trace_kind="camera_axis", camera_id=cam.camera_id),
            ))

    return traces
