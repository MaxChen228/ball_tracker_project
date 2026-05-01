"""RANSAC ballistic trajectory cleanup on noisy candidate stream.

Per session:
  - Run loose-pipeline detection on every GT frame -> candidates list per frame.
  - Run RANSAC: fit 2D ballistic model
       x(t) = ax*t + bx
       y(t) = ay*t^2 + by*t + cy
    where t = source frame index (constant frame interval).
  - Inlier: candidate (px,py) within INLIER_TOL of prediction at t.
  - Best model = max inliers.

Metrics:
  - trajectory recall (per frame chosen inlier within 10 px of GT centroid)
  - inlier rate (= per-frame frames where any candidate is inlier)
  - centroid err of chosen inlier vs GT
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"

HLO, SLO, VLO = 100, 100, 20
HHI, SHI, VHI = 125, 255, 255
LO = np.array([HLO, SLO, VLO], dtype=np.uint8)
HI = np.array([HHI, SHI, VHI], dtype=np.uint8)
MIN_AREA = 5
TOPK = 20
INLIER_TOL = 6.0  # px around model prediction
RANSAC_ITERS = 1500
RANSAC_SAMPLES = 5
GRAVITY_AY_MAX = 0.05
MIN_TRAJ_DISPLACEMENT_PX = 200.0  # span(t_min,t_max) Euclidean must exceed this — kills static-blob degenerate fits
MIN_AX_PXPF = 1.0  # min |horizontal velocity| px/frame
SEED = 0

MANIFEST = json.loads((WS / "manifest.json").read_text())
items = [it for it in MANIFEST["items"] if it.get("propagate_status") == "done"]


def detect(frame: np.ndarray) -> np.ndarray:
    """Return (N,3): [px, py, score] for top-K CCs."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, LO, HI)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < MIN_AREA: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        asp = min(w, h) / max(w, h)
        fill = a / (w * h)
        out.append((float(cents[i,0]), float(cents[i,1]),
                    a * (1+asp) * (1+fill)))
    out.sort(key=lambda r: -r[2])
    return np.array(out[:TOPK]) if out else np.empty((0,3))


def fit_ballistic(t: np.ndarray, x: np.ndarray, y: np.ndarray):
    """Fit x = ax*t + bx, y = ay*t^2 + by*t + cy. Returns coeff dict."""
    Ax = np.column_stack([t, np.ones_like(t)])
    cx, *_ = np.linalg.lstsq(Ax, x, rcond=None)
    Ay = np.column_stack([t*t, t, np.ones_like(t)])
    cy, *_ = np.linalg.lstsq(Ay, y, rcond=None)
    return cx, cy


def predict(cx, cy, t):
    px = cx[0]*t + cx[1]
    py = cy[0]*t*t + cy[1]*t + cy[2]
    return px, py


