"""Plotly 3D figure for the standalone /fit page.

Reuses the viewer's render_scene_theme constants for visual parity but
**does not** call into render_scene._build_figure — fit page is a
standalone page so we redraw the static layers (ground / plate / strike
zone / world axes / cameras) here. Short-term DRY loss is intentional;
keeps render_scene off the segmenter dependency graph.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import plotly.graph_objects as go

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
from segmenter import Segment


# Per-segment palette — distinct from camera colors so segments don't
# camouflage with their seeding rays. Lifted from lab/run.py.
_SEG_PALETTE = [
    "#E45756", "#4C78A8", "#54A24B", "#F58518",
    "#B279A2", "#72B7B2", "#FF9DA6", "#9D755D",
]


def _static_traces(scene: Scene) -> list:
    traces: list = []
    g = _GROUND_HALF_EXTENT_M
    traces.append(go.Mesh3d(
        x=[-g, g, g, -g], y=[-g, -g, g, g], z=[0.0, 0.0, 0.0, 0.0],
        i=[0, 0], j=[1, 2], k=[2, 3],
        color=_BORDER_L, opacity=0.18, name="ground (Z=0)",
        hoverinfo="skip", showlegend=False,
    ))
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
    traces.append(go.Scatter3d(
        x=sz_xs, y=sz_ys, z=sz_zs, mode="lines",
        line=dict(color=_STRIKE_ZONE_COLOR, width=_STRIKE_ZONE_LINE_WIDTH),
        name="strike zone", hoverinfo="skip", showlegend=False,
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
    ))

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

    for cam in scene.cameras:
        color = _CAMERA_COLORS.get(cam.camera_id, _FALLBACK_CAMERA_COLOR)
        cx, cy, cz = cam.center_world
        traces.append(go.Scatter3d(
            x=[cx], y=[cy], z=[cz], mode="markers+text",
            marker=dict(size=8, color=color, symbol="diamond"),
            text=[f"Cam {cam.camera_id}"], textposition="top center",
            showlegend=False,
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
            ))
    return traces


def build_fit_figure(
    scene: Scene,
    pts_in: list[Any],
    pts_sorted: np.ndarray,
    kept_mask: np.ndarray,
    segments: list[Segment],
) -> go.Figure:
    """Return a Plotly Figure mirroring viewer theme + segment overlay.

    `pts_in` is the original-order list (list[TriangulatedPoint]); used
    to render rejected (residual >= cap) points as X marks. `pts_sorted`
    is the segmenter's collapsed+filtered+time-sorted (M, 5) array.
    `kept_mask` is over the original-order list."""
    traces = _static_traces(scene)

    raw = np.array(
        [[p.t_rel_s, p.x_m, p.y_m, p.z_m, p.residual_m] for p in pts_in],
        dtype=float,
    )
    rejected = raw[~kept_mask] if raw.size else raw

    in_seg = np.zeros(pts_sorted.shape[0], dtype=bool)
    for s in segments:
        for k in s.indices:
            in_seg[k] = True
    background = pts_sorted[~in_seg] if pts_sorted.size else pts_sorted

    if rejected.size:
        traces.append(go.Scatter3d(
            x=rejected[:, 1], y=rejected[:, 2], z=rejected[:, 3],
            mode="markers",
            marker=dict(size=3, color="#444", symbol="x", opacity=0.35),
            name=f"residual≥0.20m ({len(rejected)})",
            hovertemplate="REJ residual=%{text:.3f}m<extra></extra>",
            text=rejected[:, 4],
        ))

    if background.size:
        # `pts_sorted` is the collapsed+sorted set; its first row's t is
        # the local zero we anchor the per-trace t labels to.
        t0 = float(pts_sorted[0, 0])
        traces.append(go.Scatter3d(
            x=background[:, 1], y=background[:, 2], z=background[:, 3],
            mode="markers",
            marker=dict(size=3, color="#bbb", opacity=0.55),
            name=f"survived, no segment ({background.shape[0]})",
            hovertemplate="t=%{text:.3f}s<extra></extra>",
            text=background[:, 0] - t0,
        ))

    if pts_sorted.size:
        t0 = float(pts_sorted[0, 0])
        for i, seg in enumerate(segments):
            color = _SEG_PALETTE[i % len(_SEG_PALETTE)]
            sub = pts_sorted[seg.indices]
            traces.append(go.Scatter3d(
                x=sub[:, 1], y=sub[:, 2], z=sub[:, 3],
                mode="markers",
                marker=dict(size=5, color=color),
                name=(
                    f"seg{i} pts ({len(seg.indices)}, "
                    f"{seg.speed_mph:.1f} mph, rmse={seg.rmse_m*100:.1f}cm)"
                ),
                hovertemplate=f"seg{i} t=%{{text:.3f}}s<extra></extra>",
                text=sub[:, 0] - t0,
            ))
            curve = seg.sample_curve(80)
            traces.append(go.Scatter3d(
                x=curve[:, 1], y=curve[:, 2], z=curve[:, 3],
                mode="lines",
                line=dict(color=color, width=4, dash="dash"),
                name=f"seg{i} fit",
                hoverinfo="skip",
            ))
            arrow_len = 0.3
            v_unit = seg.v0 / max(np.linalg.norm(seg.v0), 1e-6)
            tip = seg.p0 + v_unit * arrow_len
            traces.append(go.Scatter3d(
                x=[seg.p0[0], tip[0]],
                y=[seg.p0[1], tip[1]],
                z=[seg.p0[2], tip[2]],
                mode="lines",
                line=dict(color=color, width=8),
                hoverinfo="skip",
                showlegend=False,
            ))

    axis_font = dict(family="JetBrains Mono, monospace", size=11, color=_INK)
    axis_style = dict(
        backgroundcolor=_BG,
        gridcolor=_BORDER_L,
        zerolinecolor=_BORDER_BASE,
        linecolor=_BORDER_BASE,
        tickfont=dict(family="JetBrains Mono, monospace", size=10, color=_SUB),
    )

    def axis(title: str) -> dict:
        return dict(title=dict(text=title, font=axis_font), **axis_style)

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
            camera=dict(
                eye=dict(x=1.5, y=1.5, z=1.0),
                up=dict(x=0, y=0, z=1),
                center=dict(x=0, y=0.2, z=0.3),
            ),
        ),
        margin=dict(l=0, r=0, t=8, b=0),
        legend=dict(
            itemsizing="constant",
            bgcolor=_SURFACE,
            bordercolor=_BORDER_BASE,
            borderwidth=1,
            font=dict(family="JetBrains Mono, monospace", size=10, color=_INK),
            orientation="h",
            x=0.0, xanchor="left",
            y=-0.02, yanchor="top",
        ),
        font=dict(family="Noto Sans TC, sans-serif", color=_INK),
    )
    return fig


def render_fit_html(
    *,
    session_id: str,
    path: str,
    available_paths: list[str],
    n_input: int,
    n_kept: int,
    segments: list[Segment],
    fig_html: str,
) -> str:
    """Wrap the Plotly figure in viewer-themed chrome (header + segment
    table). `fig_html` is `fig.to_html(include_plotlyjs="cdn", full_html=False)`
    output so the page embeds the full Plotly bundle once."""

    rows = []
    for i, s in enumerate(segments):
        rows.append(
            f"<tr><td>{i}</td><td>{len(s.indices)}</td>"
            f"<td>{s.speed_mph:.1f}</td>"
            f"<td>{s.rmse_m*100:.1f}</td>"
            f"<td>{s.t_start:.3f}</td>"
            f"<td>{s.t_end - s.t_start:.3f}</td></tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">no segments</td></tr>')

    path_pills = "".join(
        f'<a class="path-pill{" active" if p == path else ""}"'
        f' href="/fit/{session_id}?path={p}">{p}</a>'
        for p in available_paths
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Fit · {session_id}</title>
<style>
:root {{
  --bg: {_BG}; --surface: {_SURFACE}; --ink: {_INK}; --sub: {_SUB};
  --border-base: {_BORDER_BASE}; --border-l: {_BORDER_L}; --accent: {_ACCENT};
  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  --sans: "Noto Sans TC", -apple-system, BlinkMacSystemFont, sans-serif;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; height: 100%;
  background: var(--bg); color: var(--ink); font-family: var(--sans);
  font-weight: 300; }}
.fit-page {{ display: grid; grid-template-rows: 52px auto 1fr auto; height: 100vh; }}
.nav {{ height: 52px; background: var(--surface);
  border-bottom: 1px solid var(--border-base);
  display: flex; align-items: center; gap: 16px; padding: 0 24px; }}
.brand {{ font-family: var(--mono); font-weight: 700; font-size: 14px;
  letter-spacing: 0.16em; }}
.brand .dot {{ display: inline-block; width: 7px; height: 7px;
  background: var(--ink); margin-right: 8px; vertical-align: middle; }}
.sid {{ font-family: var(--mono); font-size: 12px; color: var(--sub);
  letter-spacing: 0.04em; }}
.nav a.back {{ font-family: var(--mono); font-size: 11px;
  letter-spacing: 0.12em; text-transform: uppercase; color: var(--sub);
  text-decoration: none; margin-left: auto; }}
.nav a.back:hover {{ color: var(--ink); }}
.toolbar {{ padding: 8px 24px; display: flex; align-items: center; gap: 16px;
  border-bottom: 1px solid var(--border-base); background: var(--surface);
  font-family: var(--mono); font-size: 11px; color: var(--sub);
  letter-spacing: 0.06em; }}
.path-toggle {{ display: inline-flex; gap: 4px; }}
.path-pill {{ font-family: var(--mono); font-size: 10px;
  letter-spacing: 0.10em; text-transform: uppercase; color: var(--sub);
  background: var(--bg); border: 1px solid var(--border-base);
  padding: 4px 10px; border-radius: 3px; text-decoration: none; }}
.path-pill.active {{ color: var(--ink); border-color: var(--accent);
  background: var(--surface); }}
.stats {{ display: inline-flex; gap: 16px; }}
.stats span b {{ font-family: var(--mono); color: var(--ink);
  font-weight: 600; font-variant-numeric: tabular-nums; }}
.fit-page #fit-canvas {{ height: 100%; min-height: 0; overflow: hidden; }}
.seg-table {{ padding: 8px 24px; border-top: 1px solid var(--border-base);
  background: var(--surface); }}
.seg-table table {{ width: 100%; border-collapse: collapse;
  font-family: var(--mono); font-size: 11px; }}
.seg-table th {{ text-align: left; padding: 4px 8px; color: var(--sub);
  letter-spacing: 0.08em; text-transform: uppercase;
  border-bottom: 1px solid var(--border-base); font-weight: 500; }}
.seg-table td {{ padding: 4px 8px; font-variant-numeric: tabular-nums; }}
.seg-table td.empty {{ text-align: center; color: var(--sub); padding: 12px; }}
</style>
</head><body>
<div class="fit-page">
  <div class="nav">
    <span class="brand"><span class="dot"></span>BALL_TRACKER · FIT</span>
    <span class="sid">{session_id}</span>
    <a class="back" href="/viewer/{session_id}">&larr; viewer</a>
  </div>
  <div class="toolbar">
    <span class="path-toggle">{path_pills}</span>
    <span class="stats">
      <span>n_input <b>{n_input}</b></span>
      <span>kept <b>{n_kept}</b></span>
      <span>segments <b>{len(segments)}</b></span>
    </span>
  </div>
  <div id="fit-canvas">{fig_html}</div>
  <div class="seg-table">
    <table>
      <thead><tr><th>#</th><th>n_pts</th><th>mph</th><th>rmse (cm)</th>
        <th>t_start (s)</th><th>duration (s)</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</div>
</body></html>"""
