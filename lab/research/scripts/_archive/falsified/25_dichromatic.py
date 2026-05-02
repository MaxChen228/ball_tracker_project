"""25 — Dichromatic specular separation evaluation (Yang 2010 simplified).

Research question: does Shafer 1985 dichromatic reflection model hold on
our M1 (Mode α specular) failures? Can subtracting estimated I_specular
recover ball intrinsic blue → V11 cube re-detect?

Two modes:
  A) Full-frame preprocess → V11 detect (clean comparison, side-effect risk)
  B) ROI-only inside V11∪Y-diff candidate union → relaxed HSV gate (s_min=60)

Baseline:
  V11 alone     R = 0.905
  V11 ∪ Y-diff  R = 0.970  (target to beat)

Outputs:
  outputs/25_dichromatic.npz       — per-frame metrics
  outputs/25_visu_<src>.png        — 4-panel visualisation for 5 M1 frames

Conclusion is written into notes/18_dichromatic_results.md.
"""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import cv2
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _paths import ROOT, WS, OUT, load_manifest, SEG_BY_SLUG, read_mask

OUT.mkdir(parents=True, exist_ok=True)
M = load_manifest()

# V11 cube (canonical)
V11 = dict(h=(103, 118), s=(120, 255), v=(30, 255), aspect=0.40, fill=0.35,
           area=(3, 150_000), close=3)
# Relaxed HSV for ROI-mode-B (after specular removal)
RELAX = dict(h=(103, 118), s=(60, 255), v=(20, 255), aspect=0.40, fill=0.30,
             area=(3, 150_000), close=3)
# Y-diff config (matches 21_yplane_diff sweet spot thr=30, area_min=50)
YDIFF_THR = 30
YDIFF_AREA_MIN = 50


# ---------- detection helpers ----------
def detect(bgr: np.ndarray, cfg: dict) -> tuple[list[tuple[float, float, int]], np.ndarray]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    lo = np.array([cfg["h"][0], cfg["s"][0], cfg["v"][0]], dtype=np.uint8)
    hi = np.array([cfg["h"][1], cfg["s"][1], cfg["v"][1]], dtype=np.uint8)
    m = cv2.inRange(hsv, lo, hi)
    if cfg["close"] > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (cfg["close"], cfg["close"]))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
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
        out.append((float(cents[i, 0]), float(cents[i, 1]), a))
    return out, m


