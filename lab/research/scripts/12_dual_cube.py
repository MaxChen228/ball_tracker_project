"""Always-on dual-cube: V10 main + low-S fallback both fire, candidates
merged. Downstream physics gate (server pairing.py) filters extras.

Production semantics:
  iOS BallDetector.mm: emit *all* blobs passing area+aspect+fill, sorted
  area desc, no top-K. server iterates frame_a × frame_b pairwise and
  applies gap_threshold_m / Y-residual gates. So extra fallback cands
  ride along — only cost is extra triangulation pairs.

Test:
  recall = per-frame at-least-one cand within tol  (production metric)

Variants (all fallbacks have stricter shape gate to bound FP):
  D1  Fallback H[100,120] S[0,80]  V[180,255]  asp>=0.65 fill>=0.45 area[8,3000]
  D2  Fallback H[95,120]  S[0,100] V[160,255]  same shape gate
  D3  Fallback H[103,118] S[0,80]  V[150,255]  same shape gate (V10-hue, low-S only)
  D4  no fallback (= V10)
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

M = load_manifest()

V10 = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.50, fill=0.35, area=(5, 150_000))

FALLBACKS = {
    "D1": dict(h=(100,120), s=(0,80),  v=(180,255), aspect=0.65, fill=0.45, area=(8, 3000)),
    "D2": dict(h=(95,120),  s=(0,100), v=(160,255), aspect=0.65, fill=0.45, area=(8, 3000)),
    "D3": dict(h=(103,118), s=(0,80),  v=(150,255), aspect=0.65, fill=0.45, area=(8, 3000)),
}

def detect(bgr_or_hsv, cfg, hsv=None):
    if hsv is None:
        hsv = cv2.cvtColor(bgr_or_hsv, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h"][0], cfg["s"][0], cfg["v"][0]], dtype=np.uint8)
    hi = np.array([cfg["h"][1], cfg["s"][1], cfg["v"][1]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area"][0] or a > cfg["area"][1]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < cfg["aspect"]: continue
        fill = a/(w*h)
        if fill < cfg["fill"]: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    out.sort(key=lambda x: x[2], reverse=True)
    return out

def gt_centroid_radius(mask):
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return None, None
    return (float(xs.mean()), float(ys.mean())), float(np.sqrt(len(ys)/np.pi))

def hit(cands, gtc, r):
    tol2 = max(10.0, 0.5*r)**2
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return True
    return False

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    res = {fk: {"recall":0, "extra_cands":0, "n_frames_extra":0,
                "per_sess":{slug:{"n":0, "v10_hit":0, "merged_hit":0, "extra":0} for slug in [it["slug"] for it in items]}}
           for fk in FALLBACKS}
    n_total = 0; v10_recall = 0
    per_sess_v10 = {it["slug"]:{"n":0,"hit":0} for it in items}

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks" / SEG_BY_SLUG[slug]).glob("*.png"))
        for mp in masks:
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            gt = read_mask(mp)
            if gt is None or (gt>0).sum() < 5: continue
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            gtc, r = gt_centroid_radius(gt)
            if gtc is None: continue
            n_total += 1
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            cands_v10 = detect(None, V10, hsv=hsv)
            v10_h = hit(cands_v10, gtc, r)
            if v10_h: v10_recall += 1
            per_sess_v10[slug]["n"] += 1
            if v10_h: per_sess_v10[slug]["hit"] += 1
            for fk, cfg in FALLBACKS.items():
                cands_fb = detect(None, cfg, hsv=hsv)
                # Dedupe: drop fallback cand if its centroid within 5px of any V10 cand
                merged = list(cands_v10)
                for cb in cands_fb:
                    near = False
                    for cv in cands_v10:
                        if (cb[0]-cv[0])**2 + (cb[1]-cv[1])**2 < 25:
                            near = True; break
                    if not near:
                        merged.append(cb)
                extra = len(merged) - len(cands_v10)
                if extra > 0:
                    res[fk]["n_frames_extra"] += 1
                    res[fk]["extra_cands"] += extra
                    res[fk]["per_sess"][slug]["extra"] += extra
                m_h = hit(merged, gtc, r)
                if m_h: res[fk]["recall"] += 1; res[fk]["per_sess"][slug]["merged_hit"] += 1
                res[fk]["per_sess"][slug]["n"] += 1
                if v10_h: res[fk]["per_sess"][slug]["v10_hit"] += 1

    print(f"=== Baseline ({n_total} frames) ===")
    print(f"V10 recall = {v10_recall/n_total:.3f} ({v10_recall}/{n_total})")

    print(f"\n=== Dual-cube variant comparison ===")
    print(f"{'V':<3} {'recall':>7} {'Δpp':>6} {'recovered':>10} {'extra/frame':>12} {'frames_w_extra':>15}")
    for fk in FALLBACKS:
        s = res[fk]
        nr = s["recall"]/n_total
        delta = (s["recall"]-v10_recall)/n_total*100
        extra_per = s["extra_cands"]/n_total
        print(f"{fk:<3} {nr:>7.3f} {delta:>+6.2f} {s['recall']-v10_recall:>10} {extra_per:>12.2f} {s['n_frames_extra']:>15}")

    print(f"\n=== Per-session D1 ===")
    print(f"{'session':<28} {'n':>4} {'V10_R':>6} {'D1_R':>6} {'Δpp':>6} {'extra/f':>8}")
    fk = "D1"
    for slug, d in sorted(res[fk]["per_sess"].items()):
        if d["n"] == 0: continue
        v_r = d["v10_hit"]/d["n"]
        m_r = d["merged_hit"]/d["n"]
        delta = (m_r - v_r)*100
        extra = d["extra"]/d["n"]
        print(f"  {slug:<26} {d['n']:>4} {v_r:>6.3f} {m_r:>6.3f} {delta:>+6.2f} {extra:>8.2f}")

    print(f"\n=== Per-session D2 ===")
    fk = "D2"
    print(f"{'session':<28} {'n':>4} {'V10_R':>6} {'D2_R':>6} {'Δpp':>6} {'extra/f':>8}")
    for slug, d in sorted(res[fk]["per_sess"].items()):
        if d["n"] == 0: continue
        v_r = d["v10_hit"]/d["n"]
        m_r = d["merged_hit"]/d["n"]
        delta = (m_r - v_r)*100
        extra = d["extra"]/d["n"]
        print(f"  {slug:<26} {d['n']:>4} {v_r:>6.3f} {m_r:>6.3f} {delta:>+6.2f} {extra:>8.2f}")

    np.savez_compressed(OUT/"dual_cube.npz", res=np.array(list(res.items()), dtype=object))
    print(f"\n[done] {OUT/'dual_cube.npz'}")

if __name__ == "__main__":
    main()
