"""Offline dry-run: compare winner-only vs all-candidates triangulation.

Hypothesis: removing the 2D selector (sending every shape-gate-passed
candidate as a ray) and pushing disambiguation to 3D (epipolar gap +
segmenter physical priors) recovers the ball in cases where the
current selector locks onto a static distractor.

Doesn't touch production code. Reads existing
`data/pitches/session_*_*.json` candidates lists (already shape-gated)
and re-triangulates with the multi-ray policy.
"""
from __future__ import annotations

import bisect
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from triangulate import (
    build_K,
    camera_center_world,
    recover_extrinsics,
    triangulate_rays,
    undistorted_ray_cam,
)
from segmenter import find_segments
from render_fit import build_fit_figure, render_fit_html

DATA = Path(__file__).parent / "data"
DT_MAX = 0.006   # ±6 ms → covers one 240 fps frame jitter
GAP_MAX = 0.30   # 30 cm closest-approach: physical ball + cal noise budget


def load_cam(sid: str, cam: str) -> dict:
    return json.loads((DATA / "pitches" / f"session_{sid}_{cam}.json").read_text())


def setup(d: dict):
    intr = d["intrinsics"]
    K = build_K(intr["fx"], intr["fy"], intr["cx"], intr["cy"])
    dist = np.asarray(intr.get("distortion") or [0, 0, 0, 0, 0], dtype=np.float64)
    H = np.asarray(d["homography"], dtype=np.float64).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, dist, R, C


def make_rays(frames, K, dist, R, anchor, *, all_candidates: bool):
    """List of (t_rel_s, d_world, src_tag). src_tag distinguishes candidates."""
    out = []
    for f in frames:
        t = f["timestamp_s"] - anchor
        if all_candidates:
            cands = f.get("candidates") or []
            for i, c in enumerate(cands):
                d_cam = undistorted_ray_cam(c["px"], c["py"], K, dist)
                d_world = R.T @ d_cam
                out.append((t, d_world, f"f{f['frame_index']}#c{i}"))
        else:
            if f.get("ball_detected") and f.get("px") is not None:
                d_cam = undistorted_ray_cam(f["px"], f["py"], K, dist)
                d_world = R.T @ d_cam
                out.append((t, d_world, f"f{f['frame_index']}"))
    return out


def pair_and_triangulate(rays_a, C_a, rays_b, C_b, *, dt_max=DT_MAX, gap_max=GAP_MAX):
    """For every A ray, find every B ray within ±dt_max s, triangulate, keep
    pairs with closest-approach gap ≤ gap_max. Returns list of dicts."""
    rays_b = sorted(rays_b, key=lambda r: r[0])
    b_times = [r[0] for r in rays_b]
    pts = []
    for t_a, d_a, tag_a in rays_a:
        lo = bisect.bisect_left(b_times, t_a - dt_max)
        hi = bisect.bisect_right(b_times, t_a + dt_max)
        for j in range(lo, hi):
            t_b, d_b, tag_b = rays_b[j]
            P, gap = triangulate_rays(C_a, d_a, C_b, d_b)
            if P is None or gap > gap_max:
                continue
            pts.append({
                "t_rel_s": 0.5 * (t_a + t_b),
                "x_m": float(P[0]), "y_m": float(P[1]), "z_m": float(P[2]),
                "residual_m": float(gap),
                "tag": f"{tag_a}|{tag_b}",
            })
    return pts


@dataclass
class _Pt:
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


def run_segmenter(raw_pts):
    pts = [_Pt(p["t_rel_s"], p["x_m"], p["y_m"], p["z_m"], p["residual_m"]) for p in raw_pts]
    if not pts:
        return [], pts
    segs, _sorted = find_segments(pts)
    return segs, pts


def report(label, raw_pts):
    segs, _pts = run_segmenter(raw_pts)
    print(f"  {label:18s}: pts={len(raw_pts):4d}  segments={len(segs)}")
    for i, s in enumerate(segs):
        print(f"    seg{i}: n={len(s.indices):3d} kph={s.speed_kph:5.1f} "
              f"rmse={s.rmse_m * 100:5.1f}cm t=[{s.t_start:.3f},{s.t_end:.3f}]")


