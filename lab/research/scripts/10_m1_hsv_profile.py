"""Profile HSV distribution of M1 miss frames vs HIT frames to design
fallback cube boundaries principled-ly.

For each frame, classify M1 vs HIT under V10. For both classes, dump
per-pixel H/S/V from GT region. Output joint percentile tables.

Goal: find low-S high-V slab that
  - covers M1 GT region (recovers desaturated-highlight ball)
  - is distinct from HIT GT region (won't double-fire on success frames)
  - has minimal background overlap (kill-check)
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

M = load_manifest()

V10 = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.50, fill=0.35, area=(5, 150_000))

def detect_v10_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V10["h"][0], V10["s"][0], V10["v"][0]], dtype=np.uint8)
    hi = np.array([V10["h"][1], V10["s"][1], V10["v"][1]], dtype=np.uint8)
    return cv2.inRange(hsv, lo, hi), hsv

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    m1_h, m1_s, m1_v = [], [], []
    hit_h, hit_s, hit_v = [], [], []
    bg_low_s_high_v_count = 0  # pixels in candidate fallback cube but bg
    bg_total = 0
    fallback_cube = dict(h=(100, 120), s=(0, 119), v=(180, 255))  # tentative

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks" / SEG_BY_SLUG[slug]).glob("*.png"))
        for mp in masks:
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            gt = read_mask(mp)
            if gt is None or (gt>0).sum() < 5: continue
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            mask_v10, hsv = detect_v10_mask(frame)
            ys, xs = np.where(gt>0)
            gt_pixels_in_v10 = mask_v10[ys, xs].sum()
            is_m1 = gt_pixels_in_v10 == 0
            h_px = hsv[ys, xs, 0]; s_px = hsv[ys, xs, 1]; v_px = hsv[ys, xs, 2]
            if is_m1:
                m1_h.append(h_px); m1_s.append(s_px); m1_v.append(v_px)
            else:
                hit_h.append(h_px); hit_s.append(s_px); hit_v.append(v_px)
                # BG kill-check: pixels NOT in GT but in frame, count how many fall in fallback cube
                bg_mask = np.ones_like(gt, dtype=bool); bg_mask[ys, xs] = False
                bg_h = hsv[..., 0][bg_mask]; bg_s = hsv[..., 1][bg_mask]; bg_v = hsv[..., 2][bg_mask]
                in_fc = ((bg_h>=fallback_cube["h"][0]) & (bg_h<=fallback_cube["h"][1])
                       & (bg_s>=fallback_cube["s"][0]) & (bg_s<=fallback_cube["s"][1])
                       & (bg_v>=fallback_cube["v"][0]) & (bg_v<=fallback_cube["v"][1]))
                bg_low_s_high_v_count += int(in_fc.sum())
                bg_total += int(bg_mask.sum())

    if not m1_h:
        print("No M1 frames"); return
    m1_h = np.concatenate(m1_h); m1_s = np.concatenate(m1_s); m1_v = np.concatenate(m1_v)
    hit_h = np.concatenate(hit_h); hit_s = np.concatenate(hit_s); hit_v = np.concatenate(hit_v)

    print(f"=== M1 GT pixels (n={len(m1_h)}, {len(m1_h)/(len(m1_h)+len(hit_h))*100:.1f}% of GT) ===")
    print(f"  H  p10/p25/p50/p75/p90 = {np.percentile(m1_h,[10,25,50,75,90]).astype(int)}")
    print(f"  S  p10/p25/p50/p75/p90 = {np.percentile(m1_s,[10,25,50,75,90]).astype(int)}")
    print(f"  V  p10/p25/p50/p75/p90 = {np.percentile(m1_v,[10,25,50,75,90]).astype(int)}")
    print(f"=== HIT GT pixels (n={len(hit_h)}) ===")
    print(f"  H  p10/p25/p50/p75/p90 = {np.percentile(hit_h,[10,25,50,75,90]).astype(int)}")
    print(f"  S  p10/p25/p50/p75/p90 = {np.percentile(hit_s,[10,25,50,75,90]).astype(int)}")
    print(f"  V  p10/p25/p50/p75/p90 = {np.percentile(hit_v,[10,25,50,75,90]).astype(int)}")

    # Find S/V slab where M1 lives but HIT doesn't
    print(f"\n=== Slab design check ===")
    for s_max in [60, 80, 100, 119]:
        for v_min in [140, 160, 180, 200]:
            m1_in = ((m1_s <= s_max) & (m1_v >= v_min)).mean()
            hit_in = ((hit_s <= s_max) & (hit_v >= v_min)).mean()
            print(f"  S<={s_max:3d} ∧ V>={v_min:3d}:  M1 cover={m1_in*100:5.1f}%  HIT cover={hit_in*100:5.1f}%  ratio={m1_in/max(0.001,hit_in):.1f}x")

    print(f"\n=== Fallback cube BG kill-check ===")
    print(f"  Cube H[{fallback_cube['h'][0]},{fallback_cube['h'][1]}] S[{fallback_cube['s'][0]},{fallback_cube['s'][1]}] V[{fallback_cube['v'][0]},{fallback_cube['v'][1]}]")
    print(f"  BG pixels in fallback cube: {bg_low_s_high_v_count}/{bg_total} = {bg_low_s_high_v_count/max(1,bg_total)*100:.3f}%")

    np.savez_compressed(OUT/"m1_hsv_profile.npz",
        m1_h=m1_h, m1_s=m1_s, m1_v=m1_v,
        hit_h=hit_h, hit_s=hit_s, hit_v=hit_v)
    print(f"\n[done] {OUT/'m1_hsv_profile.npz'}")

if __name__ == "__main__":
    main()
