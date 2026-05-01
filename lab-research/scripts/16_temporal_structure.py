"""Analyze temporal structure of V11 misses on 170a6a89_b + 21af9a82_a.

Question: Are misses isolated (1-2 frame gaps with hit neighbours) or
clustered (long runs of consecutive misses)?
  - Isolated → temporal anchor (prev frame ROI) can rescue
  - Clustered → temporal anchor fails after gap > N frames

For each session, dump per-frame HIT/MISS sequence + run-length stats.
Also: for each MISS frame, distance (in frames) to nearest HIT.
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

def gt_centroid_radius(mask):
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return None, None
    return (float(xs.mean()), float(ys.mean())), float(np.sqrt(len(ys)/np.pi))

def hit_test(cands, gtc, r):
    tol2 = max(10.0, 0.5*r)**2
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return True
    return False

def runs(seq, val):
    """Return list of run lengths for value val in seq."""
    out = []
    cur = 0
    for x in seq:
        if x == val:
            cur += 1
        else:
            if cur > 0: out.append(cur)
            cur = 0
    if cur > 0: out.append(cur)
    return out

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    target = ["session_s_170a6a89_b", "session_s_21af9a82_a"]
    for it in items:
        slug = it["slug"]
        if slug not in target: continue
        in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        seq = []  # 1=HIT, 0=MISS
        gt_centroids = []  # (frame_idx, cx, cy) for hit frames
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
            cands = detect(frame)
            h = hit_test(cands, gtc, r)
            seq.append(1 if h else 0)
            if h: gt_centroids.append((len(seq)-1, gtc[0], gtc[1]))

        seq = np.array(seq)
        n = len(seq); n_hit = int(seq.sum()); n_miss = n - n_hit
        miss_runs = runs(seq, 0)
        hit_runs = runs(seq, 1)
        print(f"\n=== {slug} (n={n}) ===")
        print(f"  hits={n_hit} miss={n_miss} R={n_hit/n:.3f}")
        print(f"  miss runs: {miss_runs}")
        print(f"    p50/p90/max = {np.percentile(miss_runs,50):.0f} / {np.percentile(miss_runs,90):.0f} / {max(miss_runs)}")
        print(f"  hit runs: count={len(hit_runs)}")
        if hit_runs:
            print(f"    p50/p90/max = {np.percentile(hit_runs,50):.0f} / {np.percentile(hit_runs,90):.0f} / {max(hit_runs)}")

        # For each miss frame, distance to nearest hit frame
        miss_idx = np.where(seq==0)[0]
        hit_idx = np.where(seq==1)[0]
        if len(hit_idx) > 0 and len(miss_idx) > 0:
            d = np.array([min(abs(mi - hit_idx)) for mi in miss_idx])
            print(f"  per-miss distance to nearest hit:  p50/p75/p90/max = {np.percentile(d,50):.0f} / {np.percentile(d,75):.0f} / {np.percentile(d,90):.0f} / {d.max()}")
            for thresh in [1, 2, 3, 5, 10]:
                pct = (d <= thresh).mean()
                print(f"    miss within {thresh} frames of hit: {pct*100:.1f}%")

        # Also visualize the sequence (compact)
        s = "".join("." if x==1 else "X" for x in seq)
        for i in range(0, len(s), 100):
            print(f"  [{i:4d}] {s[i:i+100]}")

if __name__ == "__main__":
    main()
