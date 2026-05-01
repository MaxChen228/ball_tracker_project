"""Stateful ROI-FRST — V11 + Y-diff + spatially-gated FRST.

Research question: can stateful ROI-FRST push V11+Y-diff (R=0.970) toward
V11+full-FRST (R=0.996) without the catastrophic FP rate?

Design:
  Mode 1 (temporal anchor): V11 or Y-diff hit at frame t → arm ROI-FRST for
    next K=5 frames. ROI = union of 100×100 px windows around each hit centroid.
  Mode 2 (prior frame ROI): same as Mode 1 + if t has no active anchor,
    propagate last-known ROI forward indefinitely.

Both modes explicitly break the stateless single-frame contract.
No GT centre used as anchor — only V11 / Y-diff detections.

Evaluation: 9 sessions / 1073 GT frames (same harness as 19_frst.py / 21_yplane_diff.py)
Train session: session_s_16ec069a_b

Run: cd server && uv run python ../lab-research/scripts/24_roi_frst.py
"""
from __future__ import annotations
import json, time, sys
from pathlib import Path
import numpy as np
import cv2

# Force unbuffered stdout for background runs
sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parents[2]
WS   = ROOT / "lab" / "standalone_workspace"

# ── Constants ─────────────────────────────────────────────────────────────────

RADII     = [3, 5, 8, 12]
ROI_HALF  = 50           # 100×100 px ROI half-side
K_FRAMES  = 5            # temporal anchor window
YDIFF_THR = 15           # best threshold from 21_yplane_diff.py
FRST_THR  = 0.10         # same as tuned in 19_frst.py
DEDUP_R   = 5.0          # dedup radius px

# V11 shape-gate params (same as 21_yplane_diff.py)
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255),
           aspect=0.40, fill=0.35, area=(3, 150_000), close=3)


# ── Manifest loading (schema v2 with nested segments) ─────────────────────────

def load_items(M: dict) -> list[dict]:
    flat = []
    for it in M["items"]:
        for seg in it.get("segments", []):
            if seg.get("propagate_status") == "done":
                flat.append({
                    "slug": it["slug"],
                    "in_frame": seg["in_frame"],
                    "out_frame": seg["out_frame"],
                    "seg_id": seg["id"],
                })
    return flat


# ── Detectors ─────────────────────────────────────────────────────────────────

def detect_v11(bgr: np.ndarray) -> list[tuple[float, float, float]]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo  = np.array([V11["h"][0], V11["s"][0], V11["v"][0]], dtype=np.uint8)
    hi  = np.array([V11["h"][1], V11["s"][1], V11["v"][1]], dtype=np.uint8)
    m   = cv2.inRange(hsv, lo, hi)
    k   = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (V11["close"], V11["close"]))
    m   = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def detect_ydiff(prev_gray: np.ndarray, curr_gray: np.ndarray,
                 thr: int) -> list[tuple[float, float, float]]:
    d = cv2.absdiff(curr_gray, prev_gray)
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return _shape_gate(m)


def _shape_gate(m: np.ndarray) -> list[tuple[float, float, float]]:
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out: list[tuple[float, float, float]] = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < V11["area"][0] or a > V11["area"][1]:
            continue
        w_ = int(stats[i, cv2.CC_STAT_WIDTH])
        h_ = int(stats[i, cv2.CC_STAT_HEIGHT])
        if w_ <= 0 or h_ <= 0:
            continue
        asp = min(w_, h_) / max(w_, h_)
        if asp < V11["aspect"]:
            continue
        fill = a / (w_ * h_)
        if fill < V11["fill"]:
            continue
        out.append((float(cents[i, 0]), float(cents[i, 1]), float(a)))
    return out


# ── FRST (reused from 19_frst.py) ────────────────────────────────────────────