def ransac_trajectory(frames: list[tuple[int, np.ndarray]], rng: np.random.Generator):
    """frames: list of (t_raw, candidates(N,3)).
    Recenter t before fitting. Constrain ay to gravity-realistic range."""
    valid = [(t, c) for t, c in frames if len(c) > 0]
    if len(valid) < RANSAC_SAMPLES:
        return None, [], 0.0
    t0 = float(np.mean([t for t, _ in valid]))  # recenter

    def to_t(t_raw): return float(t_raw - t0)

    best_inliers = []
    best_model = None
    n_valid = len(valid)
    for _ in range(RANSAC_ITERS):
        idx_s = rng.choice(n_valid, RANSAC_SAMPLES, replace=False)
        ts = np.array([to_t(valid[i][0]) for i in idx_s])
        cand_idx = [int(rng.integers(0, len(valid[i][1]))) for i in idx_s]
        xs = np.array([valid[i][1][cand_idx[k], 0] for k, i in enumerate(idx_s)])
        ys = np.array([valid[i][1][cand_idx[k], 1] for k, i in enumerate(idx_s)])
        try:
            cx, cy = fit_ballistic(ts, xs, ys)
        except np.linalg.LinAlgError:
            continue
        if abs(cy[0]) > GRAVITY_AY_MAX:
            continue
        if abs(cx[0]) < MIN_AX_PXPF:
            continue  # static / quasi-static fit
        # Check displacement over the GT time span
        t_lo = min(to_t(v[0]) for v in valid)
        t_hi = max(to_t(v[0]) for v in valid)
        p_lo = predict(cx, cy, t_lo); p_hi = predict(cx, cy, t_hi)
        if np.hypot(p_hi[0]-p_lo[0], p_hi[1]-p_lo[1]) < MIN_TRAJ_DISPLACEMENT_PX:
            continue
        inliers = []
        for t_raw, cands in valid:
            t = to_t(t_raw)
            px, py = predict(cx, cy, t)
            d = np.hypot(cands[:,0]-px, cands[:,1]-py)
            j = int(np.argmin(d))
            if d[j] <= INLIER_TOL:
                inliers.append((t_raw, float(cands[j,0]), float(cands[j,1]), float(d[j])))
        if len(inliers) > len(best_inliers):
            best_inliers = inliers; best_model = (cx, cy)
    # Refit on all inliers
    if best_inliers and len(best_inliers) >= 4:
        ts = np.array([to_t(r[0]) for r in best_inliers])
        xs = np.array([r[1] for r in best_inliers])
        ys = np.array([r[2] for r in best_inliers])
        cx, cy = fit_ballistic(ts, xs, ys)
        refined = []
        for t_raw, cands in valid:
            t = to_t(t_raw)
            px, py = predict(cx, cy, t)
            d = np.hypot(cands[:,0]-px, cands[:,1]-py)
            j = int(np.argmin(d))
            if d[j] <= INLIER_TOL:
                refined.append((t_raw, float(cands[j,0]), float(cands[j,1]), float(d[j])))
        return (cx, cy), refined, t0
    return best_model, best_inliers, t0


def main():
    rng = np.random.default_rng(SEED)
    print(f"=== RANSAC ballistic — INLIER_TOL={INLIER_TOL}px  ITERS={RANSAC_ITERS} ===\n")
    print(f"{'session':<26}{'gt':>5}{'inliers':>9}{'recall':>8}{'mean_err':>10}{'p95_err':>9}")

    all_recall = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks"
        # Build (t, cands), GT centroids per frame
        per_frame = []
        gt_by_t = {}
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem)
            local = src - in_f
            fp = WS / "items" / slug / "frames" / f"{local:05d}.jpg"
            if not fp.exists(): continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]:
                continue
            ys = np.where(mask > 0)[0]
            if len(ys) < 20: continue
            ymask = np.where(mask > 0)
            gx, gy = float(ymask[1].mean()), float(ymask[0].mean())
            gt_by_t[src] = (gx, gy)
            per_frame.append((src, detect(frame)))
        if len(per_frame) < 4:
            continue

        model, inliers, t0 = ransac_trajectory(per_frame, rng)
        if model is None or not inliers:
            print(f"{slug:<26}{len(gt_by_t):>5d}    NO MODEL")
            continue

        cx, cy = model
        n_gt = len(gt_by_t)
        n_recovered = 0
        errs = []
        inlier_t = {r[0]: (r[1], r[2]) for r in inliers}
        for t_raw, (gx, gy) in gt_by_t.items():
            if t_raw in inlier_t:
                px, py = inlier_t[t_raw]
            else:
                px_, py_ = predict(cx, cy, t_raw - t0)
                px, py = float(px_), float(py_)
            d = float(np.hypot(px - gx, py - gy))
            errs.append(d)
            if d <= 10:
                n_recovered += 1
        recall = n_recovered / n_gt
        all_recall.append(recall)
        e = np.array(errs)
        print(f"{slug:<26}{n_gt:>5d}{len(inliers):>9d}{recall:>8.3f}{e.mean():>10.2f}{np.percentile(e,95):>9.2f}")

    print(f"\nMACRO recall: {np.mean(all_recall):.3f}")


if __name__ == "__main__":
    main()
