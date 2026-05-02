"""28b — Trajectory-consistency rerank (no filter).

Iteration on 28_traj_ransac (which over-filtered: R_top1=0.324 << PROD 0.615).
Keep V11 emit set unchanged; replace shape-cost top-1 selection with
trajectory-consistency rerank.

Method (single physics constraint, no per-session tuning):
  1. V11 detect every frame → cand list (~25 per frame, full set)
  2. For each cand C at frame f, compute trajectory_score(C) =
     max over (P, Q) sampled from cands at frame f-Δ, f+Δ
       (Δ ∈ {3,6,9}, 15+ different (P,Q) anchor pairs):
       fit parabola y(t) through (P, C, Q) → count inlier cands in
       window [f-10, f+10] with |cy_obs - cy_fit| ≤ 5 px AND a_y > 0.
  3. top-1 = max trajectory_score; ties broken by production shape cost.

Compare R_top1 vs PROD baseline 0.615.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import numpy as np
import cv2
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

sys.path.insert(0, str(ROOT / "server"))
from candidate_selector import Candidate, score_candidates  # noqa

OUT.mkdir(parents=True, exist_ok=True)

TOL_PX = 10.0
INLIER_PX = 5.0
WINDOW_HALF = 10  # frames each side, total window = 21
DELTAS = [3, 6, 9]

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

CandFeat = tuple[float, float, int, float, float]


def detect_v11(bgr: np.ndarray) -> list[CandFeat]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[CandFeat] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        asp = min(w, h) / max(w, h)
        if asp < V11["aspect"]: continue
        fill = a / (w * h)
        if fill < V11["fill"]: continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a, float(asp), float(fill)))
    return out


def gt_centroid(mask: np.ndarray) -> tuple[float, float]:
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def fit_parabola_3pt(t1, y1, t2, y2, t3, y3) -> tuple[float, float, float] | None:
    """Solve y = at²+bt+c through 3 points. Returns (a,b,c) or None on degenerate."""
    A = np.array([[t1*t1, t1, 1.0],
                  [t2*t2, t2, 1.0],
                  [t3*t3, t3, 1.0]])
    try:
        return tuple(np.linalg.solve(A, np.array([y1, y2, y3])))
    except np.linalg.LinAlgError:
        return None


def trajectory_score(target_t: int, cand_C: CandFeat,
                     win_cands: dict[int, list[CandFeat]]) -> int:
    """Best inlier-count among parabolas (P, C, Q) where P at f-Δ, Q at f+Δ.
    Inlier: any cand in window whose cy lies within INLIER_PX of fit at its t."""
    best = 0
    cy_C = cand_C[1]
    for d in DELTAS:
        for f_p in (target_t - d,):
            if f_p not in win_cands: continue
            for cP in win_cands[f_p]:
                for f_q in (target_t + d,):
                    if f_q not in win_cands: continue
                    for cQ in win_cands[f_q]:
                        fit = fit_parabola_3pt(f_p, cP[1], target_t, cy_C, f_q, cQ[1])
                        if fit is None: continue
                        a, b, c = fit
                        if a <= 0:  # gravity must be positive (Y down)
                            continue
                        # Count inliers across full window
                        count = 0
                        for f_w, cands_w in win_cands.items():
                            y_pred = a * f_w * f_w + b * f_w + c
                            for cw in cands_w:
                                if abs(cw[1] - y_pred) <= INLIER_PX:
                                    count += 1
                                    break  # one inlier per frame max
                        if count > best:
                            best = count
    return best


def shape_cost(c: CandFeat) -> float:
    cs = [Candidate(cx=c[0], cy=c[1], area=c[2], aspect=c[3], fill=c[4])]
    return score_candidates(cs)[0]


def session_pass(slug: str, in_f: int, gt_set: set[int]) -> dict:
    frames_dir = WS / "items" / slug / "frames"
    raw: dict[int, list[CandFeat]] = {}
    for fp in sorted(frames_dir.glob("*.jpg")):
        local = int(fp.stem); src = local + in_f
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None: continue
        raw[src] = detect_v11(bgr)

    # For each GT frame, rank cands by trajectory_score desc, tie-break by shape cost asc.
    masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
    sess = {"slug": slug, "frames": []}
    for src in sorted(gt_set):
        mask = read_mask(masks_dir / f"{src:05d}.png")
        if mask is None or (mask > 0).sum() < 20: continue
        gx, gy = gt_centroid(mask)
        cands = raw.get(src, [])
        # Build window cand dict
        win = {}
        for s in range(src - WINDOW_HALF, src + WINDOW_HALF + 1):
            if s in raw: win[s] = raw[s]
        # Score each cand
        scored = []
        for c in cands:
            ts = trajectory_score(src, c, win)
            sc = shape_cost(c)
            scored.append((ts, sc, c))
        scored.sort(key=lambda x: (-x[0], x[1]))  # ts desc, shape asc
        ranked = [x[2] for x in scored]

        def hit(c): return (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX
        sess["frames"].append({
            "src": src,
            "n_cands": len(cands),
            "top1_hit": (ranked and hit(ranked[0])) or False,
            "top3_hit": any(hit(c) for c in ranked[:3]),
            "emit_hit": any(hit(c) for c in cands),
            "top_traj_score": scored[0][0] if scored else 0,
        })
    return sess


def main():
    t0 = time.time()
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    grand = {"top1": [], "top3": [], "emit": []}
    per_session = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set: continue
        sess = session_pass(slug, in_f, gt_set)
        n = len(sess["frames"])
        if n == 0: continue
        top1 = sum(f["top1_hit"] for f in sess["frames"]) / n
        top3 = sum(f["top3_hit"] for f in sess["frames"]) / n
        emit = sum(f["emit_hit"] for f in sess["frames"]) / n
        sess["R_top1"] = top1; sess["R_top3"] = top3; sess["R_emit"] = emit; sess["n_gt"] = n
        for f in sess["frames"]:
            grand["top1"].append(f["top1_hit"])
            grand["top3"].append(f["top3_hit"])
            grand["emit"].append(f["emit_hit"])
        per_session.append({k: sess[k] for k in ("slug", "n_gt", "R_top1", "R_top3", "R_emit")})
        print(f"  {slug:<28} n={n:>4}  "
              f"R_top1={top1:.3f}  R_top3={top3:.3f}  R_emit={emit:.3f}", flush=True)
    R_top1 = float(np.mean(grand["top1"]))
    R_top3 = float(np.mean(grand["top3"]))
    R_emit = float(np.mean(grand["emit"]))
    print(f"\nAGGREGATE  N={len(grand['top1'])}  "
          f"R_top1={R_top1:.3f}  R_top3={R_top3:.3f}  R_emit={R_emit:.3f}")
    print(f"PROD baseline R_top1=0.615  delta={R_top1 - 0.615:+.3f}")
    worst = sorted(per_session, key=lambda s: s["R_top1"])[:5]
    print("\nWorst 5 sessions by R_top1:")
    for s in worst:
        print(f"  {s['slug']:<28} R_top1={s['R_top1']:.3f}  R_top3={s['R_top3']:.3f}")
    out = OUT / "28b_traj_rerank.json"
    out.write_text(json.dumps({
        "R_top1": R_top1, "R_top3": R_top3, "R_emit": R_emit,
        "delta_vs_prod": R_top1 - 0.615,
        "per_session": per_session,
    }, indent=2))
    print(f"\n[saved] {out}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