def detect_ydiff(prev_gray: np.ndarray, curr_gray: np.ndarray) -> list[tuple[float, float, int]]:
    d = cv2.absdiff(curr_gray, prev_gray)
    _, m = cv2.threshold(d, YDIFF_THR, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    n, _, stats, cents = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = []
    for i in range(1, n):
        a = int(stats[i, cv2.CC_STAT_AREA])
        if a < YDIFF_AREA_MIN or a > V11["area"][1]:
            continue
        w = int(stats[i, cv2.CC_STAT_WIDTH]); h = int(stats[i, cv2.CC_STAT_HEIGHT])
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


def hit_any(cands, gtc, tol2):
    return any(((cx - gtc[0]) ** 2 + (cy - gtc[1]) ** 2) <= tol2 for cx, cy, _ in cands)


# ---------- Yang 2010 specular separation ----------
def yang_separate(bgr: np.ndarray, n_iter: int = 3,
                  sigma_color: float = 0.1, sigma_space: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """Returns (diffuse_uint8_bgr, specular_intensity_uint8_2d)."""
    f = bgr.astype(np.float32) / 255.0
    eps = 1e-6
    I = f.sum(axis=2) + eps
    sigma = f.max(axis=2) / I
    sigma_d = sigma.copy()
    for _ in range(n_iter):
        sigma_d = cv2.bilateralFilter(sigma_d.astype(np.float32), d=-1,
                                      sigmaColor=sigma_color, sigmaSpace=sigma_space)
        sigma_d = np.minimum(np.maximum(sigma_d, sigma), 1.0)
    max_c = f.max(axis=2)
    denom = 1.0 - 3.0 * sigma_d
    safe = np.where(np.abs(denom) > 1e-3, denom, 1e-3)
    I_s = 3.0 * (max_c - sigma_d * I) / safe
    I_s = np.clip(I_s, 0.0, I)
    diffuse = np.clip(f - (I_s / 3.0)[..., None], 0.0, 1.0)
    return (diffuse * 255.0).astype(np.uint8), (np.clip(I_s, 0, 1) * 255.0).astype(np.uint8)


# ---------- mode classifier (matches 09_refresh M1 = HSV cube zero in GT) ----------
def classify(mask_v11: np.ndarray, gt_mask: np.ndarray, frame_bgr: np.ndarray,
             cands_v11: list, gtc, tol2) -> str:
    if hit_any(cands_v11, gtc, tol2):
        return "HIT"
    ys, xs = np.where(gt_mask > 0)
    hsv_in_gt = int(mask_v11[ys, xs].sum())
    if hsv_in_gt == 0:
        return "M1"
    # Re-use 09 logic: check CC near GT
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask_v11, connectivity=8)
    near = []
    for i in range(1, n):
        cx, cy = float(cents[i, 0]), float(cents[i, 1])
        if (cx - gtc[0]) ** 2 + (cy - gtc[1]) ** 2 <= tol2:
            near.append(i)
    if not near:
        return "M2"
    best = max(near, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    a = int(stats[best, cv2.CC_STAT_AREA])
    w = int(stats[best, cv2.CC_STAT_WIDTH]); h = int(stats[best, cv2.CC_STAT_HEIGHT])
    if a < V11["area"][0]:
        return "M2"
    asp = min(w, h) / max(w, h)
    fill = a / (w * h)
    if asp < V11["aspect"]:
        return "M3"
    if fill < V11["fill"]:
        return "M4"
    return "M5"


# ---------- ROI-only Mode B ----------
def detect_roi_dichromatic(bgr: np.ndarray, roi_centers: list[tuple[float, float]],
                           radius: int = 40) -> list[tuple[float, float, int]]:
    """Inside each ROI: Yang-separate → relaxed HSV detect. Aggregate cands."""
    if not roi_centers:
        return []
    H, W = bgr.shape[:2]
    out = []
    for cx, cy in roi_centers:
        x0 = max(0, int(cx) - radius); x1 = min(W, int(cx) + radius)
        y0 = max(0, int(cy) - radius); y1 = min(H, int(cy) + radius)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        patch = bgr[y0:y1, x0:x1]
        diff, _ = yang_separate(patch, n_iter=2)  # smaller iter for ROI
        cands_local, _ = detect(diff, RELAX)
        for lx, ly, a in cands_local:
            out.append((lx + x0, ly + y0, a))
    return out


# ---------- main ----------
def find_items():
    items = []
    for it in M["items"]:
        slug = it["slug"]
        for seg in it.get("segments", []):
            if seg.get("propagate_status") == "done" and seg.get("in_frame") is not None:
                seg_id = seg["id"]
                masks_dir = WS / "items" / slug / "masks" / seg_id
                if masks_dir.exists() and any(masks_dir.glob("*.png")):
                    items.append({"slug": slug, "in_frame": seg["in_frame"], "seg_id": seg_id})
                    break
    return items


def main():
    items = find_items()
    print(f"[info] {len(items)} sessions")

    # Per-frame records
    rows = []
    visu_targets = {}  # slug -> list of (src, local) for snapshot
    visu_picked = []   # collected image paths

    for it in items:
        slug = it["slug"]; in_f = it["in_frame"]; seg_id = it["seg_id"]
        masks_dir = WS / "items" / slug / "masks" / seg_id
        frames_dir = WS / "items" / slug / "frames"
        # Pre-load grays
        local_to_gray = {}
        for fp in sorted(frames_dir.glob("*.jpg")):
            local = int(fp.stem)
            g = read_mask(fp)
            if g is not None:
                local_to_gray[local] = g

        for mp in sorted(masks_dir.glob("*.png")):
            src = int(mp.stem); local = src - in_f
            fp = frames_dir / f"{local:05d}.jpg"
            if not fp.exists():
                continue
            gt = read_mask(mp)
            ys, xs = np.where(gt > 0)
            if len(ys) < 5:
                continue
            bgr = cv2.imread(str(fp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            gtc = (float(xs.mean()), float(ys.mean()))
            r = float(np.sqrt(len(ys) / np.pi))
            tol2 = max(10.0, 0.5 * r) ** 2

            # V11 baseline
            cands_v11, mask_v11 = detect(bgr, V11)
            hit_v11 = hit_any(cands_v11, gtc, tol2)

            # Y-diff
            cands_yd = []
            if local - 1 in local_to_gray and local in local_to_gray:
                cands_yd = detect_ydiff(local_to_gray[local - 1], local_to_gray[local])
            hit_yd = hit_any(cands_yd, gtc, tol2)

            # Mode classification (V11 perspective)
            mode = classify(mask_v11, gt, bgr, cands_v11, gtc, tol2)

            # GT S pre/post
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            gt_s_pre = float(hsv[ys, xs, 1].mean())
            gt_h_pre = float(hsv[ys, xs, 0].mean())
            gt_v_pre = float(hsv[ys, xs, 2].mean())

            # ----- Mode A: full-frame preprocess -----
            diff_full, spec_full = yang_separate(bgr, n_iter=3)
            cands_a, _ = detect(diff_full, V11)
            hit_a = hit_any(cands_a, gtc, tol2)
            hsv_post = cv2.cvtColor(diff_full, cv2.COLOR_BGR2HSV)
            gt_s_post = float(hsv_post[ys, xs, 1].mean())
            gt_v_post = float(hsv_post[ys, xs, 2].mean())

            # ----- Mode B: ROI-only on V11∪Y-diff candidate union -----
            roi_centers = [(c[0], c[1]) for c in cands_v11] + [(c[0], c[1]) for c in cands_yd]
            cands_b = detect_roi_dichromatic(bgr, roi_centers, radius=40)
            hit_b = hit_any(cands_b, gtc, tol2)

            rows.append(dict(slug=slug, src=src, mode=mode,
                             hit_v11=int(hit_v11), hit_yd=int(hit_yd),
                             hit_a=int(hit_a), hit_b=int(hit_b),
                             gt_s_pre=gt_s_pre, gt_s_post=gt_s_post,
                             gt_h_pre=gt_h_pre, gt_v_pre=gt_v_pre, gt_v_post=gt_v_post))

            # Visualisation: pick 5 M1 frames spread across worst session
            if mode == "M1" and slug == "session_s_170a6a89_b" and len(visu_picked) < 5:
                if src % 17 == 0 or src in (678, 700, 720, 740, 760):
                    panel_h = bgr.shape[0]
                    panel_w = bgr.shape[1]
                    spec_bgr = cv2.cvtColor(spec_full, cv2.COLOR_GRAY2BGR)
                    # V11 mask before/after
                    mask_pre_bgr = cv2.cvtColor(mask_v11, cv2.COLOR_GRAY2BGR)
                    _, mask_post = detect(diff_full, V11)
                    mask_post_bgr = cv2.cvtColor(mask_post, cv2.COLOR_GRAY2BGR)
                    # Crop around GT for readability (256 px)
                    cx, cy = int(gtc[0]), int(gtc[1])
                    pad = 128
                    x0 = max(0, cx - pad); x1 = min(panel_w, cx + pad)
                    y0 = max(0, cy - pad); y1 = min(panel_h, cy + pad)
                    crops = [bgr[y0:y1, x0:x1], spec_bgr[y0:y1, x0:x1],
                             diff_full[y0:y1, x0:x1], mask_post_bgr[y0:y1, x0:x1]]
                    # Equal width pad
                    H = max(c.shape[0] for c in crops)
                    W = max(c.shape[1] for c in crops)
                    pads = [cv2.copyMakeBorder(c, 0, H - c.shape[0], 0, W - c.shape[1],
                                               cv2.BORDER_CONSTANT, value=(0, 0, 0)) for c in crops]
                    # Annotate
                    labels = ["original", "specular_I", "diffuse", "V11 mask post"]
                    annotated = []
                    for img, lab in zip(pads, labels):
                        a2 = img.copy()
                        cv2.putText(a2, lab, (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
                        annotated.append(a2)
                    panel = np.hstack(annotated)
                    title = f"{slug} src={src} S {gt_s_pre:.0f}->{gt_s_post:.0f}  hit_A={int(hit_a)}"
                    cv2.putText(panel, title, (5, panel.shape[0] - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
                    out_p = OUT / f"25_visu_{slug}_{src}.png"
                    cv2.imwrite(str(out_p), panel)
                    visu_picked.append(str(out_p))

        print(f"  {slug}: rows={sum(1 for r in rows if r['slug']==slug)}")

    n = len(rows)
    if n == 0:
        print("[err] no rows")
        return
    arr = np.array([(r["hit_v11"], r["hit_yd"], r["hit_a"], r["hit_b"]) for r in rows])
    n_v11 = int(arr[:, 0].sum())
    n_yd_or = int(((arr[:, 0] | arr[:, 1])).sum())
    n_a_or = int(((arr[:, 0] | arr[:, 2])).sum())
    n_yd_a = int(((arr[:, 0] | arr[:, 1] | arr[:, 2])).sum())
    n_yd_b = int(((arr[:, 0] | arr[:, 1] | arr[:, 3])).sum())
    n_yd_ab = int(((arr[:, 0] | arr[:, 1] | arr[:, 2] | arr[:, 3])).sum())
    print(f"\n=== Recall on {n} GT frames ===")
    print(f"V11 alone               R = {n_v11/n:.3f}  ({n_v11})")
    print(f"V11 ∪ Y-diff            R = {n_yd_or/n:.3f}  ({n_yd_or})")
    print(f"V11 ∪ A (full-frame)    R = {n_a_or/n:.3f}  ({n_a_or})")
    print(f"V11 ∪ Y-diff ∪ A        R = {n_yd_a/n:.3f}  ({n_yd_a})")
    print(f"V11 ∪ Y-diff ∪ B (ROI)  R = {n_yd_b/n:.3f}  ({n_yd_b})")
    print(f"V11 ∪ Y-diff ∪ A ∪ B    R = {n_yd_ab/n:.3f}  ({n_yd_ab})")

    # Mode breakdown
    print(f"\n=== Per-mode recovery ===")
    modes = ["M1", "M2", "M3", "M4", "M5"]
    for m in modes:
        sub = [r for r in rows if r["mode"] == m]
        if not sub:
            continue
        total = len(sub)
        r_a = sum(r["hit_a"] for r in sub)
        r_b = sum(r["hit_b"] for r in sub)
        r_yd = sum(r["hit_yd"] for r in sub)
        # New saves vs (V11∪Y-diff): A or B hit AND both V11 and YD missed
        new_a = sum(1 for r in sub if r["hit_a"] and not r["hit_v11"] and not r["hit_yd"])
        new_b = sum(1 for r in sub if r["hit_b"] and not r["hit_v11"] and not r["hit_yd"])
        print(f"  {m}: n={total}  yd_recover={r_yd}/{total}  "
              f"A_recover={r_a}/{total} (new vs V11∪YD: {new_a})  "
              f"B_recover={r_b}/{total} (new: {new_b})")

    # Side effect on non-M1 baseline hits
    nonm1 = [r for r in rows if r["mode"] != "M1"]
    broken_a = sum(1 for r in nonm1 if r["hit_v11"] and not r["hit_a"])
    print(f"\nMode A side-effect: {broken_a}/{len(nonm1)} V11 hits broken by full-frame separation "
          f"= {broken_a/max(1,len(nonm1)):.1%}")

    # GT S shift on M1
    m1 = [r for r in rows if r["mode"] == "M1"]
    if m1:
        s_pre = np.array([r["gt_s_pre"] for r in m1])
        s_post = np.array([r["gt_s_post"] for r in m1])
        delta = s_post - s_pre
        print(f"\n=== M1 GT-region S shift (n={len(m1)}) ===")
        print(f"  pre  p10/p50/p90 = {np.percentile(s_pre,10):.0f}/{np.percentile(s_pre,50):.0f}/{np.percentile(s_pre,90):.0f}")
        print(f"  post p10/p50/p90 = {np.percentile(s_post,10):.0f}/{np.percentile(s_post,50):.0f}/{np.percentile(s_post,90):.0f}")
        print(f"  Δ    p10/p50/p90 = {np.percentile(delta,10):+.0f}/{np.percentile(delta,50):+.0f}/{np.percentile(delta,90):+.0f}")
        n_above = int((s_post >= V11["s"][0]).sum())
        print(f"  M1 with S_post ≥ 120 (cube floor): {n_above}/{len(m1)} = {n_above/len(m1):.1%}")

    print(f"\n[visu] saved {len(visu_picked)} panels: {visu_picked}")
    out = OUT / "25_dichromatic.npz"
    np.savez_compressed(out,
                        rows=np.array([list(r.values()) for r in rows], dtype=object),
                        keys=np.array(list(rows[0].keys())))
    print(f"[done] {out}")


if __name__ == "__main__":
    main()
