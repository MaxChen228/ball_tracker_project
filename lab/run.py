"""Run the lab segmenter on a fixed list of sessions, dump per-session
HTML plotly viz + JSON summary into lab/out/.

Run from project root:
    cd server && uv run python ../lab/run.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import plotly.graph_objects as go

LAB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LAB_DIR.parent
sys.path.insert(0, str(LAB_DIR))

from segmenter import Segment, find_segments, MPS_TO_MPH

RESULTS_DIR = PROJECT_ROOT / "server" / "data" / "results"
OUT_DIR = LAB_DIR / "out"

SESSIONS = [
    "s_f9ddcbb6",
    "s_4effbd74",
    "s_3ec36d69",
    "s_ca9ad955",
    "s_91ddc6ec",
    "s_45170b76",
    "s_962a7db9",
    "s_cc0dcaa5",
    "s_c7e88e51",
    "s_814deb32",
]

PALETTE = [
    "#E45756", "#4C78A8", "#54A24B", "#F58518",
    "#B279A2", "#72B7B2", "#FF9DA6", "#9D755D",
]


@dataclass
class RawPoint:
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


def load_points(session_id: str) -> tuple[list[RawPoint], dict]:
    p = RESULTS_DIR / f"session_{session_id}.json"
    d = json.loads(p.read_text())
    tbp = d.get("triangulated_by_path", {})
    pts_dict = tbp.get("live") or tbp.get("server_post") or []
    pts = [
        RawPoint(
            t_rel_s=float(x["t_rel_s"]),
            x_m=float(x["x_m"]),
            y_m=float(x["y_m"]),
            z_m=float(x["z_m"]),
            residual_m=float(x["residual_m"]),
        )
        for x in pts_dict
    ]
    old_fit = (d.get("ballistic_by_path") or {}).get("live") or \
              (d.get("ballistic_by_path") or {}).get("server_post") or {}
    return pts, old_fit


def render_html(
    session_id: str,
    pts_in: list[RawPoint],
    pts_sorted: np.ndarray,
    kept_mask: np.ndarray,
    segments: list[Segment],
    old_fit: dict,
    out_path: Path,
) -> None:
    raw = np.array(
        [[p.t_rel_s, p.x_m, p.y_m, p.z_m, p.residual_m] for p in pts_in],
        dtype=float,
    )
    rejected = raw[~kept_mask]
    in_seg = np.zeros(pts_sorted.shape[0], dtype=bool)
    for s in segments:
        for k in s.indices:
            in_seg[k] = True
    background = pts_sorted[~in_seg]

    fig = go.Figure()

    if rejected.size:
        fig.add_trace(go.Scatter3d(
            x=rejected[:, 1], y=rejected[:, 2], z=rejected[:, 3],
            mode="markers",
            marker=dict(size=3, color="#444", symbol="x", opacity=0.35),
            name=f"residual≥0.20m ({len(rejected)})",
            hovertemplate="REJ residual=%{text:.3f}m<extra></extra>",
            text=rejected[:, 4],
        ))

    if background.size:
        fig.add_trace(go.Scatter3d(
            x=background[:, 1], y=background[:, 2], z=background[:, 3],
            mode="markers",
            marker=dict(size=3, color="#bbb", opacity=0.55),
            name=f"survived, no segment ({background.shape[0]})",
            hovertemplate="t=%{text:.3f}s<extra></extra>",
            text=background[:, 0] - pts_sorted[0, 0],
        ))

    for i, seg in enumerate(segments):
        color = PALETTE[i % len(PALETTE)]
        sub = pts_sorted[seg.indices]
        fig.add_trace(go.Scatter3d(
            x=sub[:, 1], y=sub[:, 2], z=sub[:, 3],
            mode="markers",
            marker=dict(size=5, color=color),
            name=f"seg{i} pts ({len(seg.indices)}, {seg.speed_mph:.1f} mph, rmse={seg.rmse_m*100:.1f}cm)",
            hovertemplate=f"seg{i} t=%{{text:.3f}}s<extra></extra>",
            text=sub[:, 0] - pts_sorted[0, 0],
        ))
        curve = seg.sample_curve(80)
        fig.add_trace(go.Scatter3d(
            x=curve[:, 1], y=curve[:, 2], z=curve[:, 3],
            mode="lines",
            line=dict(color=color, width=4, dash="dash"),
            name=f"seg{i} fit",
            hoverinfo="skip",
        ))
        # v0 arrow as a short line
        arrow_len = 0.3
        v_unit = seg.v0 / max(np.linalg.norm(seg.v0), 1e-6)
        tip = seg.p0 + v_unit * arrow_len
        fig.add_trace(go.Scatter3d(
            x=[seg.p0[0], tip[0]],
            y=[seg.p0[1], tip[1]],
            z=[seg.p0[2], tip[2]],
            mode="lines",
            line=dict(color=color, width=8),
            name=f"seg{i} v0",
            hoverinfo="skip",
            showlegend=False,
        ))

    title = (
        f"{session_id} | n_in={len(pts_in)} kept={int(kept_mask.sum())} "
        f"segments={len(segments)} | OLD: n={old_fit.get('n_inliers','?')}/"
        f"{old_fit.get('n_total','?')} rmse={old_fit.get('rmse_m', float('nan'))*100:.1f}cm "
        f"speed={old_fit.get('speed_mph', float('nan')):.1f}mph g={old_fit.get('g_mode','?')}"
    )
    fig.update_layout(
        title=title,
        scene=dict(
            xaxis_title="X (m)",
            yaxis_title="Y (m)",
            zaxis_title="Z (m)",
            aspectmode="data",
        ),
        margin=dict(l=0, r=0, t=40, b=0),
        legend=dict(font=dict(size=10), bgcolor="rgba(255,255,255,0.6)"),
    )
    fig.write_html(str(out_path), include_plotlyjs="cdn")


def summarize(session_id: str, pts_in, pts_sorted, kept_mask, segments, old_fit) -> dict:
    return {
        "session_id": session_id,
        "n_input": len(pts_in),
        "n_kept_after_residual_filter": int(kept_mask.sum()),
        "n_rejected_residual": int((~kept_mask).sum()),
        "n_segments": len(segments),
        "segments": [
            {
                "n_points": len(s.indices),
                "speed_mph": s.speed_mph,
                "speed_mps": s.speed_mps,
                "rmse_cm": s.rmse_m * 100,
                "t_start_local_s": s.t_start - pts_sorted[0, 0],
                "t_end_local_s": s.t_end - pts_sorted[0, 0],
                "p0": s.p0.tolist(),
                "v0": s.v0.tolist(),
            }
            for s in segments
        ],
        "old_fit": {
            "n_inliers": old_fit.get("n_inliers"),
            "n_total": old_fit.get("n_total"),
            "rmse_m": old_fit.get("rmse_m"),
            "speed_mph": old_fit.get("speed_mph"),
            "g_mode": old_fit.get("g_mode"),
        } if old_fit else None,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    for sid in SESSIONS:
        pts_in, old_fit = load_points(sid)
        segments, pts_sorted, kept_mask = find_segments(pts_in)
        out_html = OUT_DIR / f"{sid}.html"
        render_html(sid, pts_in, pts_sorted, kept_mask, segments, old_fit, out_html)
        summary = summarize(sid, pts_in, pts_sorted, kept_mask, segments, old_fit)
        summaries.append(summary)
        seg_speeds = ",".join(f"{s.speed_mph:.1f}" for s in segments) or "-"
        print(
            f"{sid}: n={len(pts_in)} kept={int(kept_mask.sum())} "
            f"segs={len(segments)} mph=[{seg_speeds}] | "
            f"old: {old_fit.get('n_inliers','?')}/{old_fit.get('n_total','?')} "
            f"@ {old_fit.get('speed_mph', float('nan')):.1f}mph"
        )
    (OUT_DIR / "summary.json").write_text(json.dumps(summaries, indent=2))
    print(f"\nwrote {len(summaries)} sessions → {OUT_DIR}")


if __name__ == "__main__":
    main()
