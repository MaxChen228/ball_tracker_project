"""Plotly 3D scene renderer for the dashboard canvas and session viewer."""
from __future__ import annotations

from reconstruct import Scene
from render_scene_layout import default_layout_kwargs, default_scene_block
from render_scene_static import static_traces
from render_scene_theme import (
    _ACCENT,
    _CAMERA_COLORS,
    _FALLBACK_CAMERA_COLOR,
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
    presets,
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
        presets=presets,
    )


def _build_figure(scene: Scene):
    import plotly.graph_objects as go

    traces: list = static_traces(scene)

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

    fig = go.Figure(data=traces)
    fig.update_layout(**default_layout_kwargs(
        scene=default_scene_block(uirevision="viewer-scene"),
    ))
    return fig
