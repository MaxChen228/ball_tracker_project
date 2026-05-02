"""D3 fallback (low-S V10-hue cube) with top-K cap.

Q: With shape gate strict, fallback still emits ~30 blobs/frame on
   1080p (mostly bg). top-K cap is mandatory for server pairing.
   Find K that recovers most M1 frames with bounded cost.

Test K in {1, 2, 3, 5} on D3 fallback. Per-frame extra cands and
M1 recovery + per-session breakdown.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT

M = json.loads((WS / "manifest.json").read_text())

V10 = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.50, fill=0.35, area=(5, 150_000))
D3  = dict(h=(103,118), s=(0,80), v=(150,255), aspect=0.65, fill=0.45, area=(8, 3000))

KS = [1, 2, 3, 5]

def detect(hsv, cfg, top_k=None):
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
    if top_k: out = out[:top_k]
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
    res = {k: {"recall":0, "extra":0, "per_sess":{it["slug"]:{"n":0,"v_hit":0,"m_hit":0,"extra":0} for it in items}} for k in KS}
    n_total = 0; v10_recall = 0

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        for mp in masks:
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if gt is None or (gt>0).sum() < 5: continue
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            gtc, r = gt_centroid_radius(gt)
            if gtc is None: continue
            n_total += 1
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            cands_v10 = detect(hsv, V10)
            v_h = hit(cands_v10, gtc, r)
            if v_h: v10_recall += 1
            for k in KS:
                cands_fb = detect(hsv, D3, top_k=k)
                merged = list(cands_v10)
                for cb in cands_fb:
                    near = False
                    for cv in cands_v10:
                        if (cb[0]-cv[0])**2 + (cb[1]-cv[1])**2 < 25:
                            near = True; break
                    if not near:
                        merged.append(cb)
                extra = len(merged) - len(cands_v10)
                res[k]["extra"] += extra
                res[k]["per_sess"][slug]["extra"] += extra
                m_h = hit(merged, gtc, r)
                if m_h: res[k]["recall"] += 1; res[k]["per_sess"][slug]["m_hit"] += 1
                res[k]["per_sess"][slug]["n"] += 1
                if v_h: res[k]["per_sess"][slug]["v_hit"] += 1

    print(f"=== Baseline ({n_total} frames) ===")
    print(f"V10 recall = {v10_recall/n_total:.3f} ({v10_recall}/{n_total})")
    print(f"\n=== D3 + top-K ===")
    print(f"{'K':>2} {'recall':>7} {'Δpp':>6} {'recovered':>10} {'extra/frame':>12}")
    for k in KS:
        nr = res[k]["recall"]/n_total
        delta = (res[k]["recall"]-v10_recall)/n_total*100
        ex_per = res[k]["extra"]/n_total
        print(f"{k:>2} {nr:>7.3f} {delta:>+6.2f} {res[k]['recall']-v10_recall:>10} {ex_per:>12.2f}")

    print(f"\n=== Per-session at K=1 ===")
    print(f"{'session':<28} {'n':>4} {'V10_R':>6} {'M_R':>6} {'Δpp':>6} {'extra/f':>8}")
    for slug, d in sorted(res[1]["per_sess"].items()):
        if d["n"] == 0: continue
        v_r = d["v_hit"]/d["n"]; m_r = d["m_hit"]/d["n"]
        print(f"  {slug:<26} {d['n']:>4} {v_r:>6.3f} {m_r:>6.3f} {(m_r-v_r)*100:>+6.2f} {d['extra']/d['n']:>8.2f}")

    print(f"\n=== Per-session at K=3 ===")
    for slug, d in sorted(res[3]["per_sess"].items()):
        if d["n"] == 0: continue
        v_r = d["v_hit"]/d["n"]; m_r = d["m_hit"]/d["n"]
        print(f"  {slug:<26} {d['n']:>4} {v_r:>6.3f} {m_r:>6.3f} {(m_r-v_r)*100:>+6.2f} {d['extra']/d['n']:>8.2f}")

    np.savez_compressed(OUT/"dual_cube_topk.npz", res=np.array(list(res.items()), dtype=object))
    print(f"\n[done] {OUT/'dual_cube_topk.npz'}")

if __name__ == "__main__":
    main()
