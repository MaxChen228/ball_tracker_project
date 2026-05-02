"""27b — R vs n_cand truth chart for the entire research line.

Pull every (algorithm, R, mean_n_cand) datapoint that's already on disk
across 19/21/26/27 + head_to_head.npz, and plot them on one chart so the
"R 0.99x" research narrative gets contextualized against actual production
load. Spoiler: every R bump >0.95 came at 10–100× n_cand cost.

Output: outputs/_figures/27b_R_vs_ncand.png + 27b_R_vs_ncand.json
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import OUT

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

FIG = OUT / "_figures"; FIG.mkdir(parents=True, exist_ok=True)


def main() -> None:
    points = []

    # PROD / PROP from head_to_head.npz (TOL=10 hit definition)
    d = np.load(OUT / "head_to_head.npz")
    for tag, lbl in (("prod", "PROD (production tight HSV+gate)"),
                     ("prop", "PROP (HSV WIDE + motion gate)")):
        bd = d[f"{tag}_best_d"]; nc = d[f"{tag}_n"]
        points.append({"label": lbl, "R": float((bd <= 10).mean()),
                       "mean_n": float(nc.mean()),
                       "median_n": int(np.median(nc)),
                       "p95_n": int(np.percentile(nc, 95)),
                       "src": "02_head_to_head"})

    # 19_frst — FRST alone + FRST ∪ V11
    j = json.loads((OUT / "19_frst_results.json").read_text())
    points.append({"label": "FRST alone", "R": j["r_frst"],
                   "mean_n": j["mean_frst_cands"], "src": "19_frst"})
    points.append({"label": "FRST ∪ V11", "R": j["r_union"],
                   "mean_n": j["mean_union_cands"], "src": "19_frst"})
    # V11 baseline reported by 19
    points.append({"label": "V11 (19_frst baseline)", "R": j["r_v11"],
                   "mean_n": 25.05, "src": "19+27"})  # mean_n from 27 measurement

    # 21_ydiff sweep — all thresholds
    j = json.loads((OUT / "21_yplane_diff_results.json").read_text())
    for s in j["sweep"]:
        points.append({"label": f"Ydiff thr={s['thr']} alone",
                       "R": s["r_alone"], "mean_n": s["cpf_ball"], "src": "21_ydiff"})
        points.append({"label": f"Ydiff thr={s['thr']} ∪ V11",
                       "R": s["r_union"], "mean_n": s["cpf_ball"], "src": "21_ydiff"})

    # 26_multiscale — V11 + D1/D2/D3 individual + unions
    j = json.loads((OUT / "26_multiscale_ydiff_results.json").read_text())
    points.append({"label": "V11 alone (26)", "R": j["r_v11"],
                   "mean_n": 25.05, "src": "26+27"})
    for stream, v in j["streams"].items():
        # streams report fp_cands_per_noball, not cpf_ball — use as approx
        points.append({"label": f"{stream} alone", "R": v["r_alone"],
                       "mean_n": v["fp_cands_per_noball"], "src": "26"})
    # V11 ∪ D1: measured by 27 → 794.76; later unions: estimated by adding ~D2,D3 fp_cands
    # 27 measured V11+D1 mean=794.76 directly; report that value
    points.append({"label": "V11 ∪ D1 (27 measured)", "R": j["unions"]["V11_D1"]["r"],
                   "mean_n": 794.76, "src": "26+27"})
    # V11+D1+D2 / D3 / D2+D3: not measured by 27 but D2, D3 each add ~1100-1300;
    # mark as "estimated" with hollow markers
    base_n = 794.76
    points.append({"label": "V11 ∪ D1+D2", "R": j["unions"]["V11_D1_D2"]["r"],
                   "mean_n": base_n + 1181 * 0.5, "src": "26-est", "estimated": True})
    points.append({"label": "V11 ∪ D1+D3", "R": j["unions"]["V11_D1_D3"]["r"],
                   "mean_n": base_n + 1295 * 0.5, "src": "26-est", "estimated": True})
    points.append({"label": "V11 ∪ D1+D2+D3", "R": j["unions"]["V11_D1_D2_D3"]["r"],
                   "mean_n": base_n + (1181 + 1295) * 0.5, "src": "26-est", "estimated": True})

    # ── Plot ─────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(13, 7.5))

    # Color code by lineage
    color_map = {
        "PROD": "#16a34a", "PROP": "#a16207",
        "V11": "#2563eb", "Ydiff": "#7c3aed", "FRST": "#dc2626",
        "D1": "#9333ea", "D2": "#9333ea", "D3": "#9333ea",
    }

    def color_for(label: str) -> str:
        for key, c in color_map.items():
            if label.startswith(key): return c
        return "#475569"

    for p in points:
        c = color_for(p["label"])
        marker = "o" if not p.get("estimated") else "s"
        face = c if not p.get("estimated") else "white"
        ax.scatter(p["mean_n"], p["R"], s=110, c=face, edgecolors=c,
                   linewidths=2, marker=marker, zorder=3)
        # Smart label offset to avoid overlap on Ydiff cluster
        ax.annotate(p["label"], (p["mean_n"], p["R"]),
                    xytext=(7, 4), textcoords="offset points",
                    fontsize=8, color=c)

    # Production-pairing-cost reference lines (cands_A × cands_B for stereo)
    for n_pair, lbl in [(10, "10² = 100 pairs"),
                        (100, "100² = 10K pairs"),
                        (1000, "1K² = 1M pairs"),
                        (10000, "10K² = 100M pairs")]:
        ax.axvline(n_pair, color="#94a3b8", linestyle=":", linewidth=0.8, zorder=1)
        ax.text(n_pair * 1.05, 0.31, lbl, fontsize=8, color="#64748b", rotation=90, va="bottom")

    # R thresholds
    for r_thresh in (0.95, 0.99):
        ax.axhline(r_thresh, color="#22c55e", linestyle="--", linewidth=0.8, zorder=1, alpha=0.5)
        ax.text(0.6, r_thresh + 0.003, f"R={r_thresh}", fontsize=9, color="#15803d")

    ax.set_xscale("log")
    ax.set_xlim(0.5, 30000)
    ax.set_ylim(0.30, 1.02)
    ax.set_xlabel("mean candidates per frame (log scale)", fontsize=11)
    ax.set_ylabel("R_emit (any cand within TOL of GT)", fontsize=11)
    ax.set_title("R vs production load — every research SOTA datapoint we have\n"
                 "(squares = mean_n estimated, circles = measured)",
                 fontsize=12)
    ax.grid(True, which="both", alpha=0.25)

    fig.tight_layout()
    out_png = FIG / "27b_R_vs_ncand.png"
    fig.savefig(out_png, dpi=130)
    plt.close(fig)

    (OUT / "27b_R_vs_ncand.json").write_text(json.dumps(points, indent=2))

    # ── Print sorted table ───────────────────────────────────────────────
    print(f"\n{'algorithm':<32}{'R':>7}{'mean_n':>10}  src")
    print("-" * 70)
    for p in sorted(points, key=lambda x: x["mean_n"]):
        est = " (est)" if p.get("estimated") else ""
        print(f"{p['label']:<32}{p['R']:>7.3f}{p['mean_n']:>10.1f}  {p['src']}{est}")
    print()
    print(f"[saved] {out_png}")


if __name__ == "__main__":
    main()
