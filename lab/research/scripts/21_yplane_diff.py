"""Y-plane temporal contrast detector — Idea I8 validation.

Per-frame |Y_t - Y_{t-1}| event map on grayscale (proxy for NV12 Y plane).
Completely chroma-blind — bypasses V11 desat failure modes.

Evaluation:
  - Y-diff alone R / FP rate across 9 GT sessions
  - Threshold sweep [10, 15, 20, 25, 30]
  - V11 ∪ Y-diff R + mean cands/frame
  - Per-mode recovery (M1 α, M3 β, M2 fragmentation) using same classifier
    as 19_frst.py (gt_s<80 → M1, gt_h<100 → M3, else M2)
  - Ghost-blob accounting (240fps = ~32 px/frame displacement vs ~19 px ball
    diameter → two blobs per ball-in-flight frame; noted, not treated as FP)
  - Edge-case instrumentation: first paired frame (no t-1) → 0 cands logged
  - Bench: Y-diff only ms/frame

Methodology note: offline frames are JPEG (BT.601-ish luma), not literal
NV12 Y. Acceptable proxy; live performance may differ slightly at specular
edges.

Run: cd lab/research && uv run python scripts/21_yplane_diff.py
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2
import sys
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

sys.path.insert(0, str(Path(__file__).resolve().parent))

M    = load_manifest()

# ── V11 reference ──────────────────────────────────────────────────────────

V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)

THRESHOLDS = [10, 15, 20, 25, 30]


# ── Detectors ──────────────────────────────────────────────────────────────

def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, int]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def detect_ydiff(prev_gray: np.ndarray, curr_gray: np.ndarray,
                 thr: int) -> list[tuple[float, float, int]]:
    """Compute |Y_t - Y_{t-1}|, threshold, morphology, shape gate.

    Uses cv2.absdiff (uint8 safe; plain subtraction wraps).
    Returns candidates [(px, py, area)].
    """
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


# ── GT helpers ─────────────────────────────────────────────────────────────

def hit_check(cands: list[tuple[float, float, int]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2
               for cx, cy, _ in cands)


def classify_miss_mode(gt_s: float, gt_h: float) -> str:
    """Same heuristic as 19_frst.py / 02_v11_followup.md proxy.

    M1 (Mode α specular/desat): gt_s < 80
    M3 (Mode β hue-shift):      gt_h < 100
    M2 (fragmentation):         else
    """
    if gt_s < 80:
        return "M1"
    if gt_h < 100:
        return "M3"
    return "M2"


# ── Load frames for one session ────────────────────────────────────────────

def load_session_frames(item: dict) -> list[dict]:
    slug = item["slug"]
    in_f = item["in_frame"]
    masks_dir = WS / "items" / slug / "masks" / SEG_BY_SLUG[slug]
    frames_dir = WS / "items" / slug / "frames"

    rows: list[dict] = []
    for mp in sorted(masks_dir.glob("*.png")):
        src = int(mp.stem)
        local = src - in_f
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        gt = read_mask(mp)
        if gt is None:
            continue
        ball_in = int((gt > 0).sum()) >= 5
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        row: dict = dict(slug=slug, src=src, local=local, ball_in=ball_in,
                         bgr=bgr, gt=gt)
        if ball_in:
            ys, xs = np.where(gt > 0)
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            row.update(
                gtc_x=float(xs.mean()),
                gtc_y=float(ys.mean()),
                gt_area=int(len(ys)),
                gt_s=float(hsv[ys, xs, 1].mean()),
                gt_v=float(hsv[ys, xs, 2].mean()),
                gt_h=float(hsv[ys, xs, 0].mean()),
            )
        rows.append(row)
    return rows


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    items = [it for it in M["items"]
             if it.get("propagate_status") == "done" and it.get("in_frame") is not None]
    print(f"Sessions with GT: {len(items)}")

    # ── Threshold sweep aggregates ─────────────────────────────────────────
    # thr → {total_ball_in, ydiff_hits, union_hits, ydiff_cands_ball,
    #         ydiff_cands_noball, noball_frames,
    #         m1_miss, m1_recovered, m2_miss, m2_recovered, m3_miss, m3_recovered}
    sweep: dict[int, dict] = {
        t: dict(total_ball_in=0, ydiff_hits=0, union_hits=0,
                ydiff_cands_ball=0, ydiff_cands_noball=0, noball_frames=0,
                m1_miss=0, m1_rec=0, m2_miss=0, m2_rec=0, m3_miss=0, m3_rec=0,
                no_prev_count=0)
        for t in THRESHOLDS
    }
    v11_total_ball_in = 0
    v11_hits = 0

    bench_grays: list[tuple[np.ndarray, np.ndarray]] = []  # (prev, curr) pairs

    for item in items:
        slug = item["slug"]
        frames = load_session_frames(item)
        print(f"\n  [{slug}] frames={len(frames)} "
              f"ball_in={sum(r['ball_in'] for r in frames)}")

        # Build gray array indexed by local frame number for fast t-1 lookup
        local_to_gray: dict[int, np.ndarray] = {}
        for row in frames:
            local_to_gray[row["local"]] = cv2.cvtColor(row["bgr"], cv2.COLOR_BGR2GRAY)

        for row in frames:
            bgr = row["bgr"]
            local = row["local"]
            ball_in = row["ball_in"]
            gray_curr = local_to_gray[local]

            # Look up t-1: previous local index (local - 1)
            prev_local = local - 1
            gray_prev = local_to_gray.get(prev_local)
            has_prev = gray_prev is not None

            if has_prev and len(bench_grays) < 300:
                bench_grays.append((gray_prev, gray_curr))

            # V11 (threshold-independent)
            v11_cands = detect_v11(bgr)
            v11_hit = False
            if ball_in:
                v11_total_ball_in += 1
                v11_hit = hit_check(v11_cands, row["gtc_x"], row["gtc_y"], row["gt_area"])
                if v11_hit:
                    v11_hits += 1

            # Per-threshold Y-diff
            for thr, acc in sweep.items():
                if not has_prev:
                    # t-1 not available → emit 0 candidates (explicit, no fallback)
                    acc["no_prev_count"] += 1
                    ydiff_cands: list[tuple[float, float, int]] = []
                else:
                    ydiff_cands = detect_ydiff(gray_prev, gray_curr, thr)

                if ball_in:
                    acc["total_ball_in"] += 1
                    acc["ydiff_cands_ball"] += len(ydiff_cands)
                    yd_hit = hit_check(ydiff_cands, row["gtc_x"], row["gtc_y"], row["gt_area"])
                    if yd_hit:
                        acc["ydiff_hits"] += 1

                    # Union
                    # dedup: if ydiff cand within 5 px of v11 cand, drop duplicate
                    union_cands = list(v11_cands)
                    for yd in ydiff_cands:
                        if not any((yd[0] - v[0]) ** 2 + (yd[1] - v[1]) ** 2 <= 25.0
                                   for v in v11_cands):
                            union_cands.append(yd)
                    union_hit = hit_check(union_cands, row["gtc_x"], row["gtc_y"], row["gt_area"])
                    if union_hit:
                        acc["union_hits"] += 1

                    # Mode breakdown (only V11 misses)
                    if not v11_hit:
                        mode = classify_miss_mode(row["gt_s"], row["gt_h"])
                        if mode == "M1":
                            acc["m1_miss"] += 1
                            if yd_hit:
                                acc["m1_rec"] += 1
                        elif mode == "M2":
                            acc["m2_miss"] += 1
                            if yd_hit:
                                acc["m2_rec"] += 1
                        elif mode == "M3":
                            acc["m3_miss"] += 1
                            if yd_hit:
                                acc["m3_rec"] += 1
                else:
                    # no-ball frame: count spurious ydiff detections
                    acc["noball_frames"] += 1
                    acc["ydiff_cands_noball"] += len(ydiff_cands)

    # ── V11 baseline ───────────────────────────────────────────────────────
    r_v11 = v11_hits / v11_total_ball_in if v11_total_ball_in else 0.0
    print(f"\n=== V11 baseline ===")
    print(f"  R={r_v11:.4f}  ({v11_hits}/{v11_total_ball_in})")

    # ── Threshold sweep table ──────────────────────────────────────────────
    print(f"\n=== Threshold sweep ===")
    hdr = (f"{'thr':>4}  {'R_alone':>8}  {'R_union':>8}  "
           f"{'cands/f(ball)':>13}  {'cands/f(noball)':>15}  "
           f"{'FP/f':>6}  "
           f"{'M1_rec%':>8}  {'M2_rec%':>8}  {'M3_rec%':>8}  "
           f"{'no_prev':>7}")
    print(hdr)
    print("-" * len(hdr))

    sweep_rows = []
    for thr in THRESHOLDS:
        acc = sweep[thr]
        n = acc["total_ball_in"]
        nb = acc["noball_frames"]
        r_alone = acc["ydiff_hits"] / n if n else 0.0
        r_union = acc["union_hits"] / n if n else 0.0
        cpf_ball = acc["ydiff_cands_ball"] / n if n else 0.0
        cpf_noball = acc["ydiff_cands_noball"] / nb if nb else 0.0
        m1r = acc["m1_rec"] / acc["m1_miss"] * 100 if acc["m1_miss"] else 0.0
        m2r = acc["m2_rec"] / acc["m2_miss"] * 100 if acc["m2_miss"] else 0.0
        m3r = acc["m3_rec"] / acc["m3_miss"] * 100 if acc["m3_miss"] else 0.0
        print(f"  {thr:>3}  {r_alone:>8.4f}  {r_union:>8.4f}  "
              f"{cpf_ball:>13.1f}  {cpf_noball:>15.1f}  "
              f"{cpf_noball:>6.2f}  "
              f"{m1r:>7.1f}%  {m2r:>7.1f}%  {m3r:>7.1f}%  "
              f"{acc['no_prev_count']:>7}")
        sweep_rows.append(dict(
            thr=thr, r_alone=r_alone, r_union=r_union,
            cpf_ball=cpf_ball, cpf_noball=cpf_noball,
            m1_miss=acc["m1_miss"], m1_rec=acc["m1_rec"], m1_rec_pct=m1r,
            m2_miss=acc["m2_miss"], m2_rec=acc["m2_rec"], m2_rec_pct=m2r,
            m3_miss=acc["m3_miss"], m3_rec=acc["m3_rec"], m3_rec_pct=m3r,
            no_prev=acc["no_prev_count"],
        ))

    # ── Mode sanity check ──────────────────────────────────────────────────
    # Use thr=15 as reference
    acc15 = sweep[15]
    total_miss_classified = acc15["m1_miss"] + acc15["m2_miss"] + acc15["m3_miss"]
    v11_total_miss = v11_total_ball_in - v11_hits
    print(f"\n  Mode sanity (thr=15): M1={acc15['m1_miss']} M2={acc15['m2_miss']} "
          f"M3={acc15['m3_miss']} classified={total_miss_classified} "
          f"v11_miss={v11_total_miss}  "
          f"(expect canonical: 68/24/9=101 or 68/24/10=102)")

    # ── Bench ──────────────────────────────────────────────────────────────
    print(f"\n=== Bench (Y-diff only, thr=15) ===")
    if bench_grays:
        # warm-up
        for prev, curr in bench_grays[:10]:
            detect_ydiff(prev, curr, 15)
        t0 = time.perf_counter()
        for prev, curr in bench_grays:
            detect_ydiff(prev, curr, 15)
        elapsed = time.perf_counter() - t0
        ms_mean = elapsed / len(bench_grays) * 1000.0
        # p95 via per-frame timing
        per_frame_ms = []
        for prev, curr in bench_grays:
            t1 = time.perf_counter()
            detect_ydiff(prev, curr, 15)
            per_frame_ms.append((time.perf_counter() - t1) * 1000.0)
        ms_p95 = float(np.percentile(per_frame_ms, 95))
        print(f"  n={len(bench_grays)} frames  mean={ms_mean:.3f} ms  p95={ms_p95:.3f} ms")
    else:
        print("  (no bench frames available)")
        ms_mean = float("nan")
        ms_p95 = float("nan")

    # ── Ghost-blob note ────────────────────────────────────────────────────
    # At 240fps, ball displacement ≈ 30-32 px/frame (5m, 30 m/s, 1920px FOV 73°).
    # Ball diameter ≈ 19 px.  Displacement > diameter → t and t-1 ball positions
    # don't overlap → diff produces TWO blobs (leaving + entering silhouette).
    # Both pass shape gate.  Recall robust (entering blob matches GT_t).
    # This inflates cands/frame by ~1 on ball-in frames; not a defect.
    print("\n  [note] ghost blob: ~32 px displacement vs ~19 px ball diam at 240fps "
          "→ diff produces 2 blobs/frame (leaving + entering). Entering blob = GT_t."
          " Inflates cands/f ~+1 on ball-in frames; not counted as FP.")

    # ── Save results ───────────────────────────────────────────────────────
    results = dict(
        r_v11=r_v11,
        v11_total_ball_in=v11_total_ball_in,
        v11_hits=v11_hits,
        sweep=sweep_rows,
        ms_mean_thr15=ms_mean,
        ms_p95_thr15=ms_p95,
        bench_frames=len(bench_grays),
    )
    out_path = OUT / "21_yplane_diff_results.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n[done] {out_path}")


if __name__ == "__main__":
    main()
