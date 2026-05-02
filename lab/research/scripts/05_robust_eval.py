"""Robust head-to-head, three layers of mask-aware evaluation:

  Layer A — drop session_s_21af9a82_b (broken, user-flagged)
  Layer B — drop suspect mask frames (audit script 13 criteria)
  Layer C — IoU-based metric in addition to centroid distance

Compares PROD vs V10 across all three layers + per-session.

Suspect criteria (from script 13):
  area > 3× session-median  OR  bbox aspect < 0.4
  OR  fill < 0.45  OR  n_components > 1

IoU metric: for each GT mask, find the detector candidate's
"local mask" (small disk of radius √(cand.area/π) at the candidate
centroid) — actually we approximate by computing the detector CC
mask's overlap with GT mask. To avoid re-running detect inside the
loop, we fall back to: IoU(detector_disk_at_centroid, gt_mask).
Disk radius taken from candidate.area.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT


EXCLUDE_SESSIONS = {"session_s_21af9a82_b"}

PROD = dict(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255,
            aspect_min=0.75, fill_min=0.55, min_area=20)
V10 = dict(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255,
           aspect_min=0.50, fill_min=0.35, min_area=5)


def detect(frame, cfg):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h_min"], cfg["s_min"], cfg["v_min"]], dtype=np.uint8)
    hi = np.array([cfg["h_max"], cfg["s_max"], cfg["v_max"]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["min_area"] or a > 150_000: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < cfg["aspect_min"]: continue
        fill = a/(w*h)
        if fill < cfg["fill_min"]: continue
        # cc_mask = (labels == i) — collect lazily
        out.append({
            "px": float(cents[i,0]), "py": float(cents[i,1]),
            "area": a, "label_idx": i,
        })
    return out, labels


def mask_quality_suspect(mask: np.ndarray, session_median_area: float) -> bool:
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return True
    n_comp, _, _, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n_comp - 1 > 1: return True
    area = (mask>0).sum()
    if area > 3 * session_median_area: return True
    x0,x1,y0,y1 = xs.min(), xs.max(), ys.min(), ys.max()
    w = x1-x0+1; h = y1-y0+1
    asp = min(w,h)/max(w,h) if max(w,h)>0 else 0
    fill = area/(w*h) if w*h>0 else 0
    if asp < 0.4: return True
    if fill < 0.45: return True
    return False


def gt_centroid_and_radius(mask):
    ys, xs = np.where(mask>0)
    cx, cy = float(xs.mean()), float(ys.mean())
    r = float(np.sqrt(len(ys) / np.pi))
    return cx, cy, r


def best_candidate_distance(cands, gx, gy):
    if not cands: return float("inf")
    return min(np.hypot(c["px"]-gx, c["py"]-gy) for c in cands)


def best_iou(cands, labels, gt_mask):
    """Highest IoU among candidate CCs vs GT mask."""
    if not cands: return 0.0
    gt_bool = gt_mask > 0
    best = 0.0
    for c in cands:
        cc = labels == c["label_idx"]
        inter = int((cc & gt_bool).sum())
        if inter == 0: continue
        union = int(cc.sum() + gt_bool.sum() - inter)
        iou = inter / union if union > 0 else 0
        if iou > best: best = iou
    return best


def main():
    t0 = time.time()
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"]
             if it.get("propagate_status")=="done"
             and it["slug"] not in EXCLUDE_SESSIONS]

    rows = []  # one entry per (session, src) — has all three metric flavors for both pipelines
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks"
        # session-median area for suspect criterion — only over frames
        # with non-trivial mask (≥20 px). SAM2 propagation may leave
        # empty/tiny masks where it lost lock; including those in median
        # makes median=0 and breaks the >3× threshold for everyone.
        areas = []
        for mp in sorted(masks_dir.glob("*.png")):
            mk = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if mk is None: continue
            a = int((mk > 0).sum())
            if a >= 20:
                areas.append(a)
        if not areas: continue
        sess_median_area = float(np.median(areas))

        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]: continue
            ys = np.where(mask>0)[0]
            if len(ys) < 20: continue
            suspect = mask_quality_suspect(mask, sess_median_area)
            gx, gy, r_gt = gt_centroid_and_radius(mask)
            adaptive_tol = max(10.0, 0.5 * r_gt)

            row = {"slug": slug, "src": src, "suspect": suspect,
                   "r_gt": r_gt, "tol_fixed": 10.0, "tol_adaptive": adaptive_tol}

            for tag, cfg in [("prod", PROD), ("v10", V10)]:
                cands, labels = detect(frame, cfg)
                d = best_candidate_distance(cands, gx, gy)
                iou = best_iou(cands, labels, mask)
                row[f"{tag}_d"] = d
                row[f"{tag}_iou"] = iou
                row[f"{tag}_n"] = len(cands)
            rows.append(row)

    n_total = len(rows)
    n_clean = sum(1 for r in rows if not r["suspect"])
    print(f"=== Robust eval — {len(items)} sessions (excl. {sorted(EXCLUDE_SESSIONS)}) ===")
    print(f"Total GT frames: {n_total}, clean (non-suspect): {n_clean}\n")

    def report(filter_fn, tol_key, label):
        sub = [r for r in rows if filter_fn(r)]
        if not sub: return
        n = len(sub)
        for tag in ("prod", "v10"):
            d = np.array([r[f"{tag}_d"] for r in sub])
            iou = np.array([r[f"{tag}_iou"] for r in sub])
            tol = np.array([r[tol_key] for r in sub])
            R_d = (d <= tol).mean()
            R_iou3 = (iou >= 0.3).mean()
            R_iou5 = (iou >= 0.5).mean()
            iou_d_p50 = float(np.percentile(d[np.isfinite(d)], 50)) if (d[np.isfinite(d)]).size else float('nan')
            print(f"  [{label}] {tag.upper():<5} n={n}  "
                  f"R_dist={R_d:.3f}  R_IoU≥.3={R_iou3:.3f}  R_IoU≥.5={R_iou5:.3f}  "
                  f"d_p50={iou_d_p50:.2f}px  IoU_p50={float(np.percentile(iou,50)):.3f}")

    print("Layer A — all frames, fixed tol=10px:")
    report(lambda r: True, "tol_fixed", "ALL")
    print("\nLayer B — clean frames only (drop suspect masks), fixed tol=10px:")
    report(lambda r: not r["suspect"], "tol_fixed", "CLEAN")
    print("\nLayer C — clean frames, ADAPTIVE tol = max(10, 0.5×r_gt):")
    report(lambda r: not r["suspect"], "tol_adaptive", "CLEAN+ADAPT")

    print("\n=== Per-session R_dist (clean frames, adaptive tol) ===")
    print(f"{'session':<26}{'n_clean':>9}{'PROD':>8}{'V10':>8}{'Δ':>8}")
    for slug in sorted({r["slug"] for r in rows}):
        sub = [r for r in rows if r["slug"]==slug and not r["suspect"]]
        if not sub: continue
        n = len(sub)
        dp = np.array([r["prod_d"] for r in sub]); tv = np.array([r["tol_adaptive"] for r in sub])
        dv = np.array([r["v10_d"] for r in sub])
        Rp = (dp <= tv).mean(); Rv = (dv <= tv).mean()
        print(f"{slug:<26}{n:>9d}{Rp:>8.3f}{Rv:>8.3f}{Rv-Rp:>+8.3f}")

    print(f"\n[done] {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
