"""Find balanced operating point.

Production trade: R_emit=0.688, median 1 cand, p95 3 cands.
Proposed full: R_emit=0.895, median 25 cands, p95 116 cands.

Sweep variants between them to find: highest R_emit subject to
median nc <= 5 (server triangulation pair count manageable).

Variants:
 V1  Wide HSV, no motion, NO hard gate, area>=5
 V2  Wide HSV, motion, NO hard gate, area>=5         (= proposed)
 V3  Wide HSV, motion, soft gate aspect>=0.5 fill>=0.35 area>=5
 V4  Wide HSV, motion, mid gate aspect>=0.65 fill>=0.45 area>=10
 V5  Wide HSV, motion, prod gate aspect>=0.75 fill>=0.55 area>=20
 V6  Prod HSV, motion, NO hard gate, area>=5         (HSV unchanged, motion only)
 V7  Prod HSV, motion, prod gate                     (additive motion gate)
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"
TOL_PX = 10.0

WIDE = dict(h_min=100, h_max=125, s_min=100, s_max=255, v_min=20, v_max=255)
NARROW = dict(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255)
KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

MID = dict(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255)
VARIANTS = [
    ("V0_PROD",            NARROW, False, 20, 0.75, 0.55, False),
    ("V1_wide_no_motion",  WIDE,   False, 5,  0.0,  0.0,  False),
    ("V2_wide_motion",     WIDE,   True,  5,  0.0,  0.0,  True),
    ("V3_wide_mot_soft",   WIDE,   True,  5,  0.50, 0.35, True),
    ("V6_narrow_motion",   NARROW, True,  5,  0.0,  0.0,  True),
    ("V8_narrow_mot_soft", NARROW, True,  5,  0.50, 0.35, True),
    ("V9_mid_mot_soft",    MID,    True,  5,  0.50, 0.35, True),
    ("V10_mid_no_motion",  MID,    False, 5,  0.50, 0.35, False),
]


def detect(frame, cube, use_motion, min_area, asp_min, fill_min, do_close, prev_gray):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([cube["h_min"], cube["s_min"], cube["v_min"]], dtype=np.uint8)
    hi = np.array([cube["h_max"], cube["s_max"], cube["v_max"]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    cur_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if use_motion else None
    if use_motion:
        if prev_gray is None:
            return [], cur_gray
        diff = cv2.absdiff(cur_gray, prev_gray)
        _, mot = cv2.threshold(diff, 8, 255, cv2.THRESH_BINARY)
        mask = cv2.bitwise_and(mask, mot)
    if do_close:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, KERNEL)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < min_area or a > 150_000: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < asp_min: continue
        fill = a/(w*h)
        if fill < fill_min: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    return out, cur_gray


def gt_centroid(mask):
    ys, xs = np.where(mask>0); return float(xs.mean()), float(ys.mean())


def main():
    t0 = time.time()
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"] if it.get("propagate_status")=="done"]
    results = {name: [] for name, *_ in VARIANTS}

    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks"
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        prev_buf: list[np.ndarray] = []
        for fp in sorted((WS/"items"/slug/"frames").glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            pg = prev_buf[-2] if len(prev_buf) >= 2 else None
            cur_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            prev_buf.append(cur_gray)
            if len(prev_buf) > 3: prev_buf.pop(0)

            if src not in gt_set: continue
            mp = masks_dir/f"{src:05d}.png"
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if mask is None or mask.shape != frame.shape[:2]: continue
            ys = np.where(mask>0)[0]
            if len(ys) < 20: continue
            gx, gy = gt_centroid(mask)
            for name, cube, use_mot, mn_a, asp_mn, fill_mn, do_close in VARIANTS:
                cands, _ = detect(frame, cube, use_mot, mn_a, asp_mn, fill_mn, do_close, pg)
                bd = min((np.hypot(c[0]-gx, c[1]-gy) for c in cands), default=float("inf"))
                results[name].append({"slug": slug, "n": len(cands), "best_d": bd})

    print(f"=== variant sweep, {len(next(iter(results.values())))} GT frames ===\n")
    print(f"{'variant':<22}{'R_emit':>8}{'n_p50':>7}{'n_p95':>7}{'n_max':>7}{'d_p50':>8}{'d_p95':>8}")
    for name, _, *_ in VARIANTS:
        rs = results[name]
        bd = np.array([r["best_d"] for r in rs])
        nc = np.array([r["n"] for r in rs])
        R = (bd<=TOL_PX).mean()
        finite = bd[np.isfinite(bd)]
        d50 = np.percentile(finite, 50) if len(finite) else float("nan")
        d95 = np.percentile(finite, 95) if len(finite) else float("nan")
        print(f"{name:<22}{R:>8.3f}{int(np.percentile(nc,50)):>7d}{int(np.percentile(nc,95)):>7d}"
              f"{int(nc.max()):>7d}{d50:>8.2f}{d95:>8.2f}")

    # Per-session: PROD vs all interesting variants
    KEY_VARIANTS = ["V0_PROD", "V1_wide_no_motion", "V8_narrow_mot_soft", "V9_mid_mot_soft", "V10_mid_no_motion"]
    print(f"\n=== Per-session R_emit ===")
    hdr = f"{'session':<26}{'n':>5}"
    for v in KEY_VARIANTS: hdr += f"{v.replace('_','-')[:13]:>14}"
    print(hdr)
    for slug in sorted({r["slug"] for r in results["V0_PROD"]}):
        line = f"{slug:<26}{len([r for r in results['V0_PROD'] if r['slug']==slug]):>5d}"
        for v in KEY_VARIANTS:
            rs = [r for r in results[v] if r["slug"]==slug]
            r = (np.array([rr["best_d"] for rr in rs])<=TOL_PX).mean() if rs else 0
            line += f"{r:>14.3f}"
        print(line)

    # Diagnose 22d1835e_b regression: split into lit vs shadowed?
    print(f"\n=== 22d1835e_b: motion gate failure inspection ===")
    rb = [r for r in results["V2_wide_motion"] if r["slug"]=="session_s_22d1835e_b"]
    rb_no_mot = [r for r in results["V1_wide_no_motion"] if r["slug"]=="session_s_22d1835e_b"]
    miss = [(rb[i]["best_d"], rb_no_mot[i]["best_d"]) for i in range(len(rb))]
    motion_kills = sum(1 for d_mot, d_no in miss if d_mot > TOL_PX and d_no <= TOL_PX)
    print(f"  frames where wide+no_motion HIT but wide+motion MISS: {motion_kills}/{len(rb)}")
    print(f"  → these are slow-moving frames where |I_t-I_{{t-2}}| < 8 (motion gate killed real ball)")

    print(f"\n[done] {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
