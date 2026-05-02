"""Head-to-head: production iOS pipeline vs proposed pipeline.

Production system (verified by reading ball_tracker/BallDetector.mm and
server/live_pairing.py):
  - iOS emits ALL candidates passing  area>=20 ∧ aspect>=0.75 ∧ fill>=0.55
  - Server's _resolve_candidates writes a winner to frame.px/py for
    DISPLAY ONLY; the actual triangulator iterates frame_a.candidates
    × frame_b.candidates pairwise. Physics gate (gap_threshold_m,
    Y-residual) filters downstream.
  → For lab eval (mono GT), the relevant per-frame recall is
    "did iOS emit at least one candidate within TOL_PX of GT centroid?"

Proposed system:
  - HSV WIDE  H[100,125] S[100,255] V[20,255]
  - Three-frame diff motion gate (lag=2, thresh=8) AND
  - morph CLOSE 5x5
  - CC, area>=5  (NO aspect/fill hard gate)
  - emit ALL passing candidates (no top-K cap, mirrors production)

Metrics, identical for both:
  R_emit   : fraction of GT frames where any emitted candidate is within
             TOL_PX of GT centroid (== "ball reaches triangulation pool")
  best_d   : px distance of nearest emitted candidate to GT centroid
  n_cand   : emitted candidates per frame (server-side load)
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask


TOL_PX = 10.0

# Production preset (data/presets/blue_ball.json + ShapeGate.default)
PROD = dict(
    h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255,
    aspect_min=0.75, fill_min=0.55,
    min_area=20, max_area=150_000,
)
# Proposed
PROP = dict(
    h_min=100, h_max=125, s_min=100, s_max=255, v_min=20, v_max=255,
    motion_lag=2, motion_thresh=8,
    close_kernel=cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    min_area=5, max_area=150_000,
)

MANIFEST = load_manifest()
items = [it for it in MANIFEST["items"] if it.get("propagate_status") == "done"]


def detect_production(frame: np.ndarray) -> list[tuple[float, float, int]]:
    """Mirror BallDetector.mm:detectAllCandidatesScratch — emit all
    blobs passing area+aspect+fill, sorted area desc."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([PROD["h_min"], PROD["s_min"], PROD["v_min"]], dtype=np.uint8)
    hi = np.array([PROD["h_max"], PROD["s_max"], PROD["v_max"]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < PROD["min_area"] or a > PROD["max_area"]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        asp = min(w, h) / max(w, h)
        if asp < PROD["aspect_min"]: continue
        fill = a / (w * h)
        if fill < PROD["fill_min"]: continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    out.sort(key=lambda c: -c[2])
    return out


def detect_proposed(frame: np.ndarray, prev_gray: np.ndarray | None):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([PROP["h_min"], PROP["s_min"], PROP["v_min"]], dtype=np.uint8)
    hi = np.array([PROP["h_max"], PROP["s_max"], PROP["v_max"]], dtype=np.uint8)
    color_mask = cv2.inRange(hsv, lo, hi)
    cur_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if prev_gray is None:
        return [], cur_gray
    diff = cv2.absdiff(cur_gray, prev_gray)
    _, motion = cv2.threshold(diff, PROP["motion_thresh"], 255, cv2.THRESH_BINARY)
    combined = cv2.bitwise_and(color_mask, motion)
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, PROP["close_kernel"])
    n, _, stats, cents = cv2.connectedComponentsWithStats(combined, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < PROP["min_area"] or a > PROP["max_area"]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0: continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    out.sort(key=lambda c: -c[2])
    return out, cur_gray


def gt_centroid(mask):
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


def best_dist(cands, gx, gy):
    if not cands: return float("inf")
    return min(np.hypot(c[0]-gx, c[1]-gy) for c in cands)


def run():
    rows_prod, rows_prop = [], []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        prev_buf: list[np.ndarray] = []
        # iterate sequentially so motion gate has temporal context
        for fp in sorted((WS / "items" / slug / "frames").glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            # advance proposed (always, to keep buffer warm)
            pg = prev_buf[-PROP["motion_lag"]] if len(prev_buf) >= PROP["motion_lag"] else None
            cands_proposed, cur_gray = detect_proposed(frame, pg)
            prev_buf.append(cur_gray)
            if len(prev_buf) > PROP["motion_lag"] + 1: prev_buf.pop(0)
            # only score on GT frames
            if src not in gt_set: continue
            mp = masks_dir / f"{src:05d}.png"
            mask = read_mask(mp)
            if mask is None or mask.shape != frame.shape[:2]: continue
            ys = np.where(mask > 0)[0]
            if len(ys) < 20: continue
            gx, gy = gt_centroid(mask)
            cands_prod = detect_production(frame)
            rows_prod.append({
                "slug": slug, "src": src,
                "n": len(cands_prod),
                "best_d": best_dist(cands_prod, gx, gy),
            })
            rows_prop.append({
                "slug": slug, "src": src,
                "n": len(cands_proposed),
                "best_d": best_dist(cands_proposed, gx, gy),
            })
    return rows_prod, rows_prop


def report(rows, label):
    n = len(rows)
    bd = np.array([r["best_d"] for r in rows])
    nc = np.array([r["n"] for r in rows])
    R = (bd <= TOL_PX).mean()
    print(f"[{label}]  n={n}  R_emit={R:.3f}  "
          f"n_cand: median={int(np.median(nc))} p50={int(np.percentile(nc,50))} p95={int(np.percentile(nc,95))} max={int(nc.max())}  "
          f"best_d: p50={np.percentile(bd[np.isfinite(bd)],50):.2f}px p95={np.percentile(bd[np.isfinite(bd)],95):.2f}px")


def main():
    t0 = time.time()
    rows_prod, rows_prop = run()
    print(f"=== {len(rows_prod)} GT frames, TOL={TOL_PX}px (centroid match) ===\n")
    report(rows_prod, "PRODUCTION  ")
    report(rows_prop, "PROPOSED    ")

    # Per-session
    print(f"\n{'session':<26}{'n':>5}  {'PROD R':>7} {'PROP R':>7}  {'PROD nc50':>9} {'PROP nc50':>9}  {'PROD nc95':>10} {'PROP nc95':>10}")
    for slug in sorted(set(r["slug"] for r in rows_prod)):
        rp = [r for r in rows_prod if r["slug"] == slug]
        rq = [r for r in rows_prop if r["slug"] == slug]
        if not rp: continue
        bdp = np.array([r["best_d"] for r in rp]); ncp = np.array([r["n"] for r in rp])
        bdq = np.array([r["best_d"] for r in rq]); ncq = np.array([r["n"] for r in rq])
        print(f"{slug:<26}{len(rp):>5d}  "
              f"{(bdp<=TOL_PX).mean():>7.3f} {(bdq<=TOL_PX).mean():>7.3f}  "
              f"{int(np.median(ncp)):>9d} {int(np.median(ncq)):>9d}  "
              f"{int(np.percentile(ncp,95)):>10d} {int(np.percentile(ncq,95)):>10d}")

    np.savez_compressed(
        OUT / "head_to_head.npz",
        prod_slug=np.array([r["slug"] for r in rows_prod]),
        prod_src=np.array([r["src"] for r in rows_prod]),
        prod_n=np.array([r["n"] for r in rows_prod]),
        prod_best_d=np.array([r["best_d"] for r in rows_prod]),
        prop_n=np.array([r["n"] for r in rows_prop]),
        prop_best_d=np.array([r["best_d"] for r in rows_prop]),
    )
    print(f"\n[saved] {OUT/'head_to_head.npz'}  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
