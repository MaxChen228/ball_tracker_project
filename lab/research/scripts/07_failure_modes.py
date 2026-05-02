"""V10 residual failure mechanism analysis.

For frames V10 misses (best_d > 10px), classify the failure cause:

  M1  HSV upstream miss: GT region has zero pixels passing V10 cube
      → ball color is genuinely outside the cube (deep shadow, color cast)
  M2  HSV passes but no CC ≥ min_area in GT region
      → ball pixels too sparse / fragmented to form a ≥5 px CC
  M3  CC exists in GT region but fails aspect gate (<0.50)
      → motion blur or partial-occlusion elongated mask
  M4  CC exists, passes aspect, fails fill (<0.35)
      → pseudo-ball with a hole / non-convex
  M5  CC passes all gates but its centroid > 10 px from GT centroid
      → ball merged with adjacent same-color object, centroid pulled

Also profile by GT mask features:
  - GT area (small/medium/large)
  - GT mean V (lit / shadowed)
  - GT mean S (saturated / desaturated)
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS

EXCLUDE_SESSIONS = {"session_s_21af9a82_b"}
TOL = 10.0

V10 = dict(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255,
           aspect_min=0.50, fill_min=0.35, min_area=5)
LO = np.array([V10["h_min"], V10["s_min"], V10["v_min"]], dtype=np.uint8)
HI = np.array([V10["h_max"], V10["s_max"], V10["v_max"]], dtype=np.uint8)


def mask_suspect(mask, sess_med):
    a = int((mask>0).sum())
    if a < 20: return True
    n, _, _, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n - 1 > 1: return True
    if a > 3 * sess_med: return True
    ys, xs = np.where(mask>0)
    w = xs.max()-xs.min()+1; h = ys.max()-ys.min()+1
    asp = min(w,h)/max(w,h) if max(w,h)>0 else 0
    if asp < 0.4: return True
    fill = a/(w*h) if w*h>0 else 0
    if fill < 0.45: return True
    return False


def classify(frame, mask, gx, gy):
    """Return (mode, info)."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    color_mask = cv2.inRange(hsv, LO, HI)
    gt_bool = mask > 0
    n_inside = int(((color_mask > 0) & gt_bool).sum())
    if n_inside == 0:
        return "M1_HSV_miss", {"n_in_gt": 0}

    # Find CC in color_mask that maximally overlaps GT
    n, labels, stats, cents = cv2.connectedComponentsWithStats(color_mask, 8)
    best_idx = 0; best_overlap = 0
    for i in range(1, n):
        ov = int(((labels == i) & gt_bool).sum())
        if ov > best_overlap: best_overlap = ov; best_idx = i
    if best_idx == 0 or stats[best_idx, cv2.CC_STAT_AREA] < V10["min_area"]:
        return "M2_no_CC_in_GT", {"largest_overlap_cc_area": int(stats[best_idx, cv2.CC_STAT_AREA]) if best_idx else 0}

    a = int(stats[best_idx, cv2.CC_STAT_AREA])
    w = int(stats[best_idx, cv2.CC_STAT_WIDTH]); h = int(stats[best_idx, cv2.CC_STAT_HEIGHT])
    asp = min(w,h)/max(w,h) if max(w,h)>0 else 0
    fill = a/(w*h) if w*h>0 else 0
    cx, cy = float(cents[best_idx, 0]), float(cents[best_idx, 1])
    d = float(np.hypot(cx - gx, cy - gy))

    if asp < V10["aspect_min"]:
        return "M3_aspect_fail", {"aspect": asp, "area": a, "fill": fill}
    if fill < V10["fill_min"]:
        return "M4_fill_fail", {"fill": fill, "area": a, "aspect": asp}
    return "M5_centroid_drift", {"d": d, "area": a, "aspect": asp, "fill": fill}


def detect(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, LO, HI)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V10["min_area"] or a > 150_000: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < V10["aspect_min"]: continue
        fill = a/(w*h)
        if fill < V10["fill_min"]: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    return out


