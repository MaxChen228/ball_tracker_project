"""Plotly-based 3D viewer for reconstructed scenes, plus a matching
index page that lists every cycle as a clickable event row.

Both renderers return self-contained HTML strings — the `/viewer/{cycle}`
and `/` endpoints return these directly. The 3D page loads Plotly.js from
CDN so the file stays tiny and opens in any modern browser without a
build step. Future replacements (Three.js, deck.gl, etc.) only need to
re-implement these two functions; `reconstruct.Scene` and the events
dict shape are the stable hand-offs.
"""
from __future__ import annotations

import datetime as _dt
import html
from typing import Any

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
        title=f"Cycle {scene.cycle_number}  —  {subtitle}",
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


# ---------------------------------------------------------------------------
# Events index — server-rendered HTML table, no Plotly, no JS framework.
# ---------------------------------------------------------------------------


_INDEX_CSS = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 24px; color: #222; background: #fafafa; }
    h1 { font-size: 22px; margin: 0 0 4px 0; }
    .subtitle { color: #666; margin-bottom: 20px; font-size: 14px; }
    table { border-collapse: collapse; width: 100%; max-width: 980px;
            background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.05);
            border-radius: 6px; overflow: hidden; }
    th, td { padding: 10px 14px; text-align: left; font-size: 14px;
             border-bottom: 1px solid #eee; }
    th { background: #f2f4f7; font-weight: 600; color: #333; }
    tr:hover td { background: #f7faff; }
    td.num { font-variant-numeric: tabular-nums; text-align: right; }
    a { color: #0b6bcb; text-decoration: none; font-weight: 500; }
    a:hover { text-decoration: underline; }
    .status { display: inline-block; padding: 2px 8px; border-radius: 10px;
              font-size: 12px; font-weight: 600; }
    .status.paired { background: #e0f7ea; color: #1e7d45; }
    .status.paired_no_points { background: #fff3cd; color: #8a6d00; }
    .status.partial { background: #eef1f5; color: #555; }
    .status.error { background: #fdecec; color: #b3261e; }
    .empty { color: #888; font-style: italic; padding: 24px; text-align: center; }
"""


def _fmt_received_at(ts: float | None) -> str:
    if ts is None:
        return "—"
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_opt_float(v: float | None, fmt: str) -> str:
    return "—" if v is None else format(v, fmt)


def render_events_index_html(events: list[dict[str, Any]]) -> str:
    """Render the /events list as an HTML table with links into each
    cycle's /viewer/{cycle} 3D page.

    The input is the exact shape returned by `State.events()` — keeping
    this function UI-only means the JSON endpoint and the HTML page
    describe the same data."""
    if not events:
        body = '<div class="empty">No cycles received yet. Upload a pitch from the iPhone app.</div>'
    else:
        rows: list[str] = []
        for e in events:
            cams = ", ".join(html.escape(c) for c in e["cameras"]) or "—"
            counts = e.get("n_ball_frames", {}) or {}
            counts_str = ", ".join(
                f"{html.escape(c)}:{n}" for c, n in sorted(counts.items())
            ) or "—"
            status = html.escape(e["status"])
            err = e.get("error") or ""
            err_html = f' <span title="{html.escape(err)}">⚠</span>' if err else ""
            rows.append(
                "<tr>"
                f'<td><a href="/viewer/{e["cycle_number"]}">#{e["cycle_number"]}</a></td>'
                f'<td>{cams}</td>'
                f'<td><span class="status {status}">{status}</span>{err_html}</td>'
                f'<td>{_fmt_received_at(e["received_at"])}</td>'
                f'<td>{counts_str}</td>'
                f'<td class="num">{e["n_triangulated"]}</td>'
                f'<td class="num">{_fmt_opt_float(e["mean_residual_m"], ".4f")}</td>'
                f'<td class="num">{_fmt_opt_float(e["peak_z_m"], ".2f")}</td>'
                f'<td class="num">{_fmt_opt_float(e["duration_s"], ".2f")}</td>'
                "</tr>"
            )
        body = (
            "<table>"
            "<thead><tr>"
            "<th>Cycle</th><th>Cams</th><th>Status</th><th>Received</th>"
            "<th>Ball frames</th><th>3D pts</th><th>Mean resid (m)</th>"
            "<th>Peak Z (m)</th><th>Duration (s)</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody>"
            "</table>"
        )

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>ball_tracker events</title>"
        f"<style>{_INDEX_CSS}</style>"
        "</head><body>"
        "<h1>ball_tracker events</h1>"
        f'<div class="subtitle">{len(events)} cycle(s) · click a row to open the 3D viewer</div>'
        f"{body}"
        "</body></html>"
    )
