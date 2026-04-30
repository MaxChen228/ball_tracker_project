"""Shared Plotly layout helpers for 3D scene pages.

Pre-extraction `render_scene._build_figure` and `render_fit.build_fit_figure`
each carried their own copy of the axis style + layout block. Drift was
visible: dashboard's default ISO camera (eye=(1.6, 1.816, 1.56),
center=(0, 0.216, 0.76)) didn't match fit's (eye=(1.5, 1.5, 1.0),
center=(0, 0.2, 0.3)) so the same strike zone landed in different screen
positions on the two pages.

Callers can still override `camera` / `aspectmode` per-page; the helper
just sets sane defaults that match the dashboard's strike-zone-centred
ISO chip.
"""
from __future__ import annotations

from render_scene_theme import (
    _BG,
    _BORDER_BASE,
    _BORDER_L,
    _INK,
    _SUB,
    _SURFACE,
)


def _axis_dict(title_text: str) -> dict:
    return dict(
        title=dict(
            text=title_text,
            font=dict(family="JetBrains Mono, monospace", size=11, color=_INK),
        ),
        backgroundcolor=_BG,
        gridcolor=_BORDER_L,
        zerolinecolor=_BORDER_BASE,
        linecolor=_BORDER_BASE,
        tickfont=dict(family="JetBrains Mono, monospace", size=10, color=_SUB),
    )


# ISO preset baked into figures — centre = strike-zone centroid (X=0,
# Y=0.216, Z=0.76) so the box sits at the frame middle. Keep in sync
# with VIEW_PRESETS.iso in 75_view_presets.js.
_DEFAULT_CAMERA = dict(
    eye=dict(x=1.6, y=1.816, z=1.56),
    up=dict(x=0, y=0, z=1),
    center=dict(x=0, y=0.216, z=0.76),
)


def default_scene_block(
    *,
    camera: dict | None = None,
    aspectmode: str = "data",
    aspectratio: dict | None = None,
    xaxis_range: list[float] | None = None,
    yaxis_range: list[float] | None = None,
    zaxis_range: list[float] | None = None,
    uirevision: str | None = None,
) -> dict:
    """Build the `scene=` block for `fig.update_layout(scene=...)`.

    Defaults match the viewer's framing. Dashboard uses manual aspect
    + axis ranges to keep the calibration preview stable when no rays
    are present; pass those overrides explicitly."""
    block: dict = dict(
        xaxis=_axis_dict("X (left/right, m)"),
        yaxis=_axis_dict("Y (depth, m)"),
        zaxis=_axis_dict("Z (up, m)"),
        bgcolor=_BG,
        aspectmode=aspectmode,
        camera=camera if camera is not None else _DEFAULT_CAMERA,
    )
    if aspectratio is not None:
        block["aspectratio"] = aspectratio
    if xaxis_range is not None:
        block["xaxis"] = dict(block["xaxis"], range=xaxis_range)
    if yaxis_range is not None:
        block["yaxis"] = dict(block["yaxis"], range=yaxis_range)
    if zaxis_range is not None:
        block["zaxis"] = dict(block["zaxis"], range=zaxis_range)
    if uirevision is not None:
        block["uirevision"] = uirevision
    return block


def default_legend() -> dict:
    return dict(
        itemsizing="constant",
        bgcolor=_SURFACE,
        bordercolor=_BORDER_BASE,
        borderwidth=1,
        font=dict(family="JetBrains Mono, monospace", size=10, color=_INK),
        orientation="h",
        x=0.0, xanchor="left",
        y=-0.02, yanchor="top",
    )


def default_layout_kwargs(*, scene: dict | None = None) -> dict:
    """Top-level `update_layout(**kw)` shared by all 3D pages."""
    kw: dict = dict(
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        margin=dict(l=0, r=0, t=8, b=0),
        legend=default_legend(),
        font=dict(family="Noto Sans TC, sans-serif", color=_INK),
    )
    if scene is not None:
        kw["scene"] = scene
    return kw
