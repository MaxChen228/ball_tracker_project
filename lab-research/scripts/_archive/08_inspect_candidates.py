"""Quick inspection: for one session, show top-K candidates per frame
vs GT centroid, and ball-CC's rank in the score-ordered list."""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"

LO = np.array([100, 100, 20], dtype=np.uint8)
HI = np.array([125, 255, 255], dtype=np.uint8)

# With and without morph close, with and without size-aware score
def detect(frame, with_close=False, score_mode="area_shape"):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, LO, HI)
    if with_close:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < 5: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h); fill = a/(w*h)
        cx, cy = float(cents[i,0]), float(cents[i,1])
        if score_mode == "area_shape":
            sc = a * (1+asp) * (1+fill)
        elif score_mode == "ball_size_prior":
            # peak around 100-400 px (typical ball area on this rig)
            target = 250
            size_pen = np.exp(-((np.log(a) - np.log(target))**2) / (2*0.6**2))
            sc = size_pen * (1+asp) * (1+fill)
        out.append((cx, cy, a, asp, fill, sc))
    out.sort(key=lambda r: -r[-1])
    return out


MANIFEST = json.loads((WS/"manifest.json").read_text())
items = [it for it in MANIFEST["items"] if it.get("propagate_status")=="done"]

for which in [items[0], items[2]]:  # 16ec069a_b, 170a6a89_b
    slug = which["slug"]; in_f = which["in_frame"]
    masks = sorted((WS/"items"/slug/"masks").glob("*.png"))[:5]
    print(f"\n=== {slug} (first 5 GT frames) ===")
    for mp in masks:
        src = int(mp.stem); local = src - in_f
        fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
        if not fp.exists(): continue
        mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
        frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        ys, xs = np.where(mask>0)
        if len(ys)<20: continue
        gx, gy, ga = float(xs.mean()), float(ys.mean()), len(ys)
        print(f"  frame {src}: GT=({gx:.0f},{gy:.0f}) area={ga}")
        for label, (close, mode) in [("no-close + area_shape", (False, "area_shape")),
                                      ("close + area_shape",   (True,  "area_shape")),
                                      ("close + size_prior",   (True,  "ball_size_prior"))]:
            cands = detect(frame, with_close=close, score_mode=mode)
            ball_rank = -1
            for i, c in enumerate(cands):
                if np.hypot(c[0]-gx, c[1]-gy) < 10:
                    ball_rank = i; break
            print(f"    [{label}] N={len(cands)}  ball_rank={ball_rank}  "
                  f"top3={[(int(c[0]),int(c[1]),c[2]) for c in cands[:3]]}")
