"""CLAHE pre-processing experiments.

Hypothesis: desaturated balls (M1/M2) might be recovered if we boost
local contrast before HSV thresholding.

Variants applied to V11 (E6) baseline:
  C0  V11 baseline                                  (control)
  C1  CLAHE on V channel only                       (HSV → CLAHE V → back)
  C2  CLAHE on L channel (Lab)                      (Lab → CLAHE L → back)
  C3  S-channel multiplicative stretch (S *= 1.5, clip 255)  cheap saturation boost
  C4  C1 + C3                                        (V CLAHE + S stretch)

Latency cost noted per variant.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"
M = json.loads((WS / "manifest.json").read_text())

V11 = dict(h=(103,118), s=(120,255), v=(30,255), aspect=0.40, fill=0.35,
           area=(3, 150_000), close=3)

CLAHE_V = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
CLAHE_L = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

def preproc(bgr, mode):
    if mode == "C0":
        return bgr
    if mode == "C1":
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hsv[..., 2] = CLAHE_V.apply(hsv[..., 2])
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if mode == "C2":
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        lab[..., 0] = CLAHE_L.apply(lab[..., 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    if mode == "C3":
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hsv[..., 1] = np.clip(hsv[..., 1].astype(np.int32) * 3 // 2, 0, 255).astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    if mode == "C4":
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        hsv[..., 2] = CLAHE_V.apply(hsv[..., 2])
        hsv[..., 1] = np.clip(hsv[..., 1].astype(np.int32) * 3 // 2, 0, 255).astype(np.uint8)
        return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    raise ValueError(mode)

def detect(bgr_post):
    hsv = cv2.cvtColor(bgr_post, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    if V11["close"] > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
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

def hit(cands, gtc, r):
    tol2 = max(10.0, 0.5*r)**2
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return True
    return False

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    modes = ["C0", "C1", "C2", "C3", "C4"]
    res = {m: {"hit":0, "per_sess":{it["slug"]:{"n":0,"hit":0} for it in items}, "ms_total":0.0, "n_frames":0} for m in modes}
    n_total = 0
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
            for m in modes:
                t0 = time.perf_counter()
                pp = preproc(frame, m)
                cands = detect(pp)
                t1 = time.perf_counter()
                res[m]["ms_total"] += (t1-t0)*1000
                res[m]["n_frames"] += 1
                if hit(cands, gtc, r):
                    res[m]["hit"] += 1
                    res[m]["per_sess"][slug]["hit"] += 1
                res[m]["per_sess"][slug]["n"] += 1

    base = res["C0"]["hit"]
    print(f"=== CLAHE preproc ({n_total} frames) ===")
    print(f"{'V':<3} {'recall':>7} {'Δpp':>6} {'ms/frame':>9}")
    for m in modes:
        nr = res[m]["hit"]/n_total
        delta = (res[m]["hit"]-base)/n_total*100
        ms = res[m]["ms_total"]/res[m]["n_frames"]
        print(f"{m:<3} {nr:>7.3f} {delta:>+6.2f} {ms:>9.2f}")

    print(f"\n=== Per-session C1 (CLAHE V) vs C0 ===")
    for slug in sorted(res["C0"]["per_sess"].keys()):
        d0 = res["C0"]["per_sess"][slug]; d1 = res["C1"]["per_sess"][slug]
        if d0["n"] == 0: continue
        r0 = d0["hit"]/d0["n"]; r1 = d1["hit"]/d1["n"]
        print(f"  {slug:<26} n={d0['n']:>4} C0={r0:.3f} C1={r1:.3f} Δ={(r1-r0)*100:+.2f}pp")

    print(f"\n=== Per-session C4 (CLAHE V + S stretch) vs C0 ===")
    for slug in sorted(res["C0"]["per_sess"].keys()):
        d0 = res["C0"]["per_sess"][slug]; d4 = res["C4"]["per_sess"][slug]
        if d0["n"] == 0: continue
        r0 = d0["hit"]/d0["n"]; r4 = d4["hit"]/d4["n"]
        print(f"  {slug:<26} n={d0['n']:>4} C0={r0:.3f} C4={r4:.3f} Δ={(r4-r0)*100:+.2f}pp")

    np.savez_compressed(OUT/"clahe_preproc.npz", res=np.array(list(res.items()), dtype=object))
    print(f"\n[done] {OUT/'clahe_preproc.npz'}")

if __name__ == "__main__":
    main()
