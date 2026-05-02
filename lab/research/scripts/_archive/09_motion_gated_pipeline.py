"""Motion-gated loose pipeline.

Add temporal frame difference to kill static blue clutter:
  motion = |I_t - I_{t-2}| > MOTION_THRESH    (grayscale)
  combined = HSV(wide cube) AND morph_close AND motion
  → CC → top-K=20 candidates

Re-measure per-frame recall + ball rank.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"

LO = np.array([100, 100, 20], dtype=np.uint8)
HI = np.array([125, 255, 255], dtype=np.uint8)
MIN_AREA = 5
TOPK = 20
TOL = 10.0
MOTION_THRESH = 8         # pixel-diff threshold (grayscale 0-255)
MOTION_LAG = 2            # use t-2 for diff (5x at 240fps = 8.3ms apart)
CLOSE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
SIZE_TARGET = 250.0
SIZE_SIGMA = 0.7  # log-space


def detect_motion(frame_bgr: np.ndarray, prev_gray: np.ndarray | None) -> list[dict]:
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    color_mask = cv2.inRange(hsv, LO, HI)
    cur_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    if prev_gray is None:
        return [], cur_gray
    diff = cv2.absdiff(cur_gray, prev_gray)
    motion_mask = (diff > MOTION_THRESH).astype(np.uint8) * 255
    combined = cv2.bitwise_and(color_mask, motion_mask)
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, CLOSE_KERNEL)
    n, _, stats, cents = cv2.connectedComponentsWithStats(combined, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < MIN_AREA: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h); fill = a/(w*h)
        cx, cy = float(cents[i,0]), float(cents[i,1])
        size_pen = float(np.exp(-((np.log(a) - np.log(SIZE_TARGET))**2) / (2*SIZE_SIGMA**2)))
        out.append({"px":cx, "py":cy, "area":a, "aspect":asp, "fill":fill,
                    "score": size_pen * (1+asp) * (1+fill)})
    out.sort(key=lambda c: -c["score"])
    return out[:TOPK], cur_gray


def gt_centroid(mask):
    ys, xs = np.where(mask>0)
    return float(xs.mean()), float(ys.mean()), len(ys)


def main():
    t0 = time.time()
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"] if it.get("propagate_status")=="done"]
    rows = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        # Build sequential frame stream (must include frames before/after GT for prev_gray)
        masks_dir = WS/"items"/slug/"masks"
        gt_srcs = sorted(int(p.stem) for p in masks_dir.glob("*.png"))
        if not gt_srcs: continue
        # We need source frames in [min(gt)-MOTION_LAG, max(gt)] — but we only have local frames in [in_frame, out_frame]
        # local_idx = src - in_frame
        prev_buf: list[np.ndarray] = []
        local_files = sorted((WS/"items"/slug/"frames").glob("*.jpg"))
        for fp in local_files:
            local = int(fp.stem)
            src = local + in_f
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            prev_gray = prev_buf[-MOTION_LAG] if len(prev_buf) >= MOTION_LAG else None
            cands, cur_gray = detect_motion(frame, prev_gray)
            prev_buf.append(cur_gray)
            if len(prev_buf) > MOTION_LAG + 1:
                prev_buf.pop(0)
            # Only score on GT frames
            if src not in set(gt_srcs):
                continue
            mp = masks_dir / f"{src:05d}.png"
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if mask is None or mask.shape != frame.shape[:2]: continue
            ys = np.where(mask>0)[0]
            if len(ys) < 20: continue
            gx, gy, ga = gt_centroid(mask)
            best_d = float("inf"); best_rank = -1
            for rank, c in enumerate(cands):
                d = float(np.hypot(c["px"]-gx, c["py"]-gy))
                if d < best_d: best_d=d; best_rank=rank
            top1_d = float(np.hypot(cands[0]["px"]-gx, cands[0]["py"]-gy)) if cands else float("inf")
            rows.append({"slug":slug, "src":src, "n":len(cands), "best_d":best_d,
                         "best_rank":best_rank, "top1_d":top1_d, "gt_area":ga})
    n = len(rows)
    arr_b = np.array([r["best_d"] for r in rows])
    arr_t1 = np.array([r["top1_d"] for r in rows])
    arr_n = np.array([r["n"] for r in rows])
    arr_r = np.array([r["best_rank"] for r in rows])
    print(f"=== Motion-gated loose pipeline — {n} GT frames ===\n")
    print(f"per-frame any-of-top-{TOPK} recall (≤{TOL}px): {(arr_b<=TOL).mean():.3f}")
    print(f"per-frame top-1 recall (≤{TOL}px):              {(arr_t1<=TOL).mean():.3f}")
    print(f"per-frame top-3 recall:                       {(np.where(arr_r>=0,arr_r,99)<=2).mean():.3f}")
    print(f"per-frame top-5 recall:                       {(np.where(arr_r>=0,arr_r,99)<=4).mean():.3f}")
    print(f"\ncandidate count: median={int(np.median(arr_n))}  p95={int(np.percentile(arr_n,95))}  p25={int(np.percentile(arr_n,25))}  zeros={(arr_n==0).mean():.3f}")
    print("\n=== Per-session ===")
    print(f"{'session':<26}{'n':>5}{'recK':>7}{'top1':>7}{'top3':>7}{'ncand_p50':>11}")
    for slug in sorted(set(r["slug"] for r in rows)):
        rs = [r for r in rows if r["slug"]==slug]
        b = np.array([r["best_d"] for r in rs]); t1 = np.array([r["top1_d"] for r in rs])
        nc = np.array([r["n"] for r in rs]); rr = np.array([r["best_rank"] for r in rs])
        print(f"{slug:<26}{len(rs):>5d}{(b<=TOL).mean():>7.3f}{(t1<=TOL).mean():>7.3f}"
              f"{(np.where(rr>=0,rr,99)<=2).mean():>7.3f}{int(np.median(nc)):>11d}")
    np.savez_compressed(OUT/"motion_gated.npz",
                        slugs=np.array([r["slug"] for r in rows]),
                        srcs=np.array([r["src"] for r in rows]),
                        best_d=arr_b, top1_d=arr_t1, n=arr_n, best_rank=arr_r)
    print(f"\n[saved] {OUT/'motion_gated.npz'}  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
