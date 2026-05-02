"""28c — Motion-novelty rerank.

Reframe from 28b: trajectory consistency rewards static distractors
(field lines, jerseys at fixed pixel position) because they're inliers
to a near-flat parabola in every frame of the window. R_top1 = 0.205,
worse than PROD.

Real discrimination signal: **the ball moves, distractors don't**. So
score each V11 cand at frame f by how NOVEL its position is across the
neighbor window. A static distractor has a near-duplicate cand at
f-1, f+1, f-2, f+2 → high persistence → demote. The ball is alone at
its frame-f position → low persistence → promote.

Method (one physical principle, no per-session knobs):
  1. V11 detect every frame.
  2. For each cand C at frame f:
     persistence(C) = count of frames in [f-K, f+K]\\{f} that have a
                       V11 cand within MATCH_PX of C
  3. Rank cands by persistence ASC, tie-break shape cost.
  4. top-1 hit if first-ranked is within TOL of GT.

Compare to PROD baseline 0.615.
"""
from __future__ import annotations
import json, time, sys
from pathlib import Path
import numpy as np
import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

sys.path.insert(0, str(ROOT / "server"))
from candidate_selector import Candidate, score_candidates  # noqa

OUT.mkdir(parents=True, exist_ok=True)

TOL_PX = 10.0
NEIGH_HALF = 6     # frames on each side, total = 12 (50ms @ 240fps)
MATCH_PX = 5.0     # cand at f-k matches if within 5 px of cand at f
SHAPE_TIE_BREAK = True

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

CandFeat = tuple[float, float, int, float, float]


def detect_v11(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
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


def gt_centroid(mask):
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def shape_cost(c):
    return score_candidates([Candidate(cx=c[0], cy=c[1], area=c[2],
                                       aspect=c[3], fill=c[4])])[0]


def persistence(cand, neigh_cands_by_frame):
    cx, cy = cand[0], cand[1]
    tol2 = MATCH_PX * MATCH_PX
    n = 0
    for cands in neigh_cands_by_frame:
        for nc in cands:
            if (nc[0] - cx) ** 2 + (nc[1] - cy) ** 2 <= tol2:
                n += 1
                break
    return n


def main():
    t0 = time.time()
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    grand = {"top1": [], "top3": [], "emit": []}
    per_session = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set:
            continue
        # Detect every frame in clip
        frames_dir = WS / "items" / slug / "frames"
        raw: dict[int, list[CandFeat]] = {}
        for fp in sorted(frames_dir.glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None: continue
            raw[src] = detect_v11(bgr)

        sess_top1 = sess_top3 = sess_emit = 0
        sess_n = 0
        for src in sorted(gt_set):
            mask = read_mask(masks_dir / f"{src:05d}.png")
            if mask is None or (mask > 0).sum() < 20: continue
            gx, gy = gt_centroid(mask)
            cands = raw.get(src, [])
            neigh = [raw[s] for s in range(src - NEIGH_HALF, src + NEIGH_HALF + 1)
                     if s != src and s in raw]
            scored = []
            for c in cands:
                p = persistence(c, neigh)
                sc = shape_cost(c) if SHAPE_TIE_BREAK else 0.0
                scored.append((p, sc, c))
            scored.sort(key=lambda x: (x[0], x[1]))  # persistence asc, shape asc
            ranked = [x[2] for x in scored]
            def hit(c): return (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX
            sess_n += 1
            if ranked and hit(ranked[0]): sess_top1 += 1
            if any(hit(c) for c in ranked[:3]): sess_top3 += 1
            if any(hit(c) for c in cands): sess_emit += 1
            grand["top1"].append(ranked and hit(ranked[0]) or False)
            grand["top3"].append(any(hit(c) for c in ranked[:3]))
            grand["emit"].append(any(hit(c) for c in cands))
        if sess_n == 0: continue
        per_session.append({
            "slug": slug, "n_gt": sess_n,
            "R_top1": sess_top1 / sess_n, "R_top3": sess_top3 / sess_n,
            "R_emit": sess_emit / sess_n,
        })
        print(f"  {slug:<28} n={sess_n:>4}  "
              f"R_top1={sess_top1/sess_n:.3f}  R_top3={sess_top3/sess_n:.3f}  "
              f"R_emit={sess_emit/sess_n:.3f}", flush=True)
    R_top1 = float(np.mean(grand["top1"]))
    R_top3 = float(np.mean(grand["top3"]))
    R_emit = float(np.mean(grand["emit"]))
    print(f"\nAGGREGATE  N={len(grand['top1'])}  "
          f"R_top1={R_top1:.3f}  R_top3={R_top3:.3f}  R_emit={R_emit:.3f}")
    print(f"PROD baseline R_top1=0.615  delta={R_top1 - 0.615:+.3f}")
    worst = sorted(per_session, key=lambda s: s["R_top1"])[:5]
    print("\nWorst 5 sessions:")
    for s in worst:
        print(f"  {s['slug']:<28} R_top1={s['R_top1']:.3f}  R_top3={s['R_top3']:.3f}")
    out = OUT / "28c_motion_rerank.json"
    out.write_text(json.dumps({
        "R_top1": R_top1, "R_top3": R_top3, "R_emit": R_emit,
        "delta_vs_prod": R_top1 - 0.615,
        "params": {"NEIGH_HALF": NEIGH_HALF, "MATCH_PX": MATCH_PX,
                   "SHAPE_TIE_BREAK": SHAPE_TIE_BREAK, "TOL_PX": TOL_PX},
        "per_session": per_session,
    }, indent=2))
    print(f"\n[saved] {out}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
