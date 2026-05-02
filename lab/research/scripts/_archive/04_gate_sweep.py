"""Gate threshold sweep: how high can we push per-frame recall by
loosening shape gates, *given* the GT-overlapping CC already exists?

User explicit constraint: noise OK, downstream handles it. So we
sweep the recall side and ignore false-positive count for now —
that goes in the next script (FP-rate sweep on full frame).
"""
from __future__ import annotations
from pathlib import Path
import numpy as np

OUT = Path(__file__).resolve().parents[1] / "outputs"
d = np.load(OUT / "pipeline_bottleneck.npz")

L1 = d["L1"]; L2 = d["L2"]
cc_area = d["cc_area"]; cc_asp = d["cc_aspect"]; cc_fill = d["cc_fill"]
n = len(L1)

# Survivor set = frames that had a CC overlapping GT (L1==1) — its area/asp/fill stamped.
# CCs stamped only when L1=1 and best_overlap > 0; not equivalent to L2 because area gate hadn't been applied yet.
has_cc = (cc_area > 0)
print(f"Total frames analyzed: {n}")
print(f"  L1 (HSV mask hit GT):        {L1.mean():.3f}")
print(f"  has GT-overlapping CC:        {has_cc.mean():.3f}")
print(f"  Production L4 (current gate): {d['L4'].mean():.3f}")
print(f"  Production L5 (winner ok):    {(d['winner_dist']>=0).mean():.3f} returned a winner; {(np.where(d['winner_dist']>=0, d['winner_dist'], 999) <= 10).mean():.3f} within 10px")

print("\n=== Gate sweep — assume area-gate only (no aspect, no fill) ===")
for amin in [1, 3, 5, 8, 10, 15, 20]:
    pass_rate = ((cc_area >= amin) & has_cc).mean()
    print(f"  min_area={amin:>3d}  recall={pass_rate:.3f}")

print("\n=== Aspect sweep (with min_area=5) ===")
for asp_min in [0.30, 0.40, 0.50, 0.60, 0.70, 0.75, 0.80]:
    rate = (has_cc & (cc_area >= 5) & (cc_asp >= asp_min)).mean()
    print(f"  aspect_min={asp_min:.2f}  recall={rate:.3f}")

print("\n=== Fill sweep (with min_area=5, aspect_min=0.5) ===")
for fill_min in [0.20, 0.30, 0.40, 0.50, 0.55, 0.65]:
    rate = (has_cc & (cc_area >= 5) & (cc_asp >= 0.5) & (cc_fill >= fill_min)).mean()
    print(f"  fill_min={fill_min:.2f}  recall={rate:.3f}")

print("\n=== Combo: relax everything ===")
relax = (has_cc & (cc_area >= 5) & (cc_asp >= 0.5) & (cc_fill >= 0.35))
print(f"  loose gate recall: {relax.mean():.3f}  (vs production L4 {d['L4'].mean():.3f})")

print("\n=== Per-session: production L4 vs loose-gate ===")
slugs = d["slugs"].astype(str)
for s in sorted(set(slugs)):
    m = slugs == s
    n_s = int(m.sum())
    if n_s < 5: continue
    prod = d["L4"][m].mean()
    loose = relax[m].mean()
    color = L1[m].mean()
    print(f"  {s:<26} n={n_s:>4d}  L1={color:.2f}  L4={prod:.2f}  LooseL4={loose:.2f}  Δ={loose-prod:+.2f}")
