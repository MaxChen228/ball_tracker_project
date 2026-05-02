"""End-to-end evaluation of the proposed iOS loose pipeline.

Pipeline under test:
  HSV WIDE cube  H[100,125] S[100,255] V[20,255]
  → connectedComponentsWithStats
  → for each CC: area >= 5, no aspect/fill gate
  → score = area * (1+aspect) * (1+fill)
  → top-K=20 candidates per frame

Metrics:
  - per-frame recall: did any of top-K land within 10 px of GT centroid?
  - per-frame top-1 recall (would the single highest-score CC suffice?)
  - candidate count distribution (= noise estimate before CC; after CC is small)
  - centroid error of best-of-top-K
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"

# proposed wide cube
HLO, SLO, VLO = 100, 100, 20
HHI, SHI, VHI = 125, 255, 255
LO = np.array([HLO, SLO, VLO], dtype=np.uint8)
HI = np.array([HHI, SHI, VHI], dtype=np.uint8)

MIN_AREA = 5
TOPK = 20
TOL = 10.0  # px

MANIFEST = json.loads((WS / "manifest.json").read_text())
items = [it for it in MANIFEST["items"] if it.get("propagate_status") == "done"]


def detect(frame: np.ndarray) -> list[dict]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, LO, HI)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cands = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < MIN_AREA:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        asp = min(w, h) / max(w, h)
        fill = a / (w * h)
        cands.append({
            "px": float(cents[i, 0]), "py": float(cents[i, 1]),
            "area": a, "aspect": asp, "fill": fill,
            "score": a * (1 + asp) * (1 + fill),
        })
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:TOPK]


def gt_centroid(mask: np.ndarray):
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean()), len(ys)


def main():
    t0 = time.time()
    rows_per_frame = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks"
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem)
            local = src - in_f
            fp = WS / "items" / slug / "frames" / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]:
                continue
            ys = np.where(mask > 0)[0]
            if len(ys) < 20:
                continue
            gx, gy, ga = gt_centroid(mask)
            cands = detect(frame)
            # any-of-K hit?
            best_d = float("inf"); best_rank = -1
            for rank, c in enumerate(cands):
                d = float(np.hypot(c["px"] - gx, c["py"] - gy))
                if d < best_d:
                    best_d = d; best_rank = rank
            top1_d = float(np.hypot(cands[0]["px"]-gx, cands[0]["py"]-gy)) if cands else float("inf")
            rows_per_frame.append({
                "slug": slug, "src": src, "n_cand": len(cands),
                "best_d": best_d, "best_rank": best_rank,
                "top1_d": top1_d, "gt_area": ga,
            })

    n = len(rows_per_frame)
    arr_best = np.array([r["best_d"] for r in rows_per_frame])
    arr_top1 = np.array([r["top1_d"] for r in rows_per_frame])
    arr_ncand = np.array([r["n_cand"] for r in rows_per_frame])
    arr_rank = np.array([r["best_rank"] for r in rows_per_frame])

    print(f"=== Loose iOS pipeline simulation — {n} GT frames ===\n")
    print(f"per-frame any-of-top-{TOPK} recall (≤{TOL}px): {(arr_best<=TOL).mean():.3f}")
    print(f"per-frame top-1 recall (≤{TOL}px):              {(arr_top1<=TOL).mean():.3f}")
    print(f"per-frame top-1 recall (≤5px):                {(arr_top1<=5).mean():.3f}")
    print(f"per-frame top-1 recall (≤20px):               {(arr_top1<=20).mean():.3f}")

    print(f"\ncandidate count per frame:")
    print(f"  min={arr_ncand.min()}  p25={np.percentile(arr_ncand,25):.0f}  median={np.median(arr_ncand):.0f}  "
          f"p75={np.percentile(arr_ncand,75):.0f}  max={arr_ncand.max()}")
    print(f"  fraction zero-cand: {(arr_ncand==0).mean():.3f}")

    print(f"\nbest-cand rank distribution (when found):")
    found = arr_rank[arr_best <= TOL]
    if len(found):
        for r in [0, 1, 2, 5, 10, 19]:
            print(f"  rank<={r}: {(found<=r).mean():.3f}")

    print(f"\ncentroid error of best-cand (when found ≤{TOL}px):")
    found_d = arr_best[arr_best <= TOL]
    if len(found_d):
        print(f"  mean={found_d.mean():.2f}px  median={np.median(found_d):.2f}px  p95={np.percentile(found_d,95):.2f}px")

    print("\n=== Per-session ===")
    print(f"{'session':<26}{'n':>5}{'recall@K':>10}{'top1':>8}{'ncand_p50':>11}{'ncand_p95':>11}")
    for slug in sorted(set(r["slug"] for r in rows_per_frame)):
        rs = [r for r in rows_per_frame if r["slug"]==slug]
        bb = np.array([r["best_d"] for r in rs])
        nc = np.array([r["n_cand"] for r in rs])
        t1 = np.array([r["top1_d"] for r in rs])
        print(f"{slug:<26}{len(rs):>5d}{(bb<=TOL).mean():>10.3f}{(t1<=TOL).mean():>8.3f}"
              f"{np.median(nc):>11.0f}{np.percentile(nc,95):>11.0f}")

    np.savez_compressed(
        OUT / "loose_pipeline.npz",
        slugs=np.array([r["slug"] for r in rows_per_frame]),
        srcs=np.array([r["src"] for r in rows_per_frame]),
        best_d=arr_best, top1_d=arr_top1, n_cand=arr_ncand, best_rank=arr_rank,
    )
    print(f"\n[saved] {OUT/'loose_pipeline.npz'}  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