def render_html(sid: str, path: str, label: str, raw_pts, scene, out_path: Path):
    pts = [_Pt(p["t_rel_s"], p["x_m"], p["y_m"], p["z_m"], p["residual_m"]) for p in raw_pts]
    if not pts:
        out_path.write_text(f"<h1>{sid} {path} {label}: no points</h1>")
        return
    segs, pts_sorted = find_segments(pts)
    fig = build_fit_figure(scene, pts, pts_sorted, segs)
    fig_html = fig.to_html(include_plotlyjs="cdn", full_html=False)
    html = render_fit_html(
        session_id=f"{sid} [{label}]",
        path=path,
        available_paths=[path],
        n_input=len(pts),
        segments=segs,
        fig_html=fig_html,
    )
    out_path.write_text(html)
    print(f"    wrote {out_path}")


def _scene_for(sid: str):
    """Build a Scene without going through main.state — load pitches +
    SessionResult directly from disk."""
    from reconstruct import build_scene
    from schemas import FramePayload, PitchPayload, SessionResult

    pitches = {}
    for cam in "AB":
        raw = (DATA / "pitches" / f"session_{sid}_{cam}.json").read_text()
        pitches[cam] = PitchPayload.model_validate_json(raw)
    result_raw = (DATA / "results" / f"session_{sid}.json").read_text()
    result = SessionResult.model_validate_json(result_raw)
    triangulated = result.points
    tbp = result.triangulated_by_path
    return build_scene(sid, pitches, triangulated, tbp, session_result=result)


def run(sid: str, path: str):
    print(f"\n=== {sid} path={path} ===")
    da, db = load_cam(sid, "A"), load_cam(sid, "B")
    Ka, dista, Ra, Ca = setup(da)
    Kb, distb, Rb, Cb = setup(db)

    # sync chirp aligns both cams to the same anchor in their respective
    # device clock spaces — use cam-local anchor for each frame list.
    anchor_a = da["sync_anchor_timestamp_s"]
    anchor_b = db["sync_anchor_timestamp_s"]

    fA = da[f"frames_{path}"]
    fB = db[f"frames_{path}"]
    n_det_a = sum(1 for f in fA if f.get("ball_detected"))
    n_det_b = sum(1 for f in fB if f.get("ball_detected"))
    n_cand_a = sum(len(f.get("candidates") or []) for f in fA)
    n_cand_b = sum(len(f.get("candidates") or []) for f in fB)
    print(f"  cam A: {len(fA)} frames, {n_det_a} winner-detected, {n_cand_a} total candidates")
    print(f"  cam B: {len(fB)} frames, {n_det_b} winner-detected, {n_cand_b} total candidates")

    rays_a_w = make_rays(fA, Ka, dista, Ra, anchor_a, all_candidates=False)
    rays_b_w = make_rays(fB, Kb, distb, Rb, anchor_b, all_candidates=False)
    pts_base = pair_and_triangulate(rays_a_w, Ca, rays_b_w, Cb)
    report("winner-only", pts_base)

    rays_a_all = make_rays(fA, Ka, dista, Ra, anchor_a, all_candidates=True)
    rays_b_all = make_rays(fB, Kb, distb, Rb, anchor_b, all_candidates=True)
    pts_multi = pair_and_triangulate(rays_a_all, Ca, rays_b_all, Cb)
    report("multi-ray", pts_multi)

    if path == "server_post":
        out_dir = Path("/tmp/dry_run_fit")
        out_dir.mkdir(parents=True, exist_ok=True)
        scene = _scene_for(sid)
        render_html(sid, path, "winner-only", pts_base, scene,
                    out_dir / f"{sid}_{path}_winner.html")
        render_html(sid, path, "multi-ray", pts_multi, scene,
                    out_dir / f"{sid}_{path}_multi.html")


if __name__ == "__main__":
    cases = [
        ("s_f50fd07f", "live"),
        ("s_f50fd07f", "server_post"),
        ("s_962a7db9", "live"),
        ("s_962a7db9", "server_post"),
    ]
    for sid, path in cases:
        try:
            run(sid, path)
        except Exception as exc:
            print(f"\n=== {sid} path={path} ===\n  ERROR: {exc!r}")
