"""Per-frame recovery test for two-stage cube fallback.

Strategy:
  Stage 1: V10 main cube → emit if any candidate
  Stage 2 (only if stage 1 emits 0): fallback cube + strict shape gate
                                     + top-1 by area + max blob area cap

Test:
  (a) On 68 M1 miss frames: how many recover?
  (b) On 948 HIT frames: false-trigger rate (stage 2 fires when stage 1
                                              already emits something)
                         — should be 0 by construction (only fires if
                         stage 1 = 0). But also report: when stage 1 = 0
                         and stage 2 fires on a HIT frame (rare),
                         centroid distance to GT
  (c) Per-session breakdown — does 22d1835e_b stay safe?

Fallback variants to compare:
  F1  H[100,120] S[0,80]  V[180,255]   tight  (low-S high-V)
  F2  H[95,120]  S[0,100] V[160,255]   wider hue band, looser
  F3  H[100,120] S[0,60]  V[200,255]   ultra-strict highlight
  F4  Full V10 H + S[0,119] V[150,255]  match-V10-hue, low-S only
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2

ROOT = Path(__file__).resolve().parents[2]
WS = ROOT / "lab" / "standalone_workspace"
OUT = ROOT / "lab-research" / "outputs"
M = json.loads((WS / "manifest.json").read_text())

V10 = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.50, fill=0.35, area=(5, 150_000))

FALLBACKS = {
    "F1": dict(h=(100,120), s=(0,80),  v=(180,255), aspect=0.60, fill=0.40, area=(8, 5000)),
    "F2": dict(h=(95,120),  s=(0,100), v=(160,255), aspect=0.60, fill=0.40, area=(8, 5000)),
    "F3": dict(h=(100,120), s=(0,60),  v=(200,255), aspect=0.60, fill=0.40, area=(8, 5000)),
    "F4": dict(h=(103,118), s=(0,119), v=(150,255), aspect=0.60, fill=0.40, area=(8, 5000)),
}

def detect(bgr, cfg, top_k=None):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h"][0], cfg["s"][0], cfg["v"][0]], dtype=np.uint8)
    hi = np.array([cfg["h"][1], cfg["s"][1], cfg["v"][1]], dtype=np.uint8)
    mask = cv2.inRange(hsv, lo, hi)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area"][0] or a > cfg["area"][1]: continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w<=0 or h<=0: continue
        asp = min(w,h)/max(w,h)
        if asp < cfg["aspect"]: continue
        fill = a/(w*h)
        if fill < cfg["fill"]: continue
        out.append((float(cents[i,0]), float(cents[i,1]), a))
    out.sort(key=lambda x: x[2], reverse=True)
    if top_k is not None:
        out = out[:top_k]
    return out

def gt_centroid_radius(mask):
    ys, xs = np.where(mask>0)
    if len(ys) < 5: return None, None
    return (float(xs.mean()), float(ys.mean())), float(np.sqrt(len(ys)/np.pi))

def hit(cands, gtc, r):
    tol2 = max(10.0, 0.5*r)**2
    for cx, cy, _ in cands:
        if (cx-gtc[0])**2 + (cy-gtc[1])**2 <= tol2:
            return True
    return False

def main():
    items = [it for it in M["items"] if it.get("propagate_status")=="done" and it.get("in_frame") is not None]
    # Stats per fallback per session
    stats = {fk: {"recover":0, "fp_when_v10_empty_no_gt_hit":0, "stage2_fires":0,
                   "per_sess": {}} for fk in FALLBACKS}
    n_m1_total = 0; n_v10_emit_zero = 0; n_total = 0; v10_recall = 0

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]
        masks = sorted((WS/"items"/slug/"masks").glob("*.png"))
        sess_m1 = 0
        sess_recover = {fk:0 for fk in FALLBACKS}
        sess_fp = {fk:0 for fk in FALLBACKS}
        for fk in FALLBACKS:
            stats[fk]["per_sess"].setdefault(slug, {"m1":0, "recover":0, "fp":0, "stage2_fires":0})
        for mp in masks:
            src = int(mp.stem); local = src - in_f
            fp = WS/"items"/slug/"frames"/f"{local:05d}.jpg"
            if not fp.exists(): continue
            gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if gt is None or (gt>0).sum() < 5: continue
            frame = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if frame is None: continue
            gtc, r = gt_centroid_radius(gt)
            if gtc is None: continue
            n_total += 1
            cands_v10 = detect(frame, V10)
            v10_h = hit(cands_v10, gtc, r)
            if v10_h: v10_recall += 1
            if len(cands_v10) == 0:
                n_v10_emit_zero += 1
                is_m1 = not v10_h  # GT not recovered
                if is_m1:
                    sess_m1 += 1; n_m1_total += 1
                # Try each fallback
                for fk, cfg in FALLBACKS.items():
                    cands_fb = detect(frame, cfg, top_k=1)
                    if cands_fb:
                        stats[fk]["stage2_fires"] += 1
                        stats[fk]["per_sess"][slug]["stage2_fires"] += 1
                        if hit(cands_fb, gtc, r):
                            if is_m1:
                                stats[fk]["recover"] += 1
                                sess_recover[fk] += 1
                                stats[fk]["per_sess"][slug]["recover"] += 1
                        else:
                            # Stage 2 emitted a candidate that misses GT
                            stats[fk]["fp_when_v10_empty_no_gt_hit"] += 1
                            sess_fp[fk] += 1
                            stats[fk]["per_sess"][slug]["fp"] += 1
                if is_m1:
                    for fk in FALLBACKS:
                        stats[fk]["per_sess"][slug]["m1"] += 1

    print(f"=== Baseline ({n_total} frames) ===")
    print(f"V10 recall = {v10_recall/n_total:.3f}")
    print(f"V10 emits 0 in {n_v10_emit_zero} frames; M1 (no recovery possible from main) = {n_m1_total}")

    print(f"\n=== Fallback variant comparison ===")
    print(f"{'Variant':<5} {'Cube':<35} {'recovers':>10} {'fp(noGT)':>10} {'fires_total':>12} {'new_R':>8}")
    for fk, cfg in FALLBACKS.items():
        s = stats[fk]
        recovered_R = (v10_recall + s["recover"]) / n_total
        cubestr = f"H{cfg['h']} S{cfg['s']} V{cfg['v']}"
        print(f"{fk:<5} {cubestr:<35} {s['recover']:>4}/{n_m1_total:<4} {s['fp_when_v10_empty_no_gt_hit']:>10} {s['stage2_fires']:>12} {recovered_R:>8.3f}")

    print(f"\n=== Per-session F1 (kill-check on 22d1835e_b) ===")
    fk = "F1"
    print(f"{'session':<26} {'m1':>4} {'recover':>8} {'fp':>4} {'stage2_fires':>12}")
    for slug, d in sorted(stats[fk]["per_sess"].items()):
        print(f"  {slug:<24} {d['m1']:>4} {d['recover']:>8} {d['fp']:>4} {d['stage2_fires']:>12}")

    np.savez_compressed(OUT/"fallback_cube.npz", stats=np.array(list(stats.items()), dtype=object))
    print(f"\n[done] {OUT/'fallback_cube.npz'}")

if __name__ == "__main__":
    main()
