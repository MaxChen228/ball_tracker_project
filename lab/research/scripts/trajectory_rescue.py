"""Trajectory-coherence rescue: extends hybrid by replacing the
neighbor-persistence reranker on PROD-empty frames with a parabolic
trajectory score.

Why try this:
  hybrid (R=0.660) rescues 28% of the 315 PROD-empty GT frames using
  motion-novelty (penalize cands whose position appears across
  neighbor frames). That separates moving from static, but cannot
  separate ball-motion from limb / hand / other moving distractor.

  Parabolic fit adds a physics constraint: a falling ball traces a
  smooth parabola in image y over short windows; non-ball moving
  objects (limbs, players, occlusions) do not.

Method (per PROD-empty rescue frame at src):
  1. For each V11 anchor cand at src:
       walk forward/backward through neighbor frames [src-NEIGH .. src+NEIGH]
       linking nearest V11 cand under constant-velocity prediction.
       MAX_LINK_PX caps per-step jump.
  2. Track of (frame, x, y). Need >= MIN_TRACK_LEN points.
  3. Compute mean per-frame velocity. < MIN_VEL_PX/frame -> reject as
     static (matches hybrid's motion-novelty discrimination).
  4. Fit y = a*tau^2 + b*tau + c, x = d*tau + e (tau = frame - src).
     residual = std(y - fit_y) + std(x - fit_x)
  5. Score = (no_track, no_motion, residual_asc, shape_cost_asc).
     Lowest wins (becomes top-1 rescue cand).

Hyperparams (all physics-derived, no per-session tuning):
  NEIGH_HALF    = 6        ~50 ms @ 240 fps
  MAX_LINK_PX   = 25       ball motion budget per frame at game speed
  MIN_TRACK_LEN = 4        need >=3 for parabola; +1 margin
  MIN_VEL_PX    = 1.5      below this is camera/CC noise, not motion

Output: outputs/trajectory_rescue.json
"""
from __future__ import annotations
import json
import time
import sys
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

sys.path.insert(0, str(ROOT / "server"))
from candidate_selector import Candidate, score_candidates  # noqa

OUT.mkdir(parents=True, exist_ok=True)

TOL_PX = 10.0
NEIGH_HALF = 6
MAX_LINK_PX = 25.0
MIN_TRACK_LEN = 4
MIN_VEL_PX = 1.5

PROD = dict(h=(105, 112), s=(140, 255), v=(40, 255),
            aspect=0.75, fill=0.55, area=(20, 150_000))
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)


def _emit(m, cfg):
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area"][0] or a > cfg["area"][1]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        asp = min(w, h) / max(w, h)
        if asp < cfg["aspect"]: continue
        fill = a / (w * h)
        if fill < cfg["fill"]: continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a, float(asp), float(fill)))
    return out


