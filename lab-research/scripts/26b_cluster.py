"""26b — manual cluster assignment for the 21 consensus residuals."""
from __future__ import annotations
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "outputs"
table = json.loads((OUT / "26_residual_table.json").read_text())

def cluster(r):
    near_edge = r["edge_dist"] <= 10
    desat     = r["gt_hsv"][1] < 25                       # very low S — specular bleach
    dark_mid  = (r["gt_hsv"][2] < 70) and not near_edge   # GT V<70 mid-frame
    high_ydmax = (r["yd_max"] is not None and r["yd_max"] >= 100)

    if near_edge:
        return "G1_edge_clipped"
    if desat:
        return "G2_specular_desat"
    if dark_mid:
        return "G3_low_contrast_mid"
    return "G4_residual_other"

clusters = {}
for r in table:
    c = cluster(r)
    clusters.setdefault(c, []).append(r)

for c, rs in clusters.items():
    print(f"\n[{c}] n={len(rs)}")
    for r in rs:
        print(f"  {r['slug']:<26} src={r['src']:<4} mode={r['mode']} "
              f"gtc={r['gtc']} S={r['gt_hsv'][1]:>5.1f} V={r['gt_hsv'][2]:>5.1f} "
              f"ring_v={r['ring_v']:>5.1f} contrast_v={r['contrast_v']:>+6.1f} "
              f"edge_dist={int(r['edge_dist']):>3} yd_max={r['yd_max']} "
              f"runlen={r['run_len']} flight_pos={r['flight_pos']}")

# Save assignment
out = []
for r in table:
    rr = dict(r); rr["cluster"] = cluster(r); out.append(rr)
(OUT / "26_residual_clusters.json").write_text(json.dumps(out, indent=2))
print(f"\nclusters: {[(c, len(rs)) for c, rs in clusters.items()]}")
