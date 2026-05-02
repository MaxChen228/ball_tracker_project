"""Fast Radial Symmetry Transform (FRST) — grayscale ball detector.

Implementation of Loy & Zelinsky 2003, bright-only branch:
  - vote accumulation at p - r*g_hat (against gradient = radial centre voting)
  - radii [3, 5, 8, 12] px tuned for ball area range ~60-3000 px²
  - 5-px NMS via dilate comparison
  - threshold tuned on session_s_16ec069a_b (train); 8 remaining sessions = test

Evaluation:
  - FRST alone R + FP rate
  - V11 ∪ FRST (5-px dedup) R + mean cands/frame
  - Mode α (M1, n=68 high-V/low-S) vs Mode β (M3 hue-shift, n=9) vs M2 recovery
  - Bench ms/frame

Run: cd lab/research && uv run python scripts/19_frst.py
"""
from __future__ import annotations
import json, time
from pathlib import Path
import numpy as np
import cv2
import sys

# Add lab/research/scripts to path so we can import ball_detector
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ball_detector import BallDetector, BallDetectorConfig
from _paths import ROOT, WS

M    = json.loads((WS / "manifest.json").read_text())

# ------------------------------------------------------------------
# FRST implementation
# ------------------------------------------------------------------

def _precompute_gradients(gray: np.ndarray, grad_thresh: float = 5.0
                          ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compute normalised gradient unit vectors and valid-pixel flat indices.

    Returns (gx_n_flat, gy_n_flat, xs_valid_flat, ys_valid_flat) for valid pixels.
    """
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    valid = mag > grad_thresh
    inv_mag = np.where(valid, 1.0 / (mag + 1e-6), 0.0).astype(np.float32)
    gx_n = (gx * inv_mag).ravel()
    gy_n = (gy * inv_mag).ravel()
    h, w = gray.shape
    ys_flat = np.repeat(np.arange(h, dtype=np.int32), w)
    xs_flat = np.tile(np.arange(w, dtype=np.int32), h)
    vf = valid.ravel()
    return gx_n[vf], gy_n[vf], xs_flat[vf], ys_flat[vf], h, w


def frst(gray: np.ndarray, radii: list[int] | None = None) -> np.ndarray:
    """Compute FRST symmetry map (sum over radii), bright blobs only.

    Bright-only: votes at p - r*g_hat (against gradient direction) so votes
    converge at the centre of bright circular blobs (specular highlights).

    Uses np.bincount for O(N) scatter accumulation — ~5x faster than np.add.at.
    Gaussian sigma = 0.25*r per Loy 2003, alpha=1 normalisation.

    Returns S (h, w) float32 symmetry response image.
    """
    if radii is None:
        radii = [3, 5, 8, 12]
    gx_n, gy_n, xs_v, ys_v, h, w = _precompute_gradients(gray)
    S = np.zeros((h, w), dtype=np.float32)
    for r in radii:
        # Vote target: p - r*g_hat  (bright-blob polarity)
        px_v = np.clip(np.round(xs_v - r * gx_n).astype(np.int32), 0, w - 1)
        py_v = np.clip(np.round(ys_v - r * gy_n).astype(np.int32), 0, h - 1)
        idx = py_v * w + px_v
        O_r = np.bincount(idx, minlength=h * w).reshape(h, w).astype(np.float32)
        sigma = max(1.0, 0.25 * r)
        ksize = int(2 * np.ceil(2 * sigma) + 1)
        M_r = cv2.GaussianBlur(O_r, (ksize, ksize), sigma) / float(r)
        S += M_r
    return S


def frst_candidates(gray: np.ndarray,
                    radii: list[int] | None = None,
                    threshold: float = 0.5,
                    nms_r: int = 5) -> list[tuple[float, float, float]]:
    """Extract candidate centres from FRST symmetry map.

    NMS: local maxima (dilate trick).  Returns list of (px, py, score).
    """
    if radii is None:
        radii = [3, 5, 8, 12]
    S = frst(gray, radii)
    # 5-px NMS via dilate comparison (fast, no Python loop)
    ksize = 2 * nms_r + 1
    kernel = np.ones((ksize, ksize), dtype=np.uint8)
    dilated = cv2.dilate(S, kernel)
    local_max = (S == dilated) & (S > threshold)
    ys, xs = np.where(local_max)
    cands = [(float(xs[i]), float(ys[i]), float(S[ys[i], xs[i]])) for i in range(len(xs))]
    return cands


# ------------------------------------------------------------------
# Evaluation helpers
# ------------------------------------------------------------------

def hit_check(cands: list[tuple[float, float, float]],
              gtc_x: float, gtc_y: float, gt_area: int) -> bool:
    """True if any candidate within tolerance of GT centroid."""
    r = float(np.sqrt(gt_area / np.pi))
    tol2 = max(10.0, 0.5 * r) ** 2
    return any((cx - gtc_x) ** 2 + (cy - gtc_y) ** 2 <= tol2 for cx, cy, _ in cands)


def v11_cands_to_tuples(v11_cands) -> list[tuple[float, float, float]]:
    return [(c.px, c.py, float(c.area)) for c in v11_cands]


def dedup_union(v11: list[tuple], frst_c: list[tuple],
                radius: float = 5.0) -> list[tuple]:
    """Union of V11 and FRST candidates with 5-px dedup.

    If FRST candidate within `radius` px of any V11 candidate, drop it.
    V11 candidates always kept.
    """
    merged = list(v11)
    for fc in frst_c:
        fx, fy, fs = fc
        if not any((fx - vx) ** 2 + (fy - vy) ** 2 <= radius ** 2
                   for vx, vy, _ in v11):
            merged.append(fc)
    return merged


# ------------------------------------------------------------------
# Load GT for all done sessions
# ------------------------------------------------------------------

def load_session_frames(item: dict) -> list[dict]:
    """Load all GT frames for a session item. Returns list of frame dicts."""
    slug = item["slug"]
    in_f = item["in_frame"]
    masks_dir = WS / "items" / slug / "masks"
    frames_dir = WS / "items" / slug / "frames"

    rows = []
    for mp in sorted(masks_dir.glob("*.png")):
        src = int(mp.stem)
        local = src - in_f
        fp = frames_dir / f"{local:05d}.jpg"
        if not fp.exists():
            continue
        gt = cv2.imread(str(mp), cv2.IMREAD_GRAYSCALE)
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


# ------------------------------------------------------------------
# Miss classification helpers — match 18_miss_run_physics definitions
# ------------------------------------------------------------------
#
# V11 miss modes (from 02_v11_followup.md §3):
#   M1: HSV cube zero pixels in GT region (desat)
#       → gt_s very low AND hit==False
#       threshold: gt_s < 80 (from note: HIT p50=145, M1 S_mean p50=45)
#   M2: HSV pixels present but no CC ≥5 px passing gates
#       → gt_s moderate AND hit==False (residual after M1)
#   M3: CC fails aspect < 0.40 (hue-shifted frames)
#       → note says "gt_h=89 vs HIT 107", so hue offset
#
# Since we don't have the full mode labels from 15_v11_failure_modes.py,
# we approximate using the GT statistics:
#   M1 proxy: ball_in AND V11-miss AND gt_s < 80 AND gt_v > 100 (specular)
#   M3 proxy: ball_in AND V11-miss AND gt_h < 100 (hue-shifted)
#   M2 proxy: remaining V11-misses (fragmentation, low-area)

def classify_miss_mode(row: dict) -> str | None:
    """Return 'M1', 'M2', 'M3' for V11 misses, None otherwise."""
    if not row.get("ball_in"):
        return None
    if row.get("v11_hit"):
        return None  # not a miss
    gt_s = row.get("gt_s", 255)
    gt_v = row.get("gt_v", 0)
    gt_h = row.get("gt_h", 110)
    # Mode α / M1 specular: desat ball — low S, high V
    if gt_s < 80:
        return "M1"
    # Mode β / M3 hue shift: ambient color contamination
    if gt_h < 100:
        return "M3"
    # M2: fragmentation / tiny CC
    return "M2"


# ------------------------------------------------------------------
# Tune threshold on session_s_16ec069a_b
# ------------------------------------------------------------------

def tune_threshold(frames: list[dict], radii: list[int],
                   thresholds: list[float]) -> float:
    """Grid search threshold on training session; maximise recall.

    Precomputes FRST symmetry maps once, then sweeps thresholds on cached S maps.
    """
    print("  Precomputing FRST maps for train frames...")
    ball_frames = [(row, cv2.cvtColor(row["bgr"], cv2.COLOR_BGR2GRAY))
                   for row in frames if row["ball_in"]]
    print(f"  Computing FRST for {len(ball_frames)} ball-in frames...")
    # Precompute S maps once
    S_maps = []
    for i, (row, gray) in enumerate(ball_frames):
        if i % 20 == 0:
            print(f"    [{i}/{len(ball_frames)}]")
        S_maps.append(frst(gray, radii))

    # Also precompute NMS kernel
    nms_r = 5
    ksize = 2 * nms_r + 1
    kernel = np.ones((ksize, ksize), dtype=np.uint8)

    best_t, best_r = thresholds[0], -1.0
    for t in thresholds:
        hits = 0
        for (row, _), S in zip(ball_frames, S_maps):
            dilated = cv2.dilate(S, kernel)
            local_max = (S == dilated) & (S > t)
            ys, xs = np.where(local_max)
            cands = [(float(xs[i]), float(ys[i]), float(S[ys[i], xs[i]])) for i in range(len(xs))]
            if hit_check(cands, row["gtc_x"], row["gtc_y"], row["gt_area"]):
                hits += 1
        r = hits / len(ball_frames) if ball_frames else 0.0
        print(f"    t={t:.2f} -> R={r:.3f} ({hits}/{len(ball_frames)})")
        if r > best_r:
            best_r, best_t = r, t
    return best_t


# ------------------------------------------------------------------
# Benchmark
# ------------------------------------------------------------------

def bench_frst(frames_bgr: list[np.ndarray], radii: list[int],
               n: int = 100) -> float:
    """Return mean ms/frame for FRST on n random frames."""
    subset = frames_bgr[:n] if len(frames_bgr) >= n else frames_bgr
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in subset]
    t0 = time.perf_counter()
    for g in grays:
        frst(g, radii)
    elapsed = time.perf_counter() - t0
    return elapsed / len(grays) * 1000.0


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    RADII_PRIMARY = [3, 5, 8, 12]
    RADII_EXTENDED = [3, 5, 8, 12, 16, 20]

    items = [it for it in M["items"]
             if it.get("propagate_status") == "done" and it.get("in_frame") is not None]
    print(f"Sessions: {len(items)}")
    slugs = [it["slug"] for it in items]
    print(f"  train: {slugs[0]}")
    print(f"  test : {slugs[1:]}")

    TRAIN_SLUG = "session_s_16ec069a_b"
    assert TRAIN_SLUG in slugs, f"{TRAIN_SLUG} not found"

    # 1. Load train session, tune threshold
    print("\n--- Loading train session for threshold tuning ---")
    train_item = next(it for it in items if it["slug"] == TRAIN_SLUG)
    train_frames = load_session_frames(train_item)
    print(f"  loaded {len(train_frames)} frames, ball_in={sum(r['ball_in'] for r in train_frames)}")

    THRESHOLDS = [0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0, 3.0]
    best_t = tune_threshold(train_frames, RADII_PRIMARY, THRESHOLDS)
    print(f"  tuned threshold = {best_t:.2f}")

    # 2. Run full evaluation on all sessions
    v11_detector = BallDetector()
    print("\n--- Full evaluation (all 9 sessions) ---")

    total_ball_in = 0
    total_v11_hits = 0
    total_frst_hits = 0
    total_union_hits = 0

    # Mode breakdown for V11 misses
    m1_v11miss = 0; m1_frst_hits = 0
    m2_v11miss = 0; m2_frst_hits = 0
    m3_v11miss = 0; m3_frst_hits = 0

    total_frames = 0
    total_frst_cands = 0  # for FP rate
    total_non_ball_frames = 0
    total_frst_on_nonball = 0  # spurious detections on frames without ball

    union_cands_per_frame_list = []

    per_session_results = []

    for item in items:
        slug = item["slug"]
        is_train = (slug == TRAIN_SLUG)
        frames = train_frames if is_train else load_session_frames(item)

        sess_ball_in = 0
        sess_v11 = 0; sess_frst = 0; sess_union = 0

        # Precompute FRST symmetry maps for all frames in this session
        print(f"  Precomputing FRST for {len(frames)} frames [{slug[:25]}]...")
        nms_r = 5
        nms_ksize = 2 * nms_r + 1
        nms_kernel = np.ones((nms_ksize, nms_ksize), dtype=np.uint8)
        frst_cands_cache = []
        for fi, row in enumerate(frames):
            gray = cv2.cvtColor(row["bgr"], cv2.COLOR_BGR2GRAY)
            S = frst(gray, RADII_PRIMARY)
            dilated = cv2.dilate(S, nms_kernel)
            local_max = (S == dilated) & (S > best_t)
            ys2, xs2 = np.where(local_max)
            cands = [(float(xs2[i]), float(ys2[i]), float(S[ys2[i], xs2[i]])) for i in range(len(xs2))]
            frst_cands_cache.append(cands)

        for fi, row in enumerate(frames):
            bgr = row["bgr"]
            total_frames += 1

            # V11
            v11_cands = v11_detector.detect(bgr)
            row["v11_cands"] = v11_cands
            row["v11_hit"] = False

            # FRST (from cache)
            frst_c = frst_cands_cache[fi]
            row["frst_cands"] = frst_c
            row["frst_hit"] = False

            total_frst_cands += len(frst_c)

            if not row["ball_in"]:
                total_non_ball_frames += 1
                if len(frst_c) > 0:
                    total_frst_on_nonball += len(frst_c)
                continue

            sess_ball_in += 1
            total_ball_in += 1

            gtc_x = row["gtc_x"]; gtc_y = row["gtc_y"]; gt_area = row["gt_area"]

            # V11 hit
            v11_t = v11_cands_to_tuples(v11_cands)
            v11_h = hit_check(v11_t, gtc_x, gtc_y, gt_area)
            row["v11_hit"] = v11_h
            if v11_h:
                sess_v11 += 1; total_v11_hits += 1

            # FRST hit
            frst_h = hit_check(frst_c, gtc_x, gtc_y, gt_area)
            row["frst_hit"] = frst_h
            if frst_h:
                sess_frst += 1; total_frst_hits += 1

            # Union
            union = dedup_union(v11_t, frst_c)
            union_h = hit_check(union, gtc_x, gtc_y, gt_area)
            if union_h:
                sess_union += 1; total_union_hits += 1

            union_cands_per_frame_list.append(len(union))

            # Mode breakdown for V11 misses
            if not v11_h:
                mode = classify_miss_mode(row)
                if mode == "M1":
                    m1_v11miss += 1
                    if frst_h: m1_frst_hits += 1
                elif mode == "M2":
                    m2_v11miss += 1
                    if frst_h: m2_frst_hits += 1
                elif mode == "M3":
                    m3_v11miss += 1
                    if frst_h: m3_frst_hits += 1

        sess_r_v11   = sess_v11   / sess_ball_in if sess_ball_in > 0 else 0.0
        sess_r_frst  = sess_frst  / sess_ball_in if sess_ball_in > 0 else 0.0
        sess_r_union = sess_union / sess_ball_in if sess_ball_in > 0 else 0.0
        split = "train" if is_train else "test"
        per_session_results.append({
            "slug": slug, "split": split, "n": sess_ball_in,
            "v11": sess_r_v11, "frst": sess_r_frst, "union": sess_r_union,
        })
        print(f"  {slug[:30]:30s} [{split}] n={sess_ball_in:3d}  V11={sess_r_v11:.3f}  FRST={sess_r_frst:.3f}  Union={sess_r_union:.3f}")

    # Aggregate
    r_v11   = total_v11_hits   / total_ball_in
    r_frst  = total_frst_hits  / total_ball_in
    r_union = total_union_hits / total_ball_in

    mean_union_cands = np.mean(union_cands_per_frame_list) if union_cands_per_frame_list else 0.0
    fp_rate_nonball = (total_frst_on_nonball / total_non_ball_frames
                       if total_non_ball_frames > 0 else 0.0)
    mean_frst_cands = total_frst_cands / total_frames if total_frames > 0 else 0.0

    print(f"\n=== AGGREGATE ({total_ball_in} ball-in frames) ===")
    print(f"V11 alone    R = {r_v11:.4f}  (baseline, expect ~0.905)")
    print(f"FRST alone   R = {r_frst:.4f}")
    print(f"V11 ∪ FRST   R = {r_union:.4f}")
    print(f"V11 ∪ FRST mean cands/frame = {mean_union_cands:.1f}")
    print(f"FRST mean cands/frame (all frames) = {mean_frst_cands:.2f}")
    print(f"FRST FP rate on no-ball frames = {fp_rate_nonball:.2f} cands/frame")

    # Mode breakdown
    total_v11_miss = total_ball_in - total_v11_hits
    print(f"\n--- V11 miss mode breakdown (n_miss={total_v11_miss}) ---")
    print(f"  M1 (specular/desat, gt_s<80):  n={m1_v11miss:3d}  FRST recovered={m1_frst_hits:3d}  ({m1_frst_hits/m1_v11miss*100:.1f}%)" if m1_v11miss else "  M1: n=0")
    print(f"  M2 (fragmentation):             n={m2_v11miss:3d}  FRST recovered={m2_frst_hits:3d}  ({m2_frst_hits/m2_v11miss*100:.1f}%)" if m2_v11miss else "  M2: n=0")
    print(f"  M3 (hue shift, gt_h<100):       n={m3_v11miss:3d}  FRST recovered={m3_frst_hits:3d}  ({m3_frst_hits/m3_v11miss*100:.1f}%)" if m3_v11miss else "  M3: n=0")
    other = total_v11_miss - m1_v11miss - m2_v11miss - m3_v11miss
    print(f"  unclassified:                   n={other:3d}")

    # Bench — primary radii
    print("\n--- Bench ---")
    bench_frames_bgr = [row["bgr"] for row in train_frames[:100] if row["bgr"] is not None]
    ms_primary = bench_frst(bench_frames_bgr, RADII_PRIMARY)
    ms_extended = bench_frst(bench_frames_bgr, RADII_EXTENDED)
    print(f"  FRST {RADII_PRIMARY} :  {ms_primary:.1f} ms/frame (Python, 1080p, Mac)")
    print(f"  FRST {RADII_EXTENDED}: {ms_extended:.1f} ms/frame (Python, 1080p, Mac)")
    print(f"  iPhone 14 C++ estimate: ~{ms_primary * 0.10:.0f}–{ms_primary * 0.25:.0f} ms (10-25% of Python)")
    print(f"  V11 budget = 4.16 ms; full-frame FRST likely EXCEEDS budget")
    print(f"  Production integration requires ROI gating (breaks stateless contract)")

    # Extended radii recall delta — use precomputed S maps for primary, recompute for extended
    print("\n--- Extended radii [3,5,8,12,16,20] quick check on train session ---")
    train_ball_frames = [r for r in train_frames if r["ball_in"]]
    # primary recall from frst_hit already computed in main loop
    hits_pri = sum(1 for r in train_ball_frames if r.get("frst_hit"))
    r_pri = hits_pri / len(train_ball_frames) if train_ball_frames else 0.0
    # extended: recompute just for train ball frames
    nms_kernel_ext = np.ones((11, 11), dtype=np.uint8)  # 5-px NMS
    hits_ext = 0
    for row in train_ball_frames:
        gray = cv2.cvtColor(row["bgr"], cv2.COLOR_BGR2GRAY)
        S_ext = frst(gray, RADII_EXTENDED)
        dil = cv2.dilate(S_ext, nms_kernel_ext)
        lm = (S_ext == dil) & (S_ext > best_t)
        ys_e, xs_e = np.where(lm)
        cands_ext = [(float(xs_e[i]), float(ys_e[i]), float(S_ext[ys_e[i], xs_e[i]])) for i in range(len(xs_e))]
        if hit_check(cands_ext, row["gtc_x"], row["gtc_y"], row["gt_area"]):
            hits_ext += 1
    r_ext = hits_ext / len(train_ball_frames) if train_ball_frames else 0.0
    print(f"  train session: primary R={r_pri:.3f}  extended R={r_ext:.3f}  Δ={r_ext-r_pri:+.3f}")

    # Save results dict for note writing
    results = dict(
        threshold=best_t,
        radii=RADII_PRIMARY,
        total_ball_in=total_ball_in,
        r_v11=r_v11,
        r_frst=r_frst,
        r_union=r_union,
        mean_union_cands=float(mean_union_cands),
        mean_frst_cands=float(mean_frst_cands),
        fp_rate_nonball=float(fp_rate_nonball),
        m1_v11miss=m1_v11miss,
        m1_frst_recovered=m1_frst_hits,
        m2_v11miss=m2_v11miss,
        m2_frst_recovered=m2_frst_hits,
        m3_v11miss=m3_v11miss,
        m3_frst_recovered=m3_frst_hits,
        ms_per_frame_primary=ms_primary,
        ms_per_frame_extended=ms_extended,
        per_session=per_session_results,
    )
    out_path = ROOT / "lab" / "research" / "outputs" / "19_frst_results.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved: {out_path}")
    return results


if __name__ == "__main__":
    main()
