"""Ball-pixel H/S/V distribution analysis.

Find what hue/sat/val range actually covers the GT ball pixels at
{50, 90, 95, 99}% recall. Compare to current production cube
H[105,112] S[140,255] V[40,255].

Also: per-frame "is the ball entirely inside hue range X?" — i.e.
the L1 question, but parameterized.
"""
from __future__ import annotations
from pathlib import Path
import numpy as np

OUT = Path(__file__).resolve().parents[1] / "outputs"
d = np.load(OUT / "pixel_samples.npz", allow_pickle=True)
ball_hsv = d["ball_hsv"]
bg_hsv = d["bg_hsv"]

print(f"Ball pixels: {len(ball_hsv):,}    BG pixels: {len(bg_hsv):,}")

print("\n=== Ball-pixel marginal distribution (covers X% of ball pixels) ===")
print(f"{'channel':<10}{'p1':>6}{'p5':>6}{'p25':>6}{'p50':>6}{'p75':>6}{'p95':>6}{'p99':>6}")
for i, c in enumerate("HSV"):
    p = np.percentile(ball_hsv[:, i], [1, 5, 25, 50, 75, 95, 99])
    print(f"  ball.{c:<5}" + "".join(f"{int(v):>6d}" for v in p))
for i, c in enumerate("HSV"):
    p = np.percentile(bg_hsv[:, i], [1, 5, 25, 50, 75, 95, 99])
    print(f"  bg.{c:<7}" + "".join(f"{int(v):>6d}" for v in p))

# What does the current cube catch?
def cube_recall(hsv_pix, lo, hi):
    H,S,V = hsv_pix[:,0], hsv_pix[:,1], hsv_pix[:,2]
    return float(((H>=lo[0])&(H<=hi[0])&(S>=lo[1])&(S<=hi[1])&(V>=lo[2])&(V<=hi[2])).mean())

cur_lo = (105, 140, 40); cur_hi = (112, 255, 255)
print(f"\nCurrent cube H[{cur_lo[0]},{cur_hi[0]}] S[{cur_lo[1]},{cur_hi[1]}] V[{cur_lo[2]},{cur_hi[2]}]")
print(f"  ball pixel TPR: {cube_recall(ball_hsv, cur_lo, cur_hi):.3f}")
print(f"  bg   pixel FPR: {cube_recall(bg_hsv, cur_lo, cur_hi):.5f}")

# Try wider gates
print("\n=== Hue widening sweep (S[120,255], V[20,255] fixed) ===")
for h_lo, h_hi in [(105,112), (102,118), (100,125), (95,130), (90,135), (90,140), (85,140)]:
    tpr = cube_recall(ball_hsv, (h_lo,120,20), (h_hi,255,255))
    fpr = cube_recall(bg_hsv,   (h_lo,120,20), (h_hi,255,255))
    print(f"  H[{h_lo},{h_hi}]  ball_TPR={tpr:.3f}  bg_FPR={fpr:.5f}")

print("\n=== S/V widening (H[100,125] fixed) ===")
for s_lo, v_lo in [(140,40),(120,30),(100,20),(80,15),(60,10),(40,5)]:
    tpr = cube_recall(ball_hsv, (100,s_lo,v_lo), (125,255,255))
    fpr = cube_recall(bg_hsv,   (100,s_lo,v_lo), (125,255,255))
    print(f"  S[{s_lo},255] V[{v_lo},255]  ball_TPR={tpr:.3f}  bg_FPR={fpr:.5f}")

# Per-frame L1 simulation: what fraction of GT-frames have ANY pixel in cube?
# This requires session-level frame stats — we'd need the bottleneck npz which has L1 column.
import json
b = np.load(OUT / "pipeline_bottleneck.npz")
slugs = b["slugs"].astype(str)
print("\n=== Achievable L1 ceiling: how many ball pixels per frame fall in WIDE cube? ===")
print("(simulating: H[95,130] S[80,255] V[15,255] — recall-priority cube)")
print("  expected pixel TPR=", cube_recall(ball_hsv, (95,80,15), (130,255,255)))
print("  expected pixel FPR=", cube_recall(bg_hsv,   (95,80,15), (130,255,255)))
