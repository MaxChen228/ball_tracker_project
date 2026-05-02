"""Ablation: attribute the +18pp gain (PROD→V10) to individual parameter changes.

PROD = (HSV[105,112][140,255][40,255], aspect>=0.75, fill>=0.55, area>=20)
V10  = (HSV[103,118][120,255][30,255], aspect>=0.50, fill>=0.35, area>=5)

Five 1-D paths PROD→V10 (each step changes ONE axis), measure marginal
ΔR_emit. Then a leave-one-out: V10 with one axis reverted to PROD, to
show how essential each change is.

Sample: 857 clean frames (suspect masks dropped, broken session excluded).
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, load_manifest, SEG_BY_SLUG, read_mask

EXCLUDE_SESSIONS = {"session_s_21af9a82_b"}
TOL = 10.0

PROD = dict(h_min=105, h_max=112, s_min=140, s_max=255, v_min=40, v_max=255,
            aspect_min=0.75, fill_min=0.55, min_area=20)
V10  = dict(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255,
            aspect_min=0.50, fill_min=0.35, min_area=5)

# 5 axes that differ between PROD and V10
AXES = {
    "HSV_H":   ("h_min", "h_max"),
    "HSV_S":   ("s_min",),
    "HSV_V":   ("v_min",),
    "aspect":  ("aspect_min",),
    "fill":    ("fill_min",),
    "area":    ("min_area",),
}


def detect(frame, cfg):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h_min"], cfg["s_min"], cfg["v_min"]], dtype=np.uint8)
    hi = np.array([cfg["h_max"], cfg["s_max"], cfg["v_max"]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
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
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    return out


def mask_suspect(mask, sess_med):
    a = int((mask>0).sum())
    if a < 20: return True
    n, _, _, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n - 1 > 1: return True
    if a > 3 * sess_med: return True
    ys, xs = np.where(mask>0)
    w = xs.max()-xs.min()+1; h = ys.max()-ys.min()+1
    asp = min(w,h)/max(w,h) if max(w,h)>0 else 0
    if asp < 0.4: return True
    fill = a/(w*h) if w*h>0 else 0
    if fill < 0.45: return True
    return False


def best_d(cands, gx, gy):
    if not cands: return float("inf")
    return min(np.hypot(c[0]-gx, c[1]-gy) for c in cands)


def make_cfg_path(prod, v10, change_axes):
    """cfg starting from prod, applying v10 values for axes in change_axes."""
    out = dict(prod)
    for ax in change_axes:
        for k in AXES[ax]:
            if k in v10: out[k] = v10[k]
    # h_max paired with h_min
    if "HSV_H" in change_axes:
        out["h_max"] = v10["h_max"]
    return out


def main():
    MANIFEST = load_manifest()
    items = [it for it in MANIFEST["items"]
             if it.get("propagate_status")=="done"
             and it["slug"] not in EXCLUDE_SESSIONS]

    # Pre-load all clean frames
    samples = []  # list of (frame, mask, gx, gy, slug, src)
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks" / SEG_BY_SLUG[slug]
        areas = [int((read_mask(p)>0).sum())
                 for p in sorted(masks_dir.glob("*.png"))
                 if read_mask(p) is not None]
        areas = [a for a in areas if a >= 20]
        sess_med = float(np.median(areas)) if areas else 0
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            mask = read_mask(mp)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]: continue
            if mask_suspect(mask, sess_med): continue
            ys = np.where(mask>0)
            if len(ys[0]) < 20: continue
            samples.append((frame, mask, float(ys[1].mean()), float(ys[0].mean()), slug, src))

    print(f"=== Ablation on {len(samples)} clean frames ===\n")

    def eval_cfg(cfg, label):
        hits = 0
        for frame, _, gx, gy, _, _ in samples:
            d = best_d(detect(frame, cfg), gx, gy)
            if d <= TOL: hits += 1
        return hits / len(samples)

    R_prod = eval_cfg(PROD, "PROD")
    R_v10  = eval_cfg(V10,  "V10")
    print(f"PROD R = {R_prod:.4f}")
    print(f"V10  R = {R_v10:.4f}    Δ = +{R_v10-R_prod:.4f}")

    # Single-axis from PROD
    print(f"\n=== Single-axis change from PROD (which one alone helps most) ===")
    for ax in AXES:
        cfg = make_cfg_path(PROD, V10, [ax])
        R = eval_cfg(cfg, ax)
        print(f"  PROD + only {ax:<8} → R={R:.4f}  Δ_from_PROD=+{R-R_prod:.4f}")

    # Leave-one-out from V10 (which one is essential)
    print(f"\n=== Leave-one-out from V10 (which one revert hurts most) ===")
    for ax in AXES:
        keep = [a for a in AXES if a != ax]
        cfg = make_cfg_path(PROD, V10, keep)
        R = eval_cfg(cfg, f"V10 - {ax}")
        print(f"  V10 minus {ax:<8} → R={R:.4f}  Δ_from_V10={R-R_v10:+.4f}")

    # Cumulative: PROD → +HSV_H → +HSV_S → +HSV_V → +aspect → +fill → +area
    print(f"\n=== Cumulative additive (greedy by single-axis benefit) ===")
    # Determine order by single-axis gain
    gains = []
    for ax in AXES:
        cfg = make_cfg_path(PROD, V10, [ax])
        R = eval_cfg(cfg, ax)
        gains.append((R-R_prod, ax))
    gains.sort(reverse=True)
    order = [ax for _, ax in gains]
    applied = []
    R_prev = R_prod
    for ax in order:
        applied.append(ax)
        cfg = make_cfg_path(PROD, V10, applied)
        R = eval_cfg(cfg, "+".join(applied))
        print(f"  PROD + {'+'.join(applied):<40} R={R:.4f}  step Δ=+{R-R_prev:.4f}")
        R_prev = R


if __name__ == "__main__":
    main()