def gt_features(frame, mask):
    ys, xs = np.where(mask>0)
    if len(ys) == 0: return None
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    H = hsv[ys, xs, 0]; S = hsv[ys, xs, 1]; V = hsv[ys, xs, 2]
    return {
        "area": len(ys),
        "H_mean": float(H.mean()), "S_mean": float(S.mean()), "V_mean": float(V.mean()),
        "H_std":  float(H.std()),  "S_std":  float(S.std()),  "V_std":  float(V.std()),
    }


def main():
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"]
             if it.get("propagate_status")=="done"
             and it["slug"] not in EXCLUDE_SESSIONS]

    fails = []  # rows for V10 miss frames
    hits = []   # rows for V10 hit frames
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks"
        areas = [int((cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)>0).sum())
                 for p in sorted(masks_dir.glob("*.png"))
                 if cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) is not None]
        areas = [a for a in areas if a >= 20]
        sess_med = float(np.median(areas)) if areas else 0
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]: continue
            if mask_suspect(mask, sess_med): continue
            ys = np.where(mask>0)
            if len(ys[0]) < 20: continue
            gx, gy = float(ys[1].mean()), float(ys[0].mean())
            cands = detect(frame)
            d = min((np.hypot(c[0]-gx, c[1]-gy) for c in cands), default=float("inf"))
            feats = gt_features(frame, mask)
            row = {"slug": slug, "src": src, "d": d, **feats}
            if d <= TOL:
                hits.append(row)
            else:
                mode, info = classify(frame, mask, gx, gy)
                row["mode"] = mode; row["info"] = info
                fails.append(row)

    n_total = len(hits) + len(fails)
    print(f"V10 on {n_total} clean frames: {len(hits)} hit ({len(hits)/n_total:.3f}), {len(fails)} miss ({len(fails)/n_total:.3f})\n")

    print("=== Failure mode breakdown ===")
    from collections import Counter
    c = Counter(r["mode"] for r in fails)
    for mode in ["M1_HSV_miss", "M2_no_CC_in_GT", "M3_aspect_fail", "M4_fill_fail", "M5_centroid_drift"]:
        n = c[mode]
        print(f"  {mode:<22} {n:>4d}  ({n/len(fails)*100:>5.1f}% of misses, {n/n_total*100:>5.1f}% of all frames)")

    print("\n=== HSV stats: hit vs miss vs M1-only ===")
    def stats_block(rows, label):
        if not rows: return
        for k in ("H_mean", "S_mean", "V_mean", "area"):
            arr = np.array([r[k] for r in rows])
            print(f"  {label:<14} {k:<8} p10={np.percentile(arr,10):>6.1f}  p50={np.percentile(arr,50):>6.1f}  p90={np.percentile(arr,90):>6.1f}")
    stats_block(hits, "HIT")
    stats_block(fails, "MISS")
    m1_only = [r for r in fails if r["mode"] == "M1_HSV_miss"]
    stats_block(m1_only, "M1 (HSV miss)")

    # Per-session miss profile
    print("\n=== Per-session miss-mode distribution ===")
    for slug in sorted({r["slug"] for r in fails + hits}):
        sub = [r for r in fails if r["slug"]==slug]
        n_sess = sum(1 for r in fails+hits if r["slug"]==slug)
        if not sub:
            print(f"  {slug:<26} miss=0/{n_sess}"); continue
        cc = Counter(r["mode"] for r in sub)
        line = f"  {slug:<26} miss={len(sub):>3d}/{n_sess:<3d}  "
        line += " ".join(f"{m.split('_')[0]}={cc[m]}" for m in
                         ["M1_HSV_miss","M2_no_CC_in_GT","M3_aspect_fail","M4_fill_fail","M5_centroid_drift"]
                         if cc[m] > 0)
        print(line)


if __name__ == "__main__":
    main()
