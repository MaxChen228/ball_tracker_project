"""Attack M2 (CC fragmentation, 28.8% of miss) and M3 (aspect, 16.0%).

Variants:
  E0  V10 baseline                              (control)
  E1  V10 + morph CLOSE 3x3                     (M2)
  E2  V10 + morph CLOSE 5x5                     (M2 stronger)
  E3  V10 with aspect_min=0.40                  (M3)
  E4  V10 with aspect_min=0.30                  (M3 aggressive)
  E5  V10 + CLOSE 3x3 + aspect 0.40             (M2+M3 combo)
  E6  V10 + CLOSE 3x3 + aspect 0.40 + min_area 3 (E5 + tiny CC)
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

BASE = dict(h=(103,118), s=(120,255), v=(30,255))

VARIANTS = {
    "E0": dict(close=0,  aspect=0.50, fill=0.35, area_min=5, area_max=150_000),
    "E1": dict(close=3,  aspect=0.50, fill=0.35, area_min=5, area_max=150_000),
    "E2": dict(close=5,  aspect=0.50, fill=0.35, area_min=5, area_max=150_000),
    "E3": dict(close=0,  aspect=0.40, fill=0.35, area_min=5, area_max=150_000),
    "E4": dict(close=0,  aspect=0.30, fill=0.35, area_min=5, area_max=150_000),
    "E5": dict(close=3,  aspect=0.40, fill=0.35, area_min=5, area_max=150_000),
    "E6": dict(close=3,  aspect=0.40, fill=0.35, area_min=3, area_max=150_000),
}

def detect(hsv, cfg):
    lo = np.array([BASE["h"][0], BASE["s"][0], BASE["v"][0]], dtype=np.uint8)
    hi = np.array([BASE["h"][1], BASE["s"][1], BASE["v"][1]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    if cfg["close"] > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg["close"], cfg["close"]))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area_min"] or a > cfg["area_max"]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < cfg["aspect"]: continue
        fill = a/(w*h)
        if fill < cfg["fill"]: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
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
    res = {v: {"hit":0, "n_cands":0, "per_sess":{it["slug"]:{"n":0,"hit":0,"cands":0} for it in items}} for v in VARIANTS}
    n_total = 0

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
            for vk, cfg in VARIANTS.items():
                cands = detect(hsv, cfg)
                if hit(cands, gtc, r):
                    res[vk]["hit"] += 1
                    res[vk]["per_sess"][slug]["hit"] += 1
                res[vk]["n_cands"] += len(cands)
                res[vk]["per_sess"][slug]["n"] += 1
                res[vk]["per_sess"][slug]["cands"] += len(cands)

    base = res["E0"]["hit"]
    print(f"=== Variant comparison ({n_total} frames) ===")
    print(f"{'V':<4} {'recall':>7} {'Δpp_vs_E0':>10} {'cands/f':>8} {'config':<60}")
    for vk, cfg in VARIANTS.items():
        s = res[vk]
        nr = s["hit"]/n_total
        delta = (s["hit"]-base)/n_total*100
        cf = f"close={cfg['close']} asp={cfg['aspect']:.2f} fill={cfg['fill']:.2f} area_min={cfg['area_min']}"
        print(f"{vk:<4} {nr:>7.3f} {delta:>+10.2f} {s['n_cands']/n_total:>8.2f} {cf}")

    print(f"\n=== Per-session E5 (close3 + asp 0.40) vs E0 ===")
    print(f"{'session':<28} {'n':>4} {'E0_R':>6} {'E5_R':>6} {'Δpp':>6} {'cands/f':>8}")
    for slug in sorted(res["E0"]["per_sess"].keys()):
        d0 = res["E0"]["per_sess"][slug]; d5 = res["E5"]["per_sess"][slug]
        if d0["n"] == 0: continue
        r0 = d0["hit"]/d0["n"]; r5 = d5["hit"]/d5["n"]
        print(f"  {slug:<26} {d0['n']:>4} {r0:>6.3f} {r5:>6.3f} {(r5-r0)*100:>+6.2f} {d5['cands']/d5['n']:>8.2f}")

    print(f"\n=== Per-session E1 (close3 only) vs E0 ===")
    for slug in sorted(res["E0"]["per_sess"].keys()):
        d0 = res["E0"]["per_sess"][slug]; d1 = res["E1"]["per_sess"][slug]
        if d0["n"] == 0: continue
        r0 = d0["hit"]/d0["n"]; r1 = d1["hit"]/d1["n"]
        print(f"  {slug:<26} {d0['n']:>4} {r0:>6.3f} {r1:>6.3f} {(r1-r0)*100:>+6.2f} {d1['cands']/d1['n']:>8.2f}")

    np.savez_compressed(OUT/"m2_m3_attack.npz", res=np.array(list(res.items()), dtype=object))
    print(f"\n[done] {OUT/'m2_m3_attack.npz'}")

if __name__ == "__main__":
    main()
