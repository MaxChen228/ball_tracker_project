"""V11 (E6) failure mode breakdown — what's left of the 9.5% miss?
Same M1/M2/M3/M4/M5 classification as 07_failure_modes.py but using
V11 config (aspect 0.40, close 3x3, min_area 3).
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"
M = json.loads((WS / "manifest.json").read_text())

V11 = dict(h=(103,118), s=(120,255), v=(30,255), aspect=0.40, fill=0.35,
           area=(3, 150_000), close=3)

def hsv_mask(hsv, cfg):
    lo = np.array([cfg["h"][0], cfg["s"][0], cfg["v"][0]], dtype=np.uint8)
    hi = np.array([cfg["h"][1], cfg["s"][1], cfg["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    if cfg["close"] > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg["close"], cfg["close"]))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return m

def detect(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = hsv_mask(hsv, V11)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < V11["aspect"]: continue
        fill = a/(w*h)
        if fill < V11["fill"]: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    return out, m

def gt_centroid_radius(mask):
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return None, None
    return (float(xs.mean()), float(ys.mean())), float(np.sqrt(len(ys)/np.pi))

def classify(mask_v11, gt_mask, frame_bgr, cands):
    ys, xs = np.where(gt_mask>0)
    gtc = (float(xs.mean()), float(ys.mean()))
    r = float(np.sqrt(len(ys)/np.pi))
    tol2 = max(10.0, 0.5*r)**2
    if mask_v11[ys, xs].sum() == 0:
        return "M1", gtc, r
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return "HIT", gtc, r
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask_v11, connectivity=8)
    near = []
    for i in range(1, n):
        cx, cy = float(cents[i,0]), float(cents[i,1])
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            near.append(i)
    if not near:
        return "M2", gtc, r
    best = max(near, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    a = int(stats[best, cv2.CC_STAT_AREA])
    w = int(stats[best, cv2.CC_STAT_WIDTH]); h = int(stats[best, cv2.CC_STAT_HEIGHT])
    if a < V11["area"][0]: return "M2", gtc, r
    asp = min(w,h)/max(w,h)
    fill = a/(w*h)
    if asp < V11["aspect"]: return "M3", gtc, r
    if fill < V11["fill"]: return "M4", gtc, r
    return "M5", gtc, r

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    miss = {"M1":0, "M2":0, "M3":0, "M4":0, "M5":0}
    miss_per_sess = {it["slug"]:{"M1":0,"M2":0,"M3":0,"M4":0,"M5":0} for it in items}
    n_total = 0; n_hit = 0
    sat_hit = []; sat_m1 = []; sat_m2 = []; sat_m3 = []
    m1_hue_hist = []
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
            n_total += 1
            cands, m_v11 = detect(frame)
            mode, gtc, r = classify(m_v11, gt, frame, cands)
            if mode == "HIT":
                n_hit += 1
                ys, xs = np.where(gt>0)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                sat_hit.append(float(hsv[ys, xs, 1].mean()))
            else:
                miss[mode] += 1
                miss_per_sess[slug][mode] += 1
                ys, xs = np.where(gt>0)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                s_mean = float(hsv[ys, xs, 1].mean())
                if mode == "M1":
                    sat_m1.append(s_mean)
                    m1_hue_hist.append(float(np.median(hsv[ys, xs, 0])))
                elif mode == "M2": sat_m2.append(s_mean)
                elif mode == "M3": sat_m3.append(s_mean)

    print(f"=== V11 on {n_total} frames ===")
    print(f"recall = {n_hit/n_total:.3f} ({n_hit}/{n_total})")
    miss_total = sum(miss.values())
    print(f"\n=== V11 miss breakdown ({miss_total} miss) ===")
    for k, v in miss.items():
        print(f"  {k}: {v} ({v/max(1,miss_total)*100:.1f}%)")

    print(f"\n=== Per-session miss ===")
    for slug, d in sorted(miss_per_sess.items()):
        if sum(d.values()) == 0: continue
        print(f"  {slug:<26} {d}")

    print(f"\n=== Saturation (S_mean of GT region) ===")
    for k, arr in [("HIT", sat_hit), ("M1", sat_m1), ("M2", sat_m2), ("M3", sat_m3)]:
        if arr:
            a = np.array(arr)
            print(f"  {k:<4} n={len(a):>4}  S p10/p50/p90 = {np.percentile(a,10):.0f} / {np.percentile(a,50):.0f} / {np.percentile(a,90):.0f}")

    if m1_hue_hist:
        print(f"\n=== M1 GT hue median per frame (n={len(m1_hue_hist)}) ===")
        a = np.array(m1_hue_hist)
        print(f"  H_med p10/p50/p90 = {np.percentile(a,10):.0f} / {np.percentile(a,50):.0f} / {np.percentile(a,90):.0f}")

    np.savez_compressed(OUT/"v11_failure_modes.npz",
        miss=np.array(list(miss.items()), dtype=object),
        miss_per_sess=np.array(list(miss_per_sess.items()), dtype=object),
        sat_hit=np.array(sat_hit), sat_m1=np.array(sat_m1),
        sat_m2=np.array(sat_m2), sat_m3=np.array(sat_m3))
    print(f"\n[done] {OUT/'v11_failure_modes.npz'}")

if __name__ == "__main__":
    main()
