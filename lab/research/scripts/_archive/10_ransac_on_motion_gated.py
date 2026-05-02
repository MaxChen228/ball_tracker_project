"""RANSAC ballistic on motion-gated candidates.

Reads each session's full local frame range, runs motion-gated detect,
then RANSAC-fits 2D ballistic across the trajectory window.
Reports per-frame recall after RANSAC (chosen inlier or model interpolation).
"""
from __future__ import annotations
import json, time, sys
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
MOTION_THRESH = 8
MOTION_LAG = 2
CLOSE_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
SIZE_TARGET = 250.0
SIZE_SIGMA = 0.7

INLIER_TOL = 6.0
RANSAC_ITERS = 1500
RANSAC_SAMPLES = 5
GRAVITY_AY_MAX = 0.05
MIN_AX_PXPF = 1.0
MIN_DISP_PX = 200.0
SAMPLE_FROM_RANK = 5  # restrict sampling to top-K_sample per frame
SEED = 0


def detect(frame_bgr, prev_gray):
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
        size_pen = float(np.exp(-((np.log(a)-np.log(SIZE_TARGET))**2)/(2*SIZE_SIGMA**2)))
        out.append([float(cents[i,0]), float(cents[i,1]), size_pen*(1+asp)*(1+fill)])
    out.sort(key=lambda r: -r[2])
    return np.array(out[:TOPK]) if out else np.empty((0,3)), cur_gray


def fit_ballistic(t, x, y):
    Ax = np.column_stack([t, np.ones_like(t)])
    cx, *_ = np.linalg.lstsq(Ax, x, rcond=None)
    Ay = np.column_stack([t*t, t, np.ones_like(t)])
    cy, *_ = np.linalg.lstsq(Ay, y, rcond=None)
    return cx, cy


def predict(cx, cy, t):
    return cx[0]*t + cx[1], cy[0]*t*t + cy[1]*t + cy[2]


def ransac(frames, rng):
    valid = [(t, c) for t, c in frames if len(c) > 0]
    if len(valid) < RANSAC_SAMPLES:
        return None, [], 0.0
    t0 = float(np.mean([t for t, _ in valid]))
    def to_t(t): return float(t - t0)
    best_inliers, best_model = [], None
    n_valid = len(valid)
    for _ in range(RANSAC_ITERS):
        idx_s = rng.choice(n_valid, RANSAC_SAMPLES, replace=False)
        ts = np.array([to_t(valid[i][0]) for i in idx_s])
        cidx = []
        for i in idx_s:
            n_cand = len(valid[i][1])
            top = min(SAMPLE_FROM_RANK, n_cand)
            cidx.append(int(rng.integers(0, top)))
        xs = np.array([valid[i][1][cidx[k],0] for k,i in enumerate(idx_s)])
        ys = np.array([valid[i][1][cidx[k],1] for k,i in enumerate(idx_s)])
        try:
            cx, cy = fit_ballistic(ts, xs, ys)
        except np.linalg.LinAlgError: continue
        if abs(cy[0]) > GRAVITY_AY_MAX or abs(cx[0]) < MIN_AX_PXPF: continue
        t_lo = min(to_t(v[0]) for v in valid); t_hi = max(to_t(v[0]) for v in valid)
        p_lo = predict(cx, cy, t_lo); p_hi = predict(cx, cy, t_hi)
        if np.hypot(p_hi[0]-p_lo[0], p_hi[1]-p_lo[1]) < MIN_DISP_PX: continue
        inliers = []
        for tr, cands in valid:
            t = to_t(tr); px,py = predict(cx, cy, t)
            d = np.hypot(cands[:,0]-px, cands[:,1]-py)
            j = int(np.argmin(d))
            if d[j] <= INLIER_TOL:
                inliers.append((tr, float(cands[j,0]), float(cands[j,1])))
        if len(inliers) > len(best_inliers):
            best_inliers, best_model = inliers, (cx, cy)
    if best_inliers and len(best_inliers) >= 4:
        ts = np.array([to_t(r[0]) for r in best_inliers])
        xs = np.array([r[1] for r in best_inliers]); ys = np.array([r[2] for r in best_inliers])
        cx, cy = fit_ballistic(ts, xs, ys)
        ref = []
        for tr, cands in valid:
            t = to_t(tr); px,py = predict(cx, cy, t)
            d = np.hypot(cands[:,0]-px, cands[:,1]-py)
            j = int(np.argmin(d))
            if d[j] <= INLIER_TOL:
                ref.append((tr, float(cands[j,0]), float(cands[j,1])))
        return (cx, cy), ref, t0
    return best_model, best_inliers, t0


def main():
    rng = np.random.default_rng(SEED)
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"] if it.get("propagate_status")=="done"]

    print(f"=== RANSAC on motion-gated stream  TOL={INLIER_TOL}px  iters={RANSAC_ITERS} ===\n")
    print(f"{'session':<26}{'gt':>5}{'inl':>5}{'recall':>8}{'mean_err':>10}{'p95_err':>9}{'top1_iOS':>10}")

    macro_recall, macro_top1 = [], []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks"
        gt_srcs = sorted(int(p.stem) for p in masks_dir.glob("*.png"))
        if len(gt_srcs) < 5: continue
        prev_buf = []
        per_frame, gt_by_t, top1_hits = [], {}, 0
        gt_total = 0
        for fp in sorted((WS/"items"/slug/"frames").glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            pg = prev_buf[-MOTION_LAG] if len(prev_buf)>=MOTION_LAG else None
            cands, cg = detect(frame, pg)
            prev_buf.append(cg)
            if len(prev_buf) > MOTION_LAG+1: prev_buf.pop(0)
            per_frame.append((src, cands))
            if src in gt_srcs:
                mp = masks_dir/f"{src:05d}.png"
                mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
                if mask is None or mask.shape != frame.shape[:2]: continue
                ys = np.where(mask>0)[0]
                if len(ys)<20: continue
                ymask = np.where(mask>0)
                gx, gy = float(ymask[1].mean()), float(ymask[0].mean())
                gt_by_t[src] = (gx, gy)
                gt_total += 1
                if len(cands) > 0:
                    d0 = float(np.hypot(cands[0,0]-gx, cands[0,1]-gy))
                    if d0 <= 10: top1_hits += 1
        if gt_total == 0: continue
        top1_rate = top1_hits / gt_total
        macro_top1.append(top1_rate)

        model, inliers, t0 = ransac(per_frame, rng)
        if model is None or not inliers:
            print(f"{slug:<26}{gt_total:>5d}    NO MODEL")
            macro_recall.append(0.0); continue
        cx, cy = model
        inlier_t = {r[0]: (r[1], r[2]) for r in inliers}
        n_rec, errs = 0, []
        for tr, (gx, gy) in gt_by_t.items():
            if tr in inlier_t:
                px, py = inlier_t[tr]
            else:
                px_, py_ = predict(cx, cy, tr - t0); px, py = float(px_), float(py_)
            d = float(np.hypot(px-gx, py-gy)); errs.append(d)
            if d <= 10: n_rec += 1
        recall = n_rec / gt_total
        macro_recall.append(recall)
        e = np.array(errs)
        print(f"{slug:<26}{gt_total:>5d}{len(inliers):>5d}{recall:>8.3f}"
              f"{e.mean():>10.2f}{np.percentile(e,95):>9.2f}{top1_rate:>10.3f}")

    print(f"\nMACRO recall after RANSAC : {np.mean(macro_recall):.3f}")
    print(f"MACRO top-1 (no RANSAC):     {np.mean(macro_top1):.3f}")


if __name__ == "__main__":
    main()