def detect_prod(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([PROD["h"][0], PROD["s"][0], PROD["v"][0]], dtype=np.uint8)
    hi = np.array([PROD["h"][1], PROD["s"][1], PROD["v"][1]], dtype=np.uint8)
    return _emit(cv2.inRange(hsv, lo, hi), PROD)


def detect_v11(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _emit(m, V11)


def shape_cost(c):
    return score_candidates([Candidate(cx=c[0], cy=c[1], area=c[2],
                                       aspect=c[3], fill=c[4])])[0]


def gt_centroid(mask):
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def _walk(anchor_xy, anchor_t, v11_raw, direction):
    """Greedy NN extension under constant-velocity prediction."""
    pts = []
    prev = anchor_xy
    pred = anchor_xy  # initial prediction = anchor (no velocity yet)
    for step in range(1, NEIGH_HALF + 1):
        tt = anchor_t + direction * step
        cands = v11_raw.get(tt)
        if not cands: break  # broken neighborhood
        best = min(cands, key=lambda c: (c[0] - pred[0]) ** 2 + (c[1] - pred[1]) ** 2)
        d2 = (best[0] - pred[0]) ** 2 + (best[1] - pred[1]) ** 2
        if d2 > MAX_LINK_PX * MAX_LINK_PX: break  # no plausible link
        pts.append((tt, best[0], best[1]))
        # update constant-velocity prediction
        v = (best[0] - prev[0], best[1] - prev[1])
        prev = (best[0], best[1])
        pred = (best[0] + v[0], best[1] + v[1])
    return pts


def trajectory_score(anchor, anchor_t, v11_raw):
    """Lower is better. Returns (no_track, no_motion, residual, shape)."""
    sh = shape_cost(anchor)
    fwd = _walk((anchor[0], anchor[1]), anchor_t, v11_raw, +1)
    bwd = _walk((anchor[0], anchor[1]), anchor_t, v11_raw, -1)
    track = [(anchor_t, anchor[0], anchor[1])] + fwd + bwd
    if len(track) < MIN_TRACK_LEN:
        return (1, 1, 0.0, sh)
    track.sort()
    ts = np.array([p[0] for p in track], dtype=np.float64) - anchor_t
    xs = np.array([p[1] for p in track], dtype=np.float64)
    ys = np.array([p[2] for p in track], dtype=np.float64)
    # mean per-frame velocity (path length / frame span)
    span = ts[-1] - ts[0]
    if span <= 0:
        return (1, 1, 0.0, sh)
    path_len = float(np.sum(np.sqrt(np.diff(xs) ** 2 + np.diff(ys) ** 2)))
    mean_vel = path_len / span
    if mean_vel < MIN_VEL_PX:
        return (0, 1, 0.0, sh)
    py = np.polyfit(ts, ys, 2)
    px = np.polyfit(ts, xs, 1)
    res_y = float(np.std(ys - np.polyval(py, ts)))
    res_x = float(np.std(xs - np.polyval(px, ts)))
    res = res_y + res_x
    return (0, 0, res, sh)


def main():
    t0 = time.time()
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    grand = {"traj": [], "hyb_baseline": [], "prod": [],
             "rescue_attempted": [], "rescue_hit": []}
    per_session = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set: continue
        frames_dir = WS / "items" / slug / "frames"
        v11_raw = {}; prod_raw = {}
        for fp in sorted(frames_dir.glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None: continue
            v11_raw[src] = detect_v11(bgr)
            prod_raw[src] = detect_prod(bgr)

        sess_traj = sess_prod = 0; sess_n = 0
        sess_rescue_att = sess_rescue_hit = 0
        for src in sorted(gt_set):
            mask = read_mask(masks_dir / f"{src:05d}.png")
            if mask is None or (mask > 0).sum() < 20: continue
            gx, gy = gt_centroid(mask)
            def hit(c): return (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX

            prod_cands = prod_raw.get(src, [])
            v11_cands = v11_raw.get(src, [])

            prod_top1 = None
            if prod_cands:
                prod_top1 = sorted(prod_cands, key=shape_cost)[0]
            prod_hit = prod_top1 is not None and hit(prod_top1)

            traj_top1 = prod_top1
            if prod_top1 is None and v11_cands:
                scored = sorted(v11_cands, key=lambda c: trajectory_score(c, src, v11_raw))
                traj_top1 = scored[0]
                sess_rescue_att += 1
                if hit(traj_top1): sess_rescue_hit += 1
            traj_hit = traj_top1 is not None and hit(traj_top1)

            sess_n += 1
            if traj_hit: sess_traj += 1
            if prod_hit: sess_prod += 1
            grand["traj"].append(traj_hit); grand["prod"].append(prod_hit)
            grand["rescue_attempted"].append(prod_top1 is None and bool(v11_cands))
            grand["rescue_hit"].append(prod_top1 is None and traj_hit)
        if sess_n == 0: continue
        per_session.append({
            "slug": slug, "n_gt": sess_n,
            "R_traj": sess_traj / sess_n,
            "R_prod": sess_prod / sess_n,
            "rescue_att": sess_rescue_att,
            "rescue_hit": sess_rescue_hit,
            "rescue_rate": sess_rescue_hit / sess_rescue_att if sess_rescue_att else 0.0,
        })
        print(f"  {slug:<28} n={sess_n:>4}  "
              f"R_traj={sess_traj/sess_n:.3f}  R_prod={sess_prod/sess_n:.3f}  "
              f"rescue {sess_rescue_hit}/{sess_rescue_att}", flush=True)
    R_traj = float(np.mean(grand["traj"]))
    R_prod = float(np.mean(grand["prod"]))
    rescue_total = sum(1 for x in grand["rescue_attempted"] if x)
    rescue_hit_total = sum(grand["rescue_hit"])
    print(f"\nAGGREGATE  N={len(grand['traj'])}")
    print(f"  R_trajectory = {R_traj:.3f}   R_prod_only = {R_prod:.3f}   delta = {R_traj - R_prod:+.3f}")
    print(f"  R_hybrid (prior baseline, see hybrid.py) = 0.660")
    print(f"  Rescue: attempted on {rescue_total} frames, hit {rescue_hit_total} "
          f"({100 * rescue_hit_total / max(1, rescue_total):.1f}%)")

    worst = sorted(per_session, key=lambda s: s["R_traj"])[:5]
    print("\nWorst 5 sessions by R_traj:")
    for s in worst:
        print(f"  {s['slug']:<28} R_traj={s['R_traj']:.3f}  R_prod={s['R_prod']:.3f}  "
              f"rescue {s['rescue_hit']}/{s['rescue_att']}")

    out = OUT / "trajectory_rescue.json"
    out.write_text(json.dumps({
        "R_trajectory": R_traj, "R_prod_baseline": R_prod,
        "delta_vs_prod": R_traj - R_prod,
        "rescue_attempted": rescue_total, "rescue_hit": rescue_hit_total,
        "per_session": per_session,
        "hyperparams": {
            "NEIGH_HALF": NEIGH_HALF, "MAX_LINK_PX": MAX_LINK_PX,
            "MIN_TRACK_LEN": MIN_TRACK_LEN, "MIN_VEL_PX": MIN_VEL_PX,
        },
    }, indent=2))
    print(f"\n[saved] {out}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
