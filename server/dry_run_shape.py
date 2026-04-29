"""Offline dry-run: shape-weighted single-winner selector.

Re-decodes both cams' MOV, extracts every shape-gate-passed candidate
with full (area, aspect, fill) signature, then picks one winner per
frame using a configurable shape-prior cost (no temporal dependence
by default — sidesteps prev_position pollution).

Compares against:
- live winners (ground truth, recorded by iOS)
- legacy area+temporal cost (current production selector)

Per session prints summary stats; for failure-case sessions also dumps
HTML for visual review.
"""
from __future__ import annotations

import bisect
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from triangulate import (
    build_K, camera_center_world, recover_extrinsics,
    triangulate_rays, undistorted_ray_cam,
)
from segmenter import find_segments
from video import iter_frames

DATA = Path(__file__).parent / "data"
HSV_LO = (105, 140, 40)
HSV_HI = (112, 255, 255)
ASPECT_MIN = 0.56
FILL_MIN = 0.45
MIN_AREA = 15
MAX_AREA = 7000

# Domain priors (memory + project facts)
R_PX_EXPECTED = 12.0
EXPECTED_AREA = math.pi * R_PX_EXPECTED ** 2  # ≈ 452
FILL_TYPICAL = 0.68  # memory: empirical median
DT_MAX = 0.006
GAP_MAX = 0.30


@dataclass
class Cand:
    px: float
    py: float
    area: int
    aspect: float
    fill: float


@dataclass
class FrameCands:
    t_rel: float
    cands: list[Cand]


def extract_candidates(video_path: Path, anchor: float) -> list[FrameCands]:
    """Decode MOV, run HSV+CC+shape gate, return per-frame candidate
    list with full shape stats. anchor = sync_anchor_timestamp_s."""
    out: list[FrameCands] = []
    for absolute_pts_s, bgr in iter_frames(video_path, 0.0):
        # iter_frames adds video_start_pts_s — but I passed 0 to keep raw container time.
        # Need actual: t_rel = container_t + video_start - anchor. video_start tracked outside.
        out.append((absolute_pts_s, bgr))
    return out  # raw deferred — wrong, fix below


def extract_per_cam(video_path: Path, video_start_pts_s: float, anchor: float) -> list[FrameCands]:
    out: list[FrameCands] = []
    hsv_lo = np.asarray(HSV_LO, dtype=np.uint8)
    hsv_hi = np.asarray(HSV_HI, dtype=np.uint8)
    for absolute_pts_s, bgr in iter_frames(video_path, video_start_pts_s):
        t_rel = absolute_pts_s - anchor
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, hsv_lo, hsv_hi)
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        cands: list[Cand] = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < MIN_AREA or area > MAX_AREA:
                continue
            w = int(stats[i, cv2.CC_STAT_WIDTH])
            h = int(stats[i, cv2.CC_STAT_HEIGHT])
            if w <= 0 or h <= 0:
                continue
            aspect = min(w, h) / max(w, h)
            if aspect < ASPECT_MIN:
                continue
            fill = area / (w * h)
            if fill < FILL_MIN:
                continue
            cands.append(Cand(
                px=float(cents[i, 0]), py=float(cents[i, 1]),
                area=area, aspect=aspect, fill=fill,
            ))
        out.append(FrameCands(t_rel=t_rel, cands=cands))
    return out


# ------- cost variants ----------
def cost_legacy(c: Cand, area_score: float, dist_cost: float | None,
                w_area=0.3, w_dist=0.7) -> float:
    """Match production: area_score + dist_cost (or 1-area_score if no temporal)."""
    if dist_cost is None:
        return 1.0 - area_score
    return w_area * (1.0 - area_score) + w_dist * dist_cost


def cost_shape(c: Cand) -> float:
    """Pure track-independent shape prior. No area_score, no temporal.
    Each component normalized to roughly [0, 1] and summed with weights."""
    # log-space area distance: 1 octave off → 1.0
    size_pen = abs(math.log2(c.area / EXPECTED_AREA))
    size_pen = min(size_pen / 2.0, 1.0)  # 4× off saturates

    aspect_pen = (1.0 - c.aspect) / (1.0 - ASPECT_MIN)  # at gate=0.56 → 1.0
    aspect_pen = min(max(aspect_pen, 0.0), 1.0)

    fill_pen = abs(c.fill - FILL_TYPICAL) / FILL_TYPICAL
    fill_pen = min(fill_pen, 1.0)

    return 0.5 * size_pen + 0.3 * aspect_pen + 0.2 * fill_pen


