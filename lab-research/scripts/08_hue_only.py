"""Hue-only gate: drop S and V constraints entirely (S[0,255], V[0,255]).

Measure: how much recall + how much candidate explosion?
Compares against V10 baseline.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
EXCLUDE_SESSIONS = {"session_s_21af9a82_b"}
TOL = 10.0

V10 = dict(h_min=103, h_max=118, s_min=120, s_max=255, v_min=30, v_max=255,
           aspect_min=0.50, fill_min=0.35, min_area=5)


def detect(frame, cfg):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h_min"], cfg["s_min"], cfg["v_min"]], dtype=np.uint8)
    hi = np.array([cfg["h_max"], cfg["s_max"], cfg["v_max"]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
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
    n, _, _, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n - 1 > 1: return True
    if a > 3 * sess_med: return True
    ys, xs = np.where(mask>0)
    w = xs.max()-xs.min()+1; h = ys.max()-ys.min()+1
    asp = min(w,h)/max(w,h) if max(w,h)>0 else 0
    if asp < 0.4: return True
    fill = a/(w*h) if w*h>0 else 0
    if fill < 0.45: return True
    return False


VARIANTS = [
    ("V10 (baseline)",          dict(V10)),
    ("V10 + hue only (S>=0,V>=0)", {**V10, "s_min":0, "v_min":0}),
    ("hue only, no shape gate",  {**V10, "s_min":0, "v_min":0, "aspect_min":0.0, "fill_min":0.0, "min_area":5}),
    ("V10 H[100,125] hue only",  {**V10, "h_min":100, "h_max":125, "s_min":0, "v_min":0}),
    ("Wide hue [95,130] hue only", {**V10, "h_min":95, "h_max":130, "s_min":0, "v_min":0, "aspect_min":0.0, "fill_min":0.0, "min_area":5}),
]


def main():
    MANIFEST = json.loads((WS/"manifest.json").read_text())
    items = [it for it in MANIFEST["items"]
             if it.get("propagate_status")=="done"
             and it["slug"] not in EXCLUDE_SESSIONS]

    samples = []
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS/"items"/slug/"masks"
        areas = [int((cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)>0).sum())
                 for p in sorted(masks_dir.glob("*.png"))
                 if cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) is not None]
        areas = [a for a in areas if a >= 20]
        sess_med = float(np.median(areas)) if areas else 0
        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            mask = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if mask is None or frame is None or mask.shape != frame.shape[:2]: continue
            if mask_suspect(mask, sess_med): continue
            ys = np.where(mask>0)
            if len(ys[0]) < 20: continue
            samples.append((frame, float(ys[1].mean()), float(ys[0].mean()), slug))

    print(f"=== Hue-only experiment on {len(samples)} clean frames ===\n")
    print(f"{'variant':<32}{'R_emit':>8}{'nc_p50':>8}{'nc_p95':>8}{'nc_max':>8}")
    for label, cfg in VARIANTS:
        hits = 0; nc_list = []
        for frame, gx, gy, _ in samples:
            cands = detect(frame, cfg)
            nc_list.append(len(cands))
            if cands:
                d = min(np.hypot(c[0]-gx, c[1]-gy) for c in cands)
                if d <= TOL: hits += 1
        nc = np.array(nc_list)
        print(f"{label:<32}{hits/len(samples):>8.3f}{int(np.percentile(nc,50)):>8d}"
              f"{int(np.percentile(nc,95)):>8d}{int(nc.max()):>8d}")

    # Per-session for "V10 + hue only" variant
    chosen_label, chosen_cfg = VARIANTS[1]
    print(f"\n=== Per-session [{chosen_label}] ===")
    print(f"{'session':<26}{'n':>5}{'V10':>8}{'hue-only':>10}{'Δ':>8}{'nc_p95':>9}")
    for slug in sorted({s[3] for s in samples}):
        sub = [s for s in samples if s[3]==slug]
        h_v10, h_ho, ncs = 0, 0, []
        for frame, gx, gy, _ in sub:
            c10 = detect(frame, V10)
            cho = detect(frame, chosen_cfg)
            ncs.append(len(cho))
            if c10:
                if min(np.hypot(c[0]-gx, c[1]-gy) for c in c10) <= TOL: h_v10 += 1
            if cho:
                if min(np.hypot(c[0]-gx, c[1]-gy) for c in cho) <= TOL: h_ho += 1
        n = len(sub)
        print(f"{slug:<26}{n:>5d}{h_v10/n:>8.3f}{h_ho/n:>10.3f}{(h_ho-h_v10)/n:>+8.3f}"
              f"{int(np.percentile(ncs,95)):>9d}")


if __name__ == "__main__":
    main()
