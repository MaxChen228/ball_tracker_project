"""Per-frame pipeline bottleneck analysis.

For every (frame, GT mask) in done sessions, run the production
HSV → CC → shape-gate pipeline (lifted from server/detection.py) and
attribute each frame to the deepest layer it survived:

  L0  GT exists (always true here)
  L1  HSV mask has any positive pixel inside GT bbox (color hit)
  L2  A CC overlapping GT exists with area in [_MIN_AREA, _MAX_AREA]
  L3  That CC passes aspect_min
  L4  That CC passes fill_min
  L5  detect_ball returns a winner whose centroid is within R px of GT centroid

Where the cliff is between Lk -> Lk+1 IS the bottleneck.

Reads HSV/shape thresholds from data/presets/blue_ball.json.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"

# Use server's detect_ball
sys.path.insert(0, str(ROOT / "server"))
from detection import HSVRange, ShapeGate, detect_ball_with_candidates  # noqa: E402

PRESET = json.loads((ROOT / "data" / "presets" / "blue_ball.json").read_text())
HSV = HSVRange(
    h_min=PRESET["hsv"]["h_min"], h_max=PRESET["hsv"]["h_max"],
    s_min=PRESET["hsv"]["s_min"], s_max=PRESET["hsv"]["s_max"],
    v_min=PRESET["hsv"]["v_min"], v_max=PRESET["hsv"]["v_max"],
)
GATE = ShapeGate(
    aspect_min=PRESET["shape_gate"]["aspect_min"],
    fill_min=PRESET["shape_gate"]["fill_min"],
)
MIN_AREA, MAX_AREA = 20, 150_000
WINNER_TOL_PX = 10.0  # how close to GT centroid counts as a "hit"

MANIFEST = json.loads((WS / "manifest.json").read_text())


def gt_centroid(mask: np.ndarray) -> tuple[float, float, int]:
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean()), len(ys)


def evaluate_frame(frame: np.ndarray, mask: np.ndarray) -> dict:
    out = {"L1": 0, "L2": 0, "L3": 0, "L4": 0, "L5": 0,
           "gt_area": 0, "winner_dist": None,
           "cc_area": 0, "cc_aspect": 0.0, "cc_fill": 0.0}
    gx, gy, gt_area = gt_centroid(mask)
    out["gt_area"] = gt_area

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    color_mask = cv2.inRange(hsv, HSV.lo(), HSV.hi())

    # L1: HSV mask has any positive pixel inside GT region
    if (color_mask & mask).any():
        out["L1"] = 1
    else:
        return out

    # Find CC overlapping GT
    n, labels, stats, cents = cv2.connectedComponentsWithStats(color_mask, connectivity=8)
    best_idx = 0; best_overlap = 0
    for i in range(1, n):
        ys = labels == i
        ov = int((ys & (mask > 0)).sum())
        if ov > best_overlap:
            best_overlap = ov; best_idx = i
    if best_idx == 0:
        return out

    area = int(stats[best_idx, cv2.CC_STAT_AREA])
    w = int(stats[best_idx, cv2.CC_STAT_WIDTH])
    h = int(stats[best_idx, cv2.CC_STAT_HEIGHT])
    asp = min(w, h) / max(w, h) if w > 0 and h > 0 else 0
    fill = area / (w * h) if w * h > 0 else 0
    out["cc_area"] = area; out["cc_aspect"] = asp; out["cc_fill"] = fill

    # L2: area in range
    if not (MIN_AREA <= area <= MAX_AREA):
        return out
    out["L2"] = 1

    # L3: aspect
    if asp < GATE.aspect_min:
        return out
    out["L3"] = 1

    # L4: fill
    if fill < GATE.fill_min:
        return out
    out["L4"] = 1

    # L5: real detect_ball winner close to GT
    winner, _ = detect_ball_with_candidates(frame, HSV, shape_gate=GATE)
    if winner is not None:
        d = float(np.hypot(winner.px - gx, winner.py - gy))
        out["winner_dist"] = d
        if d <= WINNER_TOL_PX:
            out["L5"] = 1
    return out


def main():
    t0 = time.time()
    items = [it for it in MANIFEST["items"] if it.get("propagate_status") == "done"]

    rows = []
    for item in items:
        slug = item["slug"]
        in_f = item["in_frame"]
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
            if len(ys) < 20:  # GT too small to be meaningful
                continue
            r = evaluate_frame(frame, mask)
            r["slug"] = slug; r["src"] = src
            rows.append(r)

    if not rows:
        print("[!] no rows"); return

    # Per-session breakdown
    by_session: dict[str, list[dict]] = {}
    for r in rows:
        by_session.setdefault(r["slug"], []).append(r)

    print(f"\n{'session':<26} {'frames':>6} {'L1':>5} {'L2':>5} {'L3':>5} {'L4':>5} {'L5':>5}")
    print("-" * 64)
    for slug, rs in by_session.items():
        n = len(rs)
        def pct(k): return sum(r[k] for r in rs) / n
        print(f"{slug:<26} {n:>6d} {pct('L1'):>5.2f} {pct('L2'):>5.2f} "
              f"{pct('L3'):>5.2f} {pct('L4'):>5.2f} {pct('L5'):>5.2f}")
    n = len(rows)
    def pct(k): return sum(r[k] for r in rows) / n
    print("-" * 64)
    print(f"{'OVERALL':<26} {n:>6d} {pct('L1'):>5.2f} {pct('L2'):>5.2f} "
          f"{pct('L3'):>5.2f} {pct('L4'):>5.2f} {pct('L5'):>5.2f}")

    # Bottleneck: largest drop between consecutive layers
    print("\n=== Layer drop analysis (overall) ===")
    levels = ["L1","L2","L3","L4","L5"]
    prev = 1.0
    for L in levels:
        cur = pct(L)
        print(f"  -> {L}: {cur:.3f}  (drop {prev-cur:+.3f})")
        prev = cur

    # Failure-mode breakdown for frames that failed L4 (shape gate)
    fail_at_aspect = [r for r in rows if r["L2"] == 1 and r["L3"] == 0]
    fail_at_fill   = [r for r in rows if r["L3"] == 1 and r["L4"] == 0]
    fail_at_area   = [r for r in rows if r["L1"] == 1 and r["L2"] == 0]
    print(f"\n=== Where surviving CCs die ===")
    print(f"  area-gate kills:   {len(fail_at_area):>5d}  (CC found but area outside [{MIN_AREA},{MAX_AREA}])")
    if fail_at_area:
        a = np.array([r["cc_area"] for r in fail_at_area])
        print(f"     CC area:  median={np.median(a):.0f}  p10={np.percentile(a,10):.0f}  p90={np.percentile(a,90):.0f}")
    print(f"  aspect-gate kills: {len(fail_at_aspect):>5d}  (asp < {GATE.aspect_min})")
    if fail_at_aspect:
        a = np.array([r["cc_aspect"] for r in fail_at_aspect])
        print(f"     aspect:   median={np.median(a):.3f}  p10={np.percentile(a,10):.3f}")
    print(f"  fill-gate kills:   {len(fail_at_fill):>5d}  (fill < {GATE.fill_min})")
    if fail_at_fill:
        a = np.array([r["cc_fill"] for r in fail_at_fill])
        print(f"     fill:     median={np.median(a):.3f}  p10={np.percentile(a,10):.3f}")

    # Save raw
    np.savez_compressed(
        OUT / "pipeline_bottleneck.npz",
        slugs=np.array([r["slug"] for r in rows]),
        srcs=np.array([r["src"] for r in rows]),
        L1=np.array([r["L1"] for r in rows]),
        L2=np.array([r["L2"] for r in rows]),
        L3=np.array([r["L3"] for r in rows]),
        L4=np.array([r["L4"] for r in rows]),
        L5=np.array([r["L5"] for r in rows]),
        gt_area=np.array([r["gt_area"] for r in rows]),
        cc_area=np.array([r["cc_area"] for r in rows]),
        cc_aspect=np.array([r["cc_aspect"] for r in rows]),
        cc_fill=np.array([r["cc_fill"] for r in rows]),
        winner_dist=np.array([r["winner_dist"] if r["winner_dist"] is not None else -1
                              for r in rows]),
    )
    print(f"\n[saved] {OUT/'pipeline_bottleneck.npz'}  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