def cost_hybrid(c: Cand, area_score: float, dist_cost: float | None) -> float:
    """Shape-dominant + small temporal hint. Shape weight 0.85 / temporal 0.15."""
    shape = cost_shape(c)
    if dist_cost is None:
        return shape
    return 0.85 * shape + 0.15 * dist_cost


# ------- selector loop with temporal bookkeeping ----------
def select_winners(frames: list[FrameCands], mode: str) -> list[tuple[float, Cand | None]]:
    """Apply selector mode across the frame list. Returns list of
    (t_rel, winner) — winner=None when no candidate."""
    out: list[tuple[float, Cand | None]] = []
    prev_pos: tuple[float, float] | None = None
    prev_vel: tuple[float, float] | None = None
    prev_t: float | None = None
    for f in frames:
        if not f.cands:
            out.append((f.t_rel, None))
            prev_pos = prev_vel = None
            prev_t = None
            continue
        max_area = max(c.area for c in f.cands)
        scores = [c.area / max_area for c in f.cands]
        # dist_cost vector (or None)
        dt = (f.t_rel - prev_t) if prev_t is not None else None
        dists: list[float | None] = []
        if (prev_pos is not None and prev_vel is not None and dt is not None and dt > 0):
            px_pred = prev_pos[0] + prev_vel[0] * dt
            py_pred = prev_pos[1] + prev_vel[1] * dt
            for c in f.cands:
                d = math.hypot(c.px - px_pred, c.py - py_pred)
                dists.append(min(d / R_PX_EXPECTED / 8.0, 1.0))
        else:
            dists = [None] * len(f.cands)

        if mode == "legacy":
            costs = [cost_legacy(c, s, d) for c, s, d in zip(f.cands, scores, dists)]
        elif mode == "shape":
            costs = [cost_shape(c) for c in f.cands]
        elif mode == "hybrid":
            costs = [cost_hybrid(c, s, d) for c, s, d in zip(f.cands, scores, dists)]
        else:
            raise ValueError(mode)

        i_win = min(range(len(costs)), key=lambda i: costs[i])
        winner = f.cands[i_win]
        out.append((f.t_rel, winner))
        if prev_pos is not None and prev_t is not None:
            ddt = f.t_rel - prev_t
            if ddt > 0:
                prev_vel = (
                    (winner.px - prev_pos[0]) / ddt,
                    (winner.py - prev_pos[1]) / ddt,
                )
        prev_pos = (winner.px, winner.py)
        prev_t = f.t_rel
    return out


# ------- triangulation ------------
def setup_cam(d: dict):
    intr = d["intrinsics"]
    K = build_K(intr["fx"], intr["fy"], intr["cx"], intr["cy"])
    dist = np.asarray(intr.get("distortion") or [0, 0, 0, 0, 0], dtype=np.float64)
    H = np.asarray(d["homography"], dtype=np.float64).reshape(3, 3)
    R, t = recover_extrinsics(K, H)
    C = camera_center_world(R, t)
    return K, dist, R, C


def winners_to_rays(winners, K, dist, R):
    rays = []
    for t, w in winners:
        if w is None:
            continue
        d_cam = undistorted_ray_cam(w.px, w.py, K, dist)
        d_world = R.T @ d_cam
        rays.append((t, d_world))
    return rays


def pair_triangulate(rays_a, C_a, rays_b, C_b):
    rays_b = sorted(rays_b, key=lambda r: r[0])
    bt = [r[0] for r in rays_b]
    pts = []
    for t_a, d_a in rays_a:
        lo = bisect.bisect_left(bt, t_a - DT_MAX)
        hi = bisect.bisect_right(bt, t_a + DT_MAX)
        if lo == hi:
            continue
        # closest match
        best = min(range(lo, hi), key=lambda j: abs(bt[j] - t_a))
        t_b, d_b = rays_b[best]
        P, gap = triangulate_rays(C_a, d_a, C_b, d_b)
        if P is None or gap > GAP_MAX:
            continue
        pts.append({"t_rel_s": 0.5 * (t_a + t_b), "x_m": float(P[0]),
                    "y_m": float(P[1]), "z_m": float(P[2]),
                    "residual_m": float(gap)})
    return pts


@dataclass
class _Pt:
    t_rel_s: float
    x_m: float
    y_m: float
    z_m: float
    residual_m: float


def run_seg(pts):
    if not pts:
        return []
    objs = [_Pt(p["t_rel_s"], p["x_m"], p["y_m"], p["z_m"], p["residual_m"]) for p in pts]
    segs, _, _ = find_segments(objs)
    return segs