def frst(gray: np.ndarray, radii: list[int] = RADII) -> np.ndarray:
    """FRST symmetry map on arbitrary-size gray patch."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    valid = mag > 5.0
    inv_mag = np.where(valid, 1.0 / (mag + 1e-6), 0.0).astype(np.float32)
    gx_n = (gx * inv_mag).ravel()
    gy_n = (gy * inv_mag).ravel()
    h, w = gray.shape
    ys_flat = np.repeat(np.arange(h, dtype=np.int32), w)
    xs_flat = np.tile(np.arange(w, dtype=np.int32), h)
    vf = valid.ravel()
    gx_nv, gy_nv = gx_n[vf], gy_n[vf]
    xs_v, ys_v = xs_flat[vf], ys_flat[vf]
    S = np.zeros((h, w), dtype=np.float32)
    for r in radii:
        px_v = np.clip(np.round(xs_v - r * gx_nv).astype(np.int32), 0, w - 1)
        py_v = np.clip(np.round(ys_v - r * gy_nv).astype(np.int32), 0, h - 1)
        idx = py_v * w + px_v
        O_r = np.bincount(idx, minlength=h * w).reshape(h, w).astype(np.float32)
        sigma = max(1.0, 0.25 * r)
        ksize = int(2 * np.ceil(2 * sigma) + 1)
        M_r = cv2.GaussianBlur(O_r, (ksize, ksize), sigma) / float(r)
        S += M_r
    return S


def frst_roi_cands(gray: np.ndarray,
                   rois: list[tuple[int, int, int, int]],
                   threshold: float = FRST_THR,
                   nms_r: int = 5) -> list[tuple[float, float, float]]:
    """Run FRST in union of ROI boxes, return (px, py, score) in full coords."""
    if not rois:
        return []
    H, W = gray.shape
    ksize = 2 * nms_r + 1
    kernel = np.ones((ksize, ksize), dtype=np.uint8)
    cands: list[tuple[float, float, float]] = []
    for (x0, y0, x1, y1) in rois:
        x0 = max(0, x0); y0 = max(0, y0)
        x1 = min(W, x1); y1 = min(H, y1)
        if x1 <= x0 or y1 <= y0:
            continue
        patch = gray[y0:y1, x0:x1]
        S = frst(patch, RADII)
        dilated = cv2.dilate(S, kernel)
        lm = (S == dilated) & (S > threshold)
        ys_lm, xs_lm = np.where(lm)
        for i in range(len(ys_lm)):
            cands.append((float(xs_lm[i] + x0), float(ys_lm[i] + y0),
                          float(S[ys_lm[i], xs_lm[i]])))
    return cands


def make_roi(cx: float, cy: float, half: int, W: int, H: int
             ) -> tuple[int, int, int, int]:
    return (int(max(0, cx - half)), int(max(0, cy - half)),
            int(min(W, cx + half)), int(min(H, cy + half)))


def merge_rois(rois: list[tuple[int,int,int,int]]
               ) -> list[tuple[int,int,int,int]]:
    """Merge overlapping axis-aligned bounding boxes."""
    if not rois:
        return []
    boxes = sorted(rois, key=lambda r: r[0])
    merged = [list(boxes[0])]
    for x0, y0, x1, y1 in boxes[1:]:
        mx0, my0, mx1, my1 = merged[-1]
        if x0 <= mx1 and y0 <= my1 and x1 >= mx0 and y1 >= my0:
            merged[-1] = [min(mx0, x0), min(my0, y0),
                          max(mx1, x1), max(my1, y1)]
        else:
            merged.append([x0, y0, x1, y1])
    return [tuple(b) for b in merged]


# ── GT helpers ────────────────────────────────────────────────────────────────

def hit_check(cands: list[tuple[float, float, float]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x)**2 + (cy - gtc_y)**2 <= tol2 for cx, cy, _ in cands)


def dedup_union(primary: list[tuple], extra: list[tuple],
                r: float = DEDUP_R) -> list[tuple]:
    if not extra:
        return list(primary)
    merged = list(primary)
    r2 = r ** 2
    for ec in extra:
        ex, ey = ec[0], ec[1]
        if not any((ex - px)**2 + (ey - py)**2 <= r2
                   for px, py, _ in merged):
            merged.append(ec)
    return merged


def classify_miss_mode(gt_s: float, gt_h: float) -> str:
    if gt_s < 80:
        return "M1"
    if gt_h < 100:
        return "M3"
    return "M2"


# ── Streaming session evaluator (single-pass, minimal memory) ────────────────

def iter_session_frames(item: dict):
    """Yield frame dicts one at a time — BGR NOT stored after yield."""
    slug   = item["slug"]
    in_f   = item["in_frame"]
    seg_id = item["seg_id"]
    masks_dir  = WS / "items" / slug / "masks" / seg_id
    frames_dir = WS / "items" / slug / "frames"

    for mp in sorted(masks_dir.glob("*.png")):
        src   = int(mp.stem)
        local = src - in_f
        fp    = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
        if gt is None:
            continue
        ball_in = int((gt > 0).sum()) >= 5
        bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        row: dict = dict(slug=slug, src=src, local=local,
                         ball_in=ball_in, bgr=bgr)
        if ball_in:
            ys_gt, xs_gt = np.where(gt > 0)
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            row.update(
                gtc_x=float(xs_gt.mean()),
                gtc_y=float(ys_gt.mean()),
                gt_area=int(len(ys_gt)),
                gt_s=float(hsv[ys_gt, xs_gt, 1].mean()),
                gt_v=float(hsv[ys_gt, xs_gt, 2].mean()),
                gt_h=float(hsv[ys_gt, xs_gt, 0].mean()),
            )
        yield row


def eval_session(item: dict, K: int = K_FRAMES, roi_half: int = ROI_HALF
                 ) -> list[dict]:
    """Stream frames one at a time, maintaining only prev_gray + ROI state."""
    W: int = 0
    H: int = 0

    # Mode 1 state: list of (roi, ttl)
    m1_state: list[tuple[tuple, int]] = []
    # Mode 2 state: same + last_known_roi (persists across misses)
    m2_state: list[tuple[tuple, int]] = []
    last_known_roi: tuple | None = None
    prev_gray: np.ndarray | None = None
    results: list[dict] = []

    for row in iter_session_frames(item):
        bgr   = row["bgr"]
        local = row["local"]

        # Initialise dimensions from first frame
        if W == 0:
            H, W = bgr.shape[:2]

        gray  = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        # V11
        v11_t = detect_v11(bgr)
        # Y-diff: get all candidates (needed for hit_check coverage).
        # Limit to top-50 ONLY for dedup cost; the full list hits GT via
        # any(hit_check) scan which is O(N) not O(N²).
        yd_all = detect_ydiff(prev_gray, gray, YDIFF_THR) if prev_gray is not None else []

        # Base union for hit_check: V11 + all ydiff (no dedup overhead —
        # hit_check is O(N) linear scan, not O(N²)).
        base_union_full: list[tuple] = list(v11_t) + yd_all

        # Deduplicated base union (used for ROI anchoring only — top-50 limit
        # bounds the O(N×M) dedup cost).
        yd_top50 = sorted(yd_all, key=lambda c: c[2], reverse=True)[:50]
        base_union_dedup = dedup_union(v11_t, yd_top50)

        # New ROI: top-1 candidate by area from dedup union.
        top1 = max(base_union_dedup, key=lambda c: c[2]) if base_union_dedup else None
        new_rois: list[tuple] = []
        if top1 is not None:
            new_rois.append(make_roi(top1[0], top1[1], roi_half, W, H))

        # base_union for hit_check evaluation = full list
        base_union = base_union_full

        # ── Mode 1 ────────────────────────────────────────────────────────
        m1_active = [roi for roi, ttl in m1_state if ttl > 0]
        m1_roi_c  = frst_roi_cands(gray, merge_rois(m1_active)) if m1_active else []
        m1_union  = dedup_union(base_union, m1_roi_c)
        m1_state  = [(roi, ttl - 1) for roi, ttl in m1_state if ttl > 1]
        for roi in new_rois:
            m1_state.append((roi, K))

        # ── Mode 2 ────────────────────────────────────────────────────────
        m2_active = [roi for roi, ttl in m2_state if ttl > 0]
        if not m2_active and last_known_roi is not None:
            m2_active = [last_known_roi]
        m2_roi_c  = frst_roi_cands(gray, merge_rois(m2_active)) if m2_active else []
        m2_union  = dedup_union(base_union, m2_roi_c)
        m2_state  = [(roi, ttl - 1) for roi, ttl in m2_state if ttl > 1]
        for roi in new_rois:
            m2_state.append((roi, K))
        if new_rois:
            last_known_roi = merge_rois(new_rois)[0]

        prev_gray = gray

        # ── Record ────────────────────────────────────────────────────────
        rec: dict = dict(
            slug=row["slug"], local=local, ball_in=row["ball_in"],
            v11_hit=False, base_hit=False, m1_hit=False, m2_hit=False,
            m1_roi_cands=len(m1_roi_c), m2_roi_cands=len(m2_roi_c),
            base_cands=len(base_union),
            noball_base_cands=0, noball_m1_cands=0, noball_m2_cands=0,
        )
        if not row["ball_in"]:
            # FP count uses dedup union (comparable across detectors)
            rec["noball_base_cands"] = len(base_union_dedup)
            rec["noball_m1_cands"]   = len(dedup_union(base_union_dedup, m1_roi_c))
            rec["noball_m2_cands"]   = len(dedup_union(base_union_dedup, m2_roi_c))
        else:
            gx, gy, ga = row["gtc_x"], row["gtc_y"], row["gt_area"]
            rec["v11_hit"]  = hit_check(v11_t,      gx, gy, ga)
            rec["base_hit"] = hit_check(base_union,  gx, gy, ga)
            rec["m1_hit"]   = hit_check(m1_union,    gx, gy, ga)
            rec["m2_hit"]   = hit_check(m2_union,    gx, gy, ga)
            rec.update(gtc_x=gx, gtc_y=gy, gt_area=ga,
                       gt_s=row["gt_s"], gt_v=row["gt_v"], gt_h=row["gt_h"])
        results.append(rec)

    return results


# ── Benchmark ─────────────────────────────────────────────────────────────────

def bench_roi_frst(item: dict, roi_half: int = ROI_HALF, n: int = 100) -> dict:
    """Measure ms/frame for ROI-FRST vs full-frame FRST."""
    frames = []
    for row in iter_session_frames(item):
        frames.append(row["bgr"])
        if len(frames) >= n:
            break

    H, W = frames[0].shape[:2]
    cx, cy = W // 2, H // 2
    roi_single = make_roi(cx, cy, roi_half, W, H)

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    # Warm-up
    for g in grays[:10]:
        frst_roi_cands(g, [roi_single])

    t0 = time.perf_counter()
    for g in grays:
        frst_roi_cands(g, [roi_single])
    ms_roi = (time.perf_counter() - t0) / len(grays) * 1000.0

    # Full-frame baseline
    for g in grays[:10]:
        frst(g, RADII)
    t1 = time.perf_counter()
    for g in grays:
        frst(g, RADII)
    ms_full = (time.perf_counter() - t1) / len(grays) * 1000.0

    roi_px  = (roi_single[2] - roi_single[0]) * (roi_single[3] - roi_single[1])
    full_px = H * W
    return dict(
        ms_roi=ms_roi, ms_full=ms_full,
        speedup=ms_full / ms_roi if ms_roi > 0 else 0,
        roi_px=roi_px, full_px=full_px, fraction=roi_px / full_px,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    M = json.loads((WS / "manifest.json").read_text())
    items = load_items(M)
    print(f"Sessions: {len(items)}")
    for it in items:
        print(f"  {it['slug']}  in_frame={it['in_frame']}")

    TRAIN_SLUG = "session_s_16ec069a_b"
    all_results: list[dict] = []
    per_session: list[dict] = []
    session_results_map: dict[str, list[dict]] = {}

    for item in items:
        slug = item["slug"]
        print(f"\n[{slug}]", flush=True)
        t0 = time.perf_counter()
        results = eval_session(item)
        elapsed = time.perf_counter() - t0
        all_results.extend(results)
        session_results_map[slug] = results

        ball_r   = [r for r in results if r["ball_in"]]
        noball_r = [r for r in results if not r["ball_in"]]
        n  = len(ball_r)
        nb = len(noball_r)
        if n == 0:
            print(f"  no ball-in frames")
            continue

        v11_h  = sum(1 for r in ball_r if r["v11_hit"])
        base_h = sum(1 for r in ball_r if r["base_hit"])
        m1_h   = sum(1 for r in ball_r if r["m1_hit"])
        m2_h   = sum(1 for r in ball_r if r["m2_hit"])
        fp_base = sum(r["noball_base_cands"] for r in noball_r) / nb if nb else 0.0
        fp_m1   = sum(r["noball_m1_cands"]   for r in noball_r) / nb if nb else 0.0
        fp_m2   = sum(r["noball_m2_cands"]   for r in noball_r) / nb if nb else 0.0

        split = "train" if slug == TRAIN_SLUG else "test"
        per_session.append(dict(
            slug=slug, split=split, n=n,
            r_v11=v11_h/n, r_base=base_h/n, r_m1=m1_h/n, r_m2=m2_h/n,
            fp_base=fp_base, fp_m1=fp_m1, fp_m2=fp_m2,
        ))
        print(f"  n={n} V11={v11_h/n:.3f} Base={base_h/n:.3f} "
              f"M1={m1_h/n:.3f} M2={m2_h/n:.3f} "
              f"FP_base={fp_base:.1f} FP_m1={fp_m1:.1f} FP_m2={fp_m2:.1f} "
              f"({elapsed:.0f}s)")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    ball_all   = [r for r in all_results if r["ball_in"]]
    noball_all = [r for r in all_results if not r["ball_in"]]
    N  = len(ball_all)
    NB = len(noball_all)

    r_v11  = sum(1 for r in ball_all if r["v11_hit"])  / N
    r_base = sum(1 for r in ball_all if r["base_hit"]) / N
    r_m1   = sum(1 for r in ball_all if r["m1_hit"])   / N
    r_m2   = sum(1 for r in ball_all if r["m2_hit"])   / N
    fp_base_agg = sum(r["noball_base_cands"] for r in noball_all) / NB if NB else 0.0
    fp_m1_agg   = sum(r["noball_m1_cands"]   for r in noball_all) / NB if NB else 0.0
    fp_m2_agg   = sum(r["noball_m2_cands"]   for r in noball_all) / NB if NB else 0.0

    print(f"\n{'='*60}")
    print(f"AGGREGATE ({N} ball-in / {NB} no-ball frames)")
    print(f"{'='*60}")
    print(f"  V11 alone          R = {r_v11:.4f}  (expect 0.905)")
    print(f"  V11 + Y-diff       R = {r_base:.4f}  (expect 0.970)")
    print(f"  + ROI-FRST Mode 1  R = {r_m1:.4f}")
    print(f"  + ROI-FRST Mode 2  R = {r_m2:.4f}")
    print(f"  FP baseline        {fp_base_agg:.1f} cands/frame (no-ball)")
    print(f"  FP Mode 1          {fp_m1_agg:.1f} cands/frame (no-ball)")
    print(f"  FP Mode 2          {fp_m2_agg:.1f} cands/frame (no-ball)")

    # ── Rescue breakdown for base misses ──────────────────────────────────────
    base_misses = [r for r in ball_all if not r["base_hit"]]
    m1_rescue = sum(1 for r in base_misses if r["m1_hit"])
    m2_rescue = sum(1 for r in base_misses if r["m2_hit"])
    print(f"\n--- V11+Y-diff misses remaining: {len(base_misses)} ---")
    print(f"  Mode 1 rescued: {m1_rescue}/{len(base_misses)}")
    print(f"  Mode 2 rescued: {m2_rescue}/{len(base_misses)}")
    for mm in ["M1", "M2", "M3"]:
        mode_miss = [r for r in base_misses
                     if "gt_s" in r and classify_miss_mode(r["gt_s"], r["gt_h"]) == mm]
        if not mode_miss:
            continue
        mm1 = sum(1 for r in mode_miss if r["m1_hit"])
        mm2 = sum(1 for r in mode_miss if r["m2_hit"])
        print(f"  {mm}: n={len(mode_miss):3d}  "
              f"M1={mm1}({mm1/len(mode_miss)*100:.0f}%)  "
              f"M2={mm2}({mm2/len(mode_miss)*100:.0f}%)")

    # ── Long miss run: 170a6a89_b (reuse results from main loop) ────────────────
    print(f"\n--- Long miss run: session_s_170a6a89_b ---")
    b_res  = session_results_map.get("session_s_170a6a89_b", [])
    b_ball = [r for r in b_res if r["ball_in"]]

    # Opening run: consecutive ball-in from start where base misses
    opening_len = 0
    for r in b_ball:
        if not r["base_hit"]:
            opening_len += 1
        else:
            break

    m1_cov = sum(1 for r in b_ball[:opening_len] if r["m1_hit"])
    m2_cov = sum(1 for r in b_ball[:opening_len] if r["m2_hit"])
    print(f"  Opening miss run (base miss from start): {opening_len} frames")
    print(f"  Mode 1 covers: {m1_cov}/{opening_len}")
    print(f"  Mode 2 covers: {m2_cov}/{opening_len}")

    if b_ball:
        print(f"  Per-frame (first 25 ball-in):")
        print(f"  {'local':>6}  {'V11':>4}  {'Base':>5}  {'M1':>4}  {'M2':>4}  "
              f"{'M1c':>5}  {'M2c':>5}")
        for r in b_ball[:25]:
            print(f"  {r['local']:>6}  "
                  f"{'Y' if r['v11_hit'] else '.':>4}  "
                  f"{'Y' if r['base_hit'] else '.':>5}  "
                  f"{'Y' if r['m1_hit'] else '.':>4}  "
                  f"{'Y' if r['m2_hit'] else '.':>4}  "
                  f"{r['m1_roi_cands']:>5}  {r['m2_roi_cands']:>5}")

    # ── Bench ─────────────────────────────────────────────────────────────────
    print(f"\n--- Bench (100×100 ROI-FRST vs full-frame) ---")
    train_item = next(it for it in items if it["slug"] == TRAIN_SLUG)
    bench = bench_roi_frst(train_item)
    print(f"  ROI 100×100: {bench['ms_roi']:.2f} ms/frame")
    print(f"  Full-frame:  {bench['ms_full']:.1f} ms/frame")
    print(f"  Speedup:     {bench['speedup']:.0f}×")
    iphone_lo = bench["ms_roi"] * 0.10
    iphone_hi = bench["ms_roi"] * 0.25
    print(f"  iPhone 14 C++ estimate: ~{iphone_lo:.2f}–{iphone_hi:.2f} ms")

    # ── Per-session table ──────────────────────────────────────────────────────
    print(f"\n--- Per-session table ---")
    hdr = f"{'session':<28} {'split':>5} {'n':>5}  {'V11':>5}  {'Base':>5}  {'M1':>5}  {'M2':>5}  {'FP_b':>6}  {'FP_m1':>7}  {'FP_m2':>7}"
    print(hdr)
    print("-" * len(hdr))
    for s in per_session:
        print(f"  {s['slug'][:26]:<26} {s['split']:>5} {s['n']:>5}  "
              f"{s['r_v11']:>5.3f}  {s['r_base']:>5.3f}  "
              f"{s['r_m1']:>5.3f}  {s['r_m2']:>5.3f}  "
              f"{s['fp_base']:>6.1f}  {s['fp_m1']:>7.1f}  {s['fp_m2']:>7.1f}")
    print("-" * len(hdr))

    # ── Save ──────────────────────────────────────────────────────────────────
    out = ROOT / "lab-research" / "outputs" / "24_roi_frst_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(dict(
        total_n=N, r_v11=r_v11, r_base=r_base, r_m1=r_m1, r_m2=r_m2,
        fp_base_agg=fp_base_agg, fp_m1_agg=fp_m1_agg, fp_m2_agg=fp_m2_agg,
        base_misses_n=len(base_misses), m1_rescue=m1_rescue, m2_rescue=m2_rescue,
        long_run=dict(session="170a6a89_b", opening_len=opening_len,
                      m1_cov=m1_cov, m2_cov=m2_cov),
        per_session=per_session, bench=bench,
        params=dict(K=K_FRAMES, roi_half=ROI_HALF, ydiff_thr=YDIFF_THR,
                    frst_thr=FRST_THR, radii=RADII),
    ), indent=2))
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()
