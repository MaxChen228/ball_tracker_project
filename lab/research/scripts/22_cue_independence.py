"""Cue independence analysis — I(V11; Y-diff) mutual information + oracle ceiling.

Produces per-frame binary hit/miss vectors for:
  - V11 (HSV color)
  - Y-diff thr=15
  - Y-diff thr=30

Computes:
  1. Marginal R per cue
  2. Pairwise confusion matrices + conditional probabilities
  3. Mutual information I(X;Y) from binary contingency tables
  4. Union R for each pair and 3-way combination → diminishing returns
  5. Per-mode recovery breakdown (canonical classifier from 15_v11_failure_modes.py)
  6. Oracle ceiling (per-mode best cue selector)
  7. Gap: oracle ceiling vs simple OR union

FRST / DL not yet available — noted as N/A.

Run: cd lab/research && uv run python scripts/22_cue_independence.py
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT, WS, OUT

OUT.mkdir(parents=True, exist_ok=True)

M = json.loads((WS / "manifest.json").read_text())

# ── Detector configs ────────────────────────────────────────────────────────

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

YDIFF_THRS = [15, 30]


# ── Detectors ───────────────────────────────────────────────────────────────

def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    # Also return the HSV mask for mode classification
    return _shape_gate(m), m, cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)


def detect_ydiff(prev_gray: np.ndarray, curr_gray: np.ndarray,
                 thr: int) -> list[tuple[float, float, int]]:
    d = cv2.absdiff(curr_gray, prev_gray)
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def _shape_gate(m: np.ndarray) -> list[tuple[float, float, int]]:
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[tuple[float, float, int]] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w <= 0 or h <= 0:
            continue
        asp = min(w, h) / max(w, h)
        if asp < V11["aspect"]:
            continue
        fill = a / (w * h)
        if fill < V11["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    return out


# ── GT helpers ───────────────────────────────────────────────────────────────

def hit_check(cands: list[tuple[float, float, int]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, _ in cands)


def classify_miss_canonical(mask_v11: np.ndarray, gt_mask: np.ndarray,
                             frame_bgr: np.ndarray,
                             cands: list[tuple[float, float, int]]) -> str:
    """Canonical mode classifier from 15_v11_failure_modes.py.

    Returns: HIT / M1 / M2 / M3 / M4 / M5
    M1: HSV cube hits 0 pixels in GT region (specular/desat)
    M2: No CC near GT even though HSV has pixels (fragmentation/merge)
    M3: CC near GT but killed by aspect gate (elongated)
    M4: killed by fill gate
    M5: passed gates but centroid drift
    """
    ys, xs = np.where(gt_mask > 0)
    gtc = (float(xs.mean()), float(ys.mean()))
    r = float(np.sqrt(len(ys) / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2

    if mask_v11[ys, xs].sum() == 0:
        return "M1"

    for cx, cy, _ in cands:
        if (cx - gtc[0]) ** 2 + (cy - gtc[1]) ** 2 <= tol2:
            return "HIT"

    n, _, stats, cents = cv2.connectedComponentsWithStats(mask_v11, connectivity=8)
    near = []
    for i in range(1, n):
        cx2, cy2 = float(cents[i, 0]), float(cents[i, 1])
        if (cx2 - gtc[0]) ** 2 + (cy2 - gtc[1]) ** 2 <= tol2:
            near.append(i)
    if not near:
        return "M2"
    best = max(near, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    a = int(stats[best, cv2.CC_STAT_AREA])
    w = int(stats[best, cv2.CC_STAT_WIDTH])
    h_s = int(stats[best, cv2.CC_STAT_HEIGHT])
    if a < V11["area"][0]:
        return "M2"
    asp = min(w, h_s) / max(w, h_s)
    fill = a / (w * h_s)
    if asp < V11["aspect"]:
        return "M3"
    if fill < V11["fill"]:
        return "M4"
    return "M5"


# ── Mutual information from 2x2 contingency ──────────────────────────────────

def binary_mi(x: np.ndarray, y: np.ndarray) -> float:
    """Mutual information I(X;Y) for two binary vectors, in bits."""
    n = len(x)
    assert len(y) == n
    # contingency: rows=X, cols=Y
    c = np.zeros((2, 2), dtype=int)
    for xi, yi in zip(x, y):
        c[int(xi), int(yi)] += 1
    mi = 0.0
    for i in range(2):
        for j in range(2):
            if c[i, j] == 0:
                continue
            p_ij = c[i, j] / n
            p_i  = c[i, :].sum() / n
            p_j  = c[:, j].sum() / n
            mi += p_ij * np.log2(p_ij / (p_i * p_j))
    return float(mi)


def binary_entropy(x: np.ndarray) -> float:
    """H(X) in bits."""
    p = x.mean()
    if p == 0 or p == 1:
        return 0.0
    return float(-p * np.log2(p) - (1 - p) * np.log2(1 - p))


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # Schema v2: propagate_status + in_frame live inside segments[]
    items: list[dict] = []
    for it in M["items"]:
        slug = it["slug"]
        for seg in it.get("segments", []):
            if seg.get("propagate_status") == "done" and seg.get("in_frame") is not None:
                seg_id = seg["id"]
                masks_dir_candidate = WS / "items" / slug / "masks" / seg_id
                if masks_dir_candidate.exists() and any(masks_dir_candidate.glob("*.png")):
                    items.append({"slug": slug, "in_frame": seg["in_frame"], "seg_id": seg_id})
                    break  # one active segment per item
    print(f"Sessions with GT: {len(items)}")

    # Per-frame records (ball-in frames only)
    records: list[dict] = []

    for item in items:
        slug = item["slug"]
        in_f = item["in_frame"]
        seg_id = item["seg_id"]
        masks_dir = WS / "items" / slug / "masks" / seg_id
        frames_dir = WS / "items" / slug / "frames"

        # Load all frames (ball-in + no-ball) for gray lookup
        local_to_gray: dict[int, np.ndarray] = {}
        all_fps = sorted(frames_dir.glob("*.jpg"))
        for fp in all_fps:
            local = int(fp.stem)
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is not None:
                local_to_gray[local] = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        mask_paths = sorted(masks_dir.glob("*.png"))
        for mp in mask_paths:
            src = int(mp.stem)
            local = src - in_f
            fp = frames_dir / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
            if gt is None or (gt > 0).sum() < 5:
                continue
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue

            ys, xs = np.where(gt > 0)
            gtc_x = float(xs.mean())
            gtc_y = float(ys.mean())
            gt_area = int(len(ys))

            # V11
            v11_cands, mask_v11, hsv = detect_v11(bgr)
            v11_hit = hit_check(v11_cands, gtc_x, gtc_y, gt_area)

            # Mode (canonical)
            if v11_hit:
                mode = "HIT"
            else:
                mode = classify_miss_canonical(mask_v11, gt, bgr, v11_cands)

            # Y-diff per threshold
            gray_curr = local_to_gray.get(local)
            gray_prev = local_to_gray.get(local - 1)
            has_prev = gray_curr is not None and gray_prev is not None

            ydiff_hits: dict[int, bool] = {}
            for thr in YDIFF_THRS:
                if has_prev:
                    yd_cands = detect_ydiff(gray_prev, gray_curr, thr)
                    ydiff_hits[thr] = hit_check(yd_cands, gtc_x, gtc_y, gt_area)
                else:
                    ydiff_hits[thr] = False  # no t-1 → explicit 0

            records.append(dict(
                slug=slug,
                local=local,
                gtc_x=gtc_x, gtc_y=gtc_y,
                gt_s=float(hsv[ys, xs, 1].mean()),
                gt_h=float(hsv[ys, xs, 0].mean()),
                mode=mode,
                v11=int(v11_hit),
                yd15=int(ydiff_hits[15]),
                yd30=int(ydiff_hits[30]),
                has_prev=int(has_prev),
            ))

    n = len(records)
    print(f"\nTotal ball-in frames: {n}")

    # ── Binary vectors ───────────────────────────────────────────────────────
    v11   = np.array([r["v11"]  for r in records], dtype=int)
    yd15  = np.array([r["yd15"] for r in records], dtype=int)
    yd30  = np.array([r["yd30"] for r in records], dtype=int)
    modes = [r["mode"] for r in records]

    # ── Marginal recall ──────────────────────────────────────────────────────
    r_v11  = v11.mean()
    r_yd15 = yd15.mean()
    r_yd30 = yd30.mean()
    print(f"\n=== Marginal Recall ===")
    print(f"  V11:     R={r_v11:.4f}  hits={v11.sum()}/{n}")
    print(f"  Y-diff15: R={r_yd15:.4f}  hits={yd15.sum()}/{n}")
    print(f"  Y-diff30: R={r_yd30:.4f}  hits={yd30.sum()}/{n}")

    # ── Union recall ─────────────────────────────────────────────────────────
    union_v11_yd15 = np.maximum(v11, yd15)
    union_v11_yd30 = np.maximum(v11, yd30)
    union_yd15_yd30 = np.maximum(yd15, yd30)
    union_all3     = np.maximum(np.maximum(v11, yd15), yd30)

    r_union_v11_yd15  = union_v11_yd15.mean()
    r_union_v11_yd30  = union_v11_yd30.mean()
    r_union_yd15_yd30 = union_yd15_yd30.mean()
    r_union_all3      = union_all3.mean()

    print(f"\n=== Union Recall ===")
    print(f"  V11 ∪ Y-diff15:  R={r_union_v11_yd15:.4f}  Δ(vs best single)={r_union_v11_yd15 - r_v11:+.4f}")
    print(f"  V11 ∪ Y-diff30:  R={r_union_v11_yd30:.4f}  Δ={r_union_v11_yd30 - r_v11:+.4f}")
    print(f"  Y-diff15 ∪ Y-diff30: R={r_union_yd15_yd30:.4f}  Δ(vs yd15)={r_union_yd15_yd30 - r_yd15:+.4f}")
    print(f"  V11 ∪ Y-diff15 ∪ Y-diff30: R={r_union_all3:.4f}  Δ(vs V11∪yd15)={r_union_all3 - r_union_v11_yd15:+.4f}")

    # ── Diminishing returns ──────────────────────────────────────────────────
    print(f"\n=== Diminishing Returns (adding each cue to V11 base) ===")
    print(f"  Baseline (V11 alone):        {r_v11:.4f}")
    print(f"  + Y-diff15:                  {r_union_v11_yd15:.4f}  (+{(r_union_v11_yd15 - r_v11)*100:.2f}pp)")
    print(f"  + Y-diff30 (on V11∪yd15):    {r_union_all3:.4f}  (+{(r_union_all3 - r_union_v11_yd15)*100:.2f}pp)")
    print(f"  Saturation gap (all3 vs V11): {(r_union_all3 - r_v11)*100:.2f}pp")

    # ── Pairwise confusion + conditional P ───────────────────────────────────
    def confusion_2x2(x: np.ndarray, y: np.ndarray, name_x: str, name_y: str) -> None:
        n00 = int(((x == 0) & (y == 0)).sum())
        n01 = int(((x == 0) & (y == 1)).sum())
        n10 = int(((x == 1) & (y == 0)).sum())
        n11 = int(((x == 1) & (y == 1)).sum())
        total = n00 + n01 + n10 + n11
        p_y1_given_x1 = n11 / (n10 + n11) if (n10 + n11) > 0 else float("nan")
        p_y1_given_x0 = n01 / (n00 + n01) if (n00 + n01) > 0 else float("nan")
        print(f"\n  {name_x} vs {name_y}")
        print(f"    Confusion (row={name_x}, col={name_y}):")
        print(f"      miss/miss={n00}  miss/hit={n01}")
        print(f"      hit/miss={n10}   hit/hit={n11}")
        print(f"    P({name_y}=hit | {name_x}=hit) = {p_y1_given_x1:.3f}")
        print(f"    P({name_y}=hit | {name_x}=miss) = {p_y1_given_x0:.3f}")
        print(f"    ΔP = {p_y1_given_x1 - p_y1_given_x0:+.3f}  (0=independent, large=dependent)")
        mi = binary_mi(x, y)
        h_x = binary_entropy(x)
        h_y = binary_entropy(y)
        nmi = mi / min(h_x, h_y) if min(h_x, h_y) > 0 else 0.0
        print(f"    I({name_x};{name_y}) = {mi:.4f} bits  NMI={nmi:.3f}  H({name_x})={h_x:.4f}  H({name_y})={h_y:.4f}")

    print(f"\n=== Pairwise Confusion + Mutual Information ===")
    confusion_2x2(v11, yd15, "V11", "Y-diff15")
    confusion_2x2(v11, yd30, "V11", "Y-diff30")
    confusion_2x2(yd15, yd30, "Y-diff15", "Y-diff30")

    # ── Per-mode analysis ─────────────────────────────────────────────────────
    print(f"\n=== Per-mode breakdown (canonical classifier) ===")
    mode_list = ["M1", "M2", "M3", "M4", "M5"]

    mode_stats: dict[str, dict] = {}
    for m_label in mode_list + ["HIT"]:
        idx = np.array([i for i, r in enumerate(records) if r["mode"] == m_label])
        if len(idx) == 0:
            continue
        v11_sub  = v11[idx]
        yd15_sub = yd15[idx]
        yd30_sub = yd30[idx]
        union_sub_v11_yd15 = np.maximum(v11_sub, yd15_sub)
        union_sub_v11_yd30 = np.maximum(v11_sub, yd30_sub)
        union_sub_all3 = np.maximum(np.maximum(v11_sub, yd15_sub), yd30_sub)
        r_mode_v11   = v11_sub.mean()
        r_mode_yd15  = yd15_sub.mean()
        r_mode_yd30  = yd30_sub.mean()
        r_mode_union15 = union_sub_v11_yd15.mean()
        r_mode_union30 = union_sub_v11_yd30.mean()
        r_mode_union_all3 = union_sub_all3.mean()
        residual_all3 = int(len(idx)) - int(union_sub_all3.sum())
        best_single = max(r_mode_v11, r_mode_yd15, r_mode_yd30)
        best_cue = (["V11", "yd15", "yd30"])[np.argmax([r_mode_v11, r_mode_yd15, r_mode_yd30])]
        print(f"  {m_label:<4} n={len(idx):>4}  V11={r_mode_v11:.3f}  yd15={r_mode_yd15:.3f}  yd30={r_mode_yd30:.3f} "
              f"→ best={best_cue}({best_single:.3f})  "
              f"V11∪yd15={r_mode_union15:.3f}  V11∪yd30={r_mode_union30:.3f}  "
              f"all3={r_mode_union_all3:.3f}  residual={residual_all3}")
        mode_stats[m_label] = dict(
            n=int(len(idx)),
            r_v11=float(r_mode_v11),
            r_yd15=float(r_mode_yd15),
            r_yd30=float(r_mode_yd30),
            best_cue=best_cue,
            r_union_v11_yd15=float(r_mode_union15),
            r_union_v11_yd30=float(r_mode_union30),
            r_union_all3=float(r_mode_union_all3),
            residual_all3=residual_all3,
        )

    # ── Oracle ceiling ───────────────────────────────────────────────────────
    # Oracle per-frame: selects the best single cue per frame (knows GT)
    # This is the theoretical ceiling for single-cue selection
    oracle_vec = np.maximum(np.maximum(v11, yd15), yd30)  # = simple OR for binary
    r_oracle = oracle_vec.mean()

    # Per-mode oracle: for each mode, route to best cue for that mode
    # Only meaningful as "if we knew the mode, which cue to use?"
    oracle_mode_routed = np.zeros(n, dtype=int)
    mode_to_best: dict[str, str] = {}
    for m_label in mode_list:
        idx = np.array([i for i, r in enumerate(records) if r["mode"] == m_label])
        if len(idx) == 0:
            continue
        r_mode_v11  = v11[idx].mean()
        r_mode_yd15 = yd15[idx].mean()
        r_mode_yd30 = yd30[idx].mean()
        best_idx_within = np.argmax([r_mode_v11, r_mode_yd15, r_mode_yd30])
        best_arr = [v11, yd15, yd30][best_idx_within]
        best_label = ["V11", "Y-diff15", "Y-diff30"][best_idx_within]
        mode_to_best[m_label] = best_label
        oracle_mode_routed[idx] = best_arr[idx]

    # HIT frames: V11 already hits them
    hit_idx = np.array([i for i, r in enumerate(records) if r["mode"] == "HIT"])
    if len(hit_idx) > 0:
        oracle_mode_routed[hit_idx] = v11[hit_idx]  # already 1

    r_oracle_simple_union = r_oracle  # for binary detectors, OR = oracle per-frame
    r_oracle_mode_routed  = oracle_mode_routed.mean()

    print(f"\n=== Oracle Ceiling ===")
    print(f"  Simple OR (V11∪yd15∪yd30):          R={r_oracle_simple_union:.4f}")
    print(f"  Mode-routed oracle (best cue/mode):  R={r_oracle_mode_routed:.4f}")
    print(f"  Gap (mode_routed - simple_OR):        {(r_oracle_mode_routed - r_oracle_simple_union)*100:+.2f}pp")
    print(f"  Mode→best cue mapping: {mode_to_best}")
    print()
    print("  Note: for binary hit/miss detectors, per-frame oracle == simple OR.")
    print("  The 'mode-routed oracle' demonstrates what a perfect routing system")
    print("  achieves vs blindly OR-ing all cues. Gap should be near 0 when cues")
    print("  are all helpful for their respective modes.")

    # ── Per-mode best cue summary ─────────────────────────────────────────────
    print(f"\n=== Per-mode best cue summary ===")
    print(f"  Mode  n    V11    yd15   yd30   best cue    V11∪yd15  V11∪yd30")
    for m_label in mode_list:
        if m_label not in mode_stats:
            continue
        s = mode_stats[m_label]
        print(f"  {m_label:<4}  {s['n']:<4}  {s['r_v11']:.3f}  {s['r_yd15']:.3f}  {s['r_yd30']:.3f}  "
              f"{s['best_cue']:<10}  {s['r_union_v11_yd15']:.3f}     {s['r_union_v11_yd30']:.3f}")

    # ── Save results ──────────────────────────────────────────────────────────
    results = dict(
        n_frames=n,
        marginal=dict(r_v11=r_v11, r_yd15=r_yd15, r_yd30=r_yd30),
        union=dict(
            v11_yd15=r_union_v11_yd15,
            v11_yd30=r_union_v11_yd30,
            yd15_yd30=r_union_yd15_yd30,
            all3=r_union_all3,
        ),
        diminishing_returns=dict(
            baseline_v11=r_v11,
            plus_yd15=r_union_v11_yd15,
            marginal_yd15=r_union_v11_yd15 - r_v11,
            plus_yd30_on_union=r_union_all3,
            marginal_yd30=r_union_all3 - r_union_v11_yd15,
        ),
        pairwise_mi=dict(
            v11_vs_yd15=binary_mi(v11, yd15),
            v11_vs_yd30=binary_mi(v11, yd30),
            yd15_vs_yd30=binary_mi(yd15, yd30),
        ),
        oracle=dict(
            simple_union=r_oracle_simple_union,
            mode_routed=r_oracle_mode_routed,
            gap_pp=(r_oracle_mode_routed - r_oracle_simple_union) * 100,
        ),
        mode_stats=mode_stats,
        mode_to_best=mode_to_best,
        frst_status="N/A — Track A pending (19_frst.py output not yet generated)",
        dl_status="N/A — Track L not yet started",
    )

    out_path = OUT / "22_cue_independence.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n[done] {out_path}")


if __name__ == "__main__":
    main()
