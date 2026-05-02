"""27 — Per-algorithm candidate-count distribution.

Three algorithms, side-by-side per-frame n_cand histograms (overlay lines).
Premise: R_emit (current metric) rewards spray-and-pray. A high n_cand
inflates R because triangulation pool just needs ONE truth-near cand to
score 1.0 on that frame. Visualizing the distributions exposes how much
"production load" each variant actually creates.

Algorithms:
  PROD     production: tight HSV [105,112][140,255][40,255] + aspect>=0.75
           + fill>=0.55 + area>=20  (from data/presets/blue_ball.json)
  V11      research baseline: loose HSV [103,118][120,255][30,255]
           + aspect>=0.40 + fill>=0.35 + area>=3  (from 26_multiscale)
  V11+D1   V11 ∪ |Y[t]-Y[t-1]| diff stream (apex bonus, current SOTA candidate)

Output:
  outputs/27_cand_count_hist.png   3-line overlay (linear + log axes)
  outputs/27_cand_count_stats.json per-algo distribution stats

Run: cd lab/research && uv run python scripts/27_cand_count_hist.py
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import numpy as np
import cv2
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT.mkdir(parents=True, exist_ok=True)
FIG_DIR = OUT / "_figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

# ── Detectors (lift configs from 02_head_to_head + 26_multiscale_ydiff) ──

PROD = dict(h=(105, 112), s=(140, 255), v=(40, 255),
            aspect=0.75, fill=0.55, area=(20, 150_000))

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

YDIFF_THR = 15


def _shape_gate(m: np.ndarray, cfg: dict) -> list[tuple[float, float, int]]:
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[tuple[float, float, int]] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < cfg["area"][0] or a > cfg["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        asp = min(w, h) / max(w, h)
        if asp < cfg["aspect"]:
            continue
        fill = a / (w * h)
        if fill < cfg["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    return out


def detect_prod(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([PROD["h"][0], PROD["s"][0], PROD["v"][0]], dtype=np.uint8)
    hi = np.array([PROD["h"][1], PROD["s"][1], PROD["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    return _shape_gate(m, PROD)


def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m, V11)


def detect_ydiff(older_y: np.ndarray, curr_y: np.ndarray) -> list[tuple[float, float, int]]:
    d = cv2.absdiff(curr_y, older_y)
    _, m = cv2.threshold(d, YDIFF_THR, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m, V11)


def union_cands(*cand_lists, dedup_px: float = 5.0):
    """Merge: dedupe candidates within `dedup_px` of an earlier-list entry."""
    tol2 = dedup_px * dedup_px
    merged: list[tuple[float, float, int]] = []
    for cl in cand_lists:
        for c in cl:
            if not any((c[0] - m[0]) ** 2 + (c[1] - m[1]) ** 2 <= tol2 for m in merged):
                merged.append(c)
    return merged


def bgr_to_y(bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)[..., 0]


# ── Run ──────────────────────────────────────────────────────────────────

def run() -> dict[str, list[int]]:
    items = [it for it in load_manifest()["items"] if it.get("propagate_status") == "done"]
    counts = {"PROD": [], "V11": [], "V11+D1": []}
    n_sessions = 0
    for item in items:
        slug = item["slug"]
        in_f = item["in_frame"]
        masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
        gt_set = {int(p.stem) for p in masks_dir.glob("*.png")}
        if not gt_set:
            continue
        n_sessions += 1
        prev_y: np.ndarray | None = None
        for fp in sorted((WS / "items" / slug / "frames").glob("*.jpg")):
            local = int(fp.stem)
            src = local + in_f
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            cur_y = bgr_to_y(bgr)
            d1: list = []
            if prev_y is not None and prev_y.shape == cur_y.shape:
                d1 = detect_ydiff(prev_y, cur_y)
            prev_y = cur_y
            if src not in gt_set:
                continue
            mp = masks_dir / f"{src:05d}.png"
            mask = read_mask(mp)
            if mask is None or mask.shape != bgr.shape[:2]:
                continue
            if (mask > 0).sum() < 20:
                continue
            cands_prod = detect_prod(bgr)
            cands_v11 = detect_v11(bgr)
            cands_union = union_cands(cands_v11, d1)
            counts["PROD"].append(len(cands_prod))
            counts["V11"].append(len(cands_v11))
            counts["V11+D1"].append(len(cands_union))
        print(f"  {slug:<28} n_gt={len([s for s in gt_set])}  cumulative={len(counts['PROD'])}", flush=True)
    print(f"[done] {n_sessions} sessions, {len(counts['PROD'])} GT frames")
    return counts


def stats(arr: list[int]) -> dict:
    a = np.asarray(arr, dtype=int)
    if len(a) == 0:
        return {"n": 0}
    return {
        "n": int(len(a)),
        "mean": float(a.mean()),
        "median": int(np.median(a)),
        "p95": int(np.percentile(a, 95)),
        "p99": int(np.percentile(a, 99)),
        "max": int(a.max()),
        "frac_zero": float((a == 0).mean()),
        "frac_one": float((a == 1).mean()),
        "frac_le_3": float((a <= 3).mean()),
    }


def plot_overlay(counts: dict[str, list[int]], path: Path) -> None:
    colors = {"PROD": "#22c55e", "V11": "#3b82f6", "V11+D1": "#ef4444"}
    # Common bin edges across algos (clamp to p99 of widest distribution).
    all_max = max(np.percentile(c, 99) if c else 0 for c in counts.values())
    bin_max = int(min(all_max, 200))
    edges = np.arange(0, bin_max + 2)  # 0,1,2,...,bin_max+1

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, scale in zip(axes, ("linear", "log")):
        for name, arr in counts.items():
            if not arr:
                continue
            a = np.clip(np.asarray(arr), 0, bin_max)
            hist, _ = np.histogram(a, bins=edges)
            centers = edges[:-1]
            ax.plot(centers, hist, label=name, color=colors[name],
                    linewidth=1.8, marker="o", markersize=3)
        ax.set_xlabel("candidates per frame")
        ax.set_ylabel("frame count" + (" (log)" if scale == "log" else ""))
        ax.set_yscale(scale)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right")
        ax.set_title(f"per-frame candidate count — {scale} y")
    fig.suptitle(f"Candidate-count distribution across algorithms "
                 f"(N={len(next(iter(counts.values())))} GT frames)")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    t0 = time.time()
    counts = run()
    summary = {name: stats(arr) for name, arr in counts.items()}
    print()
    hdr = f"{'algo':<10}{'n':>6}{'mean':>8}{'med':>5}{'p95':>5}{'p99':>5}{'max':>6}{'%=0':>7}{'%=1':>7}{'%≤3':>7}"
    print(hdr)
    print("-" * len(hdr))
    for name, s in summary.items():
        print(f"{name:<10}{s['n']:>6}{s['mean']:>8.2f}{s['median']:>5}"
              f"{s['p95']:>5}{s['p99']:>5}{s['max']:>6}"
              f"{s['frac_zero']*100:>6.1f}%{s['frac_one']*100:>6.1f}%{s['frac_le_3']*100:>6.1f}%")
    out_json = OUT / "27_cand_count_stats.json"
    out_json.write_text(json.dumps({"summary": summary, "raw": counts}, indent=2))
    out_png = FIG_DIR / "27_cand_count_hist.png"
    plot_overlay(counts, out_png)
    print(f"\n[saved] {out_json}")
    print(f"[saved] {out_png}")
    print(f"[done]  {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
