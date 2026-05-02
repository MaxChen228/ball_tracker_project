"""SAM2 mask quality audit.

For each GT mask, compute:
  - area
  - bbox aspect (min(w,h)/max(w,h))  → 真球趨近 1.0
  - fill (area / bbox area)            → 真球趨近 π/4 ≈ 0.785
  - n_components: connected components of mask
                  > 1 表示 mask 多塊（SAM2 propagation 噴出去）
  - convex hull deficit (1 - area/hull_area) → 凹陷=黏到別的東西
  - centroid drift between consecutive GT frames (px)
                  超過 ball_radius × N 表示 mask 跳

輸出每 session 分布 + flag suspect frames。
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT


MANIFEST = json.loads((WS / "manifest.json").read_text())
items = [it for it in MANIFEST["items"] if it.get("propagate_status") == "done"]


def mask_stats(mask: np.ndarray) -> dict:
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return {"area": 0}
    n_comp, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    largest_idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) if n_comp > 1 else 0
    largest_area = int(stats[largest_idx, cv2.CC_STAT_AREA]) if n_comp > 1 else 0
    total_area = int((mask > 0).sum())

    # bbox of all-mask
    x_min, y_min, x_max, y_max = xs.min(), ys.min(), xs.max(), ys.max()
    w, h = x_max - x_min + 1, y_max - y_min + 1
    aspect = min(w, h) / max(w, h) if max(w, h) > 0 else 0
    fill = total_area / (w * h) if w * h > 0 else 0
    cx, cy = float(xs.mean()), float(ys.mean())

    # convex hull deficit
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    hull_area = 0
    for cnt in contours:
        hull = cv2.convexHull(cnt)
        hull_area += cv2.contourArea(hull)
    hull_deficit = 1 - total_area / hull_area if hull_area > 0 else 0.0

    return {
        "area": total_area,
        "n_comp": n_comp - 1,                # exclude bg
        "largest_frac": largest_area / total_area if total_area > 0 else 0,
        "aspect": aspect,
        "fill": fill,
        "hull_deficit": hull_deficit,
        "cx": cx, "cy": cy,
    }


def main():
    print(f"{'session':<26}{'n':>4}  {'area_p5':>8}{'area_p50':>9}{'area_p95':>9}  "
          f"{'asp_p5':>7}{'asp_p50':>8}  {'fill_p5':>8}{'fill_p50':>9}  "
          f"{'hull_p95':>9}{'multi_cc':>9}{'cx_jump':>8}")
    suspect_total = 0
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        rows = []
        for mp in masks:
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if mask is None: continue
            ys = np.where(mask>0)[0]
            if len(ys) < 5: continue
            s = mask_stats(mask); s["src"] = int(mp.stem)
            rows.append(s)
        if not rows: continue
        a = np.array([r["area"] for r in rows])
        asp = np.array([r["aspect"] for r in rows])
        fill = np.array([r["fill"] for r in rows])
        hd = np.array([r["hull_deficit"] for r in rows])
        nc = np.array([r["n_comp"] for r in rows])
        # centroid drift between consecutive masks (px)
        cx = np.array([r["cx"] for r in rows]); cy = np.array([r["cy"] for r in rows])
        srcs = np.array([r["src"] for r in rows])
        drifts = []
        for i in range(1, len(rows)):
            if srcs[i] - srcs[i-1] <= 3:  # only for nearly-consecutive frames
                drifts.append(np.hypot(cx[i]-cx[i-1], cy[i]-cy[i-1]))
        drift_p95 = np.percentile(drifts, 95) if drifts else 0.0
        multi_cc_frac = (nc > 1).mean()

        # suspect criteria:
        # area > 3× session median  OR  aspect < 0.4  OR  fill < 0.45  OR n_comp > 1
        med_a = np.median(a)
        suspect = ((a > 3 * med_a) | (asp < 0.4) | (fill < 0.45) | (nc > 1))
        suspect_n = int(suspect.sum())
        suspect_total += suspect_n
        print(f"{slug:<26}{len(rows):>4d}  "
              f"{int(np.percentile(a,5)):>8d}{int(np.percentile(a,50)):>9d}{int(np.percentile(a,95)):>9d}  "
              f"{np.percentile(asp,5):>7.3f}{np.percentile(asp,50):>8.3f}  "
              f"{np.percentile(fill,5):>8.3f}{np.percentile(fill,50):>9.3f}  "
              f"{np.percentile(hd,95):>9.3f}{multi_cc_frac:>9.3f}{drift_p95:>8.1f}  "
              f"suspect={suspect_n}")
    print(f"\nTotal suspect frames across all sessions: {suspect_total}")
    print("\nSuspect criteria: area > 3× session-median OR aspect < 0.4 OR fill < 0.45 OR n_comp > 1")
    print("(reasons mask might be merged with clutter / drifted / fragmented)")


if __name__ == "__main__":
    main()