def fastest_seg(segs):
    if not segs:
        return None
    return max(segs, key=lambda s: s.speed_kph)


# ------- per-session driver -------
def load_pitch(sid, cam):
    return json.loads((DATA / "pitches" / f"session_{sid}_{cam}.json").read_text())


def live_truth(sid):
    """Run segmenter on live winners (existing pitches JSON) → ground truth segments."""
    da, db = load_pitch(sid, "A"), load_pitch(sid, "B")
    Ka, dista, Ra, Ca = setup_cam(da)
    Kb, distb, Rb, Cb = setup_cam(db)
    aa, ab = da["sync_anchor_timestamp_s"], db["sync_anchor_timestamp_s"]
    ra = []
    for f in da.get("frames_live", []):
        if f.get("ball_detected") and f.get("px") is not None:
            d_cam = undistorted_ray_cam(f["px"], f["py"], Ka, dista)
            ra.append((f["timestamp_s"] - aa, Ra.T @ d_cam))
    rb = []
    for f in db.get("frames_live", []):
        if f.get("ball_detected") and f.get("px") is not None:
            d_cam = undistorted_ray_cam(f["px"], f["py"], Kb, distb)
            rb.append((f["timestamp_s"] - ab, Rb.T @ d_cam))
    pts = pair_triangulate(ra, Ca, rb, Cb)
    return run_seg(pts)


def run_session(sid: str):
    da, db = load_pitch(sid, "A"), load_pitch(sid, "B")
    vid_a = DATA / "videos" / f"session_{sid}_A.mov"
    vid_b = DATA / "videos" / f"session_{sid}_B.mov"
    if not vid_a.exists() or not vid_b.exists():
        return None
    Ka, dista, Ra, Ca = setup_cam(da)
    Kb, distb, Rb, Cb = setup_cam(db)
    aa, ab = da["sync_anchor_timestamp_s"], db["sync_anchor_timestamp_s"]
    vsa = da["video_start_pts_s"]
    vsb = db["video_start_pts_s"]

    t0 = time.time()
    fa = extract_per_cam(vid_a, vsa, aa)
    fb = extract_per_cam(vid_b, vsb, ab)
    decode_s = time.time() - t0

    # ground truth from live
    truth = live_truth(sid)
    truth_fast = fastest_seg(truth)

    rows = []
    for mode in ("legacy", "shape", "hybrid"):
        wa = select_winners(fa, mode)
        wb = select_winners(fb, mode)
        ra = winners_to_rays(wa, Ka, dista, Ra)
        rb = winners_to_rays(wb, Kb, distb, Rb)
        pts = pair_triangulate(ra, Ca, rb, Cb)
        segs = run_seg(pts)
        fast = fastest_seg(segs)
        rows.append({
            "mode": mode,
            "n_pts": len(pts),
            "n_segs": len(segs),
            "fast_kph": fast.speed_kph if fast else 0.0,
            "fast_n": len(fast.indices) if fast else 0,
            "fast_rmse_cm": fast.rmse_m * 100 if fast else 0.0,
        })
    return {
        "sid": sid,
        "decode_s": decode_s,
        "truth_kph": truth_fast.speed_kph if truth_fast else 0.0,
        "truth_n_segs": len(truth),
        "rows": rows,
    }


SESSIONS = [
    "s_f9ddcbb6", "s_4effbd74", "s_3ec36d69", "s_ca9ad955", "s_91ddc6ec",
    "s_45170b76", "s_cc0dcaa5", "s_c7e88e51", "s_814deb32",
    "s_f50fd07f", "s_962a7db9",
]


def main():
    print(f"{'session':12s} {'truth':>8s} | "
          f"{'legacy':>14s} | {'shape':>14s} | {'hybrid':>14s}  decode")
    print("-" * 100)
    for sid in SESSIONS:
        try:
            r = run_session(sid)
        except Exception as e:
            print(f"{sid:12s} ERROR {e!r}")
            continue
        if r is None:
            print(f"{sid:12s} (no video)")
            continue
        cells = []
        for row in r["rows"]:
            delta = row["fast_kph"] - r["truth_kph"]
            mark = "✓" if abs(delta) < 5 else ("✗" if r["truth_kph"] > 30 else "·")
            cells.append(f"{row['fast_kph']:5.1f} ({row['n_segs']}) {mark}")
        print(f"{r['sid']:12s} {r['truth_kph']:6.1f}kph | "
              f"{cells[0]:>14s} | {cells[1]:>14s} | {cells[2]:>14s}  {r['decode_s']:.1f}s")


if __name__ == "__main__":
    main()
