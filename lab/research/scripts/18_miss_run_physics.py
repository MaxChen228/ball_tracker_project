"""Physical attribution of 170a6a89_b 31-frame miss run.

Per-frame dump:
  - HIT/MISS (V11)
  - GT region mean (B, G, R, H, S, V)
  - Frame-global mean V (brightness proxy — capture-side AE hint)
  - Frame-global mean S
  - GT bbox size (proxies depth: larger = closer = motion-blur faster)

Goal: find capture-side signal that predicts miss-run onset.
Output: CSV per session + summary stats correlating MISS with each
predictor.
"""
from __future__ import annotations
import csv, json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT

M = json.loads((WS / "manifest.json").read_text())

V11 = dict(h=(103,118), s=(120,255), v=(30,255), aspect=0.40, fill=0.35,
           area=(3, 150_000), close=3)

def detect(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    if V11["close"] > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
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
    return out

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    target = ["session_s_170a6a89_b", "session_s_21af9a82_a"]
    rows_all = []
    for it in items:
        slug = it["slug"]
        if slug not in target: continue
        in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        rows = []
        for mp in masks:
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if gt is None: continue
            ball_in = (gt>0).sum() >= 5
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            global_v = float(hsv[..., 2].mean())
            global_s = float(hsv[..., 1].mean())
            global_v_std = float(hsv[..., 2].std())
            row = dict(src=src, local=local, ball_in=int(ball_in),
                       global_v=global_v, global_s=global_s, global_v_std=global_v_std)
            if ball_in:
                ys, xs = np.where(gt>0)
                gt_h = float(hsv[ys, xs, 0].mean())
                gt_s = float(hsv[ys, xs, 1].mean())
                gt_v = float(hsv[ys, xs, 2].mean())
                gt_b = float(frame[ys, xs, 0].mean())
                gt_g = float(frame[ys, xs, 1].mean())
                gt_r = float(frame[ys, xs, 2].mean())
                gt_area = int(len(ys))
                w = int(xs.max()-xs.min()+1); h = int(ys.max()-ys.min()+1)
                gt_bbox_w = w; gt_bbox_h = h
                gt_aspect_axis = min(w,h)/max(w,h)
                cands = detect(frame)
                gtc_x = float(xs.mean()); gtc_y = float(ys.mean())
                r = float(np.sqrt(gt_area/np.pi))
                tol2 = max(10.0, 0.5*r)**2
                hit = any(((cx-gtc_x)**2+(cy-gtc_y)**2) <= tol2 for cx,cy,_ in cands)
                row.update(gt_h=gt_h, gt_s=gt_s, gt_v=gt_v,
                           gt_b=gt_b, gt_g=gt_g, gt_r=gt_r,
                           gt_area=gt_area, gt_bbox_w=gt_bbox_w, gt_bbox_h=gt_bbox_h,
                           gt_aspect_axis=gt_aspect_axis, hit=int(hit))
            else:
                row.update(gt_h=None, gt_s=None, gt_v=None,
                           gt_b=None, gt_g=None, gt_r=None,
                           gt_area=0, gt_bbox_w=0, gt_bbox_h=0,
                           gt_aspect_axis=None, hit=None)
            rows.append(row)
            rows_all.append(dict(slug=slug, **row))

        # Per-session miss run analysis
        valid = [r for r in rows if r["hit"] is not None]
        seq = np.array([r["hit"] for r in valid])
        n = len(seq); n_hit = int(seq.sum()); n_miss = n - n_hit
        print(f"\n=== {slug}: paired={len(rows)} ball_in={n} hits={n_hit} miss={n_miss} R={n_hit/max(1,n):.3f} ===")

        # Compare HIT vs MISS distributions on key features
        miss_rows = [r for r in valid if r["hit"]==0]
        hit_rows = [r for r in valid if r["hit"]==1]
        if miss_rows and hit_rows:
            print(f"  Feature              HIT_p50      MISS_p50    Δ")
            for f in ["gt_s", "gt_v", "gt_h", "global_v", "global_s", "global_v_std",
                       "gt_area", "gt_aspect_axis"]:
                hv = np.array([r[f] for r in hit_rows if r[f] is not None])
                mv = np.array([r[f] for r in miss_rows if r[f] is not None])
                if len(hv)==0 or len(mv)==0: continue
                p50_h = np.percentile(hv, 50); p50_m = np.percentile(mv, 50)
                print(f"    {f:<18} {p50_h:>10.2f}  {p50_m:>10.2f}  {p50_m-p50_h:>+8.2f}")

        # Find runs of misses + look at frame BEFORE run starts
        runs = []
        cur_start = None
        for i, h in enumerate(seq):
            if h == 0 and cur_start is None:
                cur_start = i
            elif h == 1 and cur_start is not None:
                runs.append((cur_start, i))
                cur_start = None
        if cur_start is not None:
            runs.append((cur_start, len(seq)))
        print(f"  miss runs: {[(b-a, b-a) for (a,b) in [(r[0], r[1]) for r in runs]]}")
        print(f"  Long runs (>=5 frames):")
        for a, b in runs:
            if b-a < 5: continue
            run_len = b-a
            # Look at gt stats during run + 5 frames before
            run_rows = valid[a:b]
            pre_rows = valid[max(0,a-5):a]
            run_gt_s = np.mean([r["gt_s"] for r in run_rows if r["gt_s"] is not None])
            run_gt_v = np.mean([r["gt_v"] for r in run_rows if r["gt_v"] is not None])
            run_glob_v = np.mean([r["global_v"] for r in run_rows])
            pre_gt_s = np.mean([r["gt_s"] for r in pre_rows if r["gt_s"] is not None]) if pre_rows else float('nan')
            pre_gt_v = np.mean([r["gt_v"] for r in pre_rows if r["gt_v"] is not None]) if pre_rows else float('nan')
            pre_glob_v = np.mean([r["global_v"] for r in pre_rows]) if pre_rows else float('nan')
            print(f"    run [{a:3d},{b:3d}] len={run_len:2d}  pre→run gt_s {pre_gt_s:.0f}→{run_gt_s:.0f}  gt_v {pre_gt_v:.0f}→{run_gt_v:.0f}  glob_v {pre_glob_v:.0f}→{run_glob_v:.0f}")

    # Write CSV
    csv_p = OUT/"miss_run_physics.csv"
    keys = ["slug","src","local","ball_in","hit","gt_h","gt_s","gt_v","gt_b","gt_g","gt_r",
            "gt_area","gt_bbox_w","gt_bbox_h","gt_aspect_axis",
            "global_v","global_s","global_v_std"]
    with open(csv_p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows_all:
            w.writerow({k: r.get(k) for k in keys})
    print(f"\n[done] {csv_p}")

if __name__ == "__main__":
    main()
