"""Refresh baseline on 9 done sessions (was 7 in original report).

Re-runs:
  (a) PROD vs V10 head-to-head per session (centroid <= max(10, 0.5*r_GT))
  (b) V10 failure mode breakdown M1-M5

Sessions newly included vs original 7:
  - 21af9a82_b (54 GT frames, was 1 → excluded; now valid)
  - 2546618f_b (116 GT frames, brand new)
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"
OUT.mkdir(parents=True, exist_ok=True)
M = json.loads((WS / "manifest.json").read_text())

PROD = dict(h=(105, 112), s=(140, 255), v=(40, 255), aspect=0.75, fill=0.55, area=(20, 150_000))
V10  = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.50, fill=0.35, area=(5, 150_000))

def detect(bgr, cfg):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
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
    return out, mask

def gt_centroid_radius(mask):
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return None, None
    return (float(xs.mean()), float(ys.mean())), float(np.sqrt(len(ys)/np.pi))

def adaptive_recall(cands, gtc, r):
    tol = max(10.0, 0.5*r)
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol*tol:
            return True
    return False

def classify_miss(mask_hsv_v10, gt_mask, frame_bgr):
    """Returns one of M1/M2/M3/M4/M5/HIT."""
    ys, xs = np.where(gt_mask>0)
    if len(ys) < 5: return "INVALID"
    gtc = (float(xs.mean()), float(ys.mean()))
    r = float(np.sqrt(len(ys)/np.pi))
    tol2 = max(10.0, 0.5*r)**2

    # M1: HSV cube hits 0 pixels in GT region
    hsv_in_gt = mask_hsv_v10[ys, xs].sum()
    if hsv_in_gt == 0:
        return "M1"

    # Run full V10 pipeline
    cands, _ = detect(frame_bgr, V10)
    # Look for any CC near GT
    for cx, cy, a in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return "HIT"

    # No emit-passing CC near GT. Inspect why:
    # Check whether V10 mask has any CC near GT (pre-shape-gate)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask_hsv_v10, connectivity=8)
    near_ccs = []
    for i in range(1, n):
        cx, cy = float(cents[i,0]), float(cents[i,1])
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            near_ccs.append(i)
    if not near_ccs:
        return "M2"  # HSV grabbed pixels but no CC near GT (fragmented or merged-far)
    # Some CC near GT — find which gate killed it
    best = max(near_ccs, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    a = int(stats[best, cv2.CC_STAT_AREA])
    w = int(stats[best, cv2.CC_STAT_WIDTH]); h = int(stats[best, cv2.CC_STAT_HEIGHT])
    if a < V10["area"][0]:
        return "M2"
    asp = min(w,h)/max(w,h)
    fill = a/(w*h)
    if asp < V10["aspect"]: return "M3"
    if fill < V10["fill"]: return "M4"
    # CC passed gates but not within tol — must be merged-drift
    return "M5"

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    print(f"[info] {len(items)} done sessions")

    per_sess = []
    miss_by_mode = {"M1":0, "M2":0, "M3":0, "M4":0, "M5":0}
    sat_stats = {"HIT":[], "M1":[]}
    miss_per_sess = {}
    total_n = 0
    total_prod_hit = 0
    total_v10_hit = 0

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        sess_n = 0; sess_prod = 0; sess_v10 = 0
        sess_miss = {"M1":0, "M2":0, "M3":0, "M4":0, "M5":0}
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
            sess_n += 1; total_n += 1
            cands_p, _ = detect(frame, PROD)
            cands_v, mask_v = detect(frame, V10)
            if adaptive_recall(cands_p, gtc, r): sess_prod += 1; total_prod_hit += 1
            v10_hit = adaptive_recall(cands_v, gtc, r)
            if v10_hit: sess_v10 += 1; total_v10_hit += 1
            # Classify
            mode = classify_miss(mask_v, gt, frame)
            if mode == "HIT":
                ys, xs = np.where(gt>0)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                sat_stats["HIT"].append(float(hsv[ys, xs, 1].mean()))
            elif mode in sess_miss:
                sess_miss[mode] += 1
                miss_by_mode[mode] += 1
                if mode == "M1":
                    ys, xs = np.where(gt>0)
                    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    sat_stats["M1"].append(float(hsv[ys, xs, 1].mean()))
        per_sess.append((slug, sess_n, sess_prod/sess_n if sess_n else 0, sess_v10/sess_n if sess_n else 0))
        miss_per_sess[slug] = sess_miss
        print(f"  {slug}: n={sess_n}  PROD={sess_prod/max(1,sess_n):.3f}  V10={sess_v10/max(1,sess_n):.3f}  miss={sess_miss}")

    print(f"\n=== Macro on {total_n} frames ===")
    print(f"PROD R = {total_prod_hit/total_n:.3f}")
    print(f"V10  R = {total_v10_hit/total_n:.3f}")
    print(f"Δ      = {(total_v10_hit-total_prod_hit)/total_n*100:+.2f}pp")

    miss_total = sum(miss_by_mode.values())
    print(f"\n=== V10 miss breakdown ({miss_total} miss) ===")
    for k, v in miss_by_mode.items():
        print(f"  {k}: {v} ({v/max(1,miss_total)*100:.1f}%)")

    if sat_stats["HIT"] and sat_stats["M1"]:
        print(f"\n=== Saturation (S_mean of GT region) ===")
        for k in ["HIT", "M1"]:
            s = np.array(sat_stats[k])
            print(f"  {k}: n={len(s)}  p10/p50/p90 = {np.percentile(s,10):.0f} / {np.percentile(s,50):.0f} / {np.percentile(s,90):.0f}")

    out = OUT / "refresh_9sessions.npz"
    np.savez_compressed(
        out,
        per_sess=np.array(per_sess, dtype=object),
        miss_by_mode=np.array(list(miss_by_mode.items()), dtype=object),
        miss_per_sess=np.array(list(miss_per_sess.items()), dtype=object),
        sat_hit=np.array(sat_stats["HIT"]),
        sat_m1=np.array(sat_stats["M1"]),
    )
    print(f"\n[done] {out}")

if __name__ == "__main__":
    main()
