"""28d — Hybrid: PROD-first, V11+motion-novelty backstop.

PROD gives R_top1 = 0.615 because its tight HSV+shape gate has high
precision but low recall (R_emit = 0.721). On 28% of GT frames, PROD
emits nothing — those are pure misses.

V11 + motion-novelty alone gives R_top1 = 0.448 because V11 sweeps
loose (R_emit = 0.925) but motion-novelty can't beat shape on the
mixed pool of static + moving distractors.

Hybrid: for each frame
  - if PROD emits ≥1 cand → rank PROD cands by shape cost, take top-1
  - else → rank V11 cands by motion-novelty (persistence asc, shape asc),
           take top-1
This preserves PROD's wins (no degradation when PROD confident) and
adds a rescue path on PROD's emit miss.

Floor analysis: if rescue is random, R_top1_hybrid ≥ R_top1_PROD = 0.615.
Any rescue contribution > 0 gives delta > 0.
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
NEIGH_HALF = 6
MATCH_PX = 5.0

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


def persistence(cand, neigh_cands):
    cx, cy = cand[0], cand[1]
    tol2 = MATCH_PX * MATCH_PX
    n = 0
    for cl in neigh_cands:
        for nc in cl:
            if (nc[0] - cx) ** 2 + (nc[1] - cy) ** 2 <= tol2:
                n += 1; break
    return n


def gt_centroid(mask):
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def main():
    t0 = time.time()
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    grand = {"hyb": [], "prod_only": [], "rescue_attempted": [], "rescue_hit": []}
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

        sess_hyb = sess_prod = 0; sess_n = 0
        sess_rescue_att = sess_rescue_hit = 0
        for src in sorted(gt_set):
            mask = read_mask(masks_dir / f"{src:05d}.png")
            if mask is None or (mask > 0).sum() < 20: continue
            gx, gy = gt_centroid(mask)
            def hit(c): return (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX

            prod_cands = prod_raw.get(src, [])
            v11_cands = v11_raw.get(src, [])

            # PROD-only baseline top1
            prod_top1 = None
            if prod_cands:
                ranked = sorted(prod_cands, key=shape_cost)
                prod_top1 = ranked[0]
            prod_hit = prod_top1 is not None and hit(prod_top1)

            # Hybrid: PROD if it emits, else V11 motion-novelty
            hyb_top1 = prod_top1
            if prod_top1 is None and v11_cands:
                neigh = [v11_raw[s] for s in range(src - NEIGH_HALF, src + NEIGH_HALF + 1)
                         if s != src and s in v11_raw]
                scored = sorted(v11_cands, key=lambda c: (persistence(c, neigh), shape_cost(c)))
                hyb_top1 = scored[0]
                sess_rescue_att += 1
                if hit(hyb_top1): sess_rescue_hit += 1
            hyb_hit = hyb_top1 is not None and hit(hyb_top1)

            sess_n += 1
            if hyb_hit: sess_hyb += 1
            if prod_hit: sess_prod += 1
            grand["hyb"].append(hyb_hit); grand["prod_only"].append(prod_hit)
            grand["rescue_attempted"].append(prod_top1 is None and v11_cands)
            grand["rescue_hit"].append(prod_top1 is None and hyb_hit)
        if sess_n == 0: continue
        per_session.append({
            "slug": slug, "n_gt": sess_n,
            "R_hybrid": sess_hyb / sess_n,
            "R_prod": sess_prod / sess_n,
            "rescue_att": sess_rescue_att,
            "rescue_hit": sess_rescue_hit,
            "rescue_rate": sess_rescue_hit / sess_rescue_att if sess_rescue_att else 0.0,
        })
        print(f"  {slug:<28} n={sess_n:>4}  "
              f"R_hyb={sess_hyb/sess_n:.3f}  R_prod={sess_prod/sess_n:.3f}  "
              f"rescue {sess_rescue_hit}/{sess_rescue_att}", flush=True)
    R_hyb = float(np.mean(grand["hyb"]))
    R_prod = float(np.mean(grand["prod_only"]))
    rescue_total = sum(1 for x in grand["rescue_attempted"] if x)
    rescue_hit_total = sum(grand["rescue_hit"])
    print(f"\nAGGREGATE  N={len(grand['hyb'])}")
    print(f"  R_hybrid = {R_hyb:.3f}   R_prod_only = {R_prod:.3f}   delta = {R_hyb - R_prod:+.3f}")
    print(f"  Rescue: attempted on {rescue_total} frames (PROD empty + V11 has cands), "
          f"hit {rescue_hit_total} ({100 * rescue_hit_total / max(1, rescue_total):.1f}%)")

    worst = sorted(per_session, key=lambda s: s["R_hybrid"])[:5]
    print("\nWorst 5 sessions by R_hybrid:")
    for s in worst:
        print(f"  {s['slug']:<28} R_hyb={s['R_hybrid']:.3f}  R_prod={s['R_prod']:.3f}  "
              f"rescue {s['rescue_hit']}/{s['rescue_att']}")
    out = OUT / "28d_hybrid.json"
    out.write_text(json.dumps({
        "R_hybrid": R_hyb, "R_prod_baseline": R_prod,
        "delta_vs_prod": R_hyb - R_prod,
        "rescue_attempted": rescue_total, "rescue_hit": rescue_hit_total,
        "per_session": per_session,
    }, indent=2))
    print(f"\n[saved] {out}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
