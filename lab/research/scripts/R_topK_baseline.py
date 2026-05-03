"""R_top1 / R_topK using production shape-cost ranking.

Hypothesis: research SOTA's R=0.97-0.99 is spray-and-pray. If we rank
each frame's candidates by the actual production cost (shape-prior:
0.6·aspect_pen + 0.4·fill_pen, lower=better) and only count the top-K
as hits, the gap between R_emit (any) and R_top1 (winner) measures the
spray bonus. A "real" algorithm has R_top1 ≈ R_emit; a spray algorithm
has R_top1 ≪ R_emit.

Algorithms scored (mirrors cand_count_hist):
  PROD    production tight HSV+gate
  V11     research baseline loose gate
  V11∪D1  V11 + |Y[t]-Y[t-1]| diff stream union (extreme spray reference)

For each algo, emit candidates with full {cx, cy, area, aspect, fill},
sort by score_candidates(), compute hit @ top-K for K ∈ {1, 3, 5, 10, ∞}.

Output:
  outputs/R_topK_baseline.json
  outputs/_figures/R_topK_baseline.png  bar chart per algo per K + spray gap
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import numpy as np
import cv2
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

# Import production cost function so the metric matches what server actually does.
sys.path.insert(0, str(ROOT / "server"))
from candidate_selector import Candidate, score_candidates  # noqa: E402

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT.mkdir(parents=True, exist_ok=True)
FIG_DIR = OUT / "_figures"; FIG_DIR.mkdir(parents=True, exist_ok=True)

TOL_PX = 10.0
TOPK_LIST = [1, 3, 5, 10]

PROD = dict(h=(105, 112), s=(140, 255), v=(40, 255),
            aspect=0.75, fill=0.55, area=(20, 150_000))
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)
YDIFF_THR = 15

CandFeat = tuple[float, float, int, float, float]  # cx, cy, area, aspect, fill


def _emit_with_shape(m: np.ndarray, cfg: dict) -> list[CandFeat]:
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[CandFeat] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area"][0] or a > cfg["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        asp = min(w, h) / max(w, h)
        if asp < cfg["aspect"]:
            continue
        fill = a / (w * h)
        if fill < cfg["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a, float(asp), float(fill)))
    return out


def detect_prod(bgr: np.ndarray) -> list[CandFeat]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([PROD["h"][0], PROD["s"][0], PROD["v"][0]], dtype=np.uint8)
    hi = np.array([PROD["h"][1], PROD["s"][1], PROD["v"][1]], dtype=np.uint8)
    return _emit_with_shape(cv2.inRange(hsv, lo, hi), PROD)


def detect_v11(bgr: np.ndarray) -> list[CandFeat]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _emit_with_shape(m, V11)


def detect_ydiff(older_y: np.ndarray, curr_y: np.ndarray) -> list[CandFeat]:
    d = cv2.absdiff(curr_y, older_y)
    _, m = cv2.threshold(d, YDIFF_THR, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _emit_with_shape(m, V11)


def union_cands(a: list[CandFeat], b: list[CandFeat], dedup_px: float = 5.0) -> list[CandFeat]:
    tol2 = dedup_px * dedup_px
    merged = list(a)
    for c in b:
        if not any((c[0] - m[0]) ** 2 + (c[1] - m[1]) ** 2 <= tol2 for m in merged):
            merged.append(c)
    return merged


def rank_by_cost(cands: list[CandFeat]) -> list[CandFeat]:
    if not cands:
        return []
    cs = [Candidate(cx=c[0], cy=c[1], area=c[2], aspect=c[3], fill=c[4]) for c in cands]
    costs = score_candidates(cs)
    return [c for _, c in sorted(zip(costs, cands), key=lambda p: p[0])]


def hit_in_topK(ranked: list[CandFeat], gx: float, gy: float, K: int) -> bool:
    for c in ranked[:K]:
        if (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX:
            return True
    return False


def hit_anywhere(cands: list[CandFeat], gx: float, gy: float) -> bool:
    return any((c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX for c in cands)


def gt_centroid(mask: np.ndarray) -> tuple[float, float]:
    ys, xs = np.where(mask > 0)
    return float(xs.mean()), float(ys.mean())


# ── Main ─────────────────────────────────────────────────────────────────

def run():
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    # results[algo][K] = [bool per frame]; results[algo]["emit"] same.
    results = {a: {**{K: [] for K in TOPK_LIST}, "emit": [], "rank_truth": [], "n_emit": []}
               for a in ("PROD", "V11", "V11+D1")}
    for item in items:
        slug = item["slug"]; in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set:
            continue
        prev_y = None
        for fp in sorted((WS / "items" / slug / "frames").glob("*.jpg")):
            local = int(fp.stem); src = local + in_f
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            cur_y = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)[..., 0]
            d1 = detect_ydiff(prev_y, cur_y) if prev_y is not None and prev_y.shape == cur_y.shape else []
            prev_y = cur_y
            if src not in gt_set:
                continue
            mask = read_mask(masks_dir / f"{src:05d}.png")
            if mask is None or mask.shape != bgr.shape[:2] or (mask > 0).sum() < 20:
                continue
            gx, gy = gt_centroid(mask)
            cands_prod = detect_prod(bgr)
            cands_v11 = detect_v11(bgr)
            cands_union = union_cands(cands_v11, d1)
            for algo, cands in (("PROD", cands_prod), ("V11", cands_v11), ("V11+D1", cands_union)):
                ranked = rank_by_cost(cands)
                for K in TOPK_LIST:
                    results[algo][K].append(hit_in_topK(ranked, gx, gy, K))
                results[algo]["emit"].append(hit_anywhere(cands, gx, gy))
                results[algo]["n_emit"].append(len(cands))
                # Find rank of the truth-cand (or -1 if none in TOL)
                rank = -1
                for i, c in enumerate(ranked):
                    if (c[0] - gx) ** 2 + (c[1] - gy) ** 2 <= TOL_PX * TOL_PX:
                        rank = i
                        break
                results[algo]["rank_truth"].append(rank)
        print(f"  {slug:<28} cumulative_n={len(results['PROD']['emit'])}", flush=True)
    return results


def aggregate(results):
    summary = {}
    for algo, d in results.items():
        n = len(d["emit"])
        s = {"n_frames": n,
             "R_emit": float(np.mean(d["emit"]))}
        for K in TOPK_LIST:
            s[f"R_top{K}"] = float(np.mean(d[K]))
        s["spray_gap"] = s["R_emit"] - s["R_top1"]
        # rank distribution among hits
        ranks = np.array(d["rank_truth"])
        hits = ranks[ranks >= 0]
        s["truth_rank_median"] = int(np.median(hits)) if len(hits) else -1
        s["truth_rank_p95"] = int(np.percentile(hits, 95)) if len(hits) else -1
        s["truth_rank_max"] = int(hits.max()) if len(hits) else -1
        s["mean_n_emit"] = float(np.mean(d["n_emit"]))
        summary[algo] = s
    return summary


def plot(summary, path):
    algos = list(summary.keys())
    Ks = TOPK_LIST + ["emit"]
    K_labels = [f"top-{K}" for K in TOPK_LIST] + ["emit (any)"]
    colors = {"PROD": "#16a34a", "V11": "#2563eb", "V11+D1": "#dc2626"}

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6),
                                    gridspec_kw={"width_ratios": [3, 1.4]})

    # Left: grouped bars R@K per algo
    x = np.arange(len(Ks))
    bw = 0.26
    for i, algo in enumerate(algos):
        s = summary[algo]
        vals = [s[f"R_top{K}"] for K in TOPK_LIST] + [s["R_emit"]]
        bars = ax1.bar(x + (i - 1) * bw, vals, bw, label=algo,
                       color=colors[algo], edgecolor="black", linewidth=0.5)
        for b, v in zip(bars, vals):
            ax1.text(b.get_x() + b.get_width() / 2, v + 0.005, f"{v:.3f}",
                     ha="center", fontsize=8)
    ax1.set_xticks(x)
    ax1.set_xticklabels(K_labels)
    ax1.set_ylabel("R (hit rate)")
    ax1.set_title("R@K — top-K from production shape-cost ranking\n"
                  "gap between top-1 and emit = spray bonus")
    ax1.set_ylim(0, 1.05)
    ax1.axhline(0.95, color="#94a3b8", linestyle=":", linewidth=0.8)
    ax1.legend(loc="lower right")
    ax1.grid(True, alpha=0.3, axis="y")

    # Right: spray gap visualization
    gaps = [summary[a]["spray_gap"] for a in algos]
    ax2.barh(algos, gaps, color=[colors[a] for a in algos], edgecolor="black")
    for i, (a, g) in enumerate(zip(algos, gaps)):
        ax2.text(g + 0.005, i, f"{g:.3f}", va="center", fontsize=10)
    ax2.set_xlabel("R_emit − R_top1  (spray bonus)")
    ax2.set_title("How much spray\ninflates R")
    ax2.set_xlim(0, max(gaps) * 1.3 + 0.01)
    ax2.grid(True, alpha=0.3, axis="x")

    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    t0 = time.time()
    results = run()
    summary = aggregate(results)
    print()
    hdr = (f"{'algo':<10}{'n':>6}  {'R_emit':>7} {'R_top1':>7} {'R_top3':>7} "
           f"{'R_top5':>7} {'R_top10':>8}  {'spray':>7}  "
           f"{'rk_med':>7}{'rk_p95':>7}{'rk_max':>7}  {'mean_n':>8}")
    print(hdr)
    print("-" * len(hdr))
    for a, s in summary.items():
        print(f"{a:<10}{s['n_frames']:>6}  "
              f"{s['R_emit']:>7.3f} {s['R_top1']:>7.3f} {s['R_top3']:>7.3f} "
              f"{s['R_top5']:>7.3f} {s['R_top10']:>8.3f}  "
              f"{s['spray_gap']:>7.3f}  "
              f"{s['truth_rank_median']:>7}{s['truth_rank_p95']:>7}{s['truth_rank_max']:>7}  "
              f"{s['mean_n_emit']:>8.1f}")
    out_json = OUT / "R_topK_baseline.json"
    out_json.write_text(json.dumps({"summary": summary,
                                    "raw_emit": {a: results[a]["emit"] for a in results},
                                    "raw_rank_truth": {a: results[a]["rank_truth"] for a in results}},
                                    indent=2))
    out_png = FIG_DIR / "R_topK_baseline.png"
    plot(summary, out_png)
    print(f"\n[saved] {out_json}")
    print(f"[saved] {out_png}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
